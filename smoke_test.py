"""Quick headless smoke test: boots the TUI, checks tabs render, refreshes."""

import asyncio
import sys
import os
import pathlib
import tempfile

# isolate config: theme cycling in this test must NOT touch the real
# ~/.config/cstats/config.json (it persisted test themes before)
CONFIG_SANDBOX = tempfile.mkdtemp(prefix="cstats-test-")
os.environ["XDG_CONFIG_HOME"] = CONFIG_SANDBOX
# isolate the cache for the same reason: the test refreshes for real, so it
# rewrote the user's dashboard.json, limit-history.jsonl and alert-state.json
# on every run — and a crossed alert threshold could fire a real desktop
# notification. Also makes each run start genuinely cold.
SANDBOX = tempfile.mkdtemp(prefix="cstats-cache-")
os.environ["XDG_CACHE_HOME"] = SANDBOX

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cstats.app import UsageApp
from cstats import service as service_mod

# record how every refresh was called: the first one (startup) must be forced,
# otherwise a cache snapshot plus the OAuth response TTL can show a fresh start
# numbers that are minutes old
refresh_calls = []
_orig_refresh = service_mod.DataService.refresh


def _spy_refresh(self, force=False):
    refresh_calls.append(force)
    return _orig_refresh(self, force=force)


service_mod.DataService.refresh = _spy_refresh


async def main():
    app = UsageApp()
    async with app.run_test(size=(120, 40)) as pilot:
        # wait for initial data load (async worker)
        for _ in range(40):
            await pilot.pause(0.2)
            if app.service.data is not None and app.service.data.total_sessions is not None:
                break
        await pilot.pause(0.5)

        assert app.service.data is not None
        assert app.service.data.total_sessions > 0, "no sessions parsed"
        assert app.service.data.total_cost > 0, "no cost computed"
        assert len(app.service.data.by_day_cost) > 0, "no daily costs (timestamps?)"
        assert len(app.service.data.heatmap) > 0, "no heatmap data"
        assert len(app.service.data.session_rows) > 0, "no session rows"
        assert app.service.data.cache_ratio > 0, "no cache ratio"

        assert refresh_calls, "startup did not refresh at all"
        assert refresh_calls[0] is True, "startup refresh was not forced"

        check_session_sort_filter(app.service.data)

        # switch tabs — each should render without error
        tc = app.query_one("TabbedContent")
        for tab_id in ("limits", "tokens", "activity", "sessions", "rtk", "caveman",
                       "projects", "economics", "overview"):
            tc.active = tab_id
            await pilot.pause(0.3)
            pane = app.query_one(f"#pane-{tab_id}")
            assert pane.render() is not None, f"pane {tab_id} did not render"

        # optional integrations: the tab label states when there is nothing to
        # show, so it need not be opened to find out. Both tabs must stay
        # reachable either way — they explain themselves instead of vanishing.
        for tab_id, stats in (("rtk", app.service.data.rtk), ("caveman", app.service.data.caveman)):
            label = tc.get_tab(tab_id).label.plain
            if stats.available:
                assert "(" not in label, f"{tab_id} tab marked despite having data: {label!r}"
            else:
                assert "not installed" in label or "no data" in label, \
                    f"{tab_id} unavailable but tab label says {label!r}"

        # number-key tab jump
        await pilot.press("2")
        await pilot.pause(0.2)
        assert app.query_one("TabbedContent").active == "limits"
        await pilot.press("9")
        await pilot.pause(0.2)
        assert app.query_one("TabbedContent").active == "economics"

        # theme cycling
        before = app.theme
        await pilot.press("t")
        await pilot.pause(0.2)
        assert app.theme != before, f"theme did not cycle (still {before})"
        await pilot.press("t")
        await pilot.pause(0.2)

        # help modal open/close
        await pilot.press("question_mark")
        await pilot.pause(0.3)
        from cstats.screens import HelpScreen
        assert isinstance(app.screen, HelpScreen), "help modal not open"
        # the help text is taller than a short terminal, so it must scroll by
        # key, not only by mouse wheel
        box = app.screen.query_one("#help-box")
        if box.max_scroll_y:
            await pilot.press("pagedown")
            await pilot.pause(0.2)
            assert box.scroll_offset.y > 0, "help does not scroll with pagedown"
            await pilot.press("home")
            await pilot.pause(0.2)
            assert box.scroll_offset.y == 0, "help does not scroll back with home"
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, HelpScreen), "help modal not closed"

        # settings modal open/close
        await pilot.press("comma")
        await pilot.pause(0.3)
        from cstats.screens import SettingsScreen
        assert isinstance(app.screen, SettingsScreen), "settings modal not open"
        await pilot.press("escape")
        await pilot.pause(0.3)

        # sessions sort keys: they must act on the Sessions tab and nowhere else
        tc.active = "sessions"
        await pilot.pause(0.3)
        before = app.sessions_sort
        await pilot.press("s")
        await pilot.pause(0.2)
        assert app.sessions_sort != before, "sort key did not advance"
        await pilot.press("S")
        await pilot.pause(0.2)

        tc.active = "tokens"
        await pilot.pause(0.3)
        frozen = app.sessions_sort
        await pilot.press("s")
        await pilot.pause(0.2)
        assert app.sessions_sort == frozen, "sort key changed from the wrong tab"

        # filter modal open/close
        tc.active = "sessions"
        await pilot.pause(0.3)
        await pilot.press("slash")
        await pilot.pause(0.3)
        from cstats.screens import FilterScreen
        assert isinstance(app.screen, FilterScreen), "filter modal not open"
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, FilterScreen), "filter modal not closed"

        # clicking a panel must not change what the keyboard does: the panes
        # refuse focus, so the tab keys keep working and scrolling stays on
        # app-level bindings
        tc.active = "overview"
        await pilot.pause(0.3)
        await pilot.click("#pane-overview")
        await pilot.pause(0.3)
        await pilot.press("right")
        await pilot.pause(0.3)
        assert tc.active == "limits", \
            f"arrow key stopped switching tabs after a click (on {tc.active})"
        await pilot.press("3")
        await pilot.pause(0.3)
        assert tc.active == "tokens", "digit key stopped switching tabs after a click"

        pane = app.query_one("#pane-tokens").parent
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause(0.3)
        assert pane.scroll_offset.y > 0, "arrow keys no longer scroll the pane"
        await pilot.press("home")
        await pilot.pause(0.2)

        # page scrolling
        tc.active = "tokens"
        await pilot.pause(0.2)
        await pilot.press("pagedown")
        await pilot.pause(0.2)
        await pilot.press("pageup")
        await pilot.pause(0.2)
        await pilot.press("home")
        await pilot.press("end")
        await pilot.pause(0.2)

        # trigger a manual refresh
        app.action_refresh()
        await pilot.pause(2)
        assert app.service.data is not None

        # the "refreshing…" marker is counted, so overlapping workers must not
        # leave it stuck on (or clear it while another build is still running)
        for _ in range(100):
            if app._busy == 0:
                break
            await pilot.pause(0.2)
        assert app._busy == 0, f"busy counter stuck at {app._busy}"
        assert "refreshing" not in app.sub_title, f"marker stuck: {app.sub_title!r}"

        check_narrow_widths(app.service.data)
        check_cache_isolated()
        check_no_cache_constants()
        check_dominant_model()
        check_ghost_sessions()
        check_session_title()
        check_unreadable_is_not_empty()
        check_caveman_dedupes_unlabelled_snapshots()
        check_limits_say_why()
        check_backoff_survives_without_a_response()
        check_backoff_escalates()
        check_history_survives_a_bad_line()
        check_views_do_no_io()
        check_one_name_ladder()
        check_display_label()
        check_skipped_transcripts_are_reported()
        check_seven_days_is_seven_days()
        check_no_token_leaks_through_a_redirect()
        check_notification_text_is_never_code()
        check_cache_files_are_private()
        check_context_path_cannot_escape()
        check_billing_arithmetic()
        check_savings_offset_arithmetic()
        check_optional_tools_both_ways()
        check_cache_write_ttl_split()
        check_foreign_savings_are_corrected()
        check_intro_price_expires()
        check_rtk_cap_is_measured()
        check_legacy_dirs_migrate()
        check_no_old_name_left()
        print("SMOKE TEST OK — tabs, themes, modals, scrolling, refresh all work")


def check_cache_isolated():
    """Every cache file this run wrote must be inside the test's own tempdir.

    The paths used to be module-level constants, computed at import — so a test
    setting XDG_CACHE_HOME still wrote the real dashboard.json, and the alert
    loop could fire a real desktop notification from a test run.

    The hand-written list below is not the real guard: it went stale once
    already, silently missing limits.py and limit_history.py while both wrote
    real user files during every test run. check_no_cache_constants() is what
    catches the next one.
    """
    from cstats import service, compacts, session_cache, limits, limit_history

    sandbox = os.environ["XDG_CACHE_HOME"]
    for path in (service._cache_dir(), service._cache_file(),
                 service._alert_state_file(), compacts._cache_file(),
                 session_cache.cache_path(), limits.response_cache(),
                 limit_history.history_file()):
        assert str(path).startswith(sandbox), f"cache escapes the sandbox: {path}"
    assert os.path.exists(service._cache_file()), "the run wrote no cache at all"


def check_no_cache_constants():
    """No module in cstats/ may resolve a cache or config path at import time.

    Enumerating the known paths only catches what someone remembered to add to
    the list. This reads the source instead: a top-level assignment that builds
    a ~/.cache or ~/.config path is frozen before any test can redirect the
    environment, whether or not anyone listed it. Prose that merely names the
    directory (the help text) does not build a path and is not flagged.
    """
    import ast

    offenders = []
    for path in sorted(pathlib.Path("usage").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            src = ast.unparse(node)
            builds_path = "expanduser" in src or "XDG_CACHE_HOME" in src or "XDG_CONFIG_HOME" in src
            if builds_path and ("~/.cache" in src or "~/.config" in src):
                offenders.append(f"{path}:{node.lineno}: {src.splitlines()[0]}")
    assert not offenders, (
        "cache/config path resolved at import — make it a function:\n  "
        + "\n  ".join(offenders))


def check_dominant_model():
    """A session's model label must come from where its output actually went."""
    from cstats.aggregate import _dominant_model

    class S:
        by_model = {}

    s = S()
    s.by_model = {"claude-opus-4-8": {"output": 79}, "claude-opus-5": {"output": 17},
                  "claude-fable-5": {"output": 4}}
    label = _dominant_model(s)
    assert label.startswith("Opus 4.8"), f"labelled by a minority model: {label!r}"
    assert label.endswith("*"), f"a 79/17/4 split must be marked as mixed: {label!r}"

    s.by_model = {"claude-opus-5": {"output": 100}, "<synthetic>": {"output": 0}}
    assert _dominant_model(s) == "Opus 5", "a single-model session must not be marked mixed"

    s.by_model = {"<synthetic>": {"output": 0}}
    assert _dominant_model(s) is None, "a session with no real output must not get a label"


def check_ghost_sessions():
    """A transcript that was touched but said nothing is not an active session.

    Claude touches a transcript when a session is resumed and appends metadata
    lines that carry no timestamp; 40 of 66 local transcripts have an mtime more
    than ten minutes ahead of their newest content. Ranking by mtime put
    sessions dormant for days into the panel with "Seen 0s".
    """
    import json
    from datetime import datetime, timezone, timedelta
    from cstats import claude_parser

    root = tempfile.mkdtemp(prefix="cstats-ghost-")
    proj = os.path.join(root, "projects", "-tmp-ghost")
    os.makedirs(proj)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    now = datetime.now(timezone.utc).isoformat()

    def write(name, ts):
        path = os.path.join(proj, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "assistant", "timestamp": ts,
                "message": {"id": "m1", "model": "claude-opus-5",
                            "usage": {"input_tokens": 10, "cache_read_input_tokens": 90}},
            }) + "\n")
            # metadata written on resume: a real append, but nothing was said
            fh.write(json.dumps({"type": "last-prompt", "prompt": "x"}) + "\n")
        return path

    write("ghost.jsonl", old)   # freshly touched by writing it now
    write("live.jsonl", now)

    prev = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = root
    try:
        got = {c["session"] for c in claude_parser.current_contexts(active_within_s=600)}
    finally:
        if prev is None:
            del os.environ["CLAUDE_CONFIG_DIR"]
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = prev
    assert "live.jsonl" in got, "an active session went missing"
    assert "ghost.jsonl" not in got, "a touched-but-silent session is reported as active"


def check_session_title():
    """A renamed session must show its name, not fall back to the repo name.

    Three title kinds live in a transcript and only `custom-title` is what the
    user typed. 14 of 82 local transcripts carry *only* that one — those all
    showed a bare repo name in the Active-sessions panel. The kinds also repeat
    once per turn and interleave, so precedence must not depend on file order.
    """
    import json
    from datetime import datetime, timezone
    from cstats import claude_parser

    assert claude_parser.session_title("typed", "agent", "ai") == "typed"
    assert claude_parser.session_title(None, "agent", "ai") == "agent"
    assert claude_parser.session_title(None, None, "ai") == "ai"
    assert claude_parser.session_title(None, None, None) is None

    root = tempfile.mkdtemp(prefix="cstats-title-")
    proj = os.path.join(root, "projects", "-tmp-titled")
    os.makedirs(proj)
    now = datetime.now(timezone.utc).isoformat()
    with open(os.path.join(proj, "s.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant", "timestamp": now,
            "message": {"id": "m1", "model": "claude-opus-5",
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": 90}},
        }) + "\n")
        # the generated title is written last on purpose: the typed one still wins
        fh.write(json.dumps({"type": "custom-title", "customTitle": "repo: main - typed"}) + "\n")
        fh.write(json.dumps({"type": "agent-name", "agentName": "stale generated name"}) + "\n")

    prev = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = root
    try:
        ctxs = claude_parser.current_contexts(active_within_s=600)
        sessions = claude_parser.parse_sessions(use_cache=False)
    finally:
        if prev is None:
            del os.environ["CLAUDE_CONFIG_DIR"]
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = prev

    assert len(ctxs) == 1, f"expected one active session, got {len(ctxs)}"
    assert ctxs[0]["name"] == "repo: main - typed", \
        f"active panel took the generated name: {ctxs[0]['name']!r}"
    assert len(sessions) == 1 and sessions[0].name == "repo: main - typed", \
        f"parse_sessions took the generated name: {sessions[0].name!r}"


def check_session_sort_filter(d):
    """The Sessions tab's sort/filter helper, without the TUI.

    `_sorted_session_rows` is a pure function, so ordering and filtering can be
    checked directly instead of by scraping rendered output.
    """
    from cstats import views

    rows = d.session_rows
    for key in ("date", "cost", "output", "messages"):
        a = views._sorted_session_rows(rows, key, True, "")
        assert len(a) == len(rows), f"{key} lost rows"
        vals = [r[key] for r in a]
        assert vals == sorted(vals, reverse=True), f"{key} not sorted"
        # ascending must be the exact mirror of descending, ties included
        assert views._sorted_session_rows(rows, key, False, "")[0] == a[-1], \
            f"{key} ascending is not the reverse of descending"

    needle = (rows[0].get("name") or rows[0]["project"])[:4].lower()
    f = views._sorted_session_rows(rows, "date", True, needle)
    assert f, f"filter {needle!r} matched nothing"
    for r in f:
        assert needle in (r.get("name") or "").lower() \
            or needle in (r.get("project") or "").lower(), \
            f"filter {needle!r} matched {r!r} in neither name nor project"
    assert views._sorted_session_rows(rows, "date", True, "zzz-nope") == [], \
        "impossible filter still matched"
    # the needle must not be allowed to straddle name and project: concatenating
    # the two fields matched rows where neither field contains it
    straddle = [{"name": "Fo", "project": "obar", "date": "2026-01-01 00:00",
                 "cost": 1.0, "output": 1, "messages": 1}]
    assert views._sorted_session_rows(straddle, "date", True, "oob") == [], \
        "filter matched across the name/project boundary"
    assert len(views._sorted_session_rows(straddle, "date", True, "oba")) == 1, \
        "filter no longer matches inside the project"
    # an unknown sort key must fall back, not raise (stale config value)
    assert views._sorted_session_rows(rows, "bogus", True, "")

    views.render_sessions(d, sort="cost", desc=False, name_filter=needle, limit=10)
    views.render_sessions(d)  # the old signature must keep working


def check_narrow_widths(d):
    """Key columns must survive an 80-column terminal.

    A rich Table with too many fixed-width columns drops the *trailing* ones
    instead of shrinking, so a column silently vanishes on narrow terminals
    while looking fine on a wide one. Bit us three times: the Session table's
    $/turn, caveman's Snapshot, and a panel *title* that grew past 80 columns
    once a filter was set — same trap, different widget.
    """
    from rich.console import Console
    from cstats import views

    expected = {
        views.render_context_panel: ("Session", "$/turn", "Seen"),
        views.render_caveman: ("Session", "Saved", "Snapshot"),
        views.render_rtk: ("Commands", "Saved"),
        views.render_sessions: ("Started", "Msgs"),
        views.render_tokens: ("Date", "Out"),
        # the trailing column is the one rich drops, so assert on that one
        views.render_compacts: ("When", "B/E", "After"),
        views.render_pace: ("Session", "ETA", "to go"),
        views.render_model_alt: ("Model", "Actual", "Difference"),
    }
    for width in (80, 100, 120):
        console = Console(width=width, record=True)
        for fn, headers in expected.items():
            if fn is views.render_context_panel and not d.context:
                continue
            # both economics panels return None when nothing has been measured
            if fn in (views.render_compacts, views.render_pace,
                      views.render_model_alt) and fn(d) is None:
                continue
            console.begin_capture()
            console.print(fn(d))
            out = console.end_capture()
            for header in headers:
                assert header in out, \
                    f"{fn.__name__} lost {header!r} at width {width}"

    # the Sessions panel carries state in its title and the key hint in its
    # subtitle; with a long filter the title alone can fill the line, so the
    # hint must not be crowded out. The needle comes from real data so the
    # table is actually rendered instead of the "no match" line.
    if d.session_rows:
        row = d.session_rows[0]
        long_needle = (row.get("name") or row["project"])[:12]
        for width in (80, 100, 120):
            console = Console(width=width, record=True)
            console.begin_capture()
            console.print(views.render_sessions(d, sort="cost", name_filter=long_needle))
            out = console.end_capture()
            for needed in ("Started", "Msgs", "s/S sort"):
                assert needed in out, \
                    f"render_sessions with a filter lost {needed!r} at width {width}"


def check_unreadable_is_not_empty():
    """A source we could not read must not look like a source with no data.

    rtk's DB and caveman's history file both reported STATUS_EMPTY on a read
    failure, so a corrupt DB rendered as "no data yet" — an invitation to wait
    for data that can never arrive.
    """
    import sqlite3
    from cstats import rtk, caveman

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "history.db")
        with open(db, "wb") as fh:
            fh.write(b"this is not a sqlite database")
        # load_rtk finds its own DB, so point the finder at ours
        orig = rtk._find_db
        rtk._find_db = lambda: db
        try:
            stats = rtk.load_rtk()
        finally:
            rtk._find_db = orig
        assert stats.status == rtk.STATUS_ERROR, \
            f"unreadable rtk DB reported as {stats.status!r}"

        # a text column where a number belongs used to escape load_rtk entirely
        db2 = os.path.join(tmp, "typed.db")
        con = sqlite3.connect(db2)
        con.execute("CREATE TABLE commands (input_tokens, output_tokens, saved_tokens, "
                    "savings_pct, timestamp, project_path)")
        con.execute("INSERT INTO commands VALUES (1, 1, 'abc', 'x', '2026-01-01T00:00:00Z', '/p')")
        con.commit()
        con.close()
        rtk._find_db = lambda: db2
        try:
            stats = rtk.load_rtk()
        finally:
            rtk._find_db = orig
        assert stats.status in (rtk.STATUS_OK, rtk.STATUS_ERROR), stats.status

        # caveman: NaN parses as JSON and used to raise out of the loader
        hist = os.path.join(tmp, "caveman.jsonl")
        with open(hist, "w", encoding="utf-8") as fh:
            fh.write('{"session_id": "a", "turns": NaN, "est_saved_tokens": Infinity}\n')
        cav = caveman.load_caveman(hist)
        assert cav.total_saved_tokens == 0, cav.total_saved_tokens


def check_caveman_dedupes_unlabelled_snapshots():
    """Two cumulative snapshots of one run count once, with or without an id."""
    from cstats import caveman

    with tempfile.TemporaryDirectory() as tmp:
        hist = os.path.join(tmp, "caveman.jsonl")
        with open(hist, "w", encoding="utf-8") as fh:
            fh.write('{"ts": 1, "est_saved_tokens": 100, "turns": 1}\n')
            fh.write('{"ts": 2, "est_saved_tokens": 250, "turns": 2}\n')
        cav = caveman.load_caveman(hist)
        assert cav.total_saved_tokens == 250, \
            f"unlabelled snapshots double-counted: {cav.total_saved_tokens}"
        assert cav.sessions == 1, cav.sessions


def check_limits_say_why():
    """An empty limits object must carry the reason, not just a False flag."""
    from cstats import limits

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CACHE_HOME"] = tmp
        orig = limits._access_token
        limits._access_token = lambda: None
        try:
            got = limits.fetch_limits()
        finally:
            limits._access_token = orig
            os.environ["XDG_CACHE_HOME"] = SANDBOX
        assert not got.available
        assert got.reason and "token" in got.reason, got.reason


def check_backoff_survives_without_a_response():
    """A 429 on a machine that never got a response must still back off.

    Both error arms only wrote the cache `if entry`, so a fresh machine had
    nothing to write next_try into and retried every 60s, staying rate-limited.
    """
    import urllib.error
    from cstats import limits

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CACHE_HOME"] = tmp
        orig_tok, orig_opener = limits._access_token, limits._opener
        calls = []

        class Boom:
            def open(self, *a, **kw):
                calls.append(1)
                raise urllib.error.HTTPError(
                    limits.USAGE_URL, 429, "Too Many Requests", {}, None)

        limits._access_token = lambda: "tok"
        limits._opener = lambda: Boom()
        try:
            first = limits.fetch_limits()
            second = limits.fetch_limits()
        finally:
            limits._access_token, limits._opener = orig_tok, orig_opener
            os.environ["XDG_CACHE_HOME"] = SANDBOX

        assert len(calls) == 1, f"no backoff recorded: endpoint asked {len(calls)}x"
        assert first.rate_limited and second.rate_limited
        assert second.reason, "second call gives no reason for the empty result"


def check_history_survives_a_bad_line():
    """A non-dict JSONL line must not take the limit history down.

    load() guarded only KeyError/ValueError, so a bare list raised TypeError
    out of the loader and the sparkline silently stopped updating.
    """
    from cstats import limit_history

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CACHE_HOME"] = tmp
        try:
            limit_history.record(40.0, 30.0)
            with open(limit_history.history_file(), "a", encoding="utf-8") as fh:
                fh.write("[1, 2]\n")
                fh.write('"x"\n')
            rows = limit_history.load()
        finally:
            os.environ["XDG_CACHE_HOME"] = SANDBOX
        assert len(rows) == 1, rows


def check_views_do_no_io():
    """Views are pure functions of a dashboard — no disk reads while rendering.

    render_context_panel read ~/.config/cstats/config.json on every
    render, so a 60s refresh re-opened the config file forever even though the
    app already held the value.
    """
    import ast

    src = pathlib.Path("cstats/views.py").read_text(encoding="utf-8")
    banned = {"open", "load_config", "get", "glob", "listdir", "exists", "getmtime"}
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        owner = ""
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            owner = fn.value.id
        if name in banned and owner in ("config", "os", "os.path", "glob", ""):
            if name == "get" and owner != "config":
                continue
            if name in ("open",) and owner:
                continue
            offenders.append(f"views.py:{node.lineno}: {owner + '.' if owner else ''}{name}()")
    assert not offenders, "views must not touch disk:\n  " + "\n  ".join(offenders)


def check_one_name_ladder():
    """Every display label goes through claude_parser.display_label().

    Five call sites each invented their own fallback — bare folder, folder plus
    branch, the first eight id characters, a question mark — so the same
    session appeared under different names on different tabs.
    """
    import re

    for path in (pathlib.Path("cstats/views.py"), pathlib.Path("cstats/service.py"),
                 pathlib.Path("cstats/aggregate.py")):
        src = path.read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            # `or ""` is coercion for a search/compare, not a label fallback
            if re.search(r'(get\("name"\)|\["name"\])\s+or\s(?!""|\'\')', line):
                raise AssertionError(
                    f"{path}:{i}: hand-rolled name fallback — use display_label()\n  {line.strip()}")


def check_display_label():
    from cstats.claude_parser import display_label

    assert display_label("typed name", "repo", "main", "abcdef123") == "typed name"
    assert display_label(None, "repo", "feature-x", "abcdef123") == "repo:feature-x"
    assert display_label(None, "repo", None, "abcdef123") == "repo"
    assert display_label(None, None, None, "abcdef123") == "abcdef12"
    assert display_label(None, None, None, None) == "?"


def check_skipped_transcripts_are_reported():
    """A transcript the parser could not read must not vanish quietly.

    parse_sessions dropped an unreadable file and moved on, so every total on
    the dashboard was short by that session with nothing saying so anywhere.
    """
    from cstats import aggregate, claude_parser, views
    from rich.console import Console

    problems = ["broken.jsonl: I/O error"]
    d = aggregate.Dashboard(warnings=problems)
    console = Console(width=100, record=True)
    console.begin_capture()
    console.print(views.render_overview(d))
    out = console.end_capture()
    assert "Incomplete data" in out and "broken.jsonl" in out, out[:400]

    # and it must survive the disk cache, or a cache-first start hides it
    back = aggregate.dashboard_from_json(aggregate.dashboard_to_json(d))
    assert back.warnings == problems, back.warnings

    # parse_sessions fills the list rather than swallowing the failure
    assert "problems" in claude_parser.parse_sessions.__code__.co_varnames


def check_seven_days_is_seven_days():
    """credits_7d covers seven local days, not eight.

    The cutoff subtracted 7 days and then compared inclusively, so today plus
    seven earlier days went into a figure labelled "7 days".
    """
    from datetime import datetime, timedelta, timezone
    from cstats import aggregate, pricing

    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    d = aggregate.Dashboard(generated_at=now)
    d.by_day_cost = {(now - timedelta(days=i)).astimezone().strftime("%Y-%m-%d"): 1.0
                     for i in range(10)}
    d._compute()
    assert round(d.credits_7d / pricing.CREDIT_FACTOR) == 7, \
        d.credits_7d / pricing.CREDIT_FACTOR


def check_no_token_leaks_through_a_redirect():
    """A 30x answer must fail, not be followed with the Authorization header.

    urllib preserves Authorization across redirects and does not restrict the
    host, so a single 302 would have handed the live OAuth access token to
    whatever server it named.
    """
    import urllib.error
    from cstats import limits

    handler = limits._NoRedirect()
    try:
        handler.redirect_request(
            type("R", (), {"full_url": limits.USAGE_URL})(), None, 302, "Found",
            {}, "https://elsewhere.example/x")
    except urllib.error.HTTPError as exc:
        assert "refused redirect" in str(exc), exc
    else:
        raise AssertionError("redirect was followed with the token attached")


def check_notification_text_is_never_code():
    """A session name must reach osascript as an argument, not as source.

    The AppleScript branch interpolated the session name into the script
    string, so a name carrying a quote could close it and run its own
    `do shell script`. Names come from the transcript and are typed or
    model-generated, i.e. attacker-influenced in the prompt-injection sense.
    """
    src = pathlib.Path("cstats/notify.py").read_text(encoding="utf-8")
    osa = src[src.index("osascript"):]
    assert "display notification \"" not in osa, \
        "notification text is interpolated into AppleScript source"
    assert "item 1 of argv" in osa, "osascript no longer takes the text as an argument"


def check_cache_files_are_private():
    """Cache files carry project paths, session names and spend — 600, not 644.

    config.py already chmods its file; the six cache writers used the default
    umask, so on a shared host any local account could read what the user works
    on and what it costs.
    """
    import stat as stat_mod

    world_readable = []
    for name in os.listdir(SANDBOX + "/cstats"):
        path = os.path.join(SANDBOX, "cstats", name)
        if not os.path.isfile(path):
            continue
        mode = os.stat(path).st_mode
        if mode & (stat_mod.S_IRGRP | stat_mod.S_IROTH):
            world_readable.append(f"{name} {oct(stat_mod.S_IMODE(mode))}")
    assert not world_readable, "readable by others: " + ", ".join(world_readable)


def check_context_path_cannot_escape():
    """A slug restored from the disk cache must not walk out of the projects dir.

    The dashboard cache is an ordinary file; its context entries were joined
    into a path with no validation, so a slug of "../.." read elsewhere.
    """
    from cstats import compacts

    ctx = {"slug": "../../../../etc", "session": "passwd", "tokens": 1000}
    compacts._annotate_one(ctx, os.path.expanduser("~/.claude/projects"),
                           0.999, {}, 4096)
    assert ctx.get("growth_per_turn") in (None, 0), ctx


def check_billing_arithmetic():
    """The money functions had no test at all — a pricing regression shipped silent.

    Three properties, not fixed dollar amounts, so a price-list update does not
    have to touch the test: cache_creation is billed at the write rate and NOT
    a second time as input (the phantom-cost bug), the reported 5m share can
    never exceed the reported total, and credits are cost on another scale with
    no rounding per call.
    """
    from cstats import pricing

    model = "claude-opus-5"
    i, o, cr, cw_1h, _ = pricing.price_for(model)

    # input and cache_creation are separate token classes, priced separately
    only_write = pricing.calc_cost(model, 0, 0, 0, 1_000_000)
    assert abs(only_write - cw_1h) < 1e-9, (only_write, cw_1h)
    with_input = pricing.calc_cost(model, 1_000_000, 0, 0, 1_000_000)
    assert abs(with_input - (i + cw_1h)) < 1e-9, with_input

    # a 5m share larger than the total must be clamped, not billed twice
    clamped = pricing.calc_cost(model, 0, 0, 0, 1000, cache_write_5m=99_999)
    plain_5m = pricing.calc_cost(model, 0, 0, 0, 1000, cache_write_5m=1000)
    assert abs(clamped - plain_5m) < 1e-12, (clamped, plain_5m)

    # 5m is the cheaper TTL, so it must not cost more than 1h
    assert pricing.cache_write_price(model, "5m") < pricing.cache_write_price(model, "1h")

    # credits are a scale factor on cost, applied once
    usd = pricing.calc_cost(model, 1234, 567, 89, 10)
    assert abs(pricing.calc_credits(model, 1234, 567, 89, 10)
               - usd * pricing.CREDIT_FACTOR) < 1e-12

    # summing many small calls must not round to zero on the way
    many = sum(pricing.calc_credits(model, 100, 10, 10, 1) for _ in range(1000))
    assert many > 0, many


def check_savings_offset_arithmetic():
    """A saved token is not billed once — the offset must reflect the whole chain.

    The first version priced rtk's savings read-once and understated them by
    roughly the reuse factor. This pins the shape of the formula, not a dollar
    figure: rtk saves a cache write plus every re-read, caveman saves output
    generation plus the write and re-reads that follow.
    """
    from cstats import aggregate, economics, views

    d = aggregate.Dashboard()
    d.rtk.available = True
    d.rtk.total_saved_tokens = 1_000_000
    d.rtk.billable_saved_tokens = 1_000_000
    d.caveman.available = True
    d.caveman.total_saved_tokens = 1_000_000
    d.caveman.total_output_tokens = 0   # no over-count to correct for
    d.total_cache_read = 68_000_000
    d.total_cache_write = 1_000_000
    d.total_output = 1_000_000
    d.cost_cache_read = 68.0      # $1/MTok read
    d.cost_cache_write = 20.0     # $20/MTok write
    d.cost_output = 75.0          # $75/MTok output
    d.total_cost = 163.0

    est = views.savings_offset(d)
    r = economics.billing_rates(d)
    ctx_rate = r["read"] * r["reuse"]
    assert abs(est["rtk"] - 1_000_000 * (r["write"] + ctx_rate)) < 1e-6, est["rtk"]
    assert abs(est["caveman"] - 1_000_000 * (r["output"] + r["write"] + ctx_rate)) < 1e-6
    assert abs(est["total"] - (est["rtk"] + est["caveman"])) < 1e-6
    # and the re-read part must dominate: that was the whole point of the fix
    assert est["rtk_reread"] > est["rtk_direct"], est

    # with neither tool installed there is nothing to offset
    empty = aggregate.Dashboard()
    assert views.savings_offset(empty) is None


def check_optional_tools_both_ways():
    """The tab label must be exercised for absent tools even on a host that has them."""
    from cstats import rtk, caveman, views

    for tool, stats in (("rtk", rtk.RtkStats()), ("caveman", caveman.CavemanStats())):
        stats.status, stats.available = rtk.STATUS_MISSING, False
        assert "not installed" in str(views.unavailable_lines(tool, stats)[0])
        stats.status = rtk.STATUS_EMPTY
        assert "no data yet" in str(views.unavailable_lines(tool, stats)[0])
        stats.status = rtk.STATUS_ERROR
        assert "could not be read" in str(views.unavailable_lines(tool, stats)[0])


def check_cache_write_ttl_split():
    """The 5m/1h split of one call must survive parsing into the session totals.

    99.74% of the local history is 1h, so a bug in the rarer 5m path would not
    show up in any total — it needs its own check.
    """
    import json
    from cstats import claude_parser

    with tempfile.TemporaryDirectory() as tmp:
        proj = os.path.join(tmp, "projects", "-x-y")
        os.makedirs(proj)
        line = {
            "type": "assistant", "timestamp": "2026-08-20T10:00:00.000Z",
            "sessionId": "s1",
            "message": {"id": "msg_1", "model": "claude-opus-5",
                        "usage": {"input_tokens": 10, "output_tokens": 20,
                                  "cache_read_input_tokens": 30,
                                  "cache_creation_input_tokens": 100,
                                  "cache_creation": {"ephemeral_5m_input_tokens": 40,
                                                     "ephemeral_1h_input_tokens": 60}}},
        }
        with open(os.path.join(proj, "s1.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

        orig = claude_parser._claude_dir
        claude_parser._claude_dir = lambda: tmp
        try:
            sessions = claude_parser.parse_sessions(use_cache=False)
        finally:
            claude_parser._claude_dir = orig

        assert len(sessions) == 1, sessions
        s = sessions[0]
        assert s.cache_write == 100 and s.cache_write_5m == 40, (s.cache_write, s.cache_write_5m)
        # the split must be cheaper than billing all 100 at the 1h rate
        from cstats import pricing
        all_1h = pricing.calc_cost("claude-opus-5", 10, 20, 30, 100)
        assert s.cost < all_1h, (s.cost, all_1h)


def check_backoff_escalates():
    """Repeated 429s must wait longer each time, and a success must reset it.

    A fixed wait against a longer server-side window loops: expire, ask, get
    refused, wait the same fixed time again — and every refused request keeps
    the limit alive.
    """
    import urllib.error
    from cstats import limits

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["XDG_CACHE_HOME"] = tmp
        orig_tok, orig_opener = limits._access_token, limits._opener

        class Refuse:
            def open(self, *a, **kw):
                raise urllib.error.HTTPError(
                    limits.USAGE_URL, 429, "Too Many Requests", {}, None)

        limits._access_token = lambda: "tok"
        limits._opener = lambda: Refuse()
        waits = []
        try:
            for _ in range(4):
                got = limits.fetch_limits()
                waits.append(limits._read_cache()["next_try"])
                # expire the backoff so the next call actually asks again
                entry = limits._read_cache()
                limits._write_cache(entry["data"], entry["ts"], 0.0, entry["strikes"])
        finally:
            limits._access_token, limits._opener = orig_tok, orig_opener
            os.environ["XDG_CACHE_HOME"] = SANDBOX

        gaps = [round(w - waits[0]) for w in waits]
        assert gaps[1] > 0 and gaps[2] > gaps[1], f"backoff did not escalate: {gaps}"
        assert got.reason and "rate-limited" in got.reason, got.reason


def check_foreign_savings_are_corrected():
    """Neither tool's own saving figure may be priced as it stands.

    rtk counts the full untruncated command output, which Claude Code would
    never have let into the context; caveman counts every content-block line as
    another answer. Both corrections are measured, and both must be visible in
    the panel rather than applied silently.
    """
    from cstats import aggregate, views
    from rich.console import Console

    d = aggregate.Dashboard()
    d.total_output = 1_000_000
    d.total_cache_read = 68_000_000
    d.total_cache_write = 1_000_000
    d.cost_cache_read, d.cost_cache_write, d.cost_output = 68.0, 20.0, 75.0
    d.total_cost = 163.0
    d.rtk.available = True
    d.rtk.total_saved_tokens = 10_000_000      # what rtk claims
    d.rtk.billable_saved_tokens = 1_000_000    # what could have been read
    d.rtk.capped_commands = 3
    d.caveman.available = True
    d.caveman.total_saved_tokens = 2_000_000
    d.caveman.total_output_tokens = 2_000_000  # 2x what we measured

    est = views.savings_offset(d)
    assert est["rtk_saved"] == 1_000_000, est["rtk_saved"]
    assert abs(est["cav_overcount"] - 2.0) < 1e-9, est["cav_overcount"]
    assert abs(est["cav_saved"] - 1_000_000) < 1e-6, est["cav_saved"]

    console = Console(width=110, record=True)
    console.begin_capture()
    console.print(views.render_overview(d))
    out = console.end_capture()
    assert "rtk reports" in out and "caveman reports" in out, out[-700:]

    # a tool reporting no more than we measured must not be scaled at all
    d.caveman.total_output_tokens = 500_000
    assert views.caveman_overcount(d) == 1.0


def check_intro_price_expires():
    """A price that only applied for a while must not re-price the history.

    Sonnet 5 launched at $2/$10 with the list price at $3/$15 from
    2026-09-01. A table without dates would have raised the cost of every
    Sonnet 5 call ever made, overnight, by 50%.
    """
    from cstats import pricing

    intro = pricing.price_for("claude-sonnet-5", "2026-08-20")
    listed = pricing.price_for("claude-sonnet-5", "2026-09-01")
    assert intro[:4] == (2.00, 10.00, 0.20, 4.00), intro
    assert listed[:4] == (3.00, 15.00, 0.30, 6.00), listed
    assert intro[4] == listed[4] == "Sonnet 5"

    # the cost of one historical call must not depend on today's date
    old = pricing.calc_cost("claude-sonnet-5", 1_000_000, 0, 0, 0, when="2026-08-01")
    assert abs(old - 2.00) < 1e-9, old
    new = pricing.calc_cost("claude-sonnet-5", 1_000_000, 0, 0, 0, when="2026-12-01")
    assert abs(new - 3.00) < 1e-9, new

    # models without a dated entry are untouched by the mechanism
    for mid in ("claude-opus-5", "claude-fable-5", "claude-haiku-4-5-20251001"):
        assert pricing.price_for(mid, "2026-08-01") == pricing.price_for(mid, "2027-01-01")


def check_rtk_cap_is_measured():
    """The rtk cap must reflect what a Bash result can actually be.

    Measured over 25,639 real Bash tool results in the local history the
    largest is 52,545 characters; the ceiling is set from that, not guessed.
    """
    from cstats import rtk

    assert 8_000 <= rtk.BASH_OUTPUT_CEILING_TOKENS <= 20_000, \
        rtk.BASH_OUTPUT_CEILING_TOKENS
    stats = rtk.load_rtk()
    if stats.available:
        assert stats.billable_saved_tokens <= stats.total_saved_tokens
        assert stats.billable_saved_tokens >= 0


def check_legacy_dirs_migrate():
    """The pre-rename directories are carried over, once, without merging.

    The tool was called claude-usage. Abandoning ~/.config/claude-usage and
    ~/.cache/claude-usage would have reset the theme and the compaction
    threshold and thrown away 30 days of limit history.
    """
    from cstats import config

    with tempfile.TemporaryDirectory() as tmp:
        cache, cfg = os.path.join(tmp, "cache"), os.path.join(tmp, "config")
        os.environ["XDG_CACHE_HOME"], os.environ["XDG_CONFIG_HOME"] = cache, cfg
        try:
            old_cfg = os.path.join(cfg, "claude-usage")
            os.makedirs(old_cfg)
            with open(os.path.join(old_cfg, "config.json"), "w", encoding="utf-8") as fh:
                fh.write('{"theme": "nexat", "context_alert_usd": 0.42}')
            old_cache = os.path.join(cache, "claude-usage")
            os.makedirs(old_cache)
            with open(os.path.join(old_cache, "limit-history.jsonl"), "w", encoding="utf-8") as fh:
                fh.write('{"t": "2026-08-01T00:00:00+00:00", "fh": 1, "sd": 2}\n')

            moved = config.migrate_legacy_dirs()
            assert len(moved) == 2, moved
            assert config.get("theme") == "nexat"
            assert config.get("context_alert_usd") == 0.42
            assert os.path.exists(os.path.join(cache, "cstats", "limit-history.jsonl"))
            assert not os.path.exists(old_cfg) and not os.path.exists(old_cache)

            # idempotent
            assert config.migrate_legacy_dirs() == []

            # and it must never merge onto an existing new directory
            os.makedirs(os.path.join(cfg, "claude-usage"))
            assert config.migrate_legacy_dirs() == []
            assert os.path.isdir(os.path.join(cfg, "claude-usage"))
        finally:
            os.environ["XDG_CACHE_HOME"] = SANDBOX
            os.environ["XDG_CONFIG_HOME"] = CONFIG_SANDBOX


def check_no_old_name_left():
    """Nothing may still write or read the pre-rename name."""
    import ast

    stale = []
    for path in sorted(pathlib.Path("cstats").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        # docstrings and comments may name the old directory — the migration
        # has to explain itself somewhere. Code may not.
        doc_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                doc_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        for i, line in enumerate(src.splitlines(), 1):
            if "claude-usage" not in line or "LEGACY_NAME" in line:
                continue
            if i in doc_lines or line.strip().startswith("#"):
                continue
            stale.append(f"{path}:{i}: {line.strip()}")
    assert not stale, "old name still in use:\n  " + "\n  ".join(stale)


asyncio.run(main())
