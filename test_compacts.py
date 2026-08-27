"""Unit tests for cstats/compacts.py and the economics extensions.

Run: .venv/bin/python test_compacts.py

The scan cache is redirected into a tempdir (XDG_CACHE_HOME), so this test
never touches the real ~/.cache/cstats. ~/.claude is only ever read.
"""

import atexit
import os
import shutil
import sys
import tempfile
import time
import types

_TMP = tempfile.mkdtemp(prefix="cstats-compacts-test-")
os.environ["XDG_CACHE_HOME"] = _TMP
atexit.register(shutil.rmtree, _TMP, True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cstats import compacts, economics  # noqa: E402

# The documented snapshot of the reference history (ARCHITECTURE.md,
# "Measured state of the history"). Pinned so a regression that halves the event count fails loudly
# instead of passing a range check. New compactions legitimately move these —
# the test then says so and falls back to invariants, but never accepts fewer
# events than were documented.
REF = {
    "total": 97, "auto": 15, "manual": 82,
    "auto_fill": 0.9990505, "manual_fill": 0.498731,
    "ctx_after_median": 70590, "ctx_after_min": 47644, "ctx_after_max": 104283,
    "post_median": 17177, "pre_median": 567296,
    "summary_output_tokens": 4749, "no_ctx_before": 3,
}


def test_pace_survives_a_compact():
    """A compaction drops the context ~900k — the rate must stay positive."""
    samples = [(100_000, 0, False), (120_000, 60, False),
               (999_000, 600, False), (63_000, 660, True),
               (70_000, 700, False), (80_000, 740, False), (92_000, 780, False)]
    p = compacts._pace_from_samples(samples)
    assert p["growth_per_turn"] > 0, p
    # the post-compact sample IS the anchor of the new segment and is kept:
    # dropping it read 14% high (11,000/turn from 3 samples vs 9,667 from 4)
    assert p["samples"] == 4, p
    assert abs(p["growth_per_turn"] - (92_000 - 63_000) / 3) < 1e-9, p
    assert p["growth_per_hour"] > 0, p

    # same drop, but no boundary line (a /clear or a resumed session): the cut
    # at the last negative delta has to catch it, and keep the same anchor
    unmarked = [(t, ts, False) for t, ts, _ in samples]
    q = compacts._pace_from_samples(unmarked)
    assert q["growth_per_turn"] == p["growth_per_turn"], (q, p)
    assert q["samples"] == 4, q

    # too few samples: no number is better than a wrong ETA
    r = compacts._pace_from_samples([(10_000, 0, False), (20_000, 60, False)])
    assert r["growth_per_turn"] is None, r
    assert compacts._pace_from_samples([])["growth_per_turn"] is None
    # monotonic growth stays untouched
    s = compacts._pace_from_samples([(10_000, 0, False), (20_000, 60, False),
                                     (30_000, 120, False)])
    assert s["samples"] == 3 and s["growth_per_turn"] == 10_000, s
    print("  pace: compact jump, unmarked drop, short window  OK")


def test_real_scan():
    """Scan the real history and check the invariants + the postTokens trap."""
    cache = os.path.join(_TMP, "empty-cache", "compacts.json")
    t0 = time.time()
    stats = compacts.load_compacts(cache_path=cache)
    cold = time.time() - t0
    assert stats.total == len(stats.events), (stats.total, len(stats.events))
    assert stats.auto + stats.manual == stats.total
    for ev in stats.events:
        assert ev.trigger in ("auto", "manual"), ev.trigger
        assert ev.turns_after >= 0 and ev.turns_before >= 0
    assert stats.scan_s < 5.0, stats.scan_s

    if not stats.total:
        print("  real scan: no compactions in this history — status "
              f"{stats.status!r}, skipping metric checks")
        assert stats.status == compacts.STATUS_EMPTY
        assert stats.post_compact_tokens == economics.POST_COMPACT_TOKENS
        return stats, cold

    assert stats.status == compacts.STATUS_OK
    assert stats.available
    # regression guard against the postTokens trap: the billed post-compact
    # context is multiples of what Claude Code reports as postTokens
    assert stats.post_compact_tokens > stats.post_median, \
        (stats.post_compact_tokens, stats.post_median)
    assert 0.5 < stats.auto_fill_ratio <= 1.05, stats.auto_fill_ratio
    assert stats.ctx_after_min <= stats.ctx_after_median <= stats.ctx_after_max
    assert stats.pre_median > stats.post_median
    assert sum(r["n"] for r in stats.by_session.values()) == stats.total
    _check_reference(stats)
    print(f"  real scan: {stats.total} events "
          f"({stats.auto} auto / {stats.manual} manual), "
          f"post_compact={stats.post_compact_tokens:,} "
          f"(postTokens median {stats.post_median:,.0f}), "
          f"auto_fill={stats.auto_fill_ratio:.4f}, cold {cold:.2f}s  OK")
    return stats, cold


def _check_reference(stats):
    """Pin the documented measurements of the reference history.

    Fewer events than documented is always a regression. More events means the
    user compacted again since the snapshot was taken — legitimate, so the
    pinned medians are reported as stale instead of failing.
    """
    assert stats.total >= REF["total"], (
        f"history shrank: {stats.total} events, {REF['total']} documented — "
        "either a scanner regression or transcripts were deleted")
    if stats.total != REF["total"]:
        print(f"  NOTE: history grew to {stats.total} events "
              f"(reference snapshot: {REF['total']}). Update REF in this file "
              "and the 'Measured state of the history' block in ARCHITECTURE.md.")
        return
    assert (stats.auto, stats.manual) == (REF["auto"], REF["manual"]), \
        (stats.auto, stats.manual)
    assert stats.ctx_after_median == REF["ctx_after_median"], stats.ctx_after_median
    assert stats.ctx_after_min == REF["ctx_after_min"], stats.ctx_after_min
    assert stats.ctx_after_max == REF["ctx_after_max"], stats.ctx_after_max
    assert stats.post_median == REF["post_median"], stats.post_median
    assert stats.pre_median == REF["pre_median"], stats.pre_median
    # the headline figure is the same median, rounded to a whole token — an even
    # event count makes the median land on a .5, which it did not used to
    assert stats.post_compact_tokens == round(REF["ctx_after_median"])
    assert stats.summary_output_tokens == REF["summary_output_tokens"], \
        stats.summary_output_tokens
    assert abs(stats.auto_fill_ratio - REF["auto_fill"]) < 5e-6, stats.auto_fill_ratio
    assert abs(stats.manual_fill_ratio - REF["manual_fill"]) < 5e-6, \
        stats.manual_fill_ratio
    missing = sum(1 for e in stats.events if e.ctx_before is None)
    assert missing == REF["no_ctx_before"], missing
    # the compaction call is logged after the boundary and still carries the
    # OLD context — no ctx_after may be anywhere near the pre-compact size
    for e in stats.events:
        if e.ctx_after and e.pre_tokens:
            assert e.ctx_after < 0.5 * e.pre_tokens, \
                (e.session_id, e.ctx_after, e.pre_tokens)


def test_cache_effect(cold_stats, cold_s):
    """A second run must agree with the first, metric for metric."""
    warm = compacts.load_compacts()          # populates the tempdir cache
    again = compacts.load_compacts()         # now served from it

    # turn counts are deliberately left out: a session running *right now*
    # appends turns between the two scans, so only the compaction-derived
    # metrics are stable across runs (the incremental test below pins the
    # turn counting exactly, against a synthetic transcript that holds still)
    for name in ("total", "auto", "manual", "post_compact_tokens",
                 "summary_output_tokens", "pre_median", "post_median",
                 "ctx_after_median"):
        assert getattr(warm, name) == getattr(again, name), \
            f"{name}: {getattr(warm, name)!r} != {getattr(again, name)!r}"
        assert getattr(cold_stats, name) == getattr(again, name), \
            f"{name} differs from the uncached run"
    assert abs(warm.auto_fill_ratio - again.auto_fill_ratio) < 1e-9
    assert again.turns_total >= cold_stats.turns_total
    assert set(warm.by_session) == set(again.by_session)
    for sid, row in warm.by_session.items():
        for key in ("n", "auto", "manual", "dropped", "last_ts"):
            assert row[key] == again.by_session[sid][key], (sid, key)
    # no wall-clock assertion: on a busy machine that measures the scheduler,
    # not the cache. What must hold is that the cache exists and changes nothing
    assert os.path.exists(compacts._cache_file())
    print("  cache: written, second run identical to the uncached one  OK")
    return again


def test_json_roundtrip(stats):
    import json
    obj = compacts.to_json(stats)
    json.dumps(obj)  # must be JSON-native throughout
    back = compacts.from_json(obj)
    # total counts every event; the list may be cut, and then `truncated` says
    # so rather than leaving total != len(events) looking like a broken scan
    assert back.truncated is (len(stats.events) > compacts.MAX_CACHED_EVENTS)
    if not back.truncated:
        assert back.total == len(back.events), (back.total, len(back.events))
    assert back.total == stats.total
    assert back.post_compact_tokens == stats.post_compact_tokens
    assert back.auto_fill_ratio == stats.auto_fill_ratio
    assert back.by_session == stats.by_session
    assert back.measured == stats.measured
    if stats.events:
        assert back.events[-1].trigger == stats.events[-1].trigger
        assert back.events[-1].ts == stats.events[-1].ts
    assert compacts.from_json(None).total == 0
    assert compacts.from_json({}).post_compact_tokens == economics.POST_COMPACT_TOKENS
    print("  json roundtrip  OK")


def test_annotate_pace(stats):
    """annotate_pace on a session that is deliberately, synthetically live.

    The real history may have nothing active, in which case the interesting
    paths never run. So this builds a transcript with a fresh mtime under a
    fake CLAUDE_CONFIG_DIR and drives the whole chain: current_contexts finds
    it, annotate_pace measures it, and the numbers have to be the ones the
    fixture implies.
    """
    from cstats import claude_parser

    root = tempfile.mkdtemp(prefix="cstats-live-", dir=_TMP)
    proj = os.path.join(root, "projects", "home-u-repositories-live")
    os.makedirs(proj)
    path = os.path.join(proj, "s-live.jsonl")
    base = time.time() - 600
    from datetime import datetime, timezone

    def iso(offset):
        return datetime.fromtimestamp(base + offset, tz=timezone.utc).isoformat()

    pad = 12_000  # push the file past the 60 KB head-read (see _assistant_obj)
    lines = [
        _assistant("msg_l1", 900_000, iso(0), "s-live", pad),
        _boundary(iso(30), "auto", 905_000, 15_000, sid="s-live"),
        # the compaction call: logged after the boundary, still on the old
        # context. It must not become the anchor of the new segment.
        _assistant("msg_l2", 903_000, iso(60), "s-live", pad),
        _assistant("msg_l3", 60_000, iso(90), "s-live", pad),
        _assistant("msg_l4", 70_000, iso(150), "s-live", pad),
        _assistant("msg_l4", 70_000, iso(151), "s-live", pad),  # 2nd block
        _assistant("msg_l5", 80_000, iso(210), "s-live", pad),
        _assistant("msg_l6", 90_000, iso(270), "s-live", pad),
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    old_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = root
    try:
        ctxs = claude_parser.current_contexts(active_within_s=86_400)
        assert len(ctxs) == 1, ctxs
        live = compacts.load_compacts(claude_dir=root,
                                      cache_path=os.path.join(root, "s.json"))
        compacts.annotate_pace(ctxs, live)
    finally:
        if old_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_dir

    ctx = ctxs[0]
    for key in compacts.PACE_KEYS:
        assert key in ctx, f"{key} missing from annotated context"
    # samples: the 60k anchor (flagged as the post-boundary one) plus 70k, 80k,
    # 90k — the 903k compaction call is filtered, the repeated block deduped
    assert ctx["pace_samples"] == 4, ctx["pace_samples"]
    assert ctx["growth_per_turn"] == (90_000 - 60_000) / 3, ctx["growth_per_turn"]
    assert 0 < ctx["pace_span_s"] <= 200, ctx["pace_span_s"]
    assert ctx["last_compact_trigger"] == "auto", ctx["last_compact_trigger"]
    assert ctx["last_compact_ts"], ctx["last_compact_ts"]
    assert ctx["auto_threshold"] > 900_000, ctx["auto_threshold"]
    expected_turns = (ctx["auto_threshold"] - ctx["tokens"]) / ctx["growth_per_turn"]
    assert abs(ctx["turns_to_auto"] - expected_turns) < 1e-6
    assert ctx["eta_auto_s"] > 0
    # the compaction call must not have been taken as the post-compact context
    assert live.events[0].ctx_after == 60_000, live.events[0].ctx_after

    # every key is guaranteed even when the transcript does not exist: a
    # renderer indexing ctx["auto_threshold"] must not raise KeyError
    bogus = [{"slug": "does-not-exist", "session": "nope.jsonl", "tokens": 1,
              "model": "claude-opus-5"}]
    compacts.annotate_pace(bogus, stats)
    for key in compacts.PACE_KEYS:
        assert key in bogus[0], key
    assert bogus[0]["growth_per_turn"] is None
    assert bogus[0]["pace_samples"] == 0
    compacts.annotate_pace(None, stats)
    compacts.annotate_pace([], stats)

    # a sample with no timestamp is dropped, not dated to 1970 (that made
    # span_s 1.77 billion seconds and the ETA 14.7 million hours)
    real = claude_parser.current_contexts(active_within_s=86_400)
    compacts.annotate_pace(real, stats)
    for c in real:
        if c["pace_span_s"]:
            assert c["pace_span_s"] < 30 * 86_400, (c["session"], c["pace_span_s"])
    print(f"  annotate_pace: synthetic live session + {len(real)} real  OK")


def _line(obj):
    import json
    return json.dumps(obj, separators=(",", ":")) + "\n"


def _assistant_obj(mid, tokens, ts, sid="s-1", pad=0):
    """An assistant record in the real key order of current transcripts.

    That order matters: `message` (and with it `usage`) comes FIRST and
    `"type":"assistant"` follows it, with the bulky metadata trailing. A line
    torn anywhere in that trailing part still satisfies every byte prefilter,
    which is exactly what makes torn lines dangerous.

    `pad` inflates the record with filler text. Needed because
    `claude_parser._read_tail_usage` reads a 60 KB head and then reads the
    "tail" from wherever that left the handle, so a transcript below 60 KB
    yields an empty tail and never registers as an active session. Real
    transcripts are megabytes; a fixture has to be padded past that line to
    exercise the same code path.
    """
    obj = {
        "parentUuid": "p-" + mid, "isSidechain": False,
        "message": {"model": "claude-opus-5", "id": mid, "usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": tokens - 10, "output_tokens": 7},
            "content": []},
        "type": "assistant", "uuid": "u-" + mid, "timestamp": ts,
        "cwd": "/home/u/repositories/demo", "gitBranch": "main",
        "sessionId": sid, "userType": "external", "version": "2.0.0",
    }
    if pad:
        obj["message"]["content"] = [{"type": "text", "text": "f" * pad}]
    return obj


def _assistant(mid, tokens, ts, sid="s-1", pad=0):
    return _line(_assistant_obj(mid, tokens, ts, sid, pad))


def _torn_assistant(mid, tokens, ts):
    """The same record, cut inside the trailing metadata, without a newline."""
    full = _line(_assistant_obj(mid, tokens, ts)).rstrip("\n")
    cut = full[:full.index('"cwd"') + 9]
    assert '"usage"' in cut and '"type":"assistant"' in cut, \
        "fixture must reproduce a tear that passes both byte prefilters"
    return cut


def _boundary(ts, trigger, pre, post, sid="s-1"):
    return _line({
        "type": "system", "subtype": "compact_boundary", "timestamp": ts,
        "sessionId": sid, "cwd": "/tmp/x", "gitBranch": "main",
        "compactMetadata": {"trigger": trigger, "preTokens": pre,
                            "postTokens": post, "durationMs": 1234,
                            "cumulativeDroppedTokens": pre - post},
    })


def test_incremental_append():
    """A grown transcript must resume to the same result as a full rescan."""
    root = tempfile.mkdtemp(prefix="cstats-fake-", dir=_TMP)
    proj = os.path.join(root, "projects", "home-u-repositories-demo")
    os.makedirs(proj)
    path = os.path.join(proj, "s-1.jsonl")

    part1 = (_assistant("msg_01a", 100_000, "2026-08-01T10:00:00Z")
             + _assistant("msg_01a", 100_000, "2026-08-01T10:00:01Z")  # 2nd block
             + _assistant("msg_01b", 900_000, "2026-08-01T10:05:00Z")
             + _boundary("2026-08-01T10:06:00Z", "auto", 905_000, 15_000)
             + _line({"type": "user", "isCompactSummary": True,
                      "message": {"content": [{"type": "text",
                                               "text": "x" * 20_000}]}})
             + _assistant("msg_01c", 60_000, "2026-08-01T10:07:00Z"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(part1)

    cache = os.path.join(root, "scan.json")
    first = compacts.load_compacts(claude_dir=root, cache_path=cache)
    assert first.total == 1, first.total
    ev = first.events[0]
    assert ev.trigger == "auto" and ev.ctx_before == 900_000 and ev.ctx_after == 60_000
    assert ev.dropped_tokens == 840_000
    assert ev.turns_before == 2, ev.turns_before   # msg_01a deduped
    assert ev.turns_after == 1, ev.turns_after
    assert ev.summary_chars == 20_000
    assert ev.git_branch == "main" and ev.session_id == "s-1"
    assert 0.89 < ev.fill_ratio < 0.91, ev.fill_ratio  # 900k of a 1M window
    assert first.post_compact_tokens == economics.POST_COMPACT_TOKENS  # 1 < MIN_EVENTS
    assert first.measured is False

    # append more turns plus a second compaction, and leave a torn last line
    part2 = (_assistant("msg_01d", 300_000, "2026-08-01T11:00:00Z")
             + _assistant("msg_01e", 800_000, "2026-08-01T11:30:00Z")
             + _boundary("2026-08-01T11:31:00Z", "manual", 805_000, 12_000)
             + _assistant("msg_01f", 55_000, "2026-08-01T11:32:00Z")
             + _assistant("msg_01g", 90_000, "2026-08-01T11:40:00Z"))
    torn = _torn_assistant("msg_01h", 95_000, "2026-08-01T11:45:00Z")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(part2 + torn)
    os.utime(path, (time.time() + 1, time.time() + 1))

    grown = compacts.load_compacts(claude_dir=root, cache_path=cache)
    fresh = compacts.load_compacts(claude_dir=root,
                                   cache_path=os.path.join(root, "scan2.json"))
    assert grown.total == fresh.total == 2, (grown.total, fresh.total)
    for a, b in zip(grown.events, fresh.events):
        assert a.to_dict() == b.to_dict(), (a.to_dict(), b.to_dict())
    e1, e2 = grown.events
    assert e1.turns_after == 3, e1.turns_after       # c, d, e
    assert e2.turns_before == 3, e2.turns_before
    assert e2.ctx_before == 800_000 and e2.ctx_after == 55_000
    assert e2.turns_after == 2, e2.turns_after       # f, g — torn line excluded
    assert grown.by_session["s-1"]["n"] == 2

    # and the phantom turn must not be cached either: repeated fully-cached
    # runs kept re-serving turns_after=3 before the state was snapshotted at
    # newlines only
    for _ in range(3):
        again = compacts.load_compacts(claude_dir=root, cache_path=cache)
        assert again.events[-1].turns_after == 2, again.events[-1].turns_after

    # completing the line makes it count — exactly once
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(_line(_assistant_obj("msg_01h", 95_000, "2026-08-01T11:45:00Z"))
                 [len(torn):])
    os.utime(path, (time.time() + 2, time.time() + 2))
    for _ in range(3):
        healed = compacts.load_compacts(claude_dir=root, cache_path=cache)
        assert healed.events[-1].turns_after == 3, healed.events[-1].turns_after
    print("  incremental: resume == full rescan, torn line neither counted "
          "nor cached  OK")


def test_cache_idempotent_many_events():
    """Cached runs must equal a full scan even past MAX_CACHED_EVENTS.

    Truncating the cached event list made every cached run lose the events
    that fell off the front: 210 -> 209 -> 202 -> 201, monotonically down and
    never recovering, because the truncated list is the basis the next
    incremental resume builds on.
    """
    root = tempfile.mkdtemp(prefix="cstats-many-", dir=_TMP)
    proj = os.path.join(root, "projects", "home-u-repositories-many")
    os.makedirs(proj)
    path = os.path.join(proj, "s-many.jsonl")
    n = compacts.MAX_CACHED_EVENTS + 10
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(_assistant(f"msg_m{i}", 900_000, "2026-08-01T10:00:00Z",
                                sid="s-many"))
            fh.write(_boundary("2026-08-01T10:00:01Z", "auto", 905_000, 15_000,
                               sid="s-many"))
            fh.write(_assistant(f"msg_n{i}", 60_000, "2026-08-01T10:00:02Z",
                                sid="s-many"))

    cache = os.path.join(root, "scan.json")
    full = compacts.load_compacts(claude_dir=root,
                                  cache_path=os.path.join(root, "fresh.json"))
    assert full.total == n, (full.total, n)
    seen = [compacts.load_compacts(claude_dir=root, cache_path=cache).total
            for _ in range(4)]
    assert seen == [n] * 4, seen
    print(f"  idempotence: {n} events in one file, cached runs {seen}  OK")


def test_zero_token_error_line():
    """An API-error line must not become the context reading before a boundary.

    Claude Code writes an assistant line for a failed call too: every token
    count zero and an id that is not a `msg_` id. Taking that line as
    "the context before the compaction" reported a context of nothing and cost
    three real events their ctx_before (found against live data).
    """
    root = tempfile.mkdtemp(prefix="cstats-err-", dir=_TMP)
    proj = os.path.join(root, "projects", "home-u-repositories-demo")
    os.makedirs(proj)
    error_line = _line({
        "type": "assistant", "isSidechain": False, "isApiErrorMessage": True,
        "timestamp": "2026-08-01T10:06:30Z", "sessionId": "s-2",
        "message": {"id": "bbc14f0e-0000", "model": "claude-opus-5", "usage": {
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
    })
    with open(os.path.join(proj, "s-2.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(_assistant("msg_02a", 500_000, "2026-08-01T10:00:00Z")
                 + _assistant("msg_02b", 800_000, "2026-08-01T10:05:00Z")
                 + error_line
                 + _boundary("2026-08-01T10:07:00Z", "manual", 805_000, 12_000)
                 + error_line
                 + _assistant("msg_02c", 61_000, "2026-08-01T10:08:00Z"))

    stats = compacts.load_compacts(claude_dir=root,
                                   cache_path=os.path.join(root, "scan.json"))
    ev = stats.events[0]
    assert ev.ctx_before == 800_000, ev.ctx_before
    assert ev.ctx_after == 61_000, ev.ctx_after   # error line skipped after too
    assert ev.turns_before == 2, ev.turns_before  # the error line is not a turn
    assert ev.turns_after == 1, ev.turns_after
    assert ev.fill_ratio and 0.79 < ev.fill_ratio < 0.81, ev.fill_ratio
    print("  api-error line: ignored for context readings and turns  OK")


def test_economics_back_compat(stats):
    """billing_rates/session_economics must survive a dashboard sans compacts."""
    blank = types.SimpleNamespace(
        cost_cache_read=10.0, total_cache_read=20_000_000,
        cost_cache_write=5.0, total_cache_write=1_000_000,
        cost_output=2.0, total_output=500_000,
    )
    rates = economics.billing_rates(blank)
    assert rates["post_compact"] == economics.POST_COMPACT_TOKENS
    assert rates["summary_out"] == economics.SUMMARY_OUTPUT_TOKENS
    assert rates["post_measured"] is False
    assert 0.5 < rates["auto_fill"] <= 1.05

    # A real Dashboard always carries a .compacts, but an empty one: the rates
    # must fall back to the constants rather than to zeros. The case of a object
    # without the attribute at all is covered by the SimpleNamespace above.
    from cstats.aggregate import Dashboard
    d_rates = economics.billing_rates(Dashboard())
    assert d_rates["post_compact"] == economics.POST_COMPACT_TOKENS

    # two-argument call still works and equals the explicit rates value
    two = economics.session_economics(400_000, rates)
    three = economics.session_economics(400_000, rates, rates["post_compact"])
    assert two == three, (two, three)
    assert two["breakeven_turns"] > 0
    # a measured history feeds through without touching the caller
    with_measured = economics.billing_rates(
        types.SimpleNamespace(compacts=stats, **vars(blank)))
    assert with_measured["post_compact"] == (
        stats.post_compact_tokens if stats.measured else economics.POST_COMPACT_TOKENS)
    econ = economics.session_economics(400_000, with_measured)
    assert econ["after"] > 0

    # ledger on a degenerate event: boundary as the first line of the file
    broken = compacts.CompactEvent(
        trigger="auto", ctx_before=None, ctx_after=None, pre_tokens=None,
        post_tokens=None, dropped_tokens=None, summary_chars=0,
        turns_after=0, turns_before=0)
    led = economics.compact_ledger(broken, rates)
    assert led["dropped"] == 0
    assert led["breakeven_turns"] is None
    assert led["payback_x"] is None
    assert led["avoided_usd_upper"] == 0.0
    # and on a dict-shaped event, for good measure
    assert economics.compact_ledger({}, rates)["dropped"] == 0

    # a missing ctx_after must NOT fall back to postTokens: that is the number
    # documented as understating the post-compact context by ~3.7x, and using
    # it here would inflate `dropped` by the same factor
    half = {"ctx_before": 900_000, "ctx_after": None, "post_tokens": 15_000,
            "turns_after": 10, "summary_chars": 20_000}
    led_half = economics.compact_ledger(half, rates)
    assert led_half["dropped"] == 900_000 - rates["post_compact"], led_half
    assert led_half["dropped"] < 900_000 - 15_000

    if stats.events:
        real = next((e for e in stats.events if e.ctx_before and e.ctx_after), None)
        if real is not None:
            led = economics.compact_ledger(real, rates)
            assert led["dropped"] > 0
            assert led["compact_cost"] > 0
            assert led["breakeven_turns"] > 0
            assert led["saved_per_turn"] > 0

    # runway: arithmetic series, not tokens x turns
    r = economics.runway(500_000, 10_000, 980_000, rates)
    assert r["tokens_left"] == 480_000
    assert abs(r["turns_left"] - 48.0) < 1e-9
    naive = rates["read"] * 48.0 * 500_000
    assert r["usd_until"] > naive, (r["usd_until"], naive)
    assert abs(r["usd_until"] - rates["read"] * 48.0 * 740_000) < 1e-9
    # no growth rate, or threshold already passed -> no forecast
    assert economics.runway(500_000, None, 980_000, rates)["turns_left"] is None
    assert economics.runway(990_000, 10_000, 980_000, rates)["turns_left"] is None

    # advice() recalibration: with a 60k post-compact floor the old bound of 15
    # pushed the verdict out to ~175k tokens. Check the documented crossover
    # against the real rates rather than trusting the comment.
    fire = None
    for tokens in range(60_000, 400_000, 500):
        econ = economics.session_economics(tokens, rates)
        if economics.advice(econ, 0.0) == "compact":
            fire = tokens
            break
    assert fire is not None
    econ = economics.session_economics(fire, rates)
    assert econ["breakeven_turns"] <= 25
    old = economics.advice(economics.session_economics(fire, rates), 0.0,
                           max_breakeven=15)
    assert old != "compact", "recalibration is a no-op — check the rates"
    # a context at or below the post-compact size saves nothing by compacting
    assert economics.advice(
        economics.session_economics(rates["post_compact"], rates), 0.0) == ""
    print(f"  economics: back-compat, ledger, runway, advice fires at "
          f"{fire:,} tok  OK")


def main():
    print("test_compacts.py")
    test_pace_survives_a_compact()
    cold_stats, cold_s = test_real_scan()
    stats = test_cache_effect(cold_stats, cold_s)
    test_json_roundtrip(stats)
    test_annotate_pace(stats)
    test_incremental_append()
    test_cache_idempotent_many_events()
    test_zero_token_error_line()
    test_economics_back_compat(stats)
    print("TEST OK — pace, real scan, cache idempotence, json, annotate, "
          "torn lines, economics")


if __name__ == "__main__":
    main()
