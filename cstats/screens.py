"""Modal screens: help (?), settings (,) and the Sessions filter (/)."""

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Static
from textual.binding import Binding

from . import __version__
from .themes import CYCLE


HELP_TEXT = f"""\
[bold]cstats[/]  v{__version__}

[bold cyan]Keys[/]
  [bold]1-9[/]      jump to tab
  [bold]tab[/]      next tab
  [bold]shift+tab[/] previous tab
  [bold]↑/↓[/]      scroll line
  [bold]pgup/pgdn[/] scroll page
  [bold]home/end[/]  scroll to top/bottom
  [bold]t[/]        cycle theme (persisted)
  [bold]r[/]        refresh now
  [bold]s[/]        sessions tab: next sort column (persisted)
  [bold]S[/]        sessions tab: reverse sort direction (persisted)
  [bold]/[/]        sessions tab: filter by name/project/branch (empty clears)
  [bold]?[/]        this help
  [bold]q[/]        quit

[bold cyan]CLI[/]
  cstats            TUI
  cstats --line     one-line status (tmux/prompt)
  cstats --json     dashboard as JSON
  cstats --install  symlink into ~/.local/bin
  cstats --update   git pull + reinstall deps
  cstats --version

[bold cyan]Data sources[/]
  ~/.claude/projects/*/*.jsonl   sessions, tokens, cost
  OAuth /api/oauth/usage         live 5h/7d plan limits
  ~/.local/share/rtk/history.db  rtk shell-proxy savings
  ~/.claude/.caveman-history.jsonl  caveman savings

[bold cyan]Cache[/]
  ~/.cache/cstats/dashboard.json (instant startup)

[dim]costs are hypothetical API-equivalent prices, not a real bill[/]
"""


class HelpScreen(ModalScreen):
    """The key/CLI reference. Longer than a short terminal, so it scrolls.

    The scroll keys are bound on the screen and act on the container directly,
    rather than relying on it having focus — same reasoning as in the main
    window, where a click moving focus silently changed what the keys did.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
        Binding("pageup", "scroll_help('pageup')", "Page up"),
        Binding("pagedown", "scroll_help('pagedown')", "Page down"),
        Binding("up", "scroll_help('up')", "Scroll up", show=False),
        Binding("down", "scroll_help('down')", "Scroll down", show=False),
        Binding("home", "scroll_help('home')", "Top", show=False),
        Binding("end", "scroll_help('end')", "Bottom", show=False),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-box"):
            yield Static(HELP_TEXT)

    def action_scroll_help(self, where: str) -> None:
        box = self.query_one("#help-box", VerticalScroll)
        if where == "pageup":
            box.scroll_page_up()
        elif where == "pagedown":
            box.scroll_page_down()
        elif where == "up":
            box.scroll_up()
        elif where == "down":
            box.scroll_down()
        elif where == "home":
            box.scroll_home(animate=False)
        else:
            box.scroll_end(animate=False)

    def action_dismiss(self, result=None):
        self.dismiss()


class SettingsScreen(ModalScreen):
    """Live settings overlay: shows current values + how to change them."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("comma", "dismiss", "Close"),
    ]

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        text = (
            "[bold]Settings[/]\n\n"
            f"theme           [bold cyan]{self.cfg.get('theme')}[/]   ([bold]t[/] cycles {len(CYCLE)} themes)\n"
            f"refresh         [bold cyan]{self.cfg.get('refresh_ms', 60000) // 1000}s[/]  (auto)\n"
            f"compact hint    [bold cyan]${self.cfg.get('context_alert_usd', 0.10):.2f}/turn[/]  "
            "(warn above; 0 disables)\n"
            f"sessions sort   [bold cyan]{self.cfg.get('sessions_sort', 'date')} "
            f"{'desc' if self.cfg.get('sessions_desc', True) else 'asc'}[/]  "
            "([bold]s[/]/[bold]S[/] on the Sessions tab)\n"
            "\n[dim]persisted to ~/.config/cstats/config.json[/]\n"
            "[dim]esc / , to close[/]"
        )
        yield Static(text, id="settings-box")

    def action_dismiss(self, result=None):
        self.dismiss()


class FilterScreen(ModalScreen):
    """Ask for the Sessions-tab name filter. Dismisses with the new filter.

    Returns the entered string on `enter` (an empty string clears the filter)
    and None on `escape`, so the caller can tell "cleared" from "cancelled".

    `escape` is bound on the screen, not on the Input: a focused Input swallows
    plain keypresses — which is correct, typing "q" must type a q rather than
    quit the app — so without a screen-level binding the modal would have no
    way out.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: str = ""):
        super().__init__()
        self.current = current or ""

    def compose(self) -> ComposeResult:
        with Vertical(id="filter-box"):
            yield Static(
                "[bold]Filter sessions[/]\n\n"
                "[dim]case-insensitive substring of session name, project or branch[/]"
            )
            # prefill with the active filter so it can be edited, not retyped
            yield Input(value=self.current, placeholder="name or project…", id="filter-input")
            yield Static("[dim]enter applies · empty clears · esc cancels[/]")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)
