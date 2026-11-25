#!/usr/bin/env python3
"""One-line office joke for make.fun's "Make the AI Judge Laugh".

The program writes a single fixed joke to ``$MAKEFUN_OUTPUT_DIR/entry.txt``
(default ``/output``). Python 3.13 standard library only. Output is fully
deterministic: every run, by any user, from any working directory, writes
the exact same bytes.

Pipeline (in the order ``main`` calls them):

1. :func:`validate_entry` — gate checks on the joke text. Each rule is
   its own ``_reject_*`` helper so a failure names the exact rule that
   broke; the character scan is split into a code-point check and a
   Unicode-category check so each can be read and tested on its own.
2. :func:`encode_utf8` — UTF-8 encode with an exact round-trip check.
3. :func:`output_directory` — resolve ``MAKEFUN_OUTPUT_DIR`` (blank or
   missing falls back to ``/output``; an existing non-directory errors).
4. :func:`purge_stale_tmp_files` — remove ``.entry-*.tmp`` leftovers
   from interrupted earlier runs (bounded retry, never fatal).
5. :func:`publish_atomically` — temp file inside the output directory
   (same filesystem, so the rename cannot cross devices on a
   bind-mount) + fsync + ``os.replace`` + directory fsync.
6. :func:`check_written_file` — read-back check of bytes and mode.

The output directory itself is never deleted; only stale temp files
inside it are removed. ``entry.txt`` is always written with mode 0644.
"""

import os
import stat
import sys
import tempfile
import time
import unicodedata

ENTRY = (
    "The fire drill was the only meeting all year where everyone "
    "showed up on time, left early, and nobody asked for the slides."
)

MAX_CHARS = 200
MIN_CHARS = 1
EXPECTED_MODE = 0o644
DEFAULT_OUTPUT_DIR = "/output"
TMP_PREFIX = ".entry-"
TMP_SUFFIX = ".tmp"
# A temp file left by a crashed run counts as stale after one hour.
STALE_TMP_MAX_AGE_SECONDS = 3600
# Bounded retries when deleting one stale temp file.
STALE_TMP_RETRIES = 3
STALE_TMP_RETRY_DELAY_SECONDS = 0.05


def _reject_blank(text: str) -> None:
    """Fail unless the joke holds at least one non-whitespace char."""
    if not text or not text.strip():
        raise ValueError("entry must not be empty or only whitespace")


def _reject_bad_length(text: str) -> None:
    """Fail unless the joke fits the 1-200 character gate."""
    if len(text) < MIN_CHARS:
        raise ValueError("entry must be at least 1 character")
    if len(text) > MAX_CHARS:
        raise ValueError(f"entry is {len(text)} chars, over the {MAX_CHARS} limit")


def _reject_multiline(text: str) -> None:
    """Fail if the joke spans more than one line."""
    if "\n" in text or "\r" in text:
        raise ValueError("entry must be a single line")


def _reject_bom_and_c1(text: str) -> None:
    """Fail on a BOM or any C1 control character (U+0080-U+009F)."""
    for ch in text:
        code = ord(ch)
        if code == 0xFEFF:
            raise ValueError("entry contains a byte-order mark (U+FEFF)")
        if 0x0080 <= code <= 0x009F:
            raise ValueError(f"entry contains C1 control character U+{code:04X}")


def _reject_separators_and_surrogates(text: str) -> None:
    """Fail on U+2028/U+2029 separators and surrogate code points."""
    for ch in text:
        code = ord(ch)
        if 0x2028 <= code <= 0x2029:
            raise ValueError(f"entry contains separator U+{code:04X}")
        if 0xD800 <= code <= 0xDFFF:
            raise ValueError(f"entry contains surrogate U+{code:04X}")


def _reject_invisible_categories(text: str) -> None:
    """Fail on control/format/invisible Unicode categories.

    Rejected: any character in categories Cc, Cf, Cs, Zl or Zp, and
    any Zs space other than the plain space U+0020 (so non-breaking
    and other exotic spaces are refused).
    """
    for ch in text:
        code = ord(ch)
        category = unicodedata.category(ch)
        if category in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            raise ValueError(
                f"entry contains {category} character U+{code:04X}"
            )
        if category == "Zs" and ch != " ":
            raise ValueError(
                f"entry contains non-breaking space U+{code:04X}"
            )


def validate_entry(text: str) -> None:
    """Run every gate check on the joke text, in a fixed order.

    Blank, length, single-line, then the three character scans. Each
    helper raises a ``ValueError`` naming the rule that broke.
    """
    _reject_blank(text)
    _reject_bad_length(text)
    _reject_multiline(text)
    _reject_bom_and_c1(text)
    _reject_separators_and_surrogates(text)
    _reject_invisible_categories(text)


def encode_utf8(text: str) -> bytes:
    """Encode the joke as UTF-8, failing unless it round-trips exactly."""
    data = text.encode("utf-8")
    if data.decode("utf-8") != text:
        raise ValueError("entry is not valid UTF-8 round-trip text")
    return data


def output_directory() -> str:
    """Return the directory that will hold ``entry.txt``.

    ``MAKEFUN_OUTPUT_DIR`` wins when it names a real directory. A
    missing, empty, or whitespace-only value falls back to
    ``/output``. A value pointing at an existing non-directory is an
    explicit error, not something to silently work around.
    """
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR")
    candidate = raw.strip() if raw is not None else ""
    if not candidate:
        return DEFAULT_OUTPUT_DIR
    if os.path.exists(candidate) and not os.path.isdir(candidate):
        raise ValueError(
            f"MAKEFUN_OUTPUT_DIR {candidate!r} exists and is not a directory"
        )
    return candidate


def purge_stale_tmp_files(out_dir: str) -> int:
    """Delete stale ``.entry-*.tmp`` files from interrupted runs.

    Only files older than :data:`STALE_TMP_MAX_AGE_SECONDS` are
    removed, so a temp file belonging to a run still in flight is
    never touched. Each delete is retried a bounded number of times;
    a file that still cannot be removed is left in place and simply
    not counted — a stale temp file must never block a fresh entry.
    Returns the number of files actually removed.
    """
    removed = 0
    now = time.time()
    try:
        names = os.listdir(out_dir)
    except FileNotFoundError:
        return 0
    for name in names:
        if not name.startswith(TMP_PREFIX) or not name.endswith(TMP_SUFFIX):
            continue
        path = os.path.join(out_dir, name)
        try:
            if now - os.path.getmtime(path) < STALE_TMP_MAX_AGE_SECONDS:
                continue
        except OSError:
            continue
        for _attempt in range(STALE_TMP_RETRIES):
            try:
                os.unlink(path)
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(STALE_TMP_RETRY_DELAY_SECONDS)
            else:
                removed += 1
                break
    return removed


def _sync_directory(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_atomically(out_dir: str, data: bytes) -> str:
    """Write ``data`` to ``entry.txt`` in ``out_dir``, atomically.

    Bytes go to a fresh temp file in the same directory (same
    filesystem, so the rename cannot cross devices), are fsynced,
    then moved over the target with :func:`os.replace`. The directory
    itself is fsynced so the rename survives a crash. On any failure
    the temp file is removed and the original exception propagates —
    the old ``entry.txt``, if any, is left untouched. A partial write
    can therefore never leave a half-written ``entry.txt`` behind.
    """
    target = os.path.join(out_dir, "entry.txt")
    fd, tmp_path = tempfile.mkstemp(
        prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=out_dir
    )
    try:
        os.fchmod(fd, EXPECTED_MODE)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, EXPECTED_MODE)
        os.replace(tmp_path, target)
        os.chmod(target, EXPECTED_MODE)
        _sync_directory(out_dir)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def check_written_file(target: str, data: bytes) -> None:
    """Read ``entry.txt`` back; fail unless bytes and mode match."""
    with open(target, "rb") as f:
        if f.read() != data:
            raise ValueError("read-back verification failed")
    mode = stat.S_IMODE(os.stat(target).st_mode)
    if mode != EXPECTED_MODE:
        raise ValueError(f"entry.txt mode is {mode:04o}, expected 0644")


def main() -> int:
    validate_entry(ENTRY)
    data = encode_utf8(ENTRY)
    out_dir = output_directory()
    os.makedirs(out_dir, exist_ok=True)
    purge_stale_tmp_files(out_dir)
    target = publish_atomically(out_dir, data)
    check_written_file(target, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
