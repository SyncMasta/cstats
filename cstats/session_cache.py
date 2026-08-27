"""Per-file parse cache for the session transcripts.

`claude_parser.parse_sessions()` used to re-read every transcript on every
refresh — 67 files, 399 MB, ~2 s of CPU, every 60 seconds — while typically one
to three files had actually changed. This module stores what each file parsed
into, keyed on the file, so a refresh only reads the bytes that are new.

Transcripts are append-only: Claude Code writes one JSON line per record and
never rewrites earlier ones. That is the whole basis for resuming at a byte
offset. This module does not trust that property blindly, though — see
`fingerprint`.

Division of labour: this module owns the on-disk file (versioning, atomic
replace, validity decisions) and knows nothing about a `Session`.
`claude_parser` owns the mapping between a `Session` and the entry's payload.
That split is what keeps the two modules free of a circular import.

Nothing here raises. A cache that is missing, truncated, corrupt, or written by
another version reads back as "no cache", which costs a full scan and never a
wrong number.
"""

import hashlib
import json
import os

from . import config


# Bump when the entry layout changes, so old entries are discarded instead of
# misread. Deliberately separate from `aggregate.CACHE_VERSION`: that one
# versions the rendered dashboard snapshot, this one versions parser state.
# The two change for unrelated reasons and must not force each other's hand.
PARSE_VERSION = 4

# Bytes hashed at each end of a cached prefix. Enough to cover several whole
# JSONL records at the resume point; small enough that verifying all 67 files
# costs about half a megabyte of reads.
_FP_BYTES = 4096


def cache_path():
    """Path of the parse cache. Honors XDG_CACHE_HOME, read at call time.

    Read at call time, not at import: the tests point XDG_CACHE_HOME at a
    tempdir, and a module-level constant would have frozen the real path.
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "cstats", "sessions.json")


def fingerprint(path, offset):
    """Digest certifying the first `offset` bytes of `path`.

    Hashes the offset itself plus the first and last `_FP_BYTES` bytes of that
    prefix. Two small reads instead of a digest over the whole file: hashing a
    40 MB transcript costs about as much as parsing it, which is the work this
    cache exists to avoid.

    This is the guard against the two ways a plain (size, mtime) key can hand
    back a wrong resume point: a file rewritten in place while also growing,
    and a file whose mtime was restored to an older value. Both change the
    bytes at the resume point, and the digest notices.

    What it does not cover: an edit strictly inside the prefix that keeps the
    length identical, keeps the first and last `_FP_BYTES` bytes identical, and
    restores mtime to the nanosecond. That is forgery, not something any writer
    does by accident, and detecting it would mean reading every byte.

    Returns None when the prefix cannot be read; callers treat that as "no
    usable cache".
    """
    if offset <= 0:
        return None
    try:
        with open(path, "rb") as fh:
            head = fh.read(min(_FP_BYTES, offset))
            tail_start = max(0, offset - _FP_BYTES)
            fh.seek(tail_start)
            tail = fh.read(offset - tail_start)
    except OSError:
        return None
    h = hashlib.sha256()
    # the offset goes into the digest so the same bytes hashed for a different
    # prefix length cannot collide
    h.update(b"%d\n" % offset)
    h.update(head)
    h.update(tail)
    return h.hexdigest()


def plan(path, entry):
    """Decide how much of `path` has to be re-read.

    Returns `(start_offset, usable_entry)`. `usable_entry` is the cached entry
    to resume on top of, or None to parse the file from scratch.

    A usable entry is accepted in exactly two situations, and both then take the
    same code path — parse from `start_offset` to EOF, which reads zero bytes in
    the unchanged case:

    - the file is unchanged (same size, same mtime to the nanosecond) — the
      cached state stands;
    - the file grew and the cached prefix still hashes the same — the new bytes
      are appended to the cached state.

    Everything else is a full rescan: a shrunk file, a file of unchanged size
    with a changed mtime (that is an in-place rewrite, not an append), a
    mismatching digest, a nonsensical offset. The bias is deliberate — a full
    rescan costs time, a wrongly reused prefix costs correctness.
    """
    if not isinstance(entry, dict):
        return 0, None
    try:
        stat = os.stat(path)
    except OSError:
        return 0, None
    try:
        c_size = entry.get("size")
        c_mtime = entry.get("mtime_ns")
        c_off = int(entry.get("offset") or 0)
        c_fp = entry.get("fingerprint")
    except (TypeError, ValueError):
        return 0, None
    if not isinstance(c_size, int) or not isinstance(c_mtime, int):
        return 0, None
    # The offset must land inside the prefix we claim to have parsed. It can be
    # smaller than c_size: a transcript caught mid-write ends in a partial line,
    # which is never committed to the cache (see claude_parser).
    if c_off <= 0 or c_off > c_size or c_size > stat.st_size:
        return 0, None
    if c_size == stat.st_size:
        if c_mtime != stat.st_mtime_ns:
            return 0, None  # same length, new mtime -> rewritten in place
    if not c_fp or fingerprint(path, c_off) != c_fp:
        return 0, None
    return c_off, entry


def new_entry(path, offset, payload):
    """Build a cache entry for `path` describing a parsed prefix of `offset` bytes.

    `payload` is whatever the parser wants to store back (opaque here). Returns
    None when the file cannot be stat'ed or the offset is unusable, which simply
    means this file gets no cache entry this round.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    if offset <= 0 or offset > stat.st_size:
        return None
    fp = fingerprint(path, offset)
    if fp is None:
        return None
    entry = {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "offset": offset,
        "fingerprint": fp,
    }
    entry.update(payload or {})
    return entry


def load(path=None) -> dict:
    """Read the cache. Returns {file path: entry}, empty on any problem."""
    path = path or cache_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        if not isinstance(obj, dict) or obj.get("parse_version") != PARSE_VERSION:
            return {}
        files = obj.get("files")
        return files if isinstance(files, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {}


def save(files, path=None) -> None:
    """Write the cache atomically (tmp + os.replace). Never raises.

    Atomic because a refresh can be killed at any moment — a half-written cache
    would otherwise be read back as garbage on the next start. A failed write
    just means the next run does a full scan.
    """
    path = path or cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with config.open_private(tmp) as fh:
            json.dump({"parse_version": PARSE_VERSION, "files": files}, fh)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass
