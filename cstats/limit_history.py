"""Limit-utilization history: periodic snapshots of the 5h/7d OAuth percentages.

Stored append-only as JSONL in ~/.cache/cstats/limit-history.jsonl,
one line per snapshot: {"t": iso, "fh": 38.0, "sd": 35.0}.
Snapshots older than KEEP_DAYS are trimmed on write (cheap since the file is
small: ~90 days * ~288 snapshots/day at 5-min cadence would be 26k lines, we
trim to a rolling window anyway).
"""

import json
import os
from datetime import datetime, timedelta, timezone

from . import config

KEEP_DAYS = 30
MAX_LINES = 8000  # safety cap


_write_count = 0


def history_file():
    """Path of the history file. Honors XDG_CACHE_HOME, read at call time.

    Not a module-level constant: that is resolved at import, before a test can
    redirect the environment, so a test refresh appended to — and via trim()
    rewrote — the user's real sparkline history.
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "cstats", "limit-history.jsonl")


def record(five_hour_pct, seven_day_pct, now=None) -> None:
    """Append one snapshot. Silently ignores I/O errors. Trims occasionally."""
    global _write_count
    now = now or datetime.now(timezone.utc)
    line = json.dumps({"t": now.isoformat(), "fh": five_hour_pct, "sd": seven_day_pct})
    try:
        path = history_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with config.open_private(path, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    _write_count += 1
    if _write_count % 50 == 0:
        trim()


def load(days=KEEP_DAYS) -> list[dict]:
    """Load snapshots from the last `days` days, oldest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    try:
        with open(history_file(), "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # A JSONL line need not be an object: a bare list or string
                # used to raise TypeError out of this loader, and the caller
                # swallowed it, so the sparkline silently stopped updating.
                if not isinstance(obj, dict):
                    continue
                try:
                    t = datetime.fromisoformat(obj["t"])
                except (KeyError, TypeError, ValueError):
                    continue
                if t >= cutoff:
                    out.append({"t": t, "fh": obj.get("fh"), "sd": obj.get("sd")})
    except OSError:
        pass
    return out


def trim(days=KEEP_DAYS) -> None:
    """Rewrite the file keeping only the last `days` days (call occasionally)."""
    keep = load(days=days)
    if len(keep) > MAX_LINES:
        keep = keep[-MAX_LINES:]
    try:
        path = history_file()
        tmp = path + ".tmp"
        with config.open_private(tmp) as fh:
            for obj in keep:
                fh.write(json.dumps({"t": obj["t"].isoformat(), "fh": obj["fh"], "sd": obj["sd"]}) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass
