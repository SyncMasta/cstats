"""Read Claude Code's own telemetry from its Prometheus endpoint.

Why a fourth source at all: two questions the transcripts cannot answer.

- **What do subagents cost?** A subagent leaves no transcript. Measured over
  the whole local history, `isSidechain: true` appears on **0 lines**
  (ARCHITECTURE.md §6c), so a subagent's spend is not merely hard to attribute
  here — it is invisible. `claude_code.cost.usage` carries `query_source`
  (`main` / `subagent` / `auxiliary`) and splits it directly. The same labels
  attribute cost to a skill, a plugin and an individual MCP server.
- **Is our own price table right?** Every dollar this tool shows is derived
  from a price list (`pricing.py`). `claude_code.cost.usage` is Claude Code's
  own figure for the same calls — the only outside check available, and the
  kind of measurement requirement 8 asks for.

**Transport.** Claude Code writes telemetry to no file: the exporters are
`otlp`, `prometheus`, `console` and `none`, and only `OTEL_LOG_RAW_API_BODIES`
has a file mode (raw request bodies, not metrics). So unlike rtk (a SQLite DB)
and caveman (a JSONL) there is nothing lying on disk to read. The Prometheus
exporter is the one local transport that needs neither a collector nor a
listening socket in this tool: Claude Code serves a scrape endpoint and we GET
it over urllib, read-only, no new dependency.

**What that costs us**, stated here because the panel has to say it too:

- The counters live in the Claude Code **process**. They start at zero when it
  starts and vanish when it exits, so this is "what the running session has
  spent", not a history. Nothing is persisted by us either — a scrape is a
  snapshot, and treating one as a lifetime total would be wrong.
- One endpoint is one process. A second Claude Code needs its own
  `OTEL_EXPORTER_PROMETHEUS_PORT`, and this reader sees only the port it asks.

**Metric names are not stable enough to match literally.** The OTel Prometheus
exporter appends the unit and a `_total` suffix, so `claude_code.cost.usage`
arrives as `claude_code_cost_usage_USD_total` — *except* that Claude Code omits
the `USD`, `tokens` and `s` units when prometheus is the only exporter listed,
which yields `claude_code_cost_usage_total` from the same build. Matching the
literal string would silently read nothing on half the configurations, so
`_canonical()` strips the suffixes instead and the reader reports every name it
did not recognise (`unknown_metrics`) rather than dropping it in silence.
"""

import os
import re
import time
import urllib.error
import urllib.request

# The four statuses, same vocabulary as rtk.py and caveman.py so the views can
# treat every optional source alike. "error" exists for the same reason there:
# a failed read reported as "no data yet" tells you to wait for data that a
# refused connection will never bring.
from .rtk import STATUS_OK, STATUS_EMPTY, STATUS_MISSING, STATUS_ERROR

DEFAULT_PORT = 9464
DEFAULT_HOST = "127.0.0.1"
# Short on purpose: this runs inside the 60s refresh, and the common case for a
# wrong port is a connection that hangs rather than one that is refused.
TIMEOUT_S = 2.0

# Metric keys after `_canonical()`. The values are what this module calls them.
M_COST = "claude_code_cost_usage"
M_TOKENS = "claude_code_token_usage"
M_SESSIONS = "claude_code_session_count"
M_LOC = "claude_code_lines_of_code_count"
M_COMMITS = "claude_code_commit_count"
M_PRS = "claude_code_pull_request_count"
M_ACTIVE = "claude_code_active_time_total"
M_EDIT_DECISION = "claude_code_code_edit_tool_decision"

_KNOWN = {M_COST, M_TOKENS, M_SESSIONS, M_LOC, M_COMMITS, M_PRS, M_ACTIVE,
          M_EDIT_DECISION}

# Unit suffixes the exporter may or may not have appended (see the module
# docstring). Only stripped when the remainder is still a plausible name, so a
# metric that genuinely ends in one of these words cannot be truncated to
# nothing.
_UNITS = ("usd", "tokens", "seconds", "bytes", "by", "s")

# A Prometheus sample line: name{labels} value [timestamp]
_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})?\s+(\S+)(?:\s+\S+)?$')
# One label pair inside the braces. Values are quoted and may contain escaped
# quotes, so the value group stops at an unescaped one.
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"')


def endpoint():
    """URL of the scrape endpoint, honouring Claude Code's own port variable.

    Read at call time rather than bound at import: the port is the user's to
    change, and a module constant would freeze whatever the environment looked
    like when the process started.
    """
    port = os.environ.get("CSTATS_OTEL_PORT") or os.environ.get(
        "OTEL_EXPORTER_PROMETHEUS_PORT") or DEFAULT_PORT
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    host = os.environ.get("CSTATS_OTEL_HOST") or DEFAULT_HOST
    return f"http://{host}:{port}/metrics"


def _canonical(name):
    """Strip the exporter's `_total` and unit suffixes from a metric name.

    `claude_code_cost_usage_USD_total` and `claude_code_cost_usage_total` are
    the same metric from two configurations (see the module docstring), and
    `claude_code.active_time.total` legitimately *ends* in "total" — so the
    `_total` suffix is removed exactly once, before any unit is considered.
    """
    n = name.lower()
    if n.endswith("_total"):
        n = n[:-len("_total")]
    for unit in _UNITS:
        suffix = "_" + unit
        if n.endswith(suffix) and len(n) > len(suffix) + 4:
            return n[:-len(suffix)]
    return n


def _unescape(v):
    return v.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def parse_exposition(text):
    """Parse Prometheus text format into [(canonical_name, labels, value)].

    Tolerant by design: a line this does not understand is skipped rather than
    raised on. The endpoint is a foreign format we do not control, and one
    malformed sample must not cost the whole scrape.
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if not m:
            continue
        name, raw_labels, raw_value = m.group(1), m.group(2), m.group(3)
        # `_created` series carry the counter's start time, not its value, and
        # summing them into a cost total would produce epoch seconds in USD.
        if name.endswith("_created"):
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue  # NaN/+Inf placeholders for a counter with no samples yet
        if value != value:  # NaN
            continue
        labels = {}
        if raw_labels:
            for lm in _LABEL.finditer(raw_labels):
                labels[lm.group(1)] = _unescape(lm.group(2))
        out.append((_canonical(name), labels, value))
    return out


class OtelStats:
    def __init__(self):
        self.available = False
        self.status = STATUS_MISSING
        self.hint = ""
        self.endpoint = None
        # When this snapshot was taken. Every panel in this tool shows the age
        # of its own numbers, and these age faster than most: they describe a
        # process that may already have exited.
        self.scraped_at = None

        self.total_cost_usd = 0.0
        self.cost_by_source = {}      # main / subagent / auxiliary -> USD
        self.cost_by_model = {}
        self.cost_by_agent = {}       # agent.name -> USD
        self.cost_by_skill = {}
        self.cost_by_mcp_server = {}
        self.tokens_by_type = {}      # input / output / cacheRead / cacheCreation
        self.total_tokens = 0

        self.sessions = 0
        self.lines_added = 0
        self.lines_removed = 0
        self.commits = 0
        self.pull_requests = 0
        self.active_time_s = 0.0
        self.edit_decisions = {}      # accept / reject -> count

        # Names the endpoint served that this reader does not know. Surfaced
        # rather than swallowed: a renamed metric is exactly how an integration
        # like this goes quietly blind, and the panel can then say which name
        # it saw instead of showing a plausible-looking zero.
        self.unknown_metrics = []

    @property
    def subagent_share(self):
        """Fraction of scraped cost attributed to subagents, or None.

        None rather than 0.0 when there is nothing to divide: "no cost recorded"
        and "no subagent cost" are different answers.
        """
        if not self.total_cost_usd:
            return None
        return self.cost_by_source.get("subagent", 0.0) / self.total_cost_usd

    @property
    def age_s(self):
        if not self.scraped_at:
            return None
        return max(0, int(time.time() - self.scraped_at))


def _add(bucket, key, value):
    if not key:
        return
    bucket[key] = bucket.get(key, 0.0) + value


def _fetch(url, timeout=TIMEOUT_S):
    """GET the endpoint. Returns (text, None) or (None, reason)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cstats"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, "replace"), None
    except urllib.error.HTTPError as exc:
        return None, f"endpoint answered HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, str(getattr(exc, "reason", exc))
    except (OSError, ValueError) as exc:
        return None, str(exc)


def load_otel(url=None, timeout=TIMEOUT_S) -> OtelStats:
    """Scrape the endpoint once. Never raises; returns empty stats on any error.

    The four statuses carry the distinction that matters for what the user
    should do next:
      missing  nothing is listening — telemetry off, or Claude Code not running
      empty    something answered, but served no claude_code metrics
      error    it answered and we could not use the answer
      ok       claude_code metrics were read
    """
    stats = OtelStats()
    stats.endpoint = url or endpoint()

    text, reason = _fetch(stats.endpoint, timeout=timeout)
    if text is None:
        # A refused connection is the ordinary "not set up" case and must not
        # read as a defect: the exporter only listens while Claude Code runs
        # with telemetry enabled.
        low = (reason or "").lower()
        if "refused" in low or "unreachable" in low or "not known" in low:
            stats.status = STATUS_MISSING
            stats.hint = ("nothing listening on " + stats.endpoint +
                          " — start Claude Code with CLAUDE_CODE_ENABLE_TELEMETRY=1 "
                          "and OTEL_METRICS_EXPORTER=prometheus")
        else:
            stats.status = STATUS_ERROR
            stats.hint = f"could not scrape {stats.endpoint}: {reason}"
        return stats

    samples = parse_exposition(text)
    stats.scraped_at = time.time()

    seen_ours = False
    unknown = set()
    for name, labels, value in samples:
        if not name.startswith("claude_code"):
            continue
        if name not in _KNOWN:
            unknown.add(name)
            continue
        seen_ours = True
        if name == M_COST:
            stats.total_cost_usd += value
            _add(stats.cost_by_source, labels.get("query_source") or "main", value)
            _add(stats.cost_by_model, labels.get("model"), value)
            _add(stats.cost_by_agent, labels.get("agent_name"), value)
            _add(stats.cost_by_skill, labels.get("skill_name"), value)
            _add(stats.cost_by_mcp_server, labels.get("mcp_server_name"), value)
        elif name == M_TOKENS:
            stats.total_tokens += int(value)
            _add(stats.tokens_by_type, labels.get("type") or "unknown", value)
        elif name == M_SESSIONS:
            stats.sessions += int(value)
        elif name == M_LOC:
            if labels.get("type") == "removed":
                stats.lines_removed += int(value)
            else:
                stats.lines_added += int(value)
        elif name == M_COMMITS:
            stats.commits += int(value)
        elif name == M_PRS:
            stats.pull_requests += int(value)
        elif name == M_ACTIVE:
            stats.active_time_s += value
        elif name == M_EDIT_DECISION:
            _add(stats.edit_decisions, labels.get("decision") or "unknown", value)

    stats.unknown_metrics = sorted(unknown)

    if seen_ours:
        stats.status = STATUS_OK
        stats.available = True
        if unknown:
            # Not an error — a newer Claude Code emitting metrics this reader
            # predates. Worth naming so a missing panel row has an explanation.
            stats.hint = (f"{len(unknown)} unrecognised claude_code metric(s): "
                          + ", ".join(stats.unknown_metrics[:3]))
        return stats

    if any(n.startswith("claude_code") for n, _l, _v in samples):
        # Only the unknown branch produced hits: every name drifted at once.
        stats.status = STATUS_ERROR
        stats.hint = ("the endpoint serves claude_code metrics under names this "
                      "reader does not know: " + ", ".join(stats.unknown_metrics[:3]))
        return stats

    stats.status = STATUS_EMPTY
    if samples:
        # Someone else's exporter on the same port is a different problem from
        # a Claude Code that has not recorded anything yet, and the fix differs.
        stats.hint = (f"{stats.endpoint} is serving metrics, but none from "
                      "Claude Code — is another exporter on that port?")
    else:
        stats.hint = (f"{stats.endpoint} answered with nothing recorded yet — "
                      "the counters appear after the first API call")
    return stats
