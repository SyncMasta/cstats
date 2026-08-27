"""Print the measured facts about the local transcript history.

The design decisions in ARCHITECTURE.md rest on numbers that are expensive to
obtain: a full pass over ~400 MB of JSONL takes minutes. Re-deriving them by
hand wastes that time every single time, so they live in ARCHITECTURE.md as a dated
snapshot and this script regenerates them.

Reads only, streams line by line (some transcripts are >100 MB, so no
readlines()), and touches nothing outside ~/.claude/projects.

    .venv/bin/python tools/measure_facts.py
"""

import glob
import json
import os
import re
import statistics as st
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cstats.compacts import COMPACT_CALL_SHARE
from cstats.pricing import context_window

MSG_ID = re.compile(rb'"id":"(msg_[A-Za-z0-9_-]+)"')


def _claude_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.expanduser(env) if env else os.path.expanduser("~/.claude")


def _ctx_tokens(usage):
    """Context size as we bill it: everything on the input side of one call."""
    return ((usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0))


def _local_day(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().date()
    except (ValueError, AttributeError, TypeError):
        return None


def scan():
    files = glob.glob(os.path.join(_claude_dir(), "projects", "*", "*.jsonl"))
    facts = {
        "files": len(files),
        "bytes": sum(os.path.getsize(f) for f in files if os.path.exists(f)),
        "multi_day": 0, "with_days": 0, "max_span_h": 0.0,
        "assistant_lines": 0, "distinct_msg_ids": 0,
        "cache_5m": 0, "cache_1h": 0,
        "branches": set(), "max_user_hours": 0,
        "compacts": [], "quoted_metadata": 0,
        # "is this session active / what model is it" — both are shortcuts that
        # this history disproves, so both are measured rather than assumed
        "sidechain_lines": 0, "mtime_ahead": 0, "with_content": 0,
        "multi_model": 0, "worst_first_share": None, "worst_last_share": None,
        # which of the three title kinds a transcript actually carries: a file
        # with only a custom-title fell back to its repo name in the UI
        "titles": {}, "custom_only": 0, "no_title": 0,
    }

    for path in files:
        titles = set()
        first_day = last_day = None
        first_ts = last_ts = None
        user_hours = set()
        ids = set()
        first_model = last_model = None
        model_out = {}
        counted = {}   # message.id -> highest output_tokens seen (dedupe, §1)
        # a boundary needs the usage line before it (context at compaction) and
        # the first one after it (what the next call actually gets billed for)
        prev_usage = None
        pending = None

        with open(path, "rb") as fh:
            for raw in fh:
                if b'"gitBranch"' in raw and b'"compactMetadata"' not in raw:
                    m = re.search(rb'"gitBranch":"([^"]*)"', raw)
                    if m:
                        facts["branches"].add(m.group(1).decode("utf-8", "replace"))

                is_boundary = b'"compact_boundary"' in raw
                is_meta = b'"compactMetadata"' in raw
                has_usage = b'"usage"' in raw and b'"assistant"' in raw
                is_user = b'"type":"user"' in raw
                if b'"isSidechain":true' in raw:
                    facts["sidechain_lines"] += 1
                if b"-title" in raw or b'"agent-name"' in raw:
                    for kind, key in ((b"custom-title", b'"customTitle":"'),
                                      (b"agent-name", b'"agentName":"'),
                                      (b"ai-title", b'"aiTitle":"')):
                        if b'"type":"' + kind + b'"' in raw and key in raw:
                            titles.add(kind.decode())

                if not (is_boundary or is_meta or has_usage or is_user):
                    continue

                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                ts = obj.get("timestamp")
                day = _local_day(ts) if ts else None
                if day:
                    first_day = day if first_day is None or day < first_day else first_day
                    last_day = day if last_day is None or day > last_day else last_day
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    first_ts = t if first_ts is None or t < first_ts else first_ts
                    last_ts = t if last_ts is None or t > last_ts else last_ts

                typ = obj.get("type")

                if typ == "user":
                    if ts:
                        local = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                        user_hours.add((local.weekday(), local.hour))

                # a real compaction is a system line with subtype compact_boundary;
                # the same key appears inside quoted transcripts, which must not count
                cm = obj.get("compactMetadata")
                if isinstance(cm, dict):
                    if typ == "system" and obj.get("subtype") == "compact_boundary":
                        model = None
                        if prev_usage:
                            model = prev_usage[1]
                        pending = {
                            "trigger": cm.get("trigger"),
                            "pre": cm.get("preTokens") or 0,
                            "post": cm.get("postTokens") or 0,
                            "ctx_before": prev_usage[0] if prev_usage else None,
                            "model_before": model,
                            "ctx_after": None,
                            "duration_ms": cm.get("durationMs") or 0,
                        }
                        facts["compacts"].append(pending)
                    else:
                        facts["quoted_metadata"] += 1
                    continue

                if typ == "assistant":
                    facts["assistant_lines"] += 1
                    for m in MSG_ID.finditer(raw):
                        ids.add(m.group(1))
                    msg = obj.get("message") or {}
                    usage = msg.get("usage") or {}
                    if not usage:
                        continue
                    total = _ctx_tokens(usage)
                    if total <= 0:
                        continue
                    # Same dedupe as claude_parser (ARCHITECTURE.md §1), and for
                    # the same reason: the content-block lines of one API call
                    # repeat its usage with a growing output_tokens, so summing
                    # every line inflates output ~2x and skews the per-model
                    # shares this script exists to measure. Input-side numbers
                    # count once per message.id, output only by its delta.
                    mid = msg.get("id") or obj.get("requestId") or obj.get("uuid")
                    ot = usage.get("output_tokens") or 0
                    if mid is not None:
                        mid = str(mid)
                        prev = counted.get(mid)
                        if prev is not None:
                            if ot <= prev:
                                continue
                            delta, first_sight = ot - prev, False
                        else:
                            delta, first_sight = ot, True
                        counted[mid] = ot
                    else:
                        delta, first_sight = ot, True
                    if first_sight:
                        cc = usage.get("cache_creation") or {}
                        facts["cache_5m"] += cc.get("ephemeral_5m_input_tokens") or 0
                        facts["cache_1h"] += cc.get("ephemeral_1h_input_tokens") or 0
                    model = msg.get("model")
                    if model and not model.startswith("<"):
                        if first_model is None:
                            first_model = model
                        last_model = model
                        model_out[model] = model_out.get(model, 0) + delta
                    if pending is not None and pending["ctx_after"] is None:
                        # Skip the compaction call itself: it is logged after
                        # the boundary but still read the whole pre-compact
                        # context, so taking the first line verbatim reports a
                        # ~950k "post-compact" size. Same rule and same
                        # threshold as compacts.COMPACT_CALL_SHARE.
                        pre = pending["ctx_before"] or pending["pre"] or 0
                        if pre and total >= COMPACT_CALL_SHARE * pre:
                            pass
                        else:
                            pending["ctx_after"] = total
                            pending = None
                    prev_usage = (total, model)

        # mtime vs. content: a resumed session is touched and gets metadata
        # lines without a timestamp, so its file looks fresh while nothing was said
        if last_ts is not None:
            facts["with_content"] += 1
            try:
                if os.path.getmtime(path) - last_ts.timestamp() > 600:
                    facts["mtime_ahead"] += 1
            except OSError:
                pass

        # first-model-wins (what caveman records) and last-model-wins (what
        # Session.model used to record) against where the output really went
        total_out = sum(model_out.values())
        if total_out and len(model_out) > 1:
            facts["multi_model"] += 1
            for key, mid in (("worst_first_share", first_model),
                             ("worst_last_share", last_model)):
                share = model_out.get(mid, 0) / total_out
                if facts[key] is None or share < facts[key][0]:
                    facts[key] = (share, mid, len(model_out))

        # which title kinds this transcript has. "custom-title only" is the case
        # that fell back to the repo name, "none at all" is a young session
        for kind in titles:
            facts["titles"][kind] = facts["titles"].get(kind, 0) + 1
        if titles == {"custom-title"}:
            facts["custom_only"] += 1
        elif not titles:
            facts["no_title"] += 1

        facts["distinct_msg_ids"] += len(ids)
        facts["max_user_hours"] = max(facts["max_user_hours"], len(user_hours))
        if first_day and last_day:
            facts["with_days"] += 1
            if first_day != last_day:
                facts["multi_day"] += 1
        if first_ts and last_ts:
            span = (last_ts - first_ts).total_seconds() / 3600
            facts["max_span_h"] = max(facts["max_span_h"], span)

    return facts


def report(facts):
    ev = facts["compacts"]
    print(f"transcripts            {facts['files']} files, {facts['bytes'] / 1e6:.0f} MB")
    print(f"span more than a day   {facts['multi_day']} of {facts['with_days']}"
          f"   longest {facts['max_span_h']:.0f}h")
    print(f"dedupe factor          {facts['assistant_lines']} assistant lines / "
          f"{facts['distinct_msg_ids']} message ids = "
          f"{facts['assistant_lines'] / max(1, facts['distinct_msg_ids']):.2f}x")
    print(f"one session touches    up to {facts['max_user_hours']} distinct weekday/hour buckets")
    tot_cc = facts["cache_5m"] + facts["cache_1h"]
    if tot_cc:
        print(f"cache writes           {facts['cache_1h']:,} tokens 1h  vs  "
              f"{facts['cache_5m']:,} tokens 5m  ({facts['cache_1h'] / tot_cc * 100:.2f}% 1h)")
    print(f"git branches           {len(facts['branches'])} distinct")
    print(f"mtime ahead of content {facts['mtime_ahead']} of {facts['with_content']}"
          f" by more than 10min   <- mtime is NOT activity")
    print(f"sidechain lines        {facts['sidechain_lines']}"
          f"   <- subagent transcripts, if any")
    print(f"sessions over 1 model  {facts['multi_model']}")
    t = facts["titles"]
    print(f"title lines            custom-title {t.get('custom-title', 0)}"
          f"  agent-name {t.get('agent-name', 0)}  ai-title {t.get('ai-title', 0)}"
          f"   of {facts['files']} files")
    print(f"  custom-title only    {facts['custom_only']}"
          f"   (these showed the repo name before precedence was fixed)")
    print(f"  no title at all      {facts['no_title']}")
    for label, key in (("first-model-wins", "worst_first_share"),
                       ("last-model-wins ", "worst_last_share")):
        w = facts[key]
        if w:
            print(f"  {label}     worst case labels a session by {w[0] * 100:.1f}% of its "
                  f"output ({w[1]}, {w[2]} models)")

    if not ev:
        print("compactions            none recorded")
        return
    auto = [e for e in ev if e["trigger"] == "auto"]
    manual = [e for e in ev if e["trigger"] == "manual"]
    post_meta = [e["post"] for e in ev if e["post"]]
    ctx_after = [e["ctx_after"] for e in ev if e["ctx_after"]]
    print(f"compactions            {len(ev)}  ({len(auto)} auto / {len(manual)} manual)"
          f"   {facts['quoted_metadata']} quoted mentions ignored")
    print(f"  preTokens median     {st.median([e['pre'] for e in ev if e['pre']]):,.0f}")
    print(f"  postTokens median    {st.median(post_meta):,.0f}   <- metadata, NOT what is billed")
    print(f"  real context after   {st.median(ctx_after):,.0f}"
          f"   min {min(ctx_after):,}  max {max(ctx_after):,}"
          f"   ({st.median(ctx_after) / st.median(post_meta):.1f}x the metadata value)")
    print(f"  duration median      {st.median([e['duration_ms'] for e in ev]) / 1000:.0f}s")
    for label, group in (("auto", auto), ("manual", manual)):
        fills = [e["ctx_before"] / context_window(e["model_before"], e["ctx_before"]) * 100
                 for e in group if e["ctx_before"]]
        if fills:
            print(f"  fill when {label:<6}     median {st.median(fills):.2f}%"
                  f"   min {min(fills):.1f}%   n={len(fills)}")


if __name__ == "__main__":
    t0 = time.time()
    data = scan()
    report(data)
    print(f"\nscanned in {time.time() - t0:.1f}s   ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
