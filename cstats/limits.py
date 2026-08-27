"""Fetch live Claude Code usage limits from the (undocumented) OAuth endpoint.

Endpoint: GET https://api.anthropic.com/api/oauth/usage
Auth:     Bearer <token from ~/.claude/.credentials.json claudeAiOauth.accessToken>
Header:   anthropic-beta: oauth-2025-04-20
Same endpoint the interactive /usage command uses.

Returns e.g.:
    {"five_hour": {"utilization": 38.0, "resets_at": "..."},
     "seven_day": {"utilization": 35.0, "resets_at": "..."},
     "extra_usage": {"is_enabled": true, ...}}

Caching: the endpoint rate-limits hard (HTTP 429). We keep the last response
on disk together with a `next_try` timestamp. A 429 (or a network error) sets
a backoff so we stop hammering the endpoint — without it, every 60s refresh
retried, kept getting 429, and the displayed numbers froze at the last
successful fetch with nothing in the UI saying so.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from . import config

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

CACHE_TTL = 60          # serve the cached response for this long without asking
MIN_FETCH_INTERVAL = 20 # hard floor between two requests, even on a forced refresh
BACKOFF_429 = 240       # after the first rate-limit, stay quiet this long
BACKOFF_429_MAX = 1800  # ceiling for repeated rate-limits
BACKOFF_ERROR = 60      # after a network/parse error


class UsageLimits:
    def __init__(self):
        self.available = False
        self.five_hour_pct = None
        self.five_hour_resets_at = None
        self.seven_day_pct = None
        self.seven_day_resets_at = None
        # Model-family weekly windows. Plans without a separate cap for a
        # family report null for it, so these stay None and nothing is shown —
        # but where they exist (Max plans cap Opus separately) the family
        # window is regularly the one that actually blocks, and the error it
        # produces differs: switching model with /model helps against a family
        # limit and does nothing against the plan-wide weekly one.
        self.seven_day_opus_pct = None
        self.seven_day_opus_resets_at = None
        self.seven_day_sonnet_pct = None
        self.seven_day_sonnet_resets_at = None
        self.extra_usage_enabled = False
        self.extra_usage_used = 0.0
        # Credits consumed without the ceiling they run against is a number
        # with no reference size; the endpoint reports both, so show both.
        self.extra_usage_limit = None
        self.extra_usage_pct = None
        self.raw = None
        # freshness of the underlying response
        self.fetched_at = None   # epoch seconds of the response we are showing
        self.rate_limited = False
        # Seconds until the next attempt, when a backoff is in force.
        self.retry_in = None
        # Why there is nothing to show. An empty object used to mean both "no
        # OAuth token" and "the endpoint refused us", which are different
        # problems with different fixes.
        self.reason = None

    @property
    def age_s(self):
        """Seconds since the shown numbers were fetched (None if unknown)."""
        if not self.fetched_at:
            return None
        return max(0, int(time.time() - self.fetched_at))

    @property
    def stale(self):
        age = self.age_s
        return age is not None and age > CACHE_TTL


def response_cache():
    """Path of the on-disk response cache. Honors XDG_CACHE_HOME.

    Read at call time, not at import: a module-level constant is resolved
    before a test can redirect the environment, so the test run wrote the real
    ~/.cache/cstats/limits.json and destroyed the user's live backoff
    state (_ts / _next_try) on the way.
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "cstats", "limits.json")


def credentials_path():
    """Path of Claude Code's credentials file, honoring CLAUDE_CONFIG_DIR.

    A function, not a module constant, for the same reason every cache path is
    one (AGENTS.md rule 3): a constant is resolved at import, before a test can
    redirect the environment. This module was the last one still hard-coding
    ~/.claude while claude_parser, caveman and tools/measure_facts.py all
    honoured the variable, so a run against a synthetic config dir read the
    real credentials.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    base = os.path.expanduser(env) if env else os.path.expanduser("~/.claude")
    return os.path.join(base, ".credentials.json")


# macOS keeps the OAuth token in the login keychain (service name
# "Claude Code-credentials") rather than in a file, so the file is legitimately
# absent there and "no token" is the wrong conclusion to report. Reading the
# keychain would need `security find-generic-password`, which can raise a
# blocking system prompt from inside the refresh thread — not something to do
# behind the user's back, so the reason line names the keychain instead.
_MACOS_KEYCHAIN_HINT = (
    "no OAuth token: on macOS Claude Code stores it in the login keychain "
    "(service \"Claude Code-credentials\"), which this tool does not read"
)


def _no_token_reason():
    """Why there is no token, phrased for the platform we are actually on."""
    if sys.platform == "darwin":
        return _MACOS_KEYCHAIN_HINT
    return f"no OAuth token in {credentials_path()}"


def _access_token():
    try:
        with open(credentials_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        tok = (data.get("claudeAiOauth") or {}).get("accessToken")
        return tok
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _read_cache():
    """Return {"ts": float, "data": dict, "next_try": float} or None.

    `data` may be empty: the file doubles as the backoff marker, so a machine
    that has been refused before it ever got a response still remembers not to
    ask again yet. Callers must test `entry["data"]`, not just `entry`.
    """
    try:
        with open(response_cache(), "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        if not isinstance(obj, dict) or not isinstance(obj.get("data"), dict):
            return None
        return {
            "ts": float(obj.get("_ts") or 0),
            "data": obj["data"],
            "next_try": float(obj.get("_next_try") or 0),
            "strikes": int(obj.get("_strikes") or 0),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _write_cache(data: dict, ts: float, next_try: float, strikes: int = 0) -> None:
    try:
        path = response_cache()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with config.open_private(tmp) as fh:
            json.dump({"_ts": ts, "_next_try": next_try,
                       "_strikes": strikes, "data": data}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects instead of following them with the token attached.

    urllib keeps the Authorization header across a redirect and does not check
    the host, so one 30x from the endpoint (or from anything able to answer for
    it) would hand the live OAuth access token to another server. The endpoint
    does not redirect; if it starts, that is worth failing on, not following.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"refused redirect to {newurl}", headers, fp)


_OPENER = None


def _opener():
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(_NoRedirect)
    return _OPENER


def _empty(reason, rate_limited=False) -> UsageLimits:
    """Nothing to show, and why — see UsageLimits.reason."""
    limits = UsageLimits()
    limits.reason = reason
    limits.rate_limited = rate_limited
    return limits


def _from_raw(data, fetched_at=None, rate_limited=False, retry_in=None) -> UsageLimits:
    limits = UsageLimits()
    limits.available = True
    limits.raw = data
    limits.fetched_at = fetched_at
    limits.rate_limited = rate_limited
    limits.retry_in = retry_in
    fh = data.get("five_hour") or {}
    sd = data.get("seven_day") or {}
    # `or {}` is load-bearing here: an absent family cap is reported as a null
    # value for the whole key, not as an object with a null utilization.
    op = data.get("seven_day_opus") or {}
    so = data.get("seven_day_sonnet") or {}
    eu = data.get("extra_usage") or {}
    limits.five_hour_pct = fh.get("utilization")
    limits.five_hour_resets_at = fh.get("resets_at")
    limits.seven_day_pct = sd.get("utilization")
    limits.seven_day_resets_at = sd.get("resets_at")
    limits.seven_day_opus_pct = op.get("utilization")
    limits.seven_day_opus_resets_at = op.get("resets_at")
    limits.seven_day_sonnet_pct = so.get("utilization")
    limits.seven_day_sonnet_resets_at = so.get("resets_at")
    limits.extra_usage_enabled = bool(eu.get("is_enabled"))
    # used_credits / monthly_limit / utilization are each independently null
    # while extra usage is enabled but unused, so none of them may be coerced
    # to 0.0 as a group — a limit of 0 reads as "no headroom", which is the
    # opposite of "not reported".
    limits.extra_usage_used = float(eu.get("used_credits") or 0.0)
    limits.extra_usage_limit = _opt_float(eu.get("monthly_limit"))
    limits.extra_usage_pct = _opt_float(eu.get("utilization"))
    return limits


def _opt_float(v):
    """float(v), or None when the endpoint reported nothing for the field."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_limits(timeout=15, force=False) -> UsageLimits:
    """Fetch live limits. Never raises; returns empty UsageLimits on error.

    `force=True` (manual refresh) bypasses the TTL but still honours the
    backoff/min-interval, so mashing `r` cannot trigger a 429 storm.
    """
    now = time.time()
    entry = _read_cache()
    cached = entry["data"] if entry else {}
    backoff_until = entry["next_try"] if entry else 0.0
    strikes = entry["strikes"] if entry else 0

    if cached:
        age = now - entry["ts"]
        if (age < CACHE_TTL and not force) or now < backoff_until:
            # serving cached data: flag it as rate-limited only when a backoff
            # is what keeps us from refreshing something already stale
            blocked = now < backoff_until and age >= CACHE_TTL
            return _from_raw(cached, entry["ts"], rate_limited=blocked,
                             retry_in=int(backoff_until - now) if blocked else None)
    elif now < backoff_until:
        # backoff with nothing to serve: say so instead of asking again
        return _empty(f"rate-limited, next attempt in {int(backoff_until - now)}s",
                      rate_limited=True)

    tok = _access_token()
    if not tok:
        if cached:
            return _from_raw(cached, entry["ts"])
        return _empty(_no_token_reason())

    try:
        req = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": "Bearer " + tok,
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "cstats",
            },
        )
        with _opener().open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _write_cache(data, ts=now, next_try=now + MIN_FETCH_INTERVAL, strikes=0)
        return _from_raw(data, now)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            # Escalate. A fixed 240s wait against a longer server-side window
            # just loops — expire, ask, get refused, wait 240s again — and each
            # refused request keeps the limit alive. Doubling per consecutive
            # refusal finds the endpoint's actual window instead of guessing it.
            strikes += 1
            backoff = min(BACKOFF_429 * (2 ** (strikes - 1)), BACKOFF_429_MAX)
        else:
            backoff = BACKOFF_ERROR
        # Record the backoff even with no previous response: without an entry
        # to keep, nothing wrote next_try, so a fresh machine that got a 429
        # retried every 60s and stayed rate-limited.
        _write_cache(cached, ts=entry["ts"] if entry else 0.0,
                     next_try=now + backoff, strikes=strikes)
        if cached:
            return _from_raw(cached, entry["ts"], rate_limited=exc.code == 429,
                             retry_in=int(backoff))
        reason = ("rate-limited by the endpoint (HTTP 429)" if exc.code == 429
                  else f"endpoint returned HTTP {exc.code}")
        return _empty(reason, rate_limited=exc.code == 429)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as exc:
        _write_cache(cached, ts=entry["ts"] if entry else 0.0,
                     next_try=now + BACKOFF_ERROR, strikes=strikes)
        if cached:
            return _from_raw(cached, entry["ts"])
        return _empty(f"could not reach the endpoint: {exc}")
