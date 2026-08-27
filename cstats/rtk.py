"""Read rtk (Rust Token Killer) savings analytics from its SQLite DB.

DB: ~/.local/share/rtk/history.db (sometimes tracking.db).
Table `commands`:
    id, timestamp (RFC3339), original_cmd, rtk_cmd, input_tokens,
    output_tokens, saved_tokens, savings_pct, exec_time_ms, project_path
Retention is ~90 days.
"""

import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

# status values shared with caveman.py, so the views can treat both the same:
#   "ok"      data present
#   "empty"   tool is installed but has recorded nothing yet
#   "missing" tool is not installed at all
STATUS_OK, STATUS_EMPTY, STATUS_MISSING = "ok", "empty", "missing"

# rtk's own `saved_tokens` is (raw command output - compressed output), i.e. it
# prices a world where the ENTIRE untruncated output would have reached the
# model. It would not have. Measured over 25,639 real Bash results in the local
# history, the largest is 52,545 characters (~15k tokens) and the 99th
# percentile is 10,139 — Claude Code caps tool output.
#
# The difference is not academic: 74 of 14,958 rtk rows (0.5%) carry 94.3% of
# the reported saving, led by one `find / -name '*'` claiming 11.5M tokens.
# Counting a command's saving as at most what could have entered the context
# takes the total from 38.7M to ~3.0M.
#
# Both numbers are kept. `total_saved_tokens` stays rtk's own figure so the rtk
# tab still agrees with `rtk gain`; `billable_saved_tokens` is what our own
# money maths is allowed to use.
BASH_OUTPUT_CEILING_TOKENS = 15_000
# A read that failed is not an idle tool: "no data yet" invites you to wait for
# data that will never arrive while the DB is corrupt or unreadable.
STATUS_ERROR = "error"


def _find_db():
    """Locate the rtk history DB across the known platform paths."""
    candidates = []
    if os.name == "posix":
        candidates.append(os.path.expanduser("~/.local/share/rtk/history.db"))
        candidates.append(os.path.expanduser("~/.local/share/rtk/tracking.db"))
        candidates.append(os.path.expanduser("~/.config/rtk/history.db"))
        candidates.append(os.path.expanduser("~/.config/rtk/tracking.db"))
    elif os.name == "nt":
        candidates.append(os.path.expandvars(r"%APPDATA%\rtk\history.db"))
        candidates.append(os.path.expandvars(r"%APPDATA%\rtk\tracking.db"))
    else:
        candidates.append(os.path.expanduser("~/Library/Application Support/rtk/history.db"))
        candidates.append(os.path.expanduser("~/Library/Application Support/rtk/tracking.db"))

    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _ts_iso(ts):
    """RFC3339 'YYYY-MM-DDTHH:MM:SS...' -> datetime(UTC). Returns None on parse fail."""
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _local_day(ts):
    """RFC3339 timestamp -> local calendar day 'YYYY-MM-DD' (None on parse fail).

    rtk stores UTC. Bucketing on the raw string (substr) would bucket by UTC
    day, which drifts a day away from every other panel between local midnight
    and the UTC offset (00:00-02:00 in CEST).
    """
    dt = _ts_iso(ts)
    return dt.astimezone().strftime("%Y-%m-%d") if dt else None


def _utc_cutoff(days_back):
    """UTC 'YYYY-MM-DDTHH:MM:SS' string for local midnight `days_back` days ago.

    Comparable lexicographically against the stored RFC3339 timestamps (fixed
    19-char prefix), so the timestamp index stays usable.
    """
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=days_back)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class RtkStats:
    def __init__(self):
        self.available = False
        self.status = STATUS_MISSING
        self.hint = ""
        self.db_path = None
        self.total_commands = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_saved_tokens = 0        # as rtk reports it
        self.billable_saved_tokens = 0     # capped at what could have been read
        self.capped_commands = 0           # how many rows the cap touched
        self.avg_savings_pct = 0.0
        self.first_record = None
        self.last_record = None
        self.by_day = {}      # date(str) -> (commands, saved_tokens, savings_pct)
        self.today_by_project = {}  # project -> (commands, saved_tokens) today only
        self.top_projects = []


def load_rtk() -> RtkStats:
    """Load rtk savings stats. Never raises; returns empty stats on any error.

    Distinguishes four cases, because collapsing them misleads in both
    directions: not installed, installed but nothing recorded yet, read
    failure, and ok. A read failure reported as "no data yet" tells you to
    keep waiting while a corrupt DB never fills.
    """
    stats = RtkStats()
    db_path = _find_db()
    if not db_path:
        if shutil.which("rtk"):
            stats.status = STATUS_EMPTY
            stats.hint = "rtk is on PATH but has no history DB yet — run a command through it"
        else:
            stats.status = STATUS_MISSING
            stats.hint = "no rtk binary on PATH and no history DB"
        return stats
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        try:
            con.execute("PRAGMA query_only = ON")
            cur = con.cursor()

            cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                "COALESCE(SUM(saved_tokens),0), COALESCE(AVG(savings_pct),0), "
                "MIN(timestamp), MAX(timestamp), "
                # the same saving, but never crediting more than what Claude
                # Code would have let into the context in the first place
                "COALESCE(SUM(MAX(MIN(input_tokens, ?) - output_tokens, 0)),0), "
                "COALESCE(SUM(input_tokens > ?),0) FROM commands",
                (BASH_OUTPUT_CEILING_TOKENS, BASH_OUTPUT_CEILING_TOKENS),
            )
            row = cur.fetchone()
            if row and row[0]:
                stats.available = True
                stats.status = STATUS_OK
                stats.db_path = db_path
                stats.total_commands = row[0]
                stats.total_input_tokens = row[1]
                stats.total_output_tokens = row[2]
                stats.total_saved_tokens = row[3]
                stats.avg_savings_pct = row[4]
                stats.first_record = _ts_iso(row[5])
                stats.last_record = _ts_iso(row[6])
                stats.billable_saved_tokens = row[7]
                stats.capped_commands = row[8]

            # project aggregation (lifetime)
            cur.execute(
                "SELECT COALESCE(NULLIF(project_path,''), '(no project)'), COUNT(*), "
                "SUM(saved_tokens) FROM commands GROUP BY 1 ORDER BY 3 DESC LIMIT 15"
            )
            for proj, cnt, saved in cur.fetchall():
                stats.top_projects.append((proj, int(cnt), int(saved or 0)))

            # daily aggregation + today-per-project, bucketed by LOCAL day
            cur.execute(
                "SELECT timestamp, saved_tokens, savings_pct, "
                "COALESCE(NULLIF(project_path,''), '(no project)') FROM commands "
                "WHERE timestamp >= ?",
                (_utc_cutoff(44),),
            )
            today = datetime.now().astimezone().strftime("%Y-%m-%d")
            acc = {}      # day -> [commands, saved, pct_sum]
            today_acc = {}
            for ts, saved, pct, proj in cur.fetchall():
                day = _local_day(ts)
                if not day:
                    continue
                a = acc.setdefault(day, [0, 0, 0.0])
                a[0] += 1
                a[1] += int(saved or 0)
                a[2] += float(pct or 0.0)
                if day == today:
                    t = today_acc.setdefault(proj, [0, 0])
                    t[0] += 1
                    t[1] += int(saved or 0)
            for day, (cnt, saved, pct_sum) in acc.items():
                stats.by_day[day] = (cnt, saved, pct_sum / cnt if cnt else 0.0)
            for proj, (cnt, saved) in sorted(today_acc.items(), key=lambda kv: -kv[1][1]):
                stats.today_by_project[proj] = (cnt, saved)
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        # ValueError/TypeError: the columns are untyped in SQLite, so a row
        # holding text where a count belongs used to escape this handler and
        # take the whole dashboard down with it.
        stats.status = STATUS_ERROR
        stats.hint = f"history DB found but unreadable: {exc}"
        stats.db_path = db_path
        return stats
    if not stats.available:
        stats.status = STATUS_EMPTY
        stats.hint = "history DB is empty — no commands recorded (90-day retention)"
        stats.db_path = db_path
    return stats
