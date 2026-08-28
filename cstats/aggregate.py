"""Aggregate all data sources into one Dashboard snapshot."""

import os
import re
from datetime import datetime, timedelta, timezone

from . import claude_parser, rtk, caveman, pricing
from . import compacts as compacts_mod
from . import limits as limits_mod
from .rtk import RtkStats
from .caveman import CavemanStats


def _path_to_slug(path):
    """A cwd path -> the name Claude gives its transcript directory.

    Claude replaces every character outside [A-Za-z0-9-] with '-', so
    '/home/u/repo/.claude/worktrees/x' becomes '-home-u-repo--claude-worktrees-x'.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", (path or "").rstrip("/"))


def _dominant_model(s):
    """Label a session by the model that actually produced its output.

    Neither of the two "the model of this session" fields available survives
    contact with a long session. `Session.model` is last-model-wins, and
    caveman's history writes first-model-wins; a session that ran across a
    month showed "Opus 5" (17% of its output) here and "Fable 5" (4%) there,
    while 79% of it was Opus 4.8. The per-call buckets are the only honest
    source — the same fix that removed "$817 Unknown" from the overview.

    A session that spent less than 90% of its output on one model gets a "*"
    marker, because naming one model for it would be a lie in either direction.
    The marker is one character on purpose: the Model column is the narrowest
    in two tables, and a longer one is the first thing rich ellipsises away.
    """
    buckets = getattr(s, "by_model", None) or {}
    real = {mid: b.get("output", 0) for mid, b in buckets.items()
            if not mid.startswith("<")}  # "<synthetic>" is not a model
    total = sum(real.values())
    if not total:
        return None
    mid, out = max(real.items(), key=lambda kv: kv[1])
    name = claude_parser.display_name(mid)
    return name if out / total >= 0.9 else f"{name}*"


class Dashboard:
    def __init__(self, sessions=None, limits=None, rtk_stats=None, caveman_stats=None,
                 generated_at=None, context=None, compact_stats=None, warnings=None):
        self.sessions = sessions or []
        self.limits = limits or limits_mod.UsageLimits()
        self.rtk = rtk_stats or rtk.RtkStats()
        self.caveman = caveman_stats or caveman.CavemanStats()
        # measured compaction history; feeds the break-even model in economics.py
        # with real post-compact context sizes instead of an assumption
        self.compacts = compact_stats or compacts_mod.CompactStats()
        self.generated_at = generated_at or datetime.now(timezone.utc)
        self.context = context  # list of dicts from claude_parser.current_contexts()
        # Transcripts this build could not read. Dropping one silently makes
        # every total quietly short, which is worse than an ugly line saying so.
        self.warnings = list(warnings or [])

        # computed aggregates
        self.total_cost = 0.0
        self.total_input = 0
        self.total_output = 0
        self.total_cache_read = 0
        self.total_cache_write = 0
        self.total_credits = 0.0
        self.credits_7d = 0.0    # credits of the last 7 days, for the limits tab
        self.total_sessions = 0
        self.total_messages = 0
        self.cost_input = 0.0     # input+cache side of the bill
        self.cost_output = 0.0    # output side
        self.cost_cache_read = 0.0   # cache-read share of cost_input
        self.cost_cache_write = 0.0  # cache-write share of cost_input
        self.cache_ratio = 0.0    # cache_read / (input + cache_read)
        self.by_day_cost = {}    # date -> cost
        self.by_day_tokens = {}  # date -> (in, out)
        self.by_project = {}     # project -> cost
        self.by_model = {}       # display name -> (cost, in, out), last-model-wins
        self.model_tokens = {}   # raw model id -> per-call totals, for repricing
        self.heatmap = {}        # "weekday,hour" -> user messages
        self.model_totals = {}
        self.project_list = []
        self.session_rows = []   # compact per-session list for the Sessions tab
        self.session_project = {}  # session_id -> project name
        self.session_name = {}   # session_id -> session name (agent-name/ai-title)
        self.session_model = {}  # session_id -> output-weighted model label
        self.slug_name = {}      # cwd slug -> session name of the session working there
        self._compute()
        self._link_savings()

    def _compute(self):
        # credits_7d is precomputed and persisted because the limits tab used to
        # derive it from self.sessions — which is empty after a cache-first
        # render, so the panel disappeared on every startup. It is computed from
        # the daily buckets further down, not from session start dates.
        for s in self.sessions:
            self.total_cost += s.cost
            self.total_input += s.input_tokens
            self.total_output += s.output_tokens
            self.total_cache_read += s.cache_read
            self.total_cache_write += s.cache_write
            self.total_credits += s.credits
            self.total_sessions += 1
            self.total_messages += s.messages
            self.cost_input += getattr(s, "cost_input", 0.0)
            self.cost_output += getattr(s, "cost_output", 0.0)
            self.cost_cache_read += getattr(s, "cost_cache_read", 0.0)
            self.cost_cache_write += getattr(s, "cost_cache_write", 0.0)

            # Daily figures and the activity heatmap come pre-bucketed from the
            # parser, per line and by that line's own local timestamp. Bucketing
            # a whole session onto its start day was wrong: 26 of 59 transcripts
            # span more than one local day, the longest 44 days, which piled a
            # month of work onto single cells.
            for day, b in s.by_day.items():
                self.by_day_cost[day] = self.by_day_cost.get(day, 0.0) + b[0]
                i, o = self.by_day_tokens.get(day, (0, 0))
                self.by_day_tokens[day] = (i + b[1], o + b[2])
            # weekday(0=Mon) x hour, local (Python weekday() is already Mon=0)
            for key, n in s.by_hour.items():
                self.heatmap[key] = self.heatmap.get(key, 0) + n

            pname = s.project or "(unknown)"
            self.by_project[pname] = self.by_project.get(pname, 0.0) + s.cost

            # per-model token counts, keyed by the raw model id so the pricing
            # table can be indexed with it. Session.model is last-model-wins and
            # unusable for this: a session whose last line was <synthetic> threw
            # its whole cost into one bucket ($817 of the history).
            for mid, b in (getattr(s, "by_model", None) or {}).items():
                tgt = self.model_tokens.get(mid)
                if tgt is None:
                    tgt = dict.fromkeys(
                        ("calls", "input", "output", "cache_read", "cache_write",
                         "cache_write_5m", "cost"), 0)
                    self.model_tokens[mid] = tgt
                for field, value in b.items():
                    tgt[field] = tgt.get(field, 0) + value

        # by_model (display names, for the overview table) is derived from the
        # same per-call attribution as model_tokens, so the two tabs cannot
        # disagree. Deriving it from Session.model showed "Unknown $817" in the
        # overview while the Economics tab had those dollars attributed.
        for mid, b in self.model_tokens.items():
            if not (b.get("cost") or b.get("output") or b.get("input")):
                continue
            mc = self.by_model.setdefault(claude_parser.display_name(mid), [0.0, 0, 0])
            mc[0] += b.get("cost", 0.0)
            mc[1] += b.get("input", 0)
            mc[2] += b.get("output", 0)

        # cache hit ratio: of all input-side tokens, how many came from cache
        billable_in = self.total_input + self.total_cache_read
        if billable_in:
            self.cache_ratio = self.total_cache_read / billable_in * 100

        # Credits of the last 7 days, dated by consumption. Summing the credits
        # of sessions that *started* in the window counts a long session in full
        # or not at all: only 6 of 67 sessions started inside the last week while
        # the expensive ones had been running for weeks, which made the figure
        # 9.7 instead of 138.8. The daily buckets already carry the right dates,
        # and credits are exactly proportional to cost, so scaling the buckets is
        # both simpler and correct.
        if self.by_day_cost:
            # days=6, not 7: the buckets are whole local days and the cutoff
            # day is included, so subtracting 7 gave today plus seven earlier
            # days — eight days of spend under a "7 days" label.
            cutoff = (self.generated_at.astimezone() - timedelta(days=6)).strftime("%Y-%m-%d")
            recent = sum(c for day, c in self.by_day_cost.items() if day >= cutoff)
            self.credits_7d = recent * pricing.CREDIT_FACTOR

        self.model_totals = sorted(self.by_model.items(), key=lambda kv: -kv[1][0])
        self.project_list = sorted(self.by_project.items(), key=lambda kv: -kv[1])

        # compact session rows (newest first) for the Sessions tab
        rows = []
        for s in self.sessions:
            if s.start is None or s.messages == 0:
                continue  # skip empty ai-title/agent-name stubs
            rows.append({
                "id": (s.session_id or "")[:8],
                "project": s.project or "?",
                "name": s.name or "",
                # label reserve: several sessions share one repo (worktrees), so
                # the branch says more than the folder when a session is unnamed
                "branch": s.git_branch or "",
                "date": s.start.astimezone().strftime("%Y-%m-%d %H:%M") if s.start else "?",
                "model": _dominant_model(s) or "?",
                "cost": round(s.cost, 2),
                "output": s.output_tokens,
                "messages": s.messages,
            })
        rows.sort(key=lambda r: r["date"], reverse=True)
        self.session_rows = rows[:200]

        for s in self.sessions:
            if s.session_id:
                self.session_project[s.session_id] = s.project or "?"
                if s.name:
                    self.session_name[s.session_id] = s.name
                label = _dominant_model(s)
                if label:
                    self.session_model[s.session_id] = label

    def _link_savings(self):
        """Label caveman and rtk savings with the session they belong to.

        Several sessions can run in one repo (worktrees, parallel workspaces),
        so a folder name says nothing about which work the savings came from.
        caveman records a session_id, which maps straight to a session name.
        rtk only records a cwd path — but Claude derives its project directory
        from exactly that path, so the path can be slugified back to a session
        (see `_path_to_slug`). Produces:
          caveman.by_session_label: label -> (sessions, saved_tokens)
          rtk.today_display:        [(label, commands, saved_tokens)]
        """
        self.slug_name = self._build_slug_names()

        by_label = {}
        for sid, info in (self.caveman.by_session or {}).items():
            label = claude_parser.display_label(
                self.session_name.get(sid), self.session_project.get(sid),
                session_id=sid)
            s, sv = by_label.get(label, (0, 0))
            by_label[label] = (s + 1, sv + info.get("saved", 0))
        self.caveman.by_session_label = by_label

        disp = []
        for path, (cnt, saved) in (self.rtk.today_by_project or {}).items():
            disp.append((self.label_for_path(path), cnt, saved))
        disp.sort(key=lambda r: -r[2])
        self.rtk.today_display = disp

    def _build_slug_names(self):
        """cwd slug -> session name, currently-active sessions taking priority.

        An active session is the one producing rtk commands right now; only
        when no active session works in that directory do we fall back to the
        most recent named session there.
        """
        names = {}
        for ctx in (self.context or []):  # sorted by age, freshest first
            slug, name = ctx.get("slug"), ctx.get("name")
            if slug and name and slug not in names:
                names[slug] = name
        for s in self.sessions:  # newest first
            slug = os.path.basename(s.project_dir or "")
            if slug and s.name and slug not in names:
                names[slug] = s.name
        # a cache-first render has no session list and only the sessions that
        # were active when the cache was written — fill the gaps from the map
        # we persisted, so labels survive a restart
        for slug, name in (self.slug_name or {}).items():
            names.setdefault(slug, name)
        return names

    def label_for_path(self, path):
        """Session name for an rtk cwd path, else the last path segment."""
        name = self.slug_name.get(_path_to_slug(path))
        if name:
            return name
        p = (path or "").rstrip("/")
        if p == os.path.expanduser("~"):
            return "(home)"  # bare last segment would be the username
        return p.rsplit("/", 1)[-1] if "/" in p else p


def build(force_limits=False) -> Dashboard:
    """Build a fresh Dashboard from all sources.

    `force_limits` bypasses the OAuth response TTL (manual refresh).
    """
    problems = []
    sessions = claude_parser.parse_sessions(problems=problems)
    lim = limits_mod.fetch_limits(force=force_limits)
    r = rtk.load_rtk()
    c = caveman.load_caveman()
    ctx = claude_parser.current_contexts()
    cs = compacts_mod.load_compacts()
    # annotates the active contexts in place with growth rate and the projected
    # distance to the automatic compaction, using the compaction history to cut
    # the samples at the last context drop
    compacts_mod.annotate_pace(ctx, cs)
    if cs.status == compacts_mod.STATUS_ERROR and cs.hint:
        problems.append(cs.hint)
    return Dashboard(sessions, lim, r, c, context=ctx, compact_stats=cs,
                     warnings=problems)


# ---------------------------------------------------------------------------
# JSON serialization for the disk cache. Sessions are the heavy part; we
# persist only the aggregated numbers the views need, plus a few session
# counts, not the full transcripts.
# ---------------------------------------------------------------------------
# bump when the cached shape changes so old caches are discarded automatically
CACHE_VERSION = 13


def dashboard_to_json(d: Dashboard) -> dict:
    return {
        "cache_version": CACHE_VERSION,
        "generated_at": d.generated_at.isoformat(),
        "warnings": d.warnings,
        "totals": {
            "cost": d.total_cost,
            "input": d.total_input,
            "output": d.total_output,
            "cache_read": d.total_cache_read,
            "cache_write": d.total_cache_write,
            "credits": d.total_credits,
            "credits_7d": d.credits_7d,
            "sessions": d.total_sessions,
            "messages": d.total_messages,
            "cost_input": d.cost_input,
            "cost_output": d.cost_output,
            "cost_cache_read": d.cost_cache_read,
            "cost_cache_write": d.cost_cache_write,
            "cache_ratio": d.cache_ratio,
        },
        "by_day_cost": d.by_day_cost,
        "by_day_tokens": {k: list(v) for k, v in d.by_day_tokens.items()},
        "by_project": d.by_project,
        "by_model": {k: list(v) for k, v in d.by_model.items()},
        "model_totals": [[m, list(v)] for m, v in d.model_totals],
        "project_list": [[p, c] for p, c in d.project_list],
        "heatmap": d.heatmap,
        "session_rows": d.session_rows,
        "context": d.context,
        "session_project": d.session_project,
        "session_name": d.session_name,
        "session_model": d.session_model,
        "slug_name": d.slug_name,
        "limits": _limits_to_json(d.limits),
        "rtk": _rtk_to_json(d.rtk),
        "caveman": _caveman_to_json(d.caveman),
        "compacts": compacts_mod.to_json(d.compacts),
        "model_tokens": d.model_tokens,
    }


def dashboard_from_json(obj: dict) -> Dashboard | None:
    if obj.get("cache_version") != CACHE_VERSION:
        return None
    try:
        d = Dashboard(sessions=[], generated_at=datetime.fromisoformat(obj["generated_at"]))
    except (KeyError, ValueError, TypeError):
        return None

    t = obj.get("totals") or {}
    d.total_cost = t.get("cost", 0.0)
    d.total_input = t.get("input", 0)
    d.total_output = t.get("output", 0)
    d.total_cache_read = t.get("cache_read", 0)
    d.total_cache_write = t.get("cache_write", 0)
    # credits are a scaled dollar amount and therefore float; a cache written by
    # an older build would have an int here, which float() takes unchanged
    d.total_credits = float(t.get("credits") or 0.0)
    d.credits_7d = float(t.get("credits_7d") or 0.0)
    d.total_sessions = t.get("sessions", 0)
    d.total_messages = t.get("messages", 0)
    d.cost_input = t.get("cost_input", 0.0)
    d.cost_output = t.get("cost_output", 0.0)
    d.cost_cache_read = t.get("cost_cache_read", 0.0)
    d.cost_cache_write = t.get("cost_cache_write", 0.0)
    d.cache_ratio = t.get("cache_ratio", 0.0)
    d.by_day_cost = dict(obj.get("by_day_cost") or {})
    d.by_day_tokens = {k: tuple(v) for k, v in (obj.get("by_day_tokens") or {}).items()}
    d.by_project = dict(obj.get("by_project") or {})
    d.by_model = {k: list(v) for k, v in (obj.get("by_model") or {}).items()}
    d.model_totals = [[m, list(v)] for m, v in (obj.get("model_totals") or [])]
    d.project_list = [[p, float(c)] for p, c in (obj.get("project_list") or [])]
    d.heatmap = dict(obj.get("heatmap") or {})
    d.session_rows = list(obj.get("session_rows") or [])
    d.context = obj.get("context")
    d.warnings = list(obj.get("warnings") or [])
    d.session_project = dict(obj.get("session_project") or {})
    d.session_name = dict(obj.get("session_name") or {})
    d.session_model = dict(obj.get("session_model") or {})
    d.slug_name = dict(obj.get("slug_name") or {})
    d.limits = _limits_from_json(obj.get("limits") or {})
    d.rtk = _rtk_from_json(obj.get("rtk") or {})
    d.caveman = _caveman_from_json(obj.get("caveman") or {})
    d.compacts = compacts_mod.from_json(obj.get("compacts") or {})
    d.model_tokens = {k: dict(v) for k, v in (obj.get("model_tokens") or {}).items()}
    # must run last: it labels the restored rtk/caveman stats, so running it
    # before they are attached would label the empty placeholders instead
    d._link_savings()
    return d


def _limits_to_json(l) -> dict:
    return {
        "available": l.available,
        "five_hour_pct": l.five_hour_pct,
        "five_hour_resets_at": l.five_hour_resets_at,
        "seven_day_pct": l.seven_day_pct,
        "seven_day_resets_at": l.seven_day_resets_at,
        "seven_day_opus_pct": l.seven_day_opus_pct,
        "seven_day_opus_resets_at": l.seven_day_opus_resets_at,
        "seven_day_sonnet_pct": l.seven_day_sonnet_pct,
        "seven_day_sonnet_resets_at": l.seven_day_sonnet_resets_at,
        "extra_usage_enabled": l.extra_usage_enabled,
        "extra_usage_used": l.extra_usage_used,
        "extra_usage_limit": l.extra_usage_limit,
        "extra_usage_pct": l.extra_usage_pct,
        "fetched_at": l.fetched_at,
        "rate_limited": l.rate_limited,
        "reason": l.reason,
        "retry_in": l.retry_in,
    }


def _limits_from_json(obj: dict) -> limits_mod.UsageLimits:
    l = limits_mod.UsageLimits()
    l.available = bool(obj.get("available"))
    l.five_hour_pct = obj.get("five_hour_pct")
    l.five_hour_resets_at = obj.get("five_hour_resets_at")
    l.seven_day_pct = obj.get("seven_day_pct")
    l.seven_day_resets_at = obj.get("seven_day_resets_at")
    l.seven_day_opus_pct = obj.get("seven_day_opus_pct")
    l.seven_day_opus_resets_at = obj.get("seven_day_opus_resets_at")
    l.seven_day_sonnet_pct = obj.get("seven_day_sonnet_pct")
    l.seven_day_sonnet_resets_at = obj.get("seven_day_sonnet_resets_at")
    l.extra_usage_enabled = bool(obj.get("extra_usage_enabled"))
    l.extra_usage_used = float(obj.get("extra_usage_used") or 0.0)
    # None and 0.0 are different answers here (see limits._from_raw), so these
    # two round-trip as None rather than through `or 0.0`.
    l.extra_usage_limit = obj.get("extra_usage_limit")
    l.extra_usage_pct = obj.get("extra_usage_pct")
    l.fetched_at = obj.get("fetched_at")
    l.rate_limited = bool(obj.get("rate_limited"))
    l.reason = obj.get("reason")
    l.retry_in = obj.get("retry_in")
    return l


def _rtk_to_json(r) -> dict:
    return {
        "available": r.available,
        "status": r.status,
        "hint": r.hint,
        "total_commands": r.total_commands,
        "total_input_tokens": r.total_input_tokens,
        "total_output_tokens": r.total_output_tokens,
        "total_saved_tokens": r.total_saved_tokens,
        "billable_saved_tokens": r.billable_saved_tokens,
        "capped_commands": r.capped_commands,
        "avg_savings_pct": r.avg_savings_pct,
        "first_record": r.first_record.isoformat() if r.first_record else None,
        "last_record": r.last_record.isoformat() if r.last_record else None,
        "by_day": r.by_day,
        "top_projects": r.top_projects,
        "today_by_project": r.today_by_project,
    }


def _rtk_from_json(obj: dict) -> RtkStats:
    r = RtkStats()
    r.available = bool(obj.get("available"))
    r.status = obj.get("status") or ("ok" if r.available else "missing")
    r.hint = obj.get("hint") or ""
    r.total_commands = obj.get("total_commands", 0)
    r.total_input_tokens = obj.get("total_input_tokens", 0)
    r.total_output_tokens = obj.get("total_output_tokens", 0)
    r.total_saved_tokens = obj.get("total_saved_tokens", 0)
    # older caches predate the cap; falling back to the raw figure would
    # quietly restore the 13x overstatement on a cache-first render
    r.billable_saved_tokens = obj.get("billable_saved_tokens", 0)
    r.capped_commands = obj.get("capped_commands", 0)
    r.avg_savings_pct = obj.get("avg_savings_pct", 0.0)
    for attr, key in (("first_record", "first_record"), ("last_record", "last_record")):
        iso = obj.get(key)
        if iso:
            try:
                setattr(r, attr, datetime.fromisoformat(iso))
            except ValueError:
                pass
    r.by_day = {k: tuple(v) for k, v in (obj.get("by_day") or {}).items()}
    r.top_projects = [list(p) for p in (obj.get("top_projects") or [])]
    r.today_by_project = {k: tuple(v) for k, v in (obj.get("today_by_project") or {}).items()}
    return r


def _caveman_to_json(c) -> dict:
    return {
        "available": c.available,
        "status": c.status,
        "hint": c.hint,
        "sessions": c.sessions,
        "total_output_tokens": c.total_output_tokens,
        "total_saved_tokens": c.total_saved_tokens,
        "total_saved_usd": c.total_saved_usd,
        "total_turns": c.total_turns,
        "by_day": c.by_day,
        "by_session": c.by_session,
        "latest_session": c.latest_session,
    }


def _caveman_from_json(obj: dict) -> CavemanStats:
    c = CavemanStats()
    c.available = bool(obj.get("available"))
    c.status = obj.get("status") or ("ok" if c.available else "missing")
    c.hint = obj.get("hint") or ""
    c.sessions = obj.get("sessions", 0)
    c.total_output_tokens = obj.get("total_output_tokens", 0)
    c.total_saved_tokens = obj.get("total_saved_tokens", 0)
    c.total_saved_usd = obj.get("total_saved_usd", 0.0)
    c.total_turns = obj.get("total_turns", 0)
    c.by_day = {k: tuple(v) for k, v in (obj.get("by_day") or {}).items()}
    c.by_session = dict(obj.get("by_session") or {})
    c.latest_session = obj.get("latest_session")
    return c
