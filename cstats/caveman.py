"""Read caveman plugin stats from ~/.claude/.caveman-history.jsonl.

Each line (from the plugin's /caveman-stats persistence):
    {"ts": <epoch ms>, "session_id": "...", "mode": "full",
     "model": "...", "output_tokens": N, "turns": N,
     "est_saved_tokens": N, "est_saved_usd": 0.0}
Dedupe by session_id keeping the latest snapshot per session.
"""

import json
import os
from datetime import datetime, timezone

from .rtk import STATUS_OK, STATUS_EMPTY, STATUS_MISSING, STATUS_ERROR


def _claude_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.expanduser(env) if env else os.path.expanduser("~/.claude")


def _history_path():
    return os.path.join(_claude_dir(), ".caveman-history.jsonl")


def _installed() -> bool:
    """Is the caveman plugin installed? Its cache dir or mode flag proves it."""
    base = _claude_dir()
    return (os.path.isdir(os.path.join(base, "plugins", "cache", "caveman"))
            or os.path.exists(os.path.join(base, ".caveman-active")))


class CavemanStats:
    def __init__(self):
        self.available = False
        self.status = STATUS_MISSING
        self.hint = ""
        self.sessions = 0
        self.total_output_tokens = 0
        self.total_saved_tokens = 0
        self.total_saved_usd = 0.0
        self.total_turns = 0
        self.by_day = {}   # date -> (sessions, saved_tokens, output_tokens)
        self.by_session = {}  # session_id -> {ts, saved, output, model, mode}
        self.latest_session = None


def _load_jsonl(path, errors=None):
    """Yield parsed JSON objects from a JSONL file, skipping bad lines.

    A read failure is appended to `errors` rather than swallowed: an
    unreadable history file and an empty one produced the same result, so a
    permission problem rendered as "no snapshots yet" and invited the user to
    wait for data that could never arrive.
    """
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        if errors is not None:
            errors.append(str(exc))
        return


def _num(v, default=0):
    """Coerce a JSON value to a finite number; tolerate strings/garbage.

    json.loads accepts bare NaN and Infinity, and both used to pass straight
    through — the later int() then raised ValueError/OverflowError out of a
    function documented as never raising.
    """
    if isinstance(v, bool):
        return default
    if not isinstance(v, (int, float)):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def load_caveman(path=None) -> CavemanStats:
    """Load caveman lifetime + per-day stats. Never raises.

    An installed plugin with no history is the normal case, not an error: the
    plugin writes that file only when `caveman-stats.js` runs, so it stays
    absent until /caveman-stats is invoked or a Stop hook does it (see
    ARCHITECTURE.md). Says so instead of just "not found".
    """
    stats = CavemanStats()
    path = path or _history_path()
    latest = {}
    errors = []

    for obj in _load_jsonl(path, errors):
        if not isinstance(obj, dict):
            continue
        # Snapshots are cumulative per session, so only the newest one per
        # session may be counted. A line without a session_id used to be
        # accumulated on sight, which counted two snapshots of the same run
        # twice; it now shares one bucket so latest-wins applies to it too.
        sid = obj.get("session_id") or "(no session id)"
        ts = _num(obj.get("ts"))
        if sid not in latest or ts >= latest[sid][0]:
            latest[sid] = (ts, obj)

    for sid, (ts, obj) in latest.items():
        _accumulate(stats, obj)
        stats.by_session[sid] = {
            "ts": ts,
            "saved": int(_num(obj.get("est_saved_tokens"))),
            "output": int(_num(obj.get("output_tokens"))),
            "turns": int(_num(obj.get("turns"))),
            "model": obj.get("model"),
            "mode": obj.get("mode"),
        }

    stats.available = stats.sessions > 0 or stats.total_saved_tokens > 0
    if latest:
        _, newest = max(latest.items(), key=lambda kv: kv[1][0])
        stats.latest_session = newest[1]

    if errors:
        stats.status = STATUS_ERROR
        stats.hint = f"history file found but unreadable: {errors[0]}"
    elif stats.available:
        stats.status = STATUS_OK
    elif _installed():
        stats.status = STATUS_EMPTY
        stats.hint = ("plugin installed but no snapshots yet — the plugin only writes its "
                      "history when /caveman-stats runs; a Stop hook keeps it current")
    else:
        stats.status = STATUS_MISSING
        stats.hint = "caveman plugin not installed"
    return stats


def _accumulate(stats: CavemanStats, obj: dict):
    ts = _num(obj.get("ts"))
    day = None
    if ts:
        try:
            # local day — the other daily tables are local too
            day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            day = None

    st = int(_num(obj.get("est_saved_tokens")))
    ot = int(_num(obj.get("output_tokens")))
    usd = _num(obj.get("est_saved_usd"), 0.0)
    turns = int(_num(obj.get("turns")))

    stats.sessions += 1
    stats.total_saved_tokens += st
    stats.total_output_tokens += ot
    stats.total_saved_usd += usd
    stats.total_turns += turns

    if day:
        s, sv, oo = stats.by_day.get(day, (0, 0, 0))
        stats.by_day[day] = (s + 1, sv + st, oo + ot)
