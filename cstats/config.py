"""Persisted user config (~/.config/cstats/config.json, chmod 600).

Stores UI preferences across sessions: theme, refresh interval.
Honours $XDG_CONFIG_HOME. Never raises — falls back to defaults.
"""

import json
import os
import stat

DEFAULTS = {
    "theme": "orange",
    "refresh_ms": 60_000,
    # a session costing more than this per turn (context re-read alone) is
    # worth compacting; drives the overview hint and the desktop notification
    "context_alert_usd": 0.10,
    # Sessions tab sort state. The name filter is deliberately not persisted:
    # a filter that survives a restart looks like missing data.
    "sessions_sort": "date",
    "sessions_desc": True,
}


# The tool used to be called claude-usage. Everything it owns moved with the
# name, so the old directories are carried over once rather than abandoned:
# they hold the theme, the compaction threshold, 30 days of limit-history for
# the sparkline, and the parse caches that make a start take milliseconds.
LEGACY_NAME = "claude-usage"
APP_NAME = "cstats"


def _base(kind):
    """XDG base for `kind` in ("cache", "config"), read at call time."""
    env = os.environ.get("XDG_CACHE_HOME" if kind == "cache" else "XDG_CONFIG_HOME")
    return env or os.path.join(os.path.expanduser("~"), "." + kind)


def app_dir(kind):
    """Our directory under the given XDG base."""
    return os.path.join(_base(kind), APP_NAME)


def migrate_legacy_dirs() -> list:
    """Move ~/.{cache,config}/claude-usage to .../cstats. Returns what moved.

    Runs once at startup and is a no-op afterwards, on a fresh install, and in
    the tests (which point both XDG bases at an empty tempdir). Never merges:
    if the new directory already exists the old one is left untouched, because
    the new one is then the live state and guessing which file wins would be
    worse than leaving a directory behind.
    """
    moved = []
    for kind in ("cache", "config"):
        base = _base(kind)
        old = os.path.join(base, LEGACY_NAME)
        new = os.path.join(base, APP_NAME)
        if not os.path.isdir(old) or os.path.exists(new):
            continue
        try:
            os.replace(old, new)
            moved.append((old, new))
        except OSError:
            pass
    return moved


def open_private(path, mode="w"):
    """Open a file for writing, readable only by its owner (0600).

    chmod-after-write leaves a window — and leaves a 0644 file behind when the
    write fails before the chmod. These files carry project paths, session
    names and spend figures, so they are created private and stay private.
    """
    private = stat.S_IRUSR | stat.S_IWUSR
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    fd = os.open(path, flags, private)
    # O_CREAT's mode only applies to a file this call creates, so an append to
    # a file written before this helper existed would keep its old 0644.
    if os.fstat(fd).st_mode & 0o077:
        os.fchmod(fd, private)
    return os.fdopen(fd, mode, encoding="utf-8")


def _config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "cstats", "config.json")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for key in DEFAULTS:
            if key in raw:
                cfg[key] = raw[key]
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open_private(tmp) as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def get(key, default=None):
    return load_config().get(key, DEFAULTS.get(key, default))


def set_setting(key, value) -> None:
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)
