"""Tests for the incremental per-file parse cache and the transcript tail read.

The one property that matters: a refresh that uses the cache must produce a
Dashboard identical to a refresh that does not. Not similar — identical, down to
the float. Everything here builds synthetic transcripts in a tempdir and points
CLAUDE_CONFIG_DIR and XDG_CACHE_HOME at it, so no test ever reads or writes the
real ~/.claude (AGENTS.md rule 2) or the real cache.

The cases are the ones an append-only cache actually gets wrong:
  1. cold vs warm
  2. three warm runs in a row (no drift)
  3. a file that grows between runs
  4. a file that grows exactly at a line boundary
  5. a half-written last line that is completed later
  6. a shrunk file, and an mtime that jumps backwards
  7. a corrupt / foreign-version cache (must degrade to a full scan)
  8. message.ids that resume across the cache boundary
  9. `_read_tail_usage` on a transcript smaller than its own head read

Run: .venv/bin/python test_session_cache.py
"""

import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


# ---------------------------------------------------------------------------
# synthetic transcripts
# ---------------------------------------------------------------------------
BASE_TS = 1_753_000_000  # fixed epoch so day/hour buckets are reproducible


def _iso(epoch_s):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _assistant(mid, ot, ts, model="claude-opus-5", it=11, cr=1301, cc=707,
               cc5=0, branch="main", sid="sess-1"):
    return {
        "type": "assistant",
        "sessionId": sid,
        "timestamp": _iso(ts),
        "gitBranch": branch,
        "message": {
            "id": mid,
            "model": model,
            "usage": {
                "input_tokens": it,
                "output_tokens": ot,
                "cache_creation_input_tokens": cc,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cc5,
                    "ephemeral_1h_input_tokens": cc - cc5,
                },
                "cache_read_input_tokens": cr,
            },
            "content": [{"type": "tool_use", "name": "Bash"}],
        },
    }


def _user(ts, branch="main", sid="sess-1"):
    return {"type": "user", "sessionId": sid, "timestamp": _iso(ts),
            "gitBranch": branch, "message": {"role": "user", "content": "hi"}}


def _title(name, sid="sess-1"):
    return {"type": "agent-name", "sessionId": sid, "agentName": name}


def transcript_lines(turns=12, sid="sess-1", model="claude-opus-5", reopen=True):
    """A transcript with multi-block messages, model switches and a re-opened id.

    The re-opened message.id is the case the measurement found in the real
    history: one id whose content blocks are NOT contiguous, so a resume that
    kept only "the last few" entries of `counted` would count its input side
    twice. Here it sits far enough from the end to land on either side of a
    cache boundary.

    The ids carry the session id. Real message ids are globally unique, and
    reusing "msg_000" across every fixture transcript made these files look
    like forks of one another to the cross-file dedupe — which would then
    correctly refuse to bill the same call twice, and wrongly fail these tests.
    """
    lines = [_title("fixture", sid)]
    for t in range(turns):
        ts = BASE_TS + t * 3600
        mid = "msg_%s_%03d" % (sid, t)
        lines.append(_user(ts, sid=sid))
        mdl = model if t % 3 else "claude-sonnet-5"
        # three content-block lines of one call, output_tokens growing
        lines.append(_assistant(mid, 100, ts, model=mdl, cc5=17, sid=sid))
        lines.append(_assistant(mid, 250, ts, model=mdl, cc5=17, sid=sid))
        lines.append(_assistant(mid, 400, ts, model=mdl, cc5=17, sid=sid))
        if t == 3:
            # a line with no model id at all -> must land in its own bucket
            obj = _assistant("msg_nomodel_%s" % sid, 42, ts, sid=sid)
            obj["message"].pop("model")
            lines.append(obj)
    if reopen:
        # re-open an early message.id after many others have been seen
        lines.append(_assistant("msg_%s_001" % sid, 900, BASE_TS + turns * 3600,
                                model=model, sid=sid))
    return [json.dumps(o) + "\n" for o in lines]


def reopen_line(sid="sess-1", model="claude-opus-5", turns=12):
    """The line that continues this session's msg_001 long after its blocks."""
    return json.dumps(_assistant("msg_%s_001" % sid, 900, BASE_TS + turns * 3600,
                                 model=model, sid=sid)) + "\n"


def write_fixture(root, files):
    """files: {relative slug/name: list of line strings}."""
    for rel, lines in files.items():
        path = os.path.join(root, "projects", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------
FIELDS = ("total_cost", "total_input", "total_output", "total_cache_read",
          "total_cache_write", "total_credits", "credits_7d", "total_messages",
          "by_day_cost", "by_day_tokens", "heatmap", "session_rows")

FIXED_NOW = None  # set in main(), so credits_7d is deterministic


def build(use_cache):
    from cstats import claude_parser, aggregate
    sessions = claude_parser.parse_sessions(use_cache=use_cache)
    return aggregate.Dashboard(sessions, generated_at=FIXED_NOW), sessions


def model_totals(sessions):
    out = {}
    for s in sessions:
        for k, m in s.by_model.items():
            b = out.setdefault(k, {})
            for kk, vv in m.items():
                b[kk] = b.get(kk, 0) + vv
    return out


def diff(a, b, sess_a, sess_b):
    """Field names where two dashboards disagree. Empty list == identical."""
    bad = [f for f in FIELDS if getattr(a, f) != getattr(b, f)]
    if model_totals(sess_a) != model_totals(sess_b):
        bad.append("by_model")
    return bad


def check(label, results):
    ok = not results
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + ("" if ok else f"   -> differs in {results}"))
    return ok


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------
def case_cold_vs_warm():
    ref, sref = build(False)
    cold, scold = build(True)     # populates the cache
    warm, swarm = build(True)     # served from it
    return (check("1a cold cache == no cache", diff(ref, cold, sref, scold))
            and check("1b warm cache == no cache", diff(ref, warm, sref, swarm)))


def case_three_warm():
    build(True)
    ref, sref = build(False)
    ok = True
    for i in range(3):
        d, s = build(True)
        ok &= check(f"2.{i + 1} warm run {i + 1} == no cache", diff(ref, d, sref, s))
    return ok


def case_growth(root, rel, extra_lines, label):
    build(True)  # warm the cache on the current content
    path = os.path.join(root, "projects", rel)
    with open(path, "a", encoding="utf-8") as fh:
        fh.writelines(extra_lines)
    ref, sref = build(False)
    got, sgot = build(True)
    return check(label, diff(ref, got, sref, sgot))


def case_partial_line(root, rel):
    """A half-written last line must not be committed, then must count once."""
    path = os.path.join(root, "projects", rel)
    tail = json.dumps(_assistant("msg_tail", 555, BASE_TS + 99_000)) + "\n"
    # write it torn: no trailing newline, cut mid-JSON
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(tail[:len(tail) // 2])
    build(True)  # cache must commit only up to the last complete line
    ref, sref = build(False)
    got, sgot = build(True)
    ok = check("5a torn last line: cached == no cache", diff(ref, got, sref, sgot))

    # now complete the line
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(tail[len(tail) // 2:])
    ref, sref = build(False)
    got, sgot = build(True)
    ok &= check("5b completed line counted exactly once",
                diff(ref, got, sref, sgot))
    return ok


def case_shrink_and_backdate(root, rel):
    path = os.path.join(root, "projects", rel)
    build(True)
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(lines[:len(lines) // 2])
    ref, sref = build(False)
    got, sgot = build(True)
    ok = check("6a shrunk file rescanned", diff(ref, got, sref, sgot))

    # same length, older mtime, different content: an in-place rewrite
    build(True)
    with open(path, "r", encoding="utf-8") as fh:
        body = fh.read()
    replaced = body.replace('"claude-opus-5"', '"claude-sonnet-5"')
    if len(replaced) != len(body):  # keep the length identical on purpose
        replaced = body.replace("_000", "_XXX")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(replaced)
    os.utime(path, (BASE_TS, BASE_TS))  # mtime jumps backwards
    ref, sref = build(False)
    got, sgot = build(True)
    ok &= check("6b in-place rewrite with backdated mtime rescanned",
                diff(ref, got, sref, sgot))
    return ok


def case_broken_cache():
    from cstats import session_cache
    ref, sref = build(False)
    ok = True
    for label, blob in (("truncated JSON", '{"parse_version": 1, "files"'),
                        ("foreign version", '{"parse_version": 9999, "files": {}}'),
                        ("wrong shape", '[]'),
                        ("garbage", 'not json at all')):
        build(True)
        with open(session_cache.cache_path(), "w", encoding="utf-8") as fh:
            fh.write(blob)
        got, sgot = build(True)
        ok &= check(f"7 broken cache ({label}) falls back to a full scan",
                    diff(ref, got, sref, sgot))
    return ok


def case_unknown_model_bucket():
    """A billed call without a model id gets a visible key, never an empty one."""
    from cstats import claude_parser
    _, sessions = build(False)
    totals = model_totals(sessions)
    ok = check("8a no empty model key", [] if "" not in totals else [""])
    ok &= check("8b model-less call has its own bucket",
                [] if claude_parser.UNKNOWN_MODEL in totals
                else ["missing " + claude_parser.UNKNOWN_MODEL])
    return ok


def case_by_model_sums():
    d, sessions = build(False)
    totals = model_totals(sessions)
    got = sum(m["cost"] for m in totals.values())
    rel = abs(got - d.total_cost) / d.total_cost * 100 if d.total_cost else 0.0
    return check(f"9 sum(by_model cost) == total_cost (rel {rel:.6f}%)",
                 [] if rel < 0.1 else [f"{got} != {d.total_cost}"])


def case_forked_transcript(root):
    """A forked session copies older turns — the copy must not be billed again.

    Claude Code writes the inherited history into the new transcript verbatim,
    message.id included. Those lines are not new API calls. Measured before the
    guard existed: 470 ids across 7 pairs of real transcripts, $52 of spend
    counted twice.
    """
    rel_a = "-home-u-repositories-delta/sess-fork-a.jsonl"
    rel_b = "-home-u-repositories-delta/sess-fork-b.jsonl"
    path_a = os.path.join(root, "projects", rel_a)
    path_b = os.path.join(root, "projects", rel_b)
    os.makedirs(os.path.dirname(path_a), exist_ok=True)

    lines = transcript_lines(6, "sess-fork-a", reopen=False)
    with open(path_a, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    alone, _salone = build(False)

    # the fork: the first four turns copied verbatim, then its own new turn
    with open(path_b, "w", encoding="utf-8") as fh:
        fh.writelines(lines[:9])
        fh.write(json.dumps(_assistant("msg_fork_new", 700, BASE_TS + 90_000,
                                       sid="sess-fork-b")) + "\n")
    both, sboth = build(False)

    copied_cost = both.total_cost - alone.total_cost
    from cstats.pricing import calc_cost
    own = calc_cost("claude-opus-5", 11, 700, 1301, 707, 0, _iso(BASE_TS + 90_000))
    ok = abs(copied_cost - own) < 1e-9
    print(("  PASS  " if ok else "  FAIL  ")
          + "14 forked transcript bills only its own new call"
          + ("" if ok else f"   -> fork added ${copied_cost:.4f}, its own call is ${own:.4f}"))

    # and the cached path must agree with the full scan, as everywhere else
    build(True)
    warm, swarm = build(True)
    ok2 = check("14b forked transcript: warm cache == full scan",
                diff(both, warm, sboth, swarm))

    os.remove(path_a)
    os.remove(path_b)
    return ok and ok2


def case_counted_across_boundary(root):
    """A message.id continued *after* the cache boundary — the core difficulty.

    The cache is warmed on a transcript that ends mid-message, then the
    continuation line is appended. Getting this right needs `counted` restored
    from the cache: without it the continuation looks like a first sight, so the
    call's whole input side is billed a second time and its output is counted in
    full instead of as a delta.

    The negative control matters as much as the test: the same scenario is run
    again with `counted` deliberately stripped from the cache entry, and that
    MUST produce different numbers. If it does not, this test proves nothing.
    """
    from cstats import session_cache
    rel = "-home-u-repositories-gamma/sess-3.jsonl"
    path = os.path.join(root, "projects", rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(transcript_lines(12, "sess-3", reopen=False))

    build(True)                       # cache stops right before the reopen
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(reopen_line("sess-3", turns=12))
    ref, sref = build(False)
    got, sgot = build(True)
    ok = check("10a message.id resumed across the cache boundary",
               diff(ref, got, sref, sgot))

    # negative control: strip `counted`, the same run must now be wrong
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(transcript_lines(12, "sess-3", reopen=False))
    build(True)
    cache = session_cache.load()
    for key, entry in cache.items():
        if key.endswith("sess-3.jsonl"):
            entry["counted"] = {}
    session_cache.save(cache)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(reopen_line("sess-3", turns=12))
    ref2, sref2 = build(False)
    broken, sbroken = build(True)
    control = diff(ref2, broken, sref2, sbroken)
    ok &= check("10b negative control: dropping `counted` really does break it",
                [] if control else ["stripping counted changed nothing — "
                                    "test 10a proves nothing"])
    os.remove(path)
    return ok


def case_max_files_keeps_cache():
    """A capped run must not throw away the cache for the files it skipped.

    `parse_sessions(max_files=1)` only looks at the newest transcript. Writing
    back just what that run parsed would drop every other entry, so the next
    full refresh would be a cold one — the cache would be destroyed by any
    `--json --max-files` style call.
    """
    from cstats import claude_parser, session_cache
    build(True)
    before = set(session_cache.load())
    claude_parser.parse_sessions(max_files=1)
    after = set(session_cache.load())
    ok = check("11a capped run keeps the other files' entries",
               [] if before <= after else [f"lost {sorted(before - after)}"])
    ref, sref = build(False)
    got, sgot = build(True)
    ok &= check("11b full run after a capped run still matches",
                diff(ref, got, sref, sgot))
    return ok


def case_vanished_file(root):
    """An entry whose transcript is gone must not survive in the cache."""
    from cstats import session_cache
    rel = "-home-u-repositories-delta/sess-4.jsonl"
    path = os.path.join(root, "projects", rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(transcript_lines(3, "sess-4"))
    build(True)
    present = any(k.endswith("sess-4.jsonl") for k in session_cache.load())
    os.remove(path)
    build(True)
    gone = not any(k.endswith("sess-4.jsonl") for k in session_cache.load())
    return check("12 deleted transcript is pruned from the cache",
                 [] if (present and gone) else
                 [f"present_before={present} pruned_after={gone}"])


def case_tail_read_small_file(root):
    """`_read_tail_usage` must work on transcripts smaller than its head read.

    The regression: the function reads the first 60 KB to find an `ai-title`,
    which leaves the handle at EOF for any file below that size. The tail read
    then returned "" and the whole transcript looked like it had no context, so
    `current_contexts()` silently dropped it. Measured on the real history: all
    17 transcripts under 60 KB were lost — and small means young, i.e. exactly
    the sessions the Active-sessions panel and the per-turn cost warning exist
    for.

    Three sizes on purpose: below the 60 KB head read, between the head read and
    the 500 KB tail window (the same bug, milder: the data started at byte
    60.000), and above the tail window (which always worked).
    """
    from cstats.claude_parser import _read_tail_usage
    # last line of the fixture is the reopened msg_001, whose usage adds up to
    # input 11 + cache_creation 707 + cache_read 1301
    expect = 11 + 707 + 1301
    ok = True
    for label, turns in (("under the 60 KB head read", 8),
                         ("between head read and tail window", 90),
                         ("above the tail window", 400)):
        path = os.path.join(root, "projects", "-home-u-repositories-tail",
                            "t.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(transcript_lines(turns, "sess-tail"))
        size = os.path.getsize(path)
        got = _read_tail_usage(path, 500_000)
        bad = []
        if got is None:
            bad.append(f"returned None for a {size} byte transcript")
        elif got["tokens"] != expect:
            bad.append(f"tokens {got['tokens']} != {expect}")
        elif got["name"] != "fixture":
            bad.append(f"name {got['name']!r} != 'fixture'")
        ok &= check(f"13 tail read, {label} ({size // 1024} KB)", bad)
        os.remove(path)
    return ok


# ---------------------------------------------------------------------------
def main():
    global FIXED_NOW
    from datetime import datetime, timezone
    FIXED_NOW = datetime.fromtimestamp(BASE_TS + 200_000, tz=timezone.utc)

    root = tempfile.mkdtemp(prefix="cstats-test-")
    try:
        claude = os.path.join(root, "claude")
        os.environ["CLAUDE_CONFIG_DIR"] = claude
        os.environ["XDG_CACHE_HOME"] = os.path.join(root, "cache")
        write_fixture(claude, {
            "-home-u-repositories-alpha/sess-1.jsonl": transcript_lines(12, "sess-1"),
            "-home-u-repositories-beta/sess-2.jsonl": transcript_lines(5, "sess-2",
                                                                      "claude-fable-5"),
        })
        rel = "-home-u-repositories-alpha/sess-1.jsonl"

        print("parse cache — equality tests")
        ok = case_cold_vs_warm()
        ok &= case_three_warm()
        ok &= case_growth(claude, rel,
                          transcript_lines(3, "sess-1")[1:],
                          "3 file grew between runs")
        # the previous run ended exactly on a newline, so the next append starts
        # at a byte offset equal to the old size — the boundary case
        ok &= case_growth(claude, rel,
                          transcript_lines(2, "sess-1")[1:],
                          "4 file grew exactly at a line boundary")
        ok &= case_partial_line(claude, rel)
        ok &= case_shrink_and_backdate(claude, rel)
        ok &= case_broken_cache()
        ok &= case_unknown_model_bucket()
        ok &= case_by_model_sums()
        ok &= case_counted_across_boundary(claude)
        ok &= case_forked_transcript(claude)
        ok &= case_max_files_keeps_cache()
        ok &= case_vanished_file(claude)
        ok &= case_tail_read_small_file(claude)

        print("SESSION CACHE TESTS OK" if ok else "SESSION CACHE TESTS FAILED")
        return 0 if ok else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
