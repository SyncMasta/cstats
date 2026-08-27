"""Thread-safe data loader with caching + auto-refresh for the TUI."""

import json
import os
import threading
import time
from datetime import datetime, timezone

from . import aggregate
from . import claude_parser, config, economics, limit_history, notify
from .claude_parser import Session
from .caveman import CavemanStats
from .rtk import RtkStats
from .limits import UsageLimits


def _cache_dir():
    """Our cache directory, honouring XDG_CACHE_HOME.

    Every caller goes through this function rather than through a module-level
    constant: the constant was computed once at import, so a test setting
    XDG_CACHE_HOME after the import still wrote the real user cache — and, via
    the alert loop, could fire a real desktop notification.

    Read at call time so tests can redirect it, and so all three caches
    (dashboard, compaction scan, parse cache) end up in the same place — they
    used to split, with this one pinned to ~/.cache while the others followed
    the environment.
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "cstats")


def _cache_file():
    return os.path.join(_cache_dir(), "dashboard.json")


def _alert_state_file():
    return os.path.join(_cache_dir(), "alert-state.json")

ALERT_THRESHOLDS = (80, 90, 100)


class DataService:
    """Holds the latest Dashboard and refreshes it on demand.

    Cache-first: on startup the persisted JSON snapshot is loaded instantly so
    the TUI renders immediately; a background refresh then parses the real
    sources and re-caches them. A mutex guards concurrent access.
    """

    REFRESH_INTERVAL = 60  # seconds

    def __init__(self, use_cache=True):
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._data = None
        self._last_refresh = 0.0
        self._refreshing = False
        self.error = None
        self.cache_loaded = False
        self.limit_history = []
        self._last_limit_ts = None
        if use_cache:
            self._load_cache()
        self.limit_history = limit_history.load()

    # ------------------------------------------------------------------
    # alerts
    # ------------------------------------------------------------------
    def _load_alert_state(self) -> dict:
        try:
            with open(_alert_state_file(), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {"fired": []}

    def _save_alert_state(self, state: dict) -> None:
        try:
            path = _alert_state_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with config.open_private(tmp) as fh:
                json.dump(state, fh)
            os.replace(tmp, path)
        except OSError:
            pass

    def _check_alerts(self, d) -> None:
        """Fire a desktop notification when a limit crosses a threshold.

        One-shot per (window, threshold) until usage drops back below the
        threshold (reset detected via alert-state).
        """
        state = self._load_alert_state()
        fired = set(state.get("fired") or [])
        lim = d.limits

        windows = (("5h", lim.five_hour_pct), ("7d", lim.seven_day_pct)) if lim.available else ()
        for window, pct in windows:
            if pct is None:
                continue
            for thr in ALERT_THRESHOLDS:
                key = f"{window}:{thr}"
                if pct >= thr and key not in fired:
                    notify.notify(
                        "Claude usage limit",
                        f"{window} window at {pct:.0f}% (threshold {thr}%)",
                    )
                    fired.add(key)
                elif pct < thr - 5 and key in fired:
                    # usage dropped back below threshold (window reset) — re-arm
                    fired.discard(key)

        state["fired"] = sorted(fired)
        self._check_context_alerts(d, state)
        self._save_alert_state(state)

    def _check_context_alerts(self, d, state) -> None:
        """Warn when an active session got expensive enough to compact.

        Every turn re-reads the whole context, so a long session's per-turn
        cost is what hurts. Fires once per session and re-arms when its
        context drops back below half the threshold — i.e. after a compaction
        or a restart, so the next growth cycle warns again.
        """
        alert_usd = config.get("context_alert_usd", 0.10)
        if not alert_usd or not d.context:
            return
        rates = economics.billing_rates(d)
        warned = set(state.get("context_warned") or [])
        for ctx in d.context:
            sid = (ctx.get("session") or "").removesuffix(".jsonl")
            if not sid:
                continue
            econ = economics.session_economics(ctx.get("tokens") or 0, rates)
            verdict = economics.advice(econ, alert_usd)
            if verdict == "compact" and sid not in warned:
                name = claude_parser.display_label(
                    ctx.get("name"), ctx.get("project"), ctx.get("branch"), sid)
                be = econ["breakeven_turns"]
                notify.notify(
                    "Claude session worth compacting",
                    f"{name}: ${econ['per_turn']:.2f}/turn "
                    f"({econ['tokens']:,} tokens) — compacting pays back after ~{be:.0f} turns",
                )
                warned.add(sid)
            elif econ["per_turn"] < alert_usd / 2 and sid in warned:
                warned.discard(sid)  # compacted or restarted — re-arm
        state["context_warned"] = sorted(warned)

    # ------------------------------------------------------------------
    # cache
    # ------------------------------------------------------------------
    def _load_cache(self) -> bool:
        try:
            with open(_cache_file(), "r", encoding="utf-8") as fh:
                obj = json.load(fh)
            data = aggregate.dashboard_from_json(obj)
            if data is not None:
                with self._lock:
                    self._data = data
                self.cache_loaded = True
                return True
        except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
            pass
        return False

    def _save_cache(self, data) -> None:
        try:
            path = _cache_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            obj = aggregate.dashboard_to_json(data)
            tmp = path + ".tmp"
            with config.open_private(tmp) as fh:
                json.dump(obj, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # access
    # ------------------------------------------------------------------
    @property
    def data(self) -> aggregate.Dashboard:
        if self._data is None:
            self.refresh()
        return self._data

    @property
    def stale(self) -> bool:
        return (time.time() - self._last_refresh) > self.REFRESH_INTERVAL

    def refresh(self, force=False) -> aggregate.Dashboard:
        """Rebuild the dashboard.

        Periodic refreshes (`force=False`) skip when one is already running.
        A manual refresh (`force=True`) must never be a silent no-op: it waits
        for the in-flight build and then builds again, bypassing the OAuth
        response TTL so the live limits actually move.
        """
        if not force:
            with self._lock:
                if self._refreshing:
                    return self._data
                self._refreshing = True
        # forced refreshes queue up behind the running build instead of
        # returning the stale dashboard
        with self._build_lock:
            with self._lock:
                self._refreshing = True
            try:
                data = aggregate.build(force_limits=force)
                with self._lock:
                    self._data = data
                    self._last_refresh = time.time()
                    self.error = None
                self._save_cache(data)
                # snapshot the live limits for the history sparkline + alert check.
                # Only record a genuinely new OAuth response — re-recording the
                # cached one every 60s would fill the sparkline with duplicates.
                lim = data.limits
                if lim.available and lim.fetched_at != self._last_limit_ts:
                    self._last_limit_ts = lim.fetched_at
                    limit_history.record(lim.five_hour_pct, lim.seven_day_pct)
                    self.limit_history = limit_history.load()
                # context alerts do not depend on the OAuth endpoint working
                self._check_alerts(data)
            except Exception as exc:  # keep TUI alive on errors
                self.error = str(exc)
            finally:
                with self._lock:
                    self._refreshing = False
        return self._data

    def maybe_refresh(self):
        """Refresh if stale. Called from a periodic timer."""
        if self.stale:
            self.refresh()


# Keep a module-level default so other modules can share the instance.
_default_service = None


def default_service() -> DataService:
    global _default_service
    if _default_service is None:
        _default_service = DataService()
    return _default_service
