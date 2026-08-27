"""Parse Claude Code session transcripts (~/.claude/projects/*/*.jsonl).

Key facts (verified against live data):
- One API call writes one JSONL line PER CONTENT BLOCK. All lines of a call
  share the same message.id / requestId and output_tokens grows monotonically.
  Naive line-summing overcounts ~2.4x. Dedupe by message.id keeping the line
  with the largest output_tokens.
- costUSD field was removed in recent Claude Code versions -> always compute
  cost from token counts x per-model pricing.
"""

import json
import os
import glob
import time
from datetime import datetime, timezone

from . import session_cache
from .pricing import (display_name, price_for, calc_cost, calc_credits,
                      cache_write_price, CREDIT_FACTOR)


def _claude_dir():
    """Return the Claude config dir honoring CLAUDE_CONFIG_DIR."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser("~/.claude")


class Session:
    __slots__ = (
        "session_id", "project_dir", "project", "start", "end",
        "input_tokens", "output_tokens", "cache_read", "cache_write",
        "cost", "cost_input", "cost_output", "credits", "messages",
        "user_messages", "model",
        # `name` is a property over these three, so it must not be a slot
        "custom_title", "agent_name", "ai_title",
        "cost_cache_read", "cost_cache_write", "cache_write_5m",
        "git_branch", "by_day", "by_hour", "by_model",
    )

    def __init__(self, session_id, project_dir, project):
        self.session_id = session_id
        self.project_dir = project_dir
        self.project = project
        self.start = None
        self.end = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read = 0
        self.cache_write = 0
        self.cost = 0.0
        self.cost_input = 0.0
        self.cost_output = 0.0
        # input side split out, so a per-token rate can be formed against the
        # matching token count (the savings estimate needs that)
        self.cost_cache_read = 0.0
        self.cost_cache_write = 0.0
        # share of cache_write billed at the cheaper 5-minute TTL rate
        self.cache_write_5m = 0
        self.credits = 0.0
        self.messages = 0
        self.user_messages = 0
        self.model = None
        # Three title kinds live in a transcript, and only one of them is the
        # name the user sees in Claude Code: `custom-title` is what they typed,
        # `agent-name`/`ai-title` are generated. All three repeat once per turn
        # and interleave, so "keep the latest line" picked whichever happened to
        # come last — measured: 14 of 82 local transcripts carry *only* a
        # `custom-title`, which is why a renamed session fell back to its repo
        # name. `name` is derived, never assigned directly.
        self.custom_title = None
        self.agent_name = None
        self.ai_title = None
        # last observed git branch; a label reserve for sessions without a name
        self.git_branch = None
        # A transcript can span many local days (measured: 26 of 59 files do,
        # the longest 44 days), so daily figures must follow each line's own
        # timestamp instead of the session start. Both maps are plain
        # dict/list/int so a future per-file parse cache can serialize them.
        self.by_day = {}   # local "YYYY-MM-DD" -> [cost, input_tokens, output_tokens]
        self.by_hour = {}  # local "weekday,hour" (Mon=0) -> user message count
        # RAW model id -> per-model totals. Raw, not display_name, because the
        # price table is indexed on the id: this is what answers "what would the
        # same work have cost on a cheaper model". `Session.model` (last one
        # seen) cannot answer that — a session that switched models would be
        # attributed entirely to whichever model happened to come last.
        self.by_model = {}  # id -> {calls,input,output,cache_read,
                            #        cache_write,cache_write_5m,cost}

    @property
    def name(self):
        """The session's display name, by the same precedence Claude Code uses."""
        return session_title(self.custom_title, self.agent_name, self.ai_title)


def session_title(custom_title, agent_name, ai_title):
    """Pick the session name a user would recognise out of the three candidates.

    What the user typed wins over what a model generated, and of the two
    generated ones `agent-name` is the more current (`ai-title` is written once
    per turn too, but a resumed session keeps the title of the work it was
    forked from — this session carries "Review project and fix update issues"
    from a transcript two days older).
    """
    return custom_title or agent_name or ai_title or None


def display_label(name=None, project=None, branch=None, session_id=None):
    """The label to show for a session that has no name of its own.

    `session_title` decides what the name IS; this decides what to show when
    there is none. Both used to be re-invented per call site — five of them,
    all different: one showed the bare folder, one folder:branch, one the
    first eight characters of the id, two a question mark. The branch is the
    documented reserve (108 distinct values across the local history), which
    at least distinguishes two worktrees of one repo from each other.
    """
    if name:
        return name
    if project and branch:
        return f"{project}:{branch}"
    if project:
        return project
    if session_id:
        return session_id[:8]
    return "?"


_TITLE_KEYS = (("custom-title", "customTitle"),
               ("agent-name", "agentName"),
               ("ai-title", "aiTitle"))


def _scan_titles(lines, reverse=False):
    """Collect (custom_title, agent_name, ai_title) from raw transcript lines.

    With `reverse`, the first value seen per kind wins — that is the newest one
    when reading a tail backwards. Stops as soon as all three are known.
    """
    found = {}
    for line in (reversed(lines) if reverse else lines):
        # cheap prefilter before json.loads — these lines are a handful among
        # thousands, and the tail can be half a megabyte
        if "-title" not in line and '"agent-name"' not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = obj.get("type")
        for kind, key in _TITLE_KEYS:
            if typ != kind or not obj.get(key):
                continue
            if reverse and kind in found:
                break  # already have the newer one
            found[kind] = obj[key]
            break
        if len(found) == len(_TITLE_KEYS):
            break
    return tuple(found.get(kind) for kind, _ in _TITLE_KEYS)


def _parse_ts_ms(v):
    """Parse a timestamp into a datetime (UTC).

    Handles both epoch numbers (ms or s) and ISO-8601 strings — Claude Code
    session transcripts use ISO strings, rtk/caveman use epoch ms.
    """
    if v is None:
        return None
    if isinstance(v, str):
        try:
            s = v.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).astimezone(timezone.utc)
        except ValueError:
            return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v > 1e12:
        v /= 1000.0
    return datetime.fromtimestamp(v, tz=timezone.utc)


def _local_bucket(ts, cache):
    """Local day and weekday/hour bucket keys for a UTC timestamp.

    Returns ("YYYY-MM-DD", "weekday,hour") in local time, weekday Mon=0 (that
    is what Python's weekday() gives — no re-mapping).

    Results are memoized per epoch minute in `cache`. UTC offsets are always a
    whole number of minutes, so neither the local day nor the local hour can
    change inside one minute; this stays exact for the :30/:45 offset zones,
    which a per-UTC-hour cache would get wrong. The parser reads ~150k lines on
    every refresh, and the timezone conversion is the expensive part: memoizing
    it cuts the per-line cost by ~4x versus calling astimezone() per line.
    """
    key = int(ts.timestamp()) // 60
    v = cache.get(key)
    if v is None:
        local = ts.astimezone()
        v = ("%04d-%02d-%02d" % (local.year, local.month, local.day),
             "%d,%d" % (local.weekday(), local.hour))
        cache[key] = v
    return v


def _add_day(session, day, cost, input_tokens, output_tokens):
    """Accumulate one line's contribution into the session's daily bucket."""
    b = session.by_day.get(day)
    if b is None:
        session.by_day[day] = [cost, input_tokens, output_tokens]
    else:
        b[0] += cost
        b[1] += input_tokens
        b[2] += output_tokens


# Bucket key for a billed call whose line names no model. Such calls exist and
# they cost real money, so they get a visible key of their own instead of an
# empty string that reads like a bug or, worse, silently disappearing.
# `pricing.price_for` does not know it and falls back to its default rates,
# which is the same treatment the rest of the tool gives an unknown model.
UNKNOWN_MODEL = "(unknown)"


def _add_model(session, model, calls, input_tokens, output_tokens,
               cache_read, cache_write, cache_write_5m, cost):
    """Accumulate one line's contribution into the session's per-model bucket."""
    key = model or UNKNOWN_MODEL
    b = session.by_model.get(key)
    if b is None:
        session.by_model[key] = {
            "calls": calls,
            "input": input_tokens,
            "output": output_tokens,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "cache_write_5m": cache_write_5m,
            "cost": cost,
        }
    else:
        b["calls"] += calls
        b["input"] += input_tokens
        b["output"] += output_tokens
        b["cache_read"] += cache_read
        b["cache_write"] += cache_write
        b["cache_write_5m"] += cache_write_5m
        b["cost"] += cost


def _slug_to_project(slug):
    """'home-user-repositories-my-project' slug -> readable project name.

    Claude encodes the cwd as a slug by replacing path separators with '-'.
    We take everything after '-repositories-' when present, else the last
    two segments, which is the most readable short label.
    """
    marker = "-repositories-"
    if marker in slug:
        tail = slug.split(marker, 1)[1]
        # nested worktrees: keep first segment(s) as project, join rest
        parts = [p for p in tail.split("-") if p]
        if parts:
            return "-".join(parts) if len(parts) <= 3 else "-".join(parts[:3])
        # slug IS the repositories dir itself
        return "repositories"
    parts = [p for p in slug.split("-") if p]
    return parts[-1] if parts else slug


def _scan_session_file(path, slug, session=None, counted=None, start_offset=0,
                       claimed=None):
    """Parse bytes [start_offset, EOF) of one transcript into a Session.

    Returns `(session, counted, commit_offset, commit_payload)`.

    `session` and `counted` may come from the parse cache, in which case this
    picks up exactly where the previous run stopped. Passing neither parses the
    whole file, which is what a cold cache and the equality tests do.

    `commit_offset` is the offset just past the last line that ended in a
    newline, and `commit_payload` is the parser state at precisely that point.
    The distinction matters: a transcript caught mid-append ends in a
    half-written line. That line IS counted into the Session returned here — a
    full scan counts it too, and the two paths have to agree — but it is
    deliberately NOT part of the committed state. The next run resumes in front
    of it and reads it once it is complete. Committing the state after the loop
    instead is how this goes wrong: the half line gets counted once now and a
    second time once it is finished.

    Opens in binary because the cache needs real byte offsets; text-mode
    `tell()` returns an opaque cookie. Lines are handed to `json.loads` as bytes
    (it decodes UTF-8 itself, in C) and only malformed ones pay for an explicit
    lossy decode, which preserves the old `errors="replace"` behaviour.
    """
    # message.id -> how many output tokens of that message we already counted,
    # so a later content-block line with a larger snapshot contributes only its
    # delta. Carried across a resume, which is the whole difficulty here: the
    # dedupe (AGENTS.md rule 6) must not break at a cache boundary.
    counted = dict(counted) if counted else {}
    proj_name = _slug_to_project(slug)
    # epoch-minute -> (local day, "weekday,hour"); see _local_bucket
    tz_cache = {}
    offset = start_offset
    commit_offset = start_offset
    commit_payload = None

    try:
        with open(path, "rb") as fh:
            if start_offset:
                fh.seek(start_offset)
            for raw in fh:
                offset += len(raw)
                if not raw.endswith(b"\n"):
                    # half-written trailing line: freeze the cache state in
                    # front of it before counting it into the live session
                    commit_payload = _payload(session, counted)
                elif commit_payload is None:
                    commit_offset = offset
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    try:
                        obj = json.loads(raw.decode("utf-8", "replace"))
                    except (json.JSONDecodeError, ValueError):
                        continue

                typ = obj.get("type")
                ts = _parse_ts_ms(obj.get("timestamp"))
                if typ == "assistant":
                    msg = obj.get("message") or {}
                    usage = msg.get("usage") or {}
                    mid = msg.get("id") or obj.get("requestId") or obj.get("uuid")
                    ot = usage.get("output_tokens") or 0
                    if mid is not None:
                        # normalized to str because `counted` round-trips
                        # through JSON in the parse cache, where every key comes
                        # back a string; a non-str id would miss its own entry
                        # after a resume and be counted as a first sight again
                        mid = str(mid)
                        # Forking a session copies the older turns into the new
                        # transcript, message.id and all. Those copies are not
                        # new API calls and were never billed again, so the
                        # first transcript to claim an id keeps it and every
                        # later copy is skipped outright. Measured: 470 ids in
                        # 7 pairs of transcripts, worth $52 of double-counted
                        # spend before this guard existed.
                        if claimed is not None:
                            holder = claimed.get(mid)
                            if holder is not None and holder != path:
                                continue
                            claimed[mid] = path
                        if mid in counted:
                            prev = counted[mid]
                            if ot <= prev:
                                continue
                            # newer snapshot of the same API call: keep only
                            # the delta so growing output_tokens isn't summed
                            delta = ot - prev
                            counted[mid] = ot
                            first_sight = False
                        else:
                            delta = ot
                            counted[mid] = ot
                            first_sight = True
                    else:
                        delta = ot
                        first_sight = True

                    sid = obj.get("sessionId") or obj.get("session_id")
                    if sid and session is None:
                        session = Session(sid, os.path.dirname(path), proj_name)
                    if session is None:
                        session = Session(slug, os.path.dirname(path), proj_name)

                    if ts is not None:
                        if session.start is None or ts < session.start:
                            session.start = ts
                        if session.end is None or ts > session.end:
                            session.end = ts

                    model = msg.get("model") or obj.get("model")
                    if model:
                        session.model = model
                    branch = obj.get("gitBranch")
                    if branch and branch != "HEAD":
                        session.git_branch = branch  # "HEAD" is detached, no label

                    # priced at the moment of the call, not at today's rates:
                    # an introductory price that has since ended must not
                    # re-price a year of history (pricing._DATED_PRICING)
                    when = ts or session.start
                    i_price, o_price, cr_price, cw_price, _ = price_for(model, when)
                    it = usage.get("input_tokens") or 0
                    cc = usage.get("cache_creation_input_tokens") or 0
                    cr = usage.get("cache_read_input_tokens") or 0
                    # daily buckets follow THIS line's timestamp, not the session
                    # start; a session can span weeks. Filled inside the dedupe
                    # branches below so buckets and totals cannot drift apart.
                    day = None
                    if ts is not None:
                        day = _local_bucket(ts, tz_cache)[0]
                    elif session.start is not None:
                        day = _local_bucket(session.start, tz_cache)[0]
                    out_cost = (delta * o_price) / 1_000_000
                    # input-side tokens are per-call, not per-block: only count
                    # them the FIRST time we see this message.id; the content-
                    # block lines only grow output_tokens.
                    if first_sight:
                        # cache writes are billed per TTL: 5 minutes costs
                        # 1.25x input, 1 hour 2.0x. The breakdown is per call,
                        # so it belongs in this branch only. Older transcripts
                        # have no breakdown -> everything counts as 1h, which is
                        # what the code assumed before.
                        cc5 = 0
                        breakdown = usage.get("cache_creation")
                        if isinstance(breakdown, dict):
                            cc5 = breakdown.get("ephemeral_5m_input_tokens") or 0
                            if cc5 > cc:
                                cc5 = cc  # a split can't exceed the reported total
                        cc1 = cc - cc5
                        cw5_price = cache_write_price(model, "5m", when)
                        cost_write = (cc1 * cw_price + cc5 * cw5_price) / 1_000_000

                        session.input_tokens += it
                        session.cache_read += cr
                        session.cache_write += cc
                        session.cache_write_5m += cc5
                        # cost: input billed at input rate, cache-write at the
                        # write rate, NOT both (that was the phantom-cost bug)
                        session.cost_input += (it * i_price + cr * cr_price) / 1_000_000 + cost_write
                        session.cost_cache_read += (cr * cr_price) / 1_000_000
                        session.cost_cache_write += cost_write
                        call_cost = calc_cost(model, it, delta, cr, cc, cc5, when)
                        session.credits += calc_credits(model, it, delta, cr, cc, cc5, when)
                        session.cost += call_cost
                        if day is not None:
                            _add_day(session, day, call_cost, it, delta)
                        _add_model(session, model, 1, it, delta, cr, cc, cc5,
                                   call_cost)
                    else:
                        session.credits += out_cost * CREDIT_FACTOR
                        session.cost += out_cost
                        if day is not None:
                            _add_day(session, day, out_cost, 0, delta)
                        # same dedupe rule as everywhere else: a continuation
                        # line contributes only its output delta, and no call
                        _add_model(session, model, 0, 0, delta, 0, 0, 0, out_cost)
                    session.output_tokens += delta
                    session.cost_output += out_cost
                    session.messages += 1

                elif typ == "user":
                    if session is None:
                        sid = obj.get("sessionId") or obj.get("session_id") or slug
                        session = Session(sid, os.path.dirname(path), proj_name)
                    if ts is not None:
                        if session.start is None or ts < session.start:
                            session.start = ts
                        if session.end is None or ts > session.end:
                            session.end = ts
                    branch = obj.get("gitBranch")
                    if branch and branch != "HEAD":
                        session.git_branch = branch
                    # activity heatmap: one count per real user message, bucketed
                    # by the message's own local weekday/hour
                    hour_key = None
                    if ts is not None:
                        hour_key = _local_bucket(ts, tz_cache)[1]
                    elif session.start is not None:
                        hour_key = _local_bucket(session.start, tz_cache)[1]
                    if hour_key is not None:
                        session.by_hour[hour_key] = session.by_hour.get(hour_key, 0) + 1
                    session.user_messages += 1
                    session.messages += 1
                elif typ in ("ai-title", "agent-name", "custom-title"):
                    if session is None:
                        sid = obj.get("sessionId") or obj.get("session_id") or slug
                        session = Session(sid, os.path.dirname(path), proj_name)
                    # each kind is kept in its own slot (latest wins within a
                    # kind, they repeat per turn); the precedence between kinds
                    # lives in `session_title`, not in the file order
                    if obj.get("customTitle"):
                        session.custom_title = obj["customTitle"]
                    if obj.get("agentName"):
                        session.agent_name = obj["agentName"]
                    if obj.get("aiTitle"):
                        session.ai_title = obj["aiTitle"]
    except (OSError, PermissionError):
        pass

    if commit_payload is None:
        commit_payload = _payload(session, counted)
    return session, counted, commit_offset, commit_payload


# ---------------------------------------------------------------------------
# Parse cache: Session <-> the payload stored per file (see session_cache.py).
#
# `project_dir` and `project` are deliberately NOT stored. Both are pure
# functions of where the transcript lives, and deriving them on restore keeps a
# cache that outlived a moved or renamed project from resurrecting a stale path.
# ---------------------------------------------------------------------------
_SESSION_INTS = (
    "input_tokens", "output_tokens", "cache_read", "cache_write",
    "cache_write_5m", "messages", "user_messages",
)
_SESSION_FLOATS = (
    "cost", "cost_input", "cost_output", "cost_cache_read", "cost_cache_write",
    "credits",
)
# `name` is not in here: it is derived from the three title slots, and a
# property has no setter to restore into. Cached payloads written before that
# change carry a "name" key and no slots — PARSE_VERSION rejects them.
_SESSION_OPT_STRS = ("model", "custom_title", "agent_name", "ai_title",
                     "git_branch")


def _session_to_dict(s):
    """Serialize a Session for the parse cache. JSON-native types only."""
    obj = {name: getattr(s, name) for name in _SESSION_INTS}
    for name in _SESSION_FLOATS:
        obj[name] = getattr(s, name)
    for name in _SESSION_OPT_STRS:
        obj[name] = getattr(s, name)
    obj["session_id"] = s.session_id
    obj["start"] = s.start.isoformat() if s.start else None
    obj["end"] = s.end.isoformat() if s.end else None
    # copies, not references: this dict is a snapshot of state the parser goes
    # on mutating afterwards (the half-written-trailing-line case)
    obj["by_day"] = {k: list(v) for k, v in s.by_day.items()}
    obj["by_hour"] = dict(s.by_hour)
    obj["by_model"] = {k: dict(v) for k, v in s.by_model.items()}
    return obj


def _session_from_dict(obj, path, slug):
    """Rebuild a Session from a parse-cache payload.

    Raises on anything malformed — the caller turns that into a full rescan of
    the file, which is always safe.
    """
    s = Session(obj["session_id"], os.path.dirname(path), _slug_to_project(slug))
    for name in _SESSION_INTS:
        setattr(s, name, int(obj[name]))
    for name in _SESSION_FLOATS:
        setattr(s, name, float(obj[name]))
    for name in _SESSION_OPT_STRS:
        setattr(s, name, obj[name])
    s.start = _parse_ts_ms(obj["start"])
    s.end = _parse_ts_ms(obj["end"])
    s.by_day = {k: [float(v[0]), int(v[1]), int(v[2])]
                for k, v in (obj["by_day"] or {}).items()}
    s.by_hour = {k: int(v) for k, v in (obj["by_hour"] or {}).items()}
    s.by_model = {k: {kk: vv for kk, vv in v.items()}
                  for k, v in (obj["by_model"] or {}).items()}
    return s


def _payload(session, counted):
    """Snapshot of the parser state for the cache. Both members are copies.

    `counted` is stored WHOLE and is never truncated. Measured over this
    history: of 26.559 message.ids, 26.558 have all their content-block lines
    contiguous — but exactly one reappears 59 distinct ids after its first line.
    So "keep the last few" is not a safe rule, only a usually-true one, and a
    dropped entry silently re-bills a whole call's input side on the next
    resume. The full dict costs about a megabyte of JSON for the entire history,
    which is far cheaper than being wrong.
    """
    return {
        "session": _session_to_dict(session) if session is not None else None,
        "counted": dict(counted),
    }


def _parse_file(path, slug, entry, claimed=None):
    """Parse one transcript, reusing `entry` from the parse cache if it fits.

    Returns `(session_or_None, new_entry_or_None)`. A cached payload that will
    not rebuild is not an error: it just means this file is parsed from scratch,
    which is exactly what a cache miss is supposed to cost — work, never a
    different number.
    """
    start, usable = session_cache.plan(path, entry)
    session, counted = None, None
    if usable is not None:
        try:
            payload = usable.get("session")
            session = _session_from_dict(payload, path, slug) if payload else None
            counted = {str(k): int(v) for k, v in (usable.get("counted") or {}).items()}
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            session, counted, start = None, None, 0
    session, counted, offset, payload = _scan_session_file(
        path, slug, session, counted, start, claimed)
    return session, session_cache.new_entry(path, offset, payload)


def parse_sessions(max_files=None, progress_cb=None, use_cache=True, problems=None):
    """Parse all session transcripts. Returns list[Session], newest first.

    If progress_cb is given it is called with (done, total) after each file.

    `use_cache` reads and writes the per-file parse cache (`session_cache`):
    an unchanged transcript is restored from it and a grown one is resumed at a
    byte offset, so a refresh costs the new bytes instead of all 399 MB every
    60 seconds. `use_cache=False` forces a full scan and writes nothing — that
    is the reference the equality tests compare the cached path against.
    """
    base = os.path.join(_claude_dir(), "projects")
    all_files = glob.glob(os.path.join(base, "*", "*.jsonl"))

    cache = session_cache.load() if use_cache else {}

    # Order by recency so max_files keeps the NEWEST sessions rather than the
    # alphabetically-last paths. mtime is the fallback, not the measure: 54 of
    # 108 local transcripts have an mtime more than ten minutes ahead of their
    # newest content, because resuming a session touches the file and appends
    # untimestamped metadata (AGENTS.md rule 12). Where the parse cache already
    # knows a session's last timestamped line, that wins.
    def _recency(path):
        entry = cache.get(path) or {}
        end = ((entry.get("session") or {}).get("end")) if entry else None
        if end:
            try:
                return _parse_ts_ms(end).timestamp()
            except (AttributeError, ValueError, TypeError):
                pass
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0

    all_files.sort()
    files = all_files
    if max_files and len(files) > max_files:
        # pick by recency, then parse in path order: the result must not depend
        # on whether the parse cache happened to be warm
        files = sorted(sorted(all_files, key=_recency)[-max_files:])

    fresh = {}
    sessions = []
    total = len(files)
    done = 0
    # Parse sequentially; simple and safe for typical histories (< a few
    # hundred files), and with the cache in place the loop is I/O-trivial.
    # Which transcript owns which message.id. Built as we go, in the fixed path
    # order above, so the answer never depends on whether the cache was warm.
    claimed = {}

    for path in files:
        slug = os.path.basename(os.path.dirname(path))
        entry = cache.get(path)
        # A cached entry was computed without knowing about the files parsed
        # before it. If any id it counted has since been claimed elsewhere, its
        # aggregate includes a copy it may no longer count — rescan that one.
        if entry is not None:
            mine = entry.get("counted") or {}
            if any(claimed.get(str(k), path) != path for k in mine):
                entry = None
        try:
            session, entry = _parse_file(path, slug, entry, claimed)
        except Exception as exc:
            # One bad transcript must not lose the rest — but it silently
            # shortened every total on the dashboard, with nothing anywhere
            # saying a session had been dropped.
            session, entry = None, None
            if problems is not None:
                problems.append(f"{os.path.basename(path)}: {exc}")
        if session is not None:
            sessions.append(session)
        if entry is not None:
            fresh[path] = entry
        done += 1
        if progress_cb:
            progress_cb(done, total)

    if use_cache:
        # Carry over entries for files this run did not look at (max_files caps
        # the list) and drop entries whose transcript is gone. Without the
        # carry-over a single `--json --max-files` run would throw away the
        # cache for everything else.
        for path in all_files:
            if path not in fresh and path in cache:
                fresh[path] = cache[path]
        # Only write when something actually changed. The comparison costs a
        # few ms; serializing and writing the whole cache costs more than that
        # and would do it every 60 seconds forever, for nothing.
        if fresh != cache:
            session_cache.save(fresh)

    def _sort_key(s):
        return s.start or datetime.min.replace(tzinfo=timezone.utc)
    sessions.sort(key=_sort_key, reverse=True)
    return sessions


def current_contexts(active_within_s=600, tail_bytes=500_000):
    """Context-window fill of ALL currently active sessions.

    "Active" means the transcript's last *timestamped* line is within
    `active_within_s` seconds. mtime alone is not activity: 40 of 66 local
    transcripts have an mtime more than ten minutes ahead of their newest
    content, because Claude touches a file when a session is resumed and
    appends untimestamped metadata (`last-prompt`, `ai-title`, `mode`, …) on
    open. That put sessions dormant for two days into the panel with "Seen 0s"
    next to a two-day-old fill — and made the compact hint recommend them.
    mtime never runs *behind* the content, so it stays as a cheap prefilter.

    Returns a list of dicts, newest first:
        {"model": str, "tokens": int, "session": filename, "project": str,
         "slug": str, "age_s": int, "content_age_s": int, "touched_s": int}

    `age_s` is the age of the usage block the tokens come from — the age of the
    number shown, not of the file. `touched_s` is the mtime age, kept so a
    divergence can be displayed rather than hidden.
    """
    base = os.path.join(_claude_dir(), "projects")
    now = time.time()
    out = []
    for path in glob.glob(os.path.join(base, "*", "*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        touched = int(now - mtime)
        if touched > active_within_s:
            continue  # mtime never lags the content, so this only drops the dead
        ctx = _read_tail_usage(path, tail_bytes)
        if ctx is None:
            continue
        last_ts = ctx.get("last_ts")
        if last_ts is None or now - last_ts > active_within_s:
            continue  # touched, but nothing was said — a resumed dormant session
        ctx.pop("last_ts", None)
        usage_ts = ctx.pop("usage_ts", None)
        ctx["content_age_s"] = int(now - last_ts)
        ctx["touched_s"] = touched
        ctx["age_s"] = int(now - usage_ts) if usage_ts else ctx["content_age_s"]
        slug = os.path.basename(os.path.dirname(path))
        ctx["slug"] = slug  # raw cwd slug — lets rtk paths be matched to sessions
        ctx["project"] = _slug_to_project(slug)
        out.append(ctx)
    out.sort(key=lambda c: c["content_age_s"])
    return out


def _read_tail_usage(path, tail_bytes):
    """Extract the last assistant usage block from a transcript tail.

    Also picks up the session name. All three title kinds (`custom-title`,
    `agent-name`, `ai-title`) repeat once per turn, so the tail holds the
    current value of each; the head is scanned only for a transcript whose
    titles were written before the tail window begins.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(60_000).decode("utf-8", errors="replace")
            # The head read has already moved the handle. Rewinding it is not
            # cosmetic: for a transcript smaller than the head read the handle
            # now sits at EOF, so the tail read returned "" and the whole file
            # was reported as having no context at all. Measured: all 17
            # transcripts under 60 KB were dropped, and the youngest sessions —
            # exactly the ones the Active-sessions panel exists for — are the
            # small ones.
            if size > tail_bytes:
                fh.seek(-tail_bytes, os.SEEK_END)
            else:
                fh.seek(0)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    titles = _scan_titles(data.splitlines(), reverse=True)
    if not any(titles):
        titles = _scan_titles(head.splitlines())
    name = session_title(*titles)

    # Two different ages, and conflating them was the bug: `last_ts` is when
    # anything was last *said* (activity), `usage_ts` is when the fill below was
    # measured. Metadata lines carry no timestamp, so touching a file on resume
    # moves neither.
    last_ts = None
    found = None
    for line in reversed(data.splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts_ms(obj.get("timestamp"))
        if ts and (last_ts is None or ts > last_ts):
            last_ts = ts
        if found is not None:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message") or {}
        usage = msg.get("usage") or {}
        if not usage:
            continue
        total = (usage.get("input_tokens") or 0) \
            + (usage.get("cache_creation_input_tokens") or 0) \
            + (usage.get("cache_read_input_tokens") or 0)
        if total <= 0:
            continue
        found = {
            "model": msg.get("model") or obj.get("model"),
            "tokens": total,
            "session": os.path.basename(path),
            "name": name,
            "usage_ts": ts.timestamp() if ts else None,
        }
        # keep scanning: the newest timestamped line may be a user message
        # below the last assistant reply — that is still activity
        if last_ts is not None:
            break
    if found is not None:
        found["last_ts"] = last_ts.timestamp() if last_ts else None
    return found

