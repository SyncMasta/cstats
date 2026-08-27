"""What compacting actually does, measured from the transcripts.

`economics.py` estimates the cost of a compaction from constants. This module
replaces the guesses with the real thing: every compaction Claude Code has ever
performed leaves two lines in the session transcript, and around them the usage
blocks show what the context looked like before and after.

Two lines per compaction, and they are separate records:

- the boundary — `type == "system"`, `subtype == "compact_boundary"`, carrying a
  `compactMetadata` dict with trigger/preTokens/postTokens/durationMs;
- the summary — `type == "user"`, `isCompactSummary == true`, carrying the
  summary text that becomes the new context.

**`postTokens` is not the post-compaction context.** It counts only the
conversation that survived; the system prompt, CLAUDE.md, the tool definitions
and the MCP schemas are re-sent and re-billed on top of it. Measured over 43
compactions the first billed assistant call after a boundary carried a median
of ~63k tokens against a postTokens median of ~17k — a factor of 3.7. That is
why `ctx_after` (read from the actual usage block) is the number this module
reports, and `pre_tokens`/`post_tokens` are kept only as raw metadata.

A third line matters too, and it is a trap: the compaction *call* — the request
that reads the whole old context to write the summary — is logged AFTER the
boundary and after the summary. Its context is the pre-compact one (~950k), so
taking "the first usage line after the boundary" verbatim would report the
post-compact context as 950k. Two such lines exist in the reference history and
both were only harmless by accident (see `_usage_reading` on `iterations`), so
they are now filtered explicitly: a post-boundary reading above
COMPACT_CALL_SHARE of the pre-compact size is the compaction call, not the new
session.

Scanning is a single byte pass per file with byte-level prefilters, so
`json.loads` only runs on the handful of lines that matter (boundaries,
summaries, and the usage lines immediately adjacent to a boundary). The whole
history scans in well under a second, and a per-file cache keyed on
(mtime, size, boundary fingerprints) makes repeat runs cheaper still:
transcripts are append-only, so a grown file is resumed from the last byte
offset that ended on a newline.

Turn counts are an *approximation*, deliberately, and not the parser's
first-sight dedupe (AGENTS.md rule 6): a `message.id` regex counts id *changes*
rather than distinct ids, which assumes the content-block lines of one call are
contiguous. That assumption holds almost always — measured against a real set
per segment the difference was 26,473 vs 26,472 ids over the whole history
(1 in 26k) — and it buys O(1) memory, which is what lets the incremental cache
persist its position mid-file. Token and cost numbers never come from here;
they come from `claude_parser`, where the real dedupe lives.

Sidechain lines (sub-agent transcripts, `isSidechain: true`) are excluded from
turn counting and from the context readings: a sub-agent's context is its own,
it is not what the main thread pays for on the next turn. In the history this
was written against no boundary was ever adjacent to a sidechain line, so this
only guards against future data.
"""

import glob
import hashlib
import json
import os
import re
import statistics
import time
from datetime import datetime, timezone

from . import config
from . import economics
from . import pricing
from .claude_parser import _claude_dir, _parse_ts_ms, _slug_to_project


# Bump when the on-disk scan cache layout changes — a mismatch discards it.
SCAN_VERSION = 2

# Below this many events nothing counts as "measured": a single compaction is
# an anecdote, and the UI must be able to say so.
MIN_EVENTS = 3

# Claude Code's auto-compaction threshold as a share of the context window,
# used when the history contains no auto compactions to measure.
AUTO_FILL_FALLBACK = 0.98

# Pace window: how many recent turns the growth rate is averaged over, and the
# minimum needed before a rate is reported at all.
PACE_MAX_SAMPLES = 30
PACE_MIN_SAMPLES = 3

# Characters per token, for turning a summary's length into output tokens.
# Single source of truth lives in economics (compact_ledger needs it too, and
# economics must not import this module — the dependency runs the other way).
CHARS_PER_TOKEN = economics.CHARS_PER_TOKEN

# A post-boundary usage line reading at least this share of the pre-compact
# context is the compaction call itself, not the compacted session. Real
# post-compact contexts are 5-11% of pre; the compaction call is 99.9%.
COMPACT_CALL_SHARE = 0.5

# Cap on events carried in the *dashboard* JSON, so a pathological history
# cannot bloat the render cache. `total` stays authoritative and `truncated`
# says the list was cut. The per-file scan cache is never truncated: it is the
# basis for incremental resumes, and cutting it loses events permanently.
MAX_CACHED_EVENTS = 200

# Bytes hashed at the head of a file and at the resume offset, to notice an
# in-place rewrite that happens to leave (size, mtime) plausible.
FINGERPRINT_BYTES = 4096

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
# A scan that failed is not a history without compactions: the latter is
# normal, the former means every number derived from it is wrong.
STATUS_ERROR = "error"

# byte-level prefilters — cheap membership tests that keep json.loads off the
# ~99% of lines that cannot possibly be interesting
_B_BOUNDARY = b'"compact_boundary"'
_B_SUMMARY = b'"isCompactSummary"'
_B_USAGE = b'"usage"'
_B_ASSISTANT = b'"type":"assistant"'
_B_SIDECHAIN = b'"isSidechain":true'

_MSG_ID_RE = re.compile(rb'"id":"(msg_[A-Za-z0-9]+)"')

# Does this usage line report any input-side tokens at all? Claude Code writes
# assistant lines for API errors too, with every token count at zero and a
# non-`msg_` id. Such a line must not become the "context before a boundary"
# reading — it would report a context of nothing. Cheaper than json.loads.
#
# Whitespace-tolerant on purpose: the nested `usage.iterations[]` entries are
# serialized with spaces after the colon while the top level is not, and a
# future writer could switch either way. It also matches those nested counters,
# which is correct — `_usage_reading` sums them when the top level is all zero.
# A miss is never authoritative: `_has_tokens` falls back to a real parse, so a
# format change costs a little speed instead of silently zeroing every reading.
_NONZERO_RE = re.compile(
    rb'"(?:input_tokens|cache_creation_input_tokens|cache_read_input_tokens)"'
    rb'\s*:\s*[1-9]')


def _cache_file():
    """Path of the scan cache. Honors XDG_CACHE_HOME, read at call time."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "cstats", "compacts.json")


class CompactEvent:
    __slots__ = (
        "session_id", "slug", "project", "git_branch", "trigger", "ts",
        "pre_tokens", "post_tokens", "ctx_before", "ctx_after",
        "dropped_tokens", "summary_chars", "duration_ms",
        "turns_before", "turns_after", "fill_ratio",
    )

    def __init__(self, **kw):
        for name in self.__slots__:
            setattr(self, name, kw.get(name))
        if self.summary_chars is None:
            self.summary_chars = 0
        if self.turns_before is None:
            self.turns_before = 0
        if self.turns_after is None:
            self.turns_after = 0

    def to_dict(self) -> dict:
        obj = {name: getattr(self, name) for name in self.__slots__}
        obj["ts"] = self.ts.isoformat() if self.ts else None
        return obj

    @classmethod
    def from_dict(cls, obj):
        kw = dict(obj or {})
        kw["ts"] = _parse_ts_ms(kw.get("ts"))
        return cls(**{k: v for k, v in kw.items() if k in cls.__slots__})


class CompactStats:
    """Aggregate view of every compaction in the history.

    `measured` is the honest-reporting flag: with fewer than MIN_EVENTS events
    the derived constants fall back to `economics`' documented assumptions and
    the UI should say "assumed", not "measured".
    """

    def __init__(self):
        self.available = False
        self.status = STATUS_EMPTY
        self.hint = ""
        self.measured = False
        self.events = []
        # `events` may be cut when serialized; `total` always counts them all
        self.truncated = False
        self.total = 0
        self.auto = 0
        self.manual = 0
        self.auto_share = 0.0
        self.post_compact_tokens = economics.POST_COMPACT_TOKENS
        self.summary_output_tokens = economics.SUMMARY_OUTPUT_TOKENS
        self.auto_fill_ratio = AUTO_FILL_FALLBACK
        self.manual_fill_ratio = None
        self.pre_median = None
        self.post_median = None
        self.ctx_after_median = None
        self.ctx_after_min = None
        self.ctx_after_max = None
        self.duration_median_s = None
        self.turns_total = 0
        self.by_session = {}
        self.scanned_files = 0
        self.scan_s = 0.0
        self.first_ts = None
        self.last_ts = None


# ----------------------------------------------------------------------
# scanning
# ----------------------------------------------------------------------
def _loads(raw):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def _usage_reading(raw):
    """(tokens, model) of an assistant usage line, or None.

    Tokens are counted the way the rest of the tool counts context fill:
    input + cache_creation + cache_read, i.e. everything the call was billed
    for on the input side.
    """
    return _reading_from_obj(_loads(raw))


def _reading_from_obj(obj):
    """(tokens, model) of an already-parsed assistant line, or None."""
    if not obj or obj.get("type") != "assistant":
        return None
    msg = obj.get("message") or {}
    usage = msg.get("usage") or {}
    total = _input_side(usage)
    if total <= 0:
        # Some lines report all top-level counters as zero and put the real
        # numbers in `usage.iterations[]` (56,927 lines carry that array; 4 of
        # them are top-level-zero). Reading only the top level would call a
        # 950k-token context "empty" — and if this shape ever becomes the norm,
        # every reading in this module would silently go to zero.
        iterations = usage.get("iterations")
        if isinstance(iterations, list):
            total = sum(_input_side(it) for it in iterations
                        if isinstance(it, dict))
    if total <= 0:
        return None
    return total, (msg.get("model") or obj.get("model"))


def _input_side(usage) -> int:
    """input + cache_creation + cache_read of one usage dict."""
    return ((usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0))


def _has_tokens(raw) -> bool:
    """Whether a usage line carries any input-side tokens.

    Fast path is the byte regex; on a miss we pay for a parse rather than
    trusting the regex, so an unrecognized serialization degrades performance
    instead of quietly dropping every context reading.
    """
    if _NONZERO_RE.search(raw) is not None:
        return True
    return _usage_reading(raw) is not None


def _summary_chars(raw):
    """Length of a compact-summary message's text, or None if not one."""
    obj = _loads(raw)
    if not obj or obj.get("type") != "user" or obj.get("isCompactSummary") is not True:
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list):
        return sum(len(b.get("text") or "") for b in content if isinstance(b, dict))
    if isinstance(content, str):
        return len(content)
    return 0


def _blank_state() -> dict:
    return {"seg_turns": 0, "last_msg_id": None, "last_ctx": None, "open": None}


def _is_compaction_call(tokens, ev) -> bool:
    """Is this post-boundary reading the compaction request itself?

    Claude Code logs the summarizing call *after* the boundary and after the
    summary line, and that call still reads the whole pre-compact context. Two
    of the 43 reference compactions have such a line (956,230 and 933,667
    tokens); taking it as `ctx_after` would report post-compact contexts of
    ~950k instead of 47.6k and 62.9k. Real post-compact readings are 5-11% of
    the pre-compact size, so the cut sits far from both populations.
    """
    reference = ev.pre_tokens or ev.ctx_before
    if not reference or not tokens:
        return False
    return tokens >= COMPACT_CALL_SHARE * reference


def _finalize(ev, state) -> None:
    """Close an event: turns after it, and how much context it actually shed."""
    ev.turns_after = state["seg_turns"]
    if ev.ctx_before is not None and ev.ctx_after is not None:
        ev.dropped_tokens = ev.ctx_before - ev.ctx_after
    elif ev.pre_tokens is not None and ev.post_tokens is not None:
        ev.dropped_tokens = ev.pre_tokens - ev.post_tokens
    else:
        ev.dropped_tokens = None


def _scan_file(path, slug, start_offset=0, state=None):
    """One byte pass over a transcript. Returns (events, state, safe_offset).

    Only lines that end in a newline are processed at all. A transcript being
    written to can hand us a torn final line, and since a line's markers sit
    near its front (`usage` and `type":"assistant"` both precede the trailing
    metadata), a tear passes every byte prefilter and would count a phantom
    turn — which then got committed to the cache and became permanent. Only
    complete lines advance `safe_offset`, and only complete lines advance the
    state, so the two can never disagree.
    """
    events = []
    state = dict(state) if state else _blank_state()
    open_ev = CompactEvent.from_dict(state["open"]) if state.get("open") else None
    project = _slug_to_project(slug)
    # the last two assistant usage lines, kept unparsed until a boundary needs
    # one: if the newest fails to parse (a torn line, an unknown shape) the one
    # before it is still a valid reading, which beats reporting no context
    last_usage_raw = prev_usage_raw = None
    # bytes while scanning (that is what the regex yields), str in the cache
    last_mid = (state["last_msg_id"] or "").encode() or None
    offset = start_offset
    safe_offset = start_offset

    try:
        with open(path, "rb") as fh:
            if start_offset:
                fh.seek(start_offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break  # torn final line: leave it for the next scan
                offset += len(raw)
                safe_offset = offset

                if _B_BOUNDARY in raw:
                    obj = _loads(raw)
                    # Guard: transcripts get quoted back into user/assistant
                    # messages (this very module's development did it), so a
                    # plain substring match over-counts. Only a system line
                    # with a real metadata dict is an event.
                    if (obj and obj.get("type") == "system"
                            and obj.get("subtype") == "compact_boundary"
                            and isinstance(obj.get("compactMetadata"), dict)):
                        meta = obj["compactMetadata"]
                        # newest readable line wins, then the one before it,
                        # then whatever the previous incremental pass left
                        ctx_before = model_before = None
                        for candidate in (last_usage_raw, prev_usage_raw):
                            if candidate is None:
                                continue
                            reading = _usage_reading(candidate)
                            if reading:
                                ctx_before, model_before = reading
                                break
                        if ctx_before is None and state["last_ctx"]:
                            ctx_before, model_before = (state["last_ctx"][0],
                                                        state["last_ctx"][1])
                        fill = None
                        if ctx_before:
                            window = pricing.context_window(model_before, ctx_before)
                            if window:
                                fill = ctx_before / window
                        if open_ev is not None:
                            _finalize(open_ev, state)
                            events.append(open_ev)
                        open_ev = CompactEvent(
                            session_id=obj.get("sessionId") or obj.get("session_id"),
                            slug=slug,
                            project=project,
                            git_branch=obj.get("gitBranch"),
                            trigger=meta.get("trigger"),
                            ts=_parse_ts_ms(obj.get("timestamp")),
                            pre_tokens=meta.get("preTokens"),
                            post_tokens=meta.get("postTokens"),
                            ctx_before=ctx_before,
                            ctx_after=None,
                            summary_chars=0,
                            duration_ms=meta.get("durationMs"),
                            turns_before=state["seg_turns"],
                            turns_after=0,
                            fill_ratio=fill,
                        )
                        state["seg_turns"] = 0
                        last_mid = None
                        state["last_ctx"] = None
                        last_usage_raw = prev_usage_raw = None
                        continue
                    # a quoted boundary: fall through, the line may still be a
                    # real assistant usage line that happens to mention one

                if _B_SUMMARY in raw:
                    chars = _summary_chars(raw)
                    if chars is not None:
                        if open_ev is not None and not open_ev.summary_chars:
                            open_ev.summary_chars = chars
                        continue

                if _B_USAGE in raw and _B_ASSISTANT in raw:
                    if _B_SIDECHAIN in raw:
                        continue  # sub-agent turn, not the main thread's context
                    m = _MSG_ID_RE.search(raw)
                    if m:
                        mid = m.group(1)
                        # content blocks of one API call are contiguous lines
                        # sharing a message.id, so counting id changes counts
                        # distinct API calls without holding a set. Lines
                        # without a `msg_` id are not billed calls (API errors,
                        # synthetic messages) and are not turns.
                        # The contiguity holds for 26,558 of 26,559 message ids
                        # in the local history; the one exception reappears 59
                        # ids later and is counted twice. That is the price of
                        # not holding a set per file, and it is paid knowingly:
                        # turn counts here are an approximation by design, while
                        # the billed numbers in claude_parser are not.
                        if mid != last_mid:
                            last_mid = mid
                            state["seg_turns"] += 1
                    if not _has_tokens(raw):
                        continue  # zero-token line: no context reading in it
                    if open_ev is not None and open_ev.ctx_after is None:
                        reading = _usage_reading(raw)
                        if reading and not _is_compaction_call(reading[0], open_ev):
                            open_ev.ctx_after = reading[0]
                    prev_usage_raw = last_usage_raw
                    last_usage_raw = raw
    except (OSError, PermissionError):
        state["last_msg_id"] = last_mid.decode("ascii") if last_mid else None
        state["open"] = open_ev.to_dict() if open_ev is not None else None
        return events, state, safe_offset

    for candidate in (last_usage_raw, prev_usage_raw):
        if candidate is None:
            continue
        reading = _usage_reading(candidate)
        if reading:
            state["last_ctx"] = list(reading)
            break
    state["last_msg_id"] = last_mid.decode("ascii") if last_mid else None
    state["open"] = open_ev.to_dict() if open_ev is not None else None
    return events, state, safe_offset


def _materialize(events, state):
    """Full event list of a file: the closed ones plus the still-open one.

    The last boundary in a file stays "open" in the scan state — turns keep
    accruing after it as the session continues — so it is not in `events` and
    has to be closed against the current end of the file on every read,
    including a fully-cached one.
    """
    open_ev = CompactEvent.from_dict(state["open"]) if (state or {}).get("open") else None
    if open_ev is None:
        return list(events)
    _finalize(open_ev, state)
    return list(events) + [open_ev]


def _fingerprint(path, offset):
    """(head, at_offset) digests of a file, for detecting in-place rewrites.

    Both regions are stable under pure appends: the first FINGERPRINT_BYTES of
    the file, and the FINGERPRINT_BYTES ending at the byte we would resume
    from. If either moved, the file was rewritten rather than appended to and
    the cached entry must not be trusted.
    """
    try:
        with open(path, "rb") as fh:
            head = hashlib.blake2b(fh.read(FINGERPRINT_BYTES),
                                   digest_size=8).hexdigest()
            at = None
            if offset:
                fh.seek(max(0, offset - FINGERPRINT_BYTES))
                chunk = fh.read(min(offset, FINGERPRINT_BYTES))
                at = hashlib.blake2b(chunk, digest_size=8).hexdigest()
        return head, at
    except OSError:
        return None, None


def _scan_with_cache(path, slug, entry):
    """Scan one file, reusing `entry` from the cache when it still applies.

    Returns (events, new_entry). Transcripts are append-only: unchanged size,
    mtime and fingerprints mean the cached result stands; a grown file whose
    fingerprints still match is resumed from the cached offset; anything else
    (shrunk, rewritten, older mtime, fingerprint moved) is a full rescan.

    `entry["events"]` is never truncated. It is the basis every incremental
    resume builds on, so dropping the front of it would delete events that no
    later scan can recover — a slow, monotone data loss that only shows up
    after the cap is exceeded.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return [], None
    mtime, size = stat.st_mtime, stat.st_size

    cached_events = []
    start = 0
    state = None
    if isinstance(entry, dict):
        try:
            c_size = entry.get("size")
            c_mtime = entry.get("mtime")
            c_off = int(entry.get("complete_offset") or 0)
            unchanged = c_size == size and c_mtime == mtime
            grown = (isinstance(c_size, int) and size > c_size
                     and isinstance(c_mtime, (int, float)) and mtime >= c_mtime
                     and 0 < c_off <= size)
            if unchanged or grown:
                head, at = _fingerprint(path, c_off)
                if (head == entry.get("head_sig")
                        and at == entry.get("offset_sig")):
                    if unchanged:
                        events = [CompactEvent.from_dict(e)
                                  for e in entry.get("events") or []]
                        return (_materialize(events, entry.get("tail_state") or {}),
                                entry)
                    cached_events = [CompactEvent.from_dict(e)
                                     for e in entry.get("events") or []]
                    state = entry.get("tail_state") or None
                    start = c_off
        except (TypeError, ValueError):
            cached_events, start, state = [], 0, None

    new_events, state, safe_offset = _scan_file(path, slug, start, state)
    events = cached_events + new_events
    result = _materialize(events, state)

    head, at = _fingerprint(path, safe_offset)
    entry = {
        "mtime": mtime,
        "size": size,
        "complete_offset": safe_offset,
        "head_sig": head,
        "offset_sig": at,
        "events": [e.to_dict() for e in events],
        "tail_state": state,
    }
    return result, entry


def _load_scan_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        if not isinstance(obj, dict) or obj.get("scan_version") != SCAN_VERSION:
            return {}
        files = obj.get("files")
        return files if isinstance(files, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {}


def _save_scan_cache(path, files):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with config.open_private(tmp) as fh:
            json.dump({"scan_version": SCAN_VERSION, "files": files}, fh)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass


# ----------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------
def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def load_compacts(claude_dir=None, cache_path=None) -> CompactStats:
    """Every compaction in the history, plus the constants derived from them.

    Never raises: a broken cache, an unreadable transcript or a missing
    ~/.claude all yield an empty CompactStats, like the other loaders.
    """
    stats = CompactStats()
    started = time.time()
    try:
        base = os.path.join(claude_dir or _claude_dir(), "projects")
        cache_path = cache_path or _cache_file()
        cache = _load_scan_cache(cache_path)
        fresh = {}
        events = []
        skipped = []
        files = sorted(glob.glob(os.path.join(base, "*", "*.jsonl")))
        for path in files:
            slug = os.path.basename(os.path.dirname(path))
            try:
                got, entry = _scan_with_cache(path, slug, cache.get(path))
            except Exception as exc:
                # one bad transcript must not lose the rest, but keep the
                # existing cache entry so the next run does not rescan it, and
                # remember that this scan was incomplete
                skipped.append(f"{os.path.basename(path)}: {exc}")
                if path in cache:
                    fresh[path] = cache[path]
                continue
            if entry is not None:
                fresh[path] = entry
            events.extend(got)
        stats.scanned_files = len(files)
        _save_scan_cache(cache_path, fresh)
        events.sort(key=lambda e: e.ts or datetime.min.replace(tzinfo=timezone.utc))
        _summarize(stats, events)
        if skipped:
            stats.hint = (f"{len(skipped)} transcript(s) could not be scanned: "
                          + "; ".join(skipped[:3]))
    except Exception as exc:
        # A scan that failed outright is not a history without compactions:
        # the latter is normal and makes the tool fall back to an assumed
        # post-compact size, the former means the numbers shown are wrong.
        stats.status = STATUS_ERROR
        stats.hint = f"compaction scan failed: {exc}"
    stats.scan_s = time.time() - started
    return stats


def _summarize(stats, events) -> None:
    """Fill the aggregate fields from a chronological list of events."""
    stats.events = events
    stats.total = len(events)
    stats.available = stats.total >= 1
    stats.status = STATUS_OK if stats.available else STATUS_EMPTY
    stats.measured = stats.total >= MIN_EVENTS
    if not events:
        return

    stats.auto = sum(1 for e in events if e.trigger == "auto")
    stats.manual = sum(1 for e in events if e.trigger == "manual")
    stats.auto_share = stats.auto / stats.total
    stats.pre_median = _median([e.pre_tokens for e in events])
    stats.post_median = _median([e.post_tokens for e in events])

    after = [e.ctx_after for e in events if e.ctx_after]
    if after:
        stats.ctx_after_median = statistics.median(after)
        stats.ctx_after_min = min(after)
        stats.ctx_after_max = max(after)
    # Derived constants only replace the assumptions once there are enough
    # samples to mean something; below MIN_EVENTS the economics constants win.
    if stats.measured and after:
        stats.post_compact_tokens = int(round(stats.ctx_after_median))
    summaries = [e.summary_chars for e in events if e.summary_chars]
    if stats.measured and summaries:
        stats.summary_output_tokens = int(statistics.median(summaries)) // CHARS_PER_TOKEN

    auto_fills = [e.fill_ratio for e in events
                  if e.trigger == "auto" and e.fill_ratio]
    if stats.measured and auto_fills:
        stats.auto_fill_ratio = float(statistics.median(auto_fills))
    manual_fills = [e.fill_ratio for e in events
                    if e.trigger == "manual" and e.fill_ratio]
    if manual_fills:
        stats.manual_fill_ratio = float(statistics.median(manual_fills))

    durations = [e.duration_ms for e in events if e.duration_ms]
    if durations:
        stats.duration_median_s = statistics.median(durations) / 1000.0
    stats.turns_total = sum(e.turns_after or 0 for e in events)

    by_session = {}
    for e in events:
        sid = e.session_id or e.slug or "?"
        row = by_session.setdefault(sid, {
            "n": 0, "auto": 0, "manual": 0, "dropped": 0,
            "turns_after": 0, "last_ts": None,
        })
        row["n"] += 1
        if e.trigger == "auto":
            row["auto"] += 1
        elif e.trigger == "manual":
            row["manual"] += 1
        row["dropped"] += e.dropped_tokens or 0
        row["turns_after"] += e.turns_after or 0
        if e.ts is not None:
            iso = e.ts.isoformat()
            if row["last_ts"] is None or iso > row["last_ts"]:
                row["last_ts"] = iso
    stats.by_session = by_session

    stamps = [e.ts for e in events if e.ts is not None]
    if stamps:
        stats.first_ts = min(stamps)
        stats.last_ts = max(stamps)


# ----------------------------------------------------------------------
# pace: how fast an active context is heading for the auto-compact line
# ----------------------------------------------------------------------
# Every key `annotate_pace` guarantees on an annotated context dict. Renderers
# index these directly, so they are written even when measurement fails.
PACE_KEYS = ("growth_per_turn", "growth_per_hour", "pace_samples",
             "pace_span_s", "auto_threshold", "turns_to_auto", "eta_auto_s",
             "last_compact_ts", "last_compact_trigger")



def _pace_from_samples(samples) -> dict:
    """Growth rate of a context from (tokens, epoch_s, is_boundary) samples.

    The trap this exists for: context does not grow monotonically. Every
    compaction drops it by ~900k, and a naive first-to-last difference over
    such a window reports a *negative* growth rate — i.e. "you will never hit
    the limit" right after the limit was hit.

    So the window is cut twice and the later cut wins: **at** the last
    boundary sample, and **at** the last sample smaller than its predecessor —
    the latter catching `/clear` and resumed sessions, neither of which writes
    a boundary.

    Both cuts keep the sample they land on, because that sample is the anchor
    of the new segment: the first real call after a compaction (~63k, the
    compaction call itself is filtered out upstream). Dropping it measured 14%
    high on live sessions — 11,000 tok/turn from 3 samples instead of 9,667
    from 4 — and it delayed the first forecast by a turn for no gain.

    Pure function, no I/O. Returns growth_per_turn = None when fewer than
    PACE_MIN_SAMPLES turns remain: a rate from two points is noise, and a
    wrong ETA is worse than no ETA. The mean over the window is used, not the
    median — tool results are spikes and a forecast needs the expected value,
    which is exactly what (last - first) / (n - 1) gives.
    """
    out = {"growth_per_turn": None, "growth_per_hour": None,
           "samples": 0, "span_s": 0.0}
    if not samples:
        return out

    last_boundary = -1
    last_drop = -1
    for i, sample in enumerate(samples):
        if sample[2]:
            last_boundary = i
        if i and sample[0] < samples[i - 1][0]:
            last_drop = i
    start = max(last_boundary, last_drop, 0)
    seg = samples[start:]
    if len(seg) > PACE_MAX_SAMPLES:
        seg = seg[-PACE_MAX_SAMPLES:]

    n = len(seg)
    out["samples"] = n
    if n < 2:
        return out
    span = float(seg[-1][1] - seg[0][1])
    out["span_s"] = span
    if n < PACE_MIN_SAMPLES:
        return out
    delta = seg[-1][0] - seg[0][0]
    out["growth_per_turn"] = delta / (n - 1)
    if span > 0:
        out["growth_per_hour"] = delta / span * 3600.0
    return out


def _tail_samples(path, tail_bytes):
    """(samples, last_boundary) from a transcript tail.

    One sample per distinct message.id — the content-block lines of one call
    repeat the same input counts, and counting them as separate turns would
    dilute the per-turn growth rate towards zero.

    Samples without a usable timestamp are dropped rather than dated to the
    epoch: one such sample made `span_s` 1.77 billion seconds and turned the
    ETA into 14.7 million hours.
    """
    samples = []
    last_boundary = None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(-tail_bytes, os.SEEK_END)
                fh.readline()  # drop the partial first line
            data = fh.read()
    except OSError:
        return samples, last_boundary

    seen = None
    pending_boundary = False
    pre_tokens = None
    for raw in data.splitlines():
        if _B_BOUNDARY in raw:
            obj = _loads(raw)
            if (obj and obj.get("type") == "system"
                    and obj.get("subtype") == "compact_boundary"
                    and isinstance(obj.get("compactMetadata"), dict)):
                pending_boundary = True
                last_boundary = obj
                pre_tokens = (obj["compactMetadata"] or {}).get("preTokens")
                seen = None
                continue
        if _B_USAGE not in raw or _B_ASSISTANT not in raw or _B_SIDECHAIN in raw:
            continue
        if not _has_tokens(raw):
            continue  # API-error line, no context in it
        m = _MSG_ID_RE.search(raw)
        if m is None:
            continue  # not a billed API call
        mid = m.group(1)
        if mid == seen:
            continue  # another content block of the same call, same context
        obj = _loads(raw)
        reading = _reading_from_obj(obj)
        if reading is None:
            continue
        total = reading[0]
        if (pending_boundary and pre_tokens
                and total >= COMPACT_CALL_SHARE * pre_tokens):
            continue  # the compaction call itself, still on the old context
        ts = _parse_ts_ms(obj.get("timestamp"))
        if ts is None:
            continue  # undatable sample: it would wreck the span and the ETA
        samples.append((total, ts.timestamp(), pending_boundary))
        pending_boundary = False
        seen = mid
    return samples, last_boundary


def annotate_pace(contexts, stats, tail_bytes=500_000) -> None:
    """Add growth rate and auto-compact forecast to each active context.

    Mutates the dicts from `claude_parser.current_contexts()` in place, so
    "active" stays defined in exactly one place. All values are JSON-native and
    therefore ride along in the dashboard cache for free.

    Every key in PACE_KEYS is written on every context, whatever happens —
    including when the transcript is unreadable or `~/.claude` is missing. A
    renderer may safely index them; only their values can be None.
    """
    if not contexts:
        return
    for ctx in contexts:
        for key in PACE_KEYS:
            ctx.setdefault(key, None)
    try:
        base = os.path.join(_claude_dir(), "projects")
    except Exception:
        return
    # newest boundary per session, as a fallback when the tail predates it
    last_by_session = {}
    for ev in getattr(stats, "events", None) or []:
        sid = ev.session_id
        if not sid or ev.ts is None:
            continue
        prev = last_by_session.get(sid)
        if prev is None or (prev.ts or datetime.min.replace(tzinfo=timezone.utc)) < ev.ts:
            last_by_session[sid] = ev

    fill = getattr(stats, "auto_fill_ratio", None) or AUTO_FILL_FALLBACK
    for ctx in contexts:
        try:
            _annotate_one(ctx, base, fill, last_by_session, tail_bytes)
        except Exception:
            pass  # keys are already seeded with None above


def _annotate_one(ctx, base, fill, last_by_session, tail_bytes) -> None:
    tokens = ctx.get("tokens") or 0
    model = ctx.get("model")
    # slug and session come back verbatim from the on-disk dashboard cache,
    # which is a file like any other: a slug of "../.." would read outside
    # ~/.claude/projects entirely. Neither is ever a path, so refuse anything
    # that looks like one.
    slug, session = ctx.get("slug") or "", ctx.get("session") or ""
    if os.sep in slug or os.sep in session or ".." in (slug, session):
        return
    path = os.path.join(base, slug, session)
    samples, boundary = _tail_samples(path, tail_bytes)
    pace = _pace_from_samples(samples)

    window = pricing.context_window(model, tokens)
    threshold = int(round(fill * window))
    growth = pace["growth_per_turn"]
    per_hour = pace["growth_per_hour"]
    left = threshold - tokens

    turns_to_auto = left / growth if (growth and growth > 0 and left > 0) else None
    eta = left / per_hour * 3600.0 if (per_hour and per_hour > 0 and left > 0) else None

    last_ts = last_trigger = None
    if boundary:
        meta = boundary.get("compactMetadata") or {}
        ts = _parse_ts_ms(boundary.get("timestamp"))
        last_ts = ts.isoformat() if ts else None
        last_trigger = meta.get("trigger")
    else:
        sid = (ctx.get("session") or "").removesuffix(".jsonl")
        ev = last_by_session.get(sid)
        if ev is not None:
            last_ts = ev.ts.isoformat() if ev.ts else None
            last_trigger = ev.trigger

    ctx["growth_per_turn"] = float(growth) if growth is not None else None
    ctx["growth_per_hour"] = float(per_hour) if per_hour is not None else None
    ctx["pace_samples"] = int(pace["samples"])
    ctx["pace_span_s"] = float(pace["span_s"])
    ctx["auto_threshold"] = threshold
    ctx["turns_to_auto"] = float(turns_to_auto) if turns_to_auto is not None else None
    ctx["eta_auto_s"] = float(eta) if eta is not None else None
    ctx["last_compact_ts"] = last_ts
    ctx["last_compact_trigger"] = last_trigger


# ----------------------------------------------------------------------
# serialization (dashboard cache)
# ----------------------------------------------------------------------
def to_json(stats) -> dict:
    return {
        "available": stats.available,
        "status": stats.status,
        "measured": stats.measured,
        "total": stats.total,
        "auto": stats.auto,
        "manual": stats.manual,
        "auto_share": stats.auto_share,
        "post_compact_tokens": stats.post_compact_tokens,
        "summary_output_tokens": stats.summary_output_tokens,
        "auto_fill_ratio": stats.auto_fill_ratio,
        "manual_fill_ratio": stats.manual_fill_ratio,
        "pre_median": stats.pre_median,
        "post_median": stats.post_median,
        "ctx_after_median": stats.ctx_after_median,
        "ctx_after_min": stats.ctx_after_min,
        "ctx_after_max": stats.ctx_after_max,
        "duration_median_s": stats.duration_median_s,
        "turns_total": stats.turns_total,
        "by_session": stats.by_session,
        "scanned_files": stats.scanned_files,
        "scan_s": stats.scan_s,
        "first_ts": stats.first_ts.isoformat() if stats.first_ts else None,
        "last_ts": stats.last_ts.isoformat() if stats.last_ts else None,
        # `total` counts every event; the list may be cut for cache size, and
        # `truncated` says so — otherwise `total == len(events)` would look
        # like a broken invariant after a round trip.
        "truncated": stats.truncated or len(stats.events) > MAX_CACHED_EVENTS,
        "events": [e.to_dict() for e in stats.events[-MAX_CACHED_EVENTS:]],
    }


def from_json(obj) -> CompactStats:
    stats = CompactStats()
    if not isinstance(obj, dict):
        return stats
    stats.events = [CompactEvent.from_dict(e) for e in obj.get("events") or []]
    for name in ("available", "status", "measured", "total", "auto", "manual",
                 "truncated",
                 "auto_share", "post_compact_tokens", "summary_output_tokens",
                 "auto_fill_ratio", "manual_fill_ratio", "pre_median",
                 "post_median", "ctx_after_median", "ctx_after_min",
                 "ctx_after_max", "duration_median_s", "turns_total",
                 "by_session", "scanned_files", "scan_s"):
        if name in obj and obj[name] is not None:
            setattr(stats, name, obj[name])
    stats.first_ts = _parse_ts_ms(obj.get("first_ts"))
    stats.last_ts = _parse_ts_ms(obj.get("last_ts"))
    return stats
