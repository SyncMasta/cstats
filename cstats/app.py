"""Textual TUI for combined Claude Code + rtk + caveman usage dashboard."""

from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer
from textual.widgets import Header, Footer, TabbedContent, TabPane, Static
from textual import work
from textual.binding import Binding

from .service import DataService
from . import views, config, themes
from .screens import FilterScreen, HelpScreen, SettingsScreen

TAB_IDS = ("overview", "limits", "tokens", "activity", "sessions", "rtk", "caveman",
           "projects", "economics")
TAB_TITLES = ("Overview", "Limits", "Tokens", "Activity", "Sessions", "rtk", "caveman",
              "Projects", "Economics")


class UsageApp(App):
    """Main TUI app with auto-refreshing tabs."""

    TITLE = "Claude Usage Dashboard"

    CSS = """
    HelpScreen, SettingsScreen, FilterScreen {
        align: center middle;
    }
    #help-box, #settings-box, #filter-box {
        width: 64;
        height: auto;
        /* the help text is longer than a short terminal; cap it so the box
           scrolls instead of being cut off at the screen edge */
        max-height: 90%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("1", "goto_tab('overview')", "Overview", show=False),
        Binding("2", "goto_tab('limits')", "Limits", show=False),
        Binding("3", "goto_tab('tokens')", "Tokens", show=False),
        Binding("4", "goto_tab('activity')", "Activity", show=False),
        Binding("5", "goto_tab('sessions')", "Sessions", show=False),
        Binding("6", "goto_tab('rtk')", "rtk", show=False),
        Binding("7", "goto_tab('caveman')", "caveman", show=False),
        Binding("8", "goto_tab('projects')", "Projects", show=False),
        Binding("9", "goto_tab('economics')", "Economics", show=False),
        Binding("pageup", "scroll_page('up')", "Page up", show=True),
        Binding("pagedown", "scroll_page('down')", "Page down", show=True),
        # the panes refuse focus, so line scrolling has to live here too
        Binding("up", "scroll_line('up')", "Scroll up", show=False),
        Binding("down", "scroll_line('down')", "Scroll down", show=False),
        Binding("home", "scroll_edge('home')", "Top", show=False),
        Binding("end", "scroll_edge('end')", "Bottom", show=False),
        Binding("t", "cycle_theme", "Theme", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        # Sessions-tab only, hence show=False: advertising them in the footer
        # would be misleading on the seven tabs where they do nothing. They are
        # named in the Sessions panel title and in the help modal instead.
        Binding("s", "sessions_sort_key", "Sort sessions", show=False),
        Binding("S", "sessions_sort_dir", "Sort direction", show=False),
        Binding("slash", "sessions_filter", "Filter sessions", show=False),
        Binding("question_mark", "help", "Help", show=True),
        Binding("comma", "settings", "Settings", show=False),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, use_cache=True, theme=None):
        super().__init__()
        self.service = DataService(use_cache=use_cache)
        self.cfg = config.load_config()
        self.refresh_ms = self.cfg.get("refresh_ms", 60_000)
        self._busy = 0  # running refresh workers (drives the title-bar marker)
        # Sessions tab view state. Sort key and direction are persisted like the
        # theme; the name filter is transient on purpose — a filter surviving a
        # restart would look like lost sessions.
        self.sessions_sort = self.cfg.get("sessions_sort", "date")
        # compare against False rather than casting: a hand-edited config with
        # the string "false" would pass bool() as True and silently sort the
        # wrong way round
        self.sessions_desc = self.cfg.get("sessions_desc", True) not in (False, "false", 0)
        self.sessions_filter = ""
        if theme:
            self.cfg["theme"] = theme

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="overview"):
            for tab_id, title in zip(TAB_IDS, TAB_TITLES):
                with TabPane(title, id=tab_id):
                    pane = ScrollableContainer(Static(id=f"pane-{tab_id}"))
                    # A click used to move focus into the container, after which
                    # the arrow keys scrolled the panel instead of switching
                    # tabs — clicking a panel silently changed what the keyboard
                    # did. Nothing is lost by refusing focus: the scroll keys are
                    # app-level bindings that act on the visible pane, and the
                    # mouse wheel does not need focus either.
                    pane.can_focus = False
                    yield pane
        yield Footer()

    def on_mount(self) -> None:
        themes.register_custom_themes(self)
        theme = self.cfg.get("theme", "orange")
        self.theme = theme if theme in self.available_themes else "orange"
        self.sub_title = self._subtitle()
        self.set_interval(self.refresh_ms / 1000, self._auto_refresh)
        # render cached data immediately (if any), then force one refresh:
        # the cache snapshot can be hours old and the OAuth response TTL would
        # otherwise serve a stale reading to a freshly started TUI
        if self.service.cache_loaded:
            self._render_all()
        else:
            self._show_loading()
        self.refresh_data(force=True)

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------
    def _auto_refresh(self) -> None:
        self.refresh_data()

    @work(exclusive=True, thread=True, group="refresh")
    def refresh_data(self, force=False, notify_done=False) -> None:
        self._call_ui(self._set_busy, +1)
        try:
            self.service.refresh(force=force)
            self.call_from_thread(self._render_all)
            if notify_done:
                self.call_from_thread(self._notify_refreshed)
        finally:
            self._call_ui(self._set_busy, -1)

    def _call_ui(self, fn, *args) -> None:
        """call_from_thread that tolerates a shutting-down app.

        Runs from the refresh worker's `finally`, which also fires while the
        app is tearing down — a raise there would surface as a worker error.
        """
        try:
            self.call_from_thread(fn, *args)
        except Exception:
            pass

    def _subtitle(self, busy=False) -> str:
        base = f"Claude Code + rtk + caveman  ·  theme: {self.theme}"
        return f"{base}  ·  refreshing…" if busy else base

    def _set_busy(self, delta: int) -> None:
        """Show in the title bar that a build is running.

        A full rebuild parses every transcript, so the startup refresh takes a
        moment; without this the cached numbers look like the final ones.
        Counted, not a flag: `r` starts a second worker while a thread worker
        that was cancelled for exclusivity still runs to its `finally`, and a
        flag would clear the marker while a build is still going.
        """
        self._busy = max(0, self._busy + delta)
        self.sub_title = self._subtitle(self._busy > 0)

    def _show_error(self, message: str) -> None:
        """No dashboard at all — show why, in every pane."""
        for tab_id in TAB_IDS:
            self.query_one(f"#pane-{tab_id}", Static).update(
                f"Could not load usage data:\n\n{message}\n\n"
                "Press r to retry. Check ~/.claude/projects and "
                "~/.claude/.credentials.json."
            )

    def _show_loading(self) -> None:
        """First run without a cache: say so instead of showing empty panes."""
        for tab_id in TAB_IDS:
            self.query_one(f"#pane-{tab_id}", Static).update(
                "Loading usage data — parsing session transcripts…"
            )

    def _notify_refreshed(self) -> None:
        d = self.service.data
        if d is None:
            self.notify(f"Refresh failed: {self.service.error or 'no data'}",
                        severity="error", timeout=5)
            return
        lim = d.limits
        msg = f"Updated {d.generated_at.astimezone().strftime('%H:%M:%S')}"
        if lim.available and lim.rate_limited:
            msg += " — limits rate-limited, showing last known values"
        self.notify(msg, timeout=3)

    def _render_all(self) -> None:
        # always render the service's current dashboard, never a snapshot a
        # slower worker was holding on to — otherwise a late auto-refresh can
        # overwrite the result of a manual one
        d = self.service.data
        err = self.service.error
        if d is None:
            # the very first build failed and left nothing to render. Say so in
            # every pane instead of raising out of the worker, which killed the
            # app: a broken data source must never take the dashboard with it.
            self._show_error(err or "no data could be loaded")
            return
        self.query_one("#pane-overview", Static).update(views.render_overview(d, error=err, alert_usd=self.cfg.get("context_alert_usd")))
        self.query_one("#pane-limits", Static).update(views.render_limits(d, history=self.service.limit_history))
        self.query_one("#pane-tokens", Static).update(views.render_tokens(d))
        self.query_one("#pane-activity", Static).update(views.render_activity(d))
        self.query_one("#pane-sessions", Static).update(self._sessions_renderable(d))
        self.query_one("#pane-rtk", Static).update(views.render_rtk(d))
        self.query_one("#pane-caveman", Static).update(views.render_caveman(d))
        self.query_one("#pane-projects", Static).update(views.render_projects(d))
        self.query_one("#pane-economics", Static).update(views.render_economics(d))
        self._mark_optional_tabs(d)

    def _sessions_renderable(self, d):
        return views.render_sessions(
            d, sort=self.sessions_sort, desc=self.sessions_desc,
            name_filter=self.sessions_filter,
        )

    def _render_sessions_pane(self) -> None:
        """Re-render only the Sessions pane.

        A sort or filter keypress changes nothing on the other seven panes, and
        a full `_render_all()` per keystroke rebuilds every table.
        """
        d = self.service.data
        if d is None:  # first run, data still loading
            return
        self.query_one("#pane-sessions", Static).update(self._sessions_renderable(d))

    def _mark_optional_tabs(self, d) -> None:
        """Flag the tabs of optional integrations that have nothing to show.

        rtk and caveman are add-ons; without them the tab still opens and
        explains itself. Marking the label saves opening a tab to find out.
        """
        tc = self.query_one(TabbedContent)
        for tab_id, stats in (("rtk", d.rtk), ("caveman", d.caveman)):
            title = dict(zip(TAB_IDS, TAB_TITLES))[tab_id]
            if not stats.available:
                status = getattr(stats, "status", "")
                suffix = {"missing": "not installed",
                          "error": "unreadable"}.get(status, "no data")
                title = f"{title} ({suffix})"
            try:
                tc.get_tab(tab_id).label = title
            except Exception:  # tab not mounted yet — next render fixes it
                pass

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def action_refresh(self) -> None:
        self.notify("Refreshing data...", timeout=2)
        self.refresh_data(force=True, notify_done=True)

    def action_goto_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def _active_scroll(self):
        tc = self.query_one(TabbedContent)
        for tab_id in TAB_IDS:
            if tc.active == tab_id:
                return self.query_one(f"#pane-{tab_id}").parent
        return None

    def action_scroll_page(self, direction: str) -> None:
        container = self._active_scroll()
        if container is None:
            return
        if direction == "up":
            container.scroll_page_up()
        else:
            container.scroll_page_down()

    def action_scroll_line(self, direction: str) -> None:
        container = self._active_scroll()
        if container is None:
            return
        if direction == "up":
            container.scroll_up()
        else:
            container.scroll_down()

    def action_scroll_edge(self, edge: str) -> None:
        container = self._active_scroll()
        if container is None:
            return
        if edge == "home":
            container.scroll_home(animate=False)
        else:
            container.scroll_end(animate=False)

    def action_cycle_theme(self) -> None:
        try:
            idx = themes.CYCLE.index(self.theme)
        except ValueError:
            idx = 0
        new_theme = themes.CYCLE[(idx + 1) % len(themes.CYCLE)]
        self.theme = new_theme
        config.set_setting("theme", new_theme)
        self.sub_title = self._subtitle(self._busy > 0)
        self.notify(f"theme: {new_theme}", timeout=2)

    def _on_sessions_tab(self) -> bool:
        """True on the Sessions tab; otherwise say so and do nothing.

        Sorting and filtering only affect one tab. Silently changing hidden
        state — or jumping to the Sessions tab uninvited — would both surprise;
        a notification explains where the key works.
        """
        if self.query_one(TabbedContent).active == "sessions":
            return True
        self.notify("sorting works on the Sessions tab (5)", timeout=2)
        return False

    def action_sessions_sort_key(self) -> None:
        if not self._on_sessions_tab():
            return
        keys = views.SESSION_SORT_KEYS
        try:
            idx = keys.index(self.sessions_sort)
        except ValueError:
            idx = 0
        self.sessions_sort = keys[(idx + 1) % len(keys)]
        config.set_setting("sessions_sort", self.sessions_sort)
        self._render_sessions_pane()

    def action_sessions_sort_dir(self) -> None:
        if not self._on_sessions_tab():
            return
        self.sessions_desc = not self.sessions_desc
        config.set_setting("sessions_desc", self.sessions_desc)
        self._render_sessions_pane()

    def action_sessions_filter(self) -> None:
        if not self._on_sessions_tab():
            return
        self.push_screen(FilterScreen(self.sessions_filter), self._apply_sessions_filter)

    def _apply_sessions_filter(self, value) -> None:
        """Callback of FilterScreen: str applies (empty clears), None cancels."""
        if value is None:
            return
        self.sessions_filter = value.strip()
        self._render_sessions_pane()
        # put the focus back on the pane; a leftover focus on the dismissed
        # modal's input would keep eating keypresses
        container = self._active_scroll()
        if container is not None:
            try:
                container.focus()
            except Exception:
                pass

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_settings(self) -> None:
        self.cfg = config.load_config()
        self.push_screen(SettingsScreen(self.cfg))


def main(use_cache=True, theme=None, inline=False) -> int:
    UsageApp(use_cache=use_cache, theme=theme).run(inline=inline)
    return 0
