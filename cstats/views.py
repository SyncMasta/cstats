"""Render helpers for the TUI. Each function returns a rich renderable."""

import time
from datetime import datetime, timedelta, timezone

from rich.bar import Bar
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from .pricing import display_name, context_window
from . import aggregate, claude_parser, config, economics


def _local(iso, fmt="%Y-%m-%d %H:%M"):
    """Parse an ISO-8601 timestamp and format it in the local timezone."""
    if not iso:
        return ""
    try:
        s = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone().strftime(fmt)
    except (ValueError, AttributeError):
        return iso[:16].replace("T", " ")


def _parse_iso(iso):
    """ISO-8601 -> aware datetime or None."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _project_runout(pct, resets_at, window_hours, now=None):
    """Project when the limit hits 100% at the current linear pace.

    Returns (datetime|None, str) — the projected runout time if it falls
    inside the window, plus a human verdict.
    """
    if pct is None or pct <= 0:
        return None, ""
    if pct >= 100:
        return None, "already exhausted"
    now = now or datetime.now(timezone.utc)
    end = _parse_iso(resets_at)
    if end is None:
        return None, ""
    start = end - timedelta(hours=window_hours)
    elapsed_h = (now - start).total_seconds() / 3600
    remaining_h = (end - now).total_seconds() / 3600
    if elapsed_h <= 0 or remaining_h <= 0:
        return None, ""
    rate_per_h = pct / elapsed_h  # % per hour at current pace
    if rate_per_h <= 0:
        return None, ""
    hours_to_100 = (100 - pct) / rate_per_h
    if hours_to_100 >= remaining_h:
        return None, "limit holds at current pace"
    runout = now + timedelta(hours=hours_to_100)
    return runout, "exhausts before reset"


def fmt_cost(v):
    return f"${v:,.2f}"


def fmt_int(v):
    return f"{v:,}"


def fmt_short(n):
    """Compact token count: 86301 -> '86k', 1000000 -> '1.00M'."""
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_ago(seconds):
    """Human age: '12s', '4min', '3h', '2d'. Empty string for None."""
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    m = s // 60
    if m < 90:
        return f"{m}min"
    h = m // 60
    if h < 48:
        return f"{h}h"
    return f"{h // 24}d"


def _freshness(fetched_at, rate_limited=False, label="fetched", retry_in=None):
    """One dim line stating how old a data point is; red when it is stuck.

    `fetched_at` is an epoch timestamp (float) or None. `retry_in` is the
    remaining backoff in seconds — without it "rate-limited" reads as broken
    rather than as a wait with a known end.
    """
    if not fetched_at:
        return Text("age unknown", style="dim")
    age = int(time.time() - fetched_at)
    stamp = datetime.fromtimestamp(fetched_at).strftime("%H:%M:%S")
    txt = f"{label} {stamp} ({fmt_ago(age)} ago)"
    if rate_limited:
        wait = f", retrying in {fmt_ago(int(retry_in))}" if retry_in else ""
        return Text(txt + f"  · endpoint rate-limited{wait}, values may lag", style="red")
    return Text(txt, style="dim")


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values, width=40):
    """Render a list of numbers as a unicode sparkline."""
    if not values:
        return Text("", style="dim")
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    # downsample to width
    vals = list(values)
    if len(vals) > width:
        step = len(vals) / width
        vals = [vals[int(i * step)] for i in range(width)]
    out = Text()
    for v in vals:
        idx = int((v - lo) / span * (len(_SPARK_CHARS) - 1))
        out.append(_SPARK_CHARS[idx], style="green")
    return out


def bar(pct, width=20):
    """Return a colored progress bar string for a percentage (0-100)."""
    if pct is None:
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * width))
    f = "\u2588" * filled
    e = "\u2591" * (width - filled)
    color = "green" if pct < 70 else ("yellow" if pct < 90 else "red")
    return Text(f + e, style=color)


def fmt_until(seconds):
    """Time remaining, coarse: '2h 14min', '3d 4h', 'now'."""
    if seconds is None or seconds <= 0:
        return "now"
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}min"
    if s < 48 * 3600:
        h, m = divmod(s // 60, 60)
        return f"{h}h {m}min" if m else f"{h}h"
    d, h = divmod(s // 3600, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _limit_window(title, pct, resets_at):
    """One usage window as a small block: bar, percentage, when it resets.

    Keeping the reset time under its own bar is the point — stacked full-width
    rows put the two windows' numbers far apart and left the right half empty.
    """
    block = [
        Text(title, style="bold cyan"),
        bar(pct, width=18) + Text(f"  {pct:.1f}%" if pct is not None else "  n/a",
                                  style="bold"),
    ]
    end = _parse_iso(resets_at)
    if end is not None:
        left = (end - datetime.now(timezone.utc)).total_seconds()
        block.append(Text(f"resets {_local(resets_at, '%a %H:%M')} · in {fmt_until(left)}",
                          style="dim"))
    return Group(*block)


def kpi(label, value, sub=""):
    t = Text()
    t.append(f"{label}\n", style="bold cyan")
    t.append(str(value), style="bold white")
    if sub:
        t.append(f"\n{sub}", style="dim")
    return t


def fmt_cost_rate(usd_per_token):
    return f"${usd_per_token * 1_000_000:,.2f}"


def savings_offset(d: aggregate.Dashboard):
    """What the same work would have cost without rtk/caveman.

    The naive version priced rtk's saved tokens at the blended input rate and
    understated them by roughly the reuse factor below: a token that never
    enters the transcript is not paid for once. It is never written to the
    prompt cache, and — decisively — never re-read on any later turn of that
    session. cache_read / cache_write is exactly how often an average cached
    token gets read again, so it is the multiplier on the downstream cost.

    Rates come from what was actually billed per token class, not from a list
    price, so a mixed-model history prices itself correctly.
    """
    if not (d.rtk.available or d.caveman.available):
        return None
    r = economics.billing_rates(d)
    read_rate, write_rate, out_rate, reuse = r["read"], r["write"], r["output"], r["reuse"]
    ctx_rate = read_rate * reuse  # downstream cost of one token sitting in context

    # Neither tool's own saving may be priced as it stands (requirement 10):
    # rtk counts output that could never have reached the context, and caveman
    # counts every content-block line as another answer. Both corrections are
    # measured, not guessed — see rtk.BASH_OUTPUT_CEILING_TOKENS and
    # caveman_overcount() — and both are reported in the panel footnote.
    rtk_saved = d.rtk.billable_saved_tokens or 0
    cav_saved = (d.caveman.total_saved_tokens or 0) / caveman_overcount(d)
    # rtk keeps shell output out of the prompt: a cache write that never
    # happens, plus every re-read that would have followed it
    rtk_direct, rtk_reread = rtk_saved * write_rate, rtk_saved * ctx_rate
    # caveman prevents output tokens: billed as output when generated, and
    # they then join the prompt like any other text (written once, re-read)
    cav_direct, cav_reread = cav_saved * out_rate, cav_saved * (write_rate + ctx_rate)

    return {
        "rtk_direct": rtk_direct, "rtk_reread": rtk_reread,
        "rtk": rtk_direct + rtk_reread,
        "cav_direct": cav_direct, "cav_reread": cav_reread,
        "caveman": cav_direct + cav_reread,
        "total": rtk_direct + rtk_reread + cav_direct + cav_reread,
        "reuse": reuse, "read_rate": read_rate, "write_rate": write_rate,
        "out_rate": out_rate,
        "rtk_saved": rtk_saved, "cav_saved": cav_saved,
        "rtk_reported": d.rtk.total_saved_tokens or 0,
        "cav_reported": d.caveman.total_saved_tokens or 0,
        "cav_overcount": caveman_overcount(d),
        "rtk_capped_commands": getattr(d.rtk, "capped_commands", 0),
    }


def caveman_overcount(d):
    """How much caveman over-reports, measured against our own transcripts.

    caveman sums every assistant line without deduplicating `message.id`, so
    its output figure runs well above what was billed (2.15x here). Its saving
    is counted the same way, so dividing by the ratio we can actually measure
    brings it back to the same scale as everything else on this dashboard.

    Returns 1.0 when the comparison is not possible (no caveman data, or
    caveman reporting less than we measured), so the correction can only ever
    shrink a claim, never inflate one.
    """
    claimed = getattr(d.caveman, "total_output_tokens", 0) or 0
    ours = d.total_output or 0
    if claimed <= 0 or ours <= 0 or claimed <= ours:
        return 1.0
    return claimed / ours


TOOL_INFO = {
    "rtk": ("rtk (shell proxy)", "compresses shell output before it reaches the model",
            "~/.local/share/rtk/history.db"),
    "caveman": ("caveman (output compression)", "compresses the agent's own wording",
                "~/.claude/.caveman-history.jsonl"),
}


def unavailable_lines(tool, stats, verbose=False):
    """Why an optional integration has nothing to show, phrased as optional.

    Both tools are add-ons: the dashboard's own numbers (limits, tokens, cost,
    context) never depend on them. Saying only "not found" reads like a broken
    dashboard, so state whether it is absent or merely idle, and say plainly
    that nothing else is affected.
    """
    _, what, path = TOOL_INFO.get(tool, (tool, "", ""))
    status = getattr(stats, "status", "missing")
    missing = status == "missing"
    if status == "error":
        head, style = f"{tool} could not be read", "red"
    elif missing:
        head, style = f"{tool} not installed", "dim"
    else:
        head, style = f"{tool} has no data yet", "yellow"
    rows = [Text(head, style=style)]
    hint = getattr(stats, "hint", "")
    if hint:
        rows.append(Text(hint, style="dim"))
    if verbose:
        if what:
            rows.append(Text(f"what it does: {what}", style="dim"))
        rows.append(Text(f"expected at: {path}", style="dim"))
    rows.append(Text("optional — everything else on this dashboard works without it",
                     style="dim italic"))
    return rows


def _label_rows(rows):
    """Amount-first rows for the half-width side panels.

    Session names are long and the panels are narrow, so the label goes last:
    it is the only part that may be ellipsized, and the number always stays
    readable. `rows` is [(label, amount, note)].
    """
    grid = Table.grid(padding=(0, 1))
    # fixed widths on the numbers: only the label may be squeezed
    grid.add_column(justify="right", no_wrap=True, width=7, style="green")
    grid.add_column(no_wrap=True, width=5, style="dim")
    grid.add_column(overflow="ellipsis", no_wrap=True, ratio=1)
    for label, amount, note in rows:
        grid.add_row(amount, note, Text(label, style="dim"))
    return grid


def render_context_panel(d: aggregate.Dashboard, max_rows=8, alert_usd=None):
    """Context-window fill of the active sessions, as an aligned table.

    A plain concatenated line does not align: session names vary from 5 to 50
    characters, so a `{name:<30}` pad shifts every following column. A table
    gives each field its own column and truncates only the name.

    The `$/turn` column is the point of the panel: every turn re-reads the
    whole context, so that number is what the session costs to keep going.
    """
    # The default comes from the defaults table, not from the config file: a
    # view that reads disk is no longer a pure function of its dashboard, and
    # this one re-read ~/.config/cstats/config.json on every 60s render.
    # The app passes the user's configured value in.
    if alert_usd is None:
        alert_usd = config.DEFAULTS["context_alert_usd"]
    rates = economics.billing_rates(d)

    # expand + ratio on the two text columns, fixed widths on everything else:
    # a fixed-width Session column made rich drop the trailing columns instead
    # of shrinking, so $/turn silently vanished below ~120 columns
    tbl = Table(box=None, show_header=True, header_style="dim", padding=(0, 1), expand=True)
    tbl.add_column("Session", no_wrap=True, overflow="ellipsis", ratio=2, min_width=10)
    tbl.add_column("Context fill", no_wrap=True, width=12)
    tbl.add_column("", justify="right", no_wrap=True, width=6)   # percentage
    tbl.add_column("Tokens", justify="right", no_wrap=True, width=11)
    tbl.add_column("$/turn", justify="right", no_wrap=True, width=6)
    tbl.add_column("Model", no_wrap=True, width=9)
    tbl.add_column("Project", no_wrap=True, overflow="ellipsis", ratio=1, min_width=6, style="dim")
    tbl.add_column("Seen", justify="right", no_wrap=True, width=4)

    total = 0
    worst = None  # (per_turn, name, econ) of the session most worth compacting
    for ctx in (d.context or [])[:max_rows]:
        tokens = ctx.get("tokens") or 0
        total += tokens
        window = context_window(ctx.get("model"), tokens)
        pct = tokens / window * 100 if window else 0
        # the age of the usage block the tokens come from, not of the file: the
        # file's mtime moves when a session is merely resumed, which showed a
        # two-day-old fill as "Seen 0s" and made the hint below recommend
        # compacting a session that had not had a turn since
        age = ctx.get("age_s", 0)
        frozen = age >= 300
        name = claude_parser.display_label(
            ctx.get("name"), ctx.get("project"), ctx.get("branch"), ctx.get("session"))
        seen = Text(fmt_ago(age), style="dim red" if frozen else "dim")

        econ = economics.session_economics(tokens, rates)
        verdict = economics.advice(econ, alert_usd)
        cost_style = {"compact": "bold red", "watch": "yellow"}.get(verdict, "dim")
        if verdict == "compact" and not frozen and (worst is None or econ["per_turn"] > worst[0]):
            worst = (econ["per_turn"], name, econ)

        tbl.add_row(
            Text(name, style="bold cyan"),
            bar(pct, width=12),  # must match the column width or it clips
            Text(f"{pct:.1f}%", style="bold" if pct >= 80 else ""),
            Text(f"{fmt_short(tokens)}/{fmt_short(window)}", style="dim"),
            Text(f"${econ['per_turn']:.3f}", style=cost_style),
            Text(display_name(ctx.get("model") or ""), style="dim"),
            ctx.get("project") or "?",
            seen,
        )

    n = len(d.context or [])
    body = [tbl]
    if n:
        hidden = f"  (+{n - max_rows} more)" if n > max_rows else ""
        body.append(Text(f" {n} active · {fmt_short(total)} context tokens in flight{hidden}",
                         style="dim"))
    if worst:
        per_turn, name, econ = worst
        be = econ["breakeven_turns"]
        short = name if len(name) <= 28 else name[:27] + "…"
        body.append(Text(
            f" compact \"{short}\": ${per_turn:.3f} → ${econ['after']:.3f}/turn, "
            f"breaks even after ~{be:.0f} turns (${econ['compact_cost']:.2f} to compact)",
            style="bold yellow"))
    title = "Active sessions" if n > 1 else "Current session"
    return Panel(Group(*body), title=f"{title} ({n})", border_style="yellow")


def render_overview(d: aggregate.Dashboard, width=100, error=None, alert_usd=None):
    """Overview tab: KPIs + live limits + rtk/caveman summary."""
    items = []

    # error banner when the last refresh failed (stale data is still shown)
    if error:
        items.append(Panel(
            Text(f"Last refresh failed: {error}\nShowing cached/stale data. Check network and ~/.claude/.credentials.json.", style="bold red"),
            title="Warning", border_style="red",
        ))

    # Transcripts the build could not read. Every one of them makes the totals
    # below quietly short, so the shortfall is stated rather than absorbed.
    warnings = getattr(d, "warnings", None)
    if warnings:
        shown = warnings[:3]
        more = f"\n(+{len(warnings) - len(shown)} more)" if len(warnings) > len(shown) else ""
        items.append(Panel(
            Text(f"{len(warnings)} source(s) skipped — totals below are incomplete:\n"
                 + "\n".join(shown) + more, style="yellow"),
            title="Incomplete data", border_style="yellow",
        ))

    # Live usage limits — the two windows side by side. Stacked, each row used
    # a quarter of a wide terminal and the reset times sat far from the bar
    # they belong to.
    if d.limits.available:
        windows = Table.grid(expand=True, padding=(0, 2))
        windows.add_column(ratio=1)
        windows.add_column(ratio=1)
        windows.add_row(
            _limit_window("5h session window", d.limits.five_hour_pct,
                          d.limits.five_hour_resets_at),
            _limit_window("7d weekly window", d.limits.seven_day_pct,
                          d.limits.seven_day_resets_at),
        )
        foot = _freshness(d.limits.fetched_at, d.limits.rate_limited,
                      retry_in=getattr(d.limits, "retry_in", None))
        if d.limits.extra_usage_enabled:
            foot = Text("extra usage: enabled  ·  ", style="green") + foot
        items.append(Panel(Group(windows, Text(""), foot),
                           title="Live usage limits", border_style="cyan"))
    elif getattr(d.limits, "reason", None):
        # The headline numbers are missing — say why here rather than only on
        # the Limits tab, where the user would have to go looking for it.
        items.append(Panel(Text(f"Live usage limits unavailable — {d.limits.reason}.",
                                style="red"),
                           title="Live usage limits", border_style="red"))

    # current session context-window fill (all active sessions)
    if d.context:
        items.append(render_context_panel(d, alert_usd=alert_usd))

    # KPI grid
    grid = Table.grid(expand=True)
    grid.add_column(justify="left")
    grid.add_column(justify="left")
    grid.add_column(justify="left")
    grid.add_column(justify="left")
    grid.add_row(
        kpi("API-equiv. cost", fmt_cost(d.total_cost)),
        kpi("Sessions", fmt_int(d.total_sessions), f"{fmt_int(d.total_messages)} messages"),
        kpi("Input tokens", fmt_int(d.total_input)),
        kpi("Output tokens", fmt_int(d.total_output)),
    )
    grid.add_row(
        kpi("Cache read", fmt_int(d.total_cache_read)),
        kpi("Cache write", fmt_int(d.total_cache_write)),
        kpi("Cache hit ratio", f"{d.cache_ratio:.1f}%", "of input-side tokens"),
        kpi("Generated", d.generated_at.strftime("%H:%M:%S")),
    )
    items.append(Panel(grid, title="Totals (all sessions)", border_style="blue"))

    # rtk + caveman savings side by side
    savings = Table.grid(expand=True)
    savings.add_column(ratio=1)
    savings.add_column(ratio=1)
    rtk_txt = []
    if d.rtk.available:
        rtk_txt = [
            Text(f"commands: {fmt_int(d.rtk.total_commands)}", style="bold"),
            Text(f"saved tokens: {fmt_int(d.rtk.total_saved_tokens)}", style="green"),
            # rtk's own figure above, ours below: it prices output that could
            # never have reached the model, so the two differ by ~13x here
            Text(f"  of which could have reached the context: "
                 f"{fmt_int(d.rtk.billable_saved_tokens)}", style="dim"),
            Text(f"avg savings: {d.rtk.avg_savings_pct:.1f}%"),
            Text(f"input: {fmt_int(d.rtk.total_input_tokens)}  output: {fmt_int(d.rtk.total_output_tokens)}", style="dim"),
        ]
        today = getattr(d.rtk, "today_display", None) or []
        if today:
            rtk_txt.append(Text("today by session:", style="bold"))
            rtk_txt.append(_label_rows([(lbl, fmt_short(saved), f"×{fmt_int(cnt)}")
                                        for lbl, cnt, saved in today[:4]]))
        if d.rtk.last_record:
            local = d.rtk.last_record.astimezone()
            age = (datetime.now(timezone.utc) - d.rtk.last_record).total_seconds()
            rtk_txt.append(Text(f"last command: {local.strftime('%H:%M:%S')} ({fmt_ago(age)} ago)",
                                style="dim"))
    else:
        rtk_txt = unavailable_lines("rtk", d.rtk)
    cm_txt = []
    if d.caveman.available:
        cm_txt = [
            Text(f"sessions: {fmt_int(d.caveman.sessions)}", style="bold"),
            Text(f"saved output tokens: {fmt_int(d.caveman.total_saved_tokens)}", style="green"),
            Text(f"output tokens: {fmt_int(d.caveman.total_output_tokens)}  "
                 f"(vs {fmt_int(d.total_output)} billed here)", style="dim"),
            Text(f"turns: {fmt_int(d.caveman.total_turns)}", style="dim"),
        ]
        by_label = getattr(d.caveman, "by_session_label", None) or {}
        if by_label:
            cm_txt.append(Text("by session:", style="bold"))
            cm_txt.append(_label_rows([
                (label, fmt_short(saved), f"×{s}" if s > 1 else "")
                for label, (s, saved) in sorted(by_label.items(), key=lambda kv: -kv[1][1])[:4]
            ]))
        if d.caveman.latest_session:
            cm_txt.append(Text(f"mode: {d.caveman.latest_session.get('mode','')}", style="dim"))
            # caveman only writes a snapshot when its stats hook runs, so this
            # can legitimately lag far behind the other panels
            ts = d.caveman.latest_session.get("ts")
            if ts:
                cm_txt.append(_freshness(float(ts) / 1000, label="last snapshot"))
    else:
        cm_txt = unavailable_lines("caveman", d.caveman)

    savings.add_row(
        Panel(Group(*rtk_txt), title="rtk (shell proxy)",
              border_style="green" if d.rtk.available else "dim"),
        Panel(Group(*cm_txt), title="caveman (output compression)",
              border_style="yellow" if d.caveman.available else "dim"),
    )
    items.append(savings)

    # savings offset: what would the same usage have cost without rtk/caveman?
    est = savings_offset(d)
    if est and est["total"] > 0:
        eff = d.total_cost + est["total"]
        pct = est["total"] / eff * 100 if eff else 0
        # only show a column per tool that is actually installed — a $0.00
        # column for a missing tool looks like a broken calculation
        cells = []
        if d.rtk.available:
            cells.append(kpi("rtk offset", fmt_cost(est["rtk"]),
                             f"{fmt_cost(est['rtk_direct'])} entering + "
                             f"{fmt_cost(est['rtk_reread'])} re-reads"))
        if d.caveman.available:
            cells.append(kpi("caveman offset", fmt_cost(est["caveman"]),
                             f"{fmt_cost(est['cav_direct'])} generating + "
                             f"{fmt_cost(est['cav_reread'])} in context"))
        cells.append(kpi("total offset", fmt_cost(est["total"]),
                         f"~{pct:.1f}% on top without tools"))
        off = Table.grid(expand=True)
        for _ in cells:
            off.add_column(ratio=1)
        off.add_row(*cells)
        note = Text(
            f"A saved token is not billed once. It never enters the prompt cache "
            f"({fmt_cost_rate(est['write_rate'])}/MTok) and is never re-read on the later turns "
            f"of its session — {est['reuse']:.0f}× on average here "
            f"({fmt_short(d.total_cache_read)} reads / {fmt_short(d.total_cache_write)} writes "
            f"at {fmt_cost_rate(est['read_rate'])}/MTok).",
            style="dim",
        )
        body = [off, Text(""), note]

        # Say out loud that neither tool's own figure was used as given.
        corrections = []
        if d.rtk.available and est["rtk_reported"] > est["rtk_saved"]:
            corrections.append(
                f"rtk reports {fmt_short(est['rtk_reported'])} saved tokens; "
                f"{fmt_short(est['rtk_saved'])} is counted. Its figure prices the full "
                f"untruncated command output, but Claude Code caps tool output — the largest "
                f"Bash result in this history is ~15k tokens. "
                f"{est['rtk_capped_commands']} command(s) hit that cap.")
        if d.caveman.available and est["cav_overcount"] > 1.0:
            corrections.append(
                f"caveman reports {fmt_short(est['cav_reported'])} saved tokens; "
                f"{fmt_short(est['cav_saved'])} is counted. It sums every content-block line "
                f"without deduplicating message.id, which puts its output "
                f"{est['cav_overcount']:.2f}× above what these transcripts were billed for.")
        for line in corrections:
            body.append(Text(""))
            body.append(Text(line, style="dim italic"))
        # Why a tool contributes zero, in its own words. Deriving this from
        # `available` alone labelled an installed-but-idle tool "not installed
        # here", contradicting the panel above that got it right.
        zero = {"missing": [], "empty": [], "error": []}
        for name, st in (("rtk", d.rtk), ("caveman", d.caveman)):
            if not st.available:
                zero.get(getattr(st, "status", "missing"), zero["missing"]).append(name)
        notes = []
        if zero["missing"]:
            notes.append(f"{' and '.join(zero['missing'])} not installed here")
        if zero["empty"]:
            notes.append(f"{' and '.join(zero['empty'])} installed but has recorded nothing yet")
        if zero["error"]:
            notes.append(f"{' and '.join(zero['error'])} could not be read")
        if notes:
            body.append(Text("Counts as zero: " + "; ".join(notes) + ".",
                             style="dim italic"))
        items.append(Panel(Group(*body),
                           title="Savings offset (estimated)", border_style="cyan"))

    # Top models. The share bar carries the comparison the numbers only imply,
    # and it is what makes the panel use the width it already occupied.
    if d.model_totals:
        mt = Table(box=box.SIMPLE_HEAVY, expand=True)
        mt.add_column("Model", overflow="ellipsis", no_wrap=True, ratio=1, min_width=9)
        mt.add_column("Cost", justify="right", no_wrap=True, width=10)
        mt.add_column("", ratio=2, min_width=6)
        mt.add_column("Share", justify="right", no_wrap=True, width=6)
        mt.add_column("In", justify="right", no_wrap=True, width=8)
        mt.add_column("Out", justify="right", no_wrap=True, width=9)
        total = sum(v[0] for v in d.by_model.values()) or 1.0
        top = d.model_totals[0][1][0] if d.model_totals else 1.0
        for name, (cost, i, o) in d.model_totals[:8]:
            share = cost / total * 100 if total else 0
            mt.add_row(name, fmt_cost(cost),
                       Bar(size=top or 1.0, begin=0, end=cost, color="magenta"),
                       f"{share:.1f}%", fmt_short(i), fmt_short(o))
        items.append(Panel(mt, title="Usage by model", border_style="magenta"))

    return Group(*items)


def render_limits(d: aggregate.Dashboard, width=100, history=None):
    """Limits tab: detailed 5h/7d info + history sparkline + projection."""
    items = []
    if not d.limits.available:
        # Name the actual reason. "no OAuth token or endpoint error" covered
        # two problems with different fixes and told you neither.
        why = getattr(d.limits, "reason", None) or "no OAuth token or endpoint error"
        items.append(Panel(Text(f"Live limits unavailable — {why}.", style="red"),
                           title="Usage limits"))
        return Group(*items)

    # Same two blocks as the overview, deliberately: a four-column table of the
    # same two rows sat in the left third of the tab and repeated what the Pace
    # panel below already says in words.
    windows = Table.grid(expand=True, padding=(0, 2))
    windows.add_column(ratio=1)
    windows.add_column(ratio=1)
    windows.add_row(
        _limit_window("5h session window", d.limits.five_hour_pct,
                      d.limits.five_hour_resets_at),
        _limit_window("7d weekly window", d.limits.seven_day_pct,
                      d.limits.seven_day_resets_at),
    )
    foot = _freshness(d.limits.fetched_at, d.limits.rate_limited,
                      retry_in=getattr(d.limits, "retry_in", None))
    if d.limits.extra_usage_enabled:
        foot = Text("extra usage: enabled  \u00b7  ", style="green") + foot
    items.append(Panel(Group(windows, Text(""), foot),
                       title="Plan limits (live, from OAuth endpoint)",
                       border_style="cyan"))

    # utilization history sparklines (last 30 days, recorded per refresh)
    if history:
        fh_pts = [p["fh"] for p in history if p.get("fh") is not None]
        sd_pts = [p["sd"] for p in history if p.get("sd") is not None]
        if len(fh_pts) >= 2 or len(sd_pts) >= 2:
            rows = []
            if len(fh_pts) >= 2:
                rows.append(Text("5h  ", style="bold") + sparkline(fh_pts[-60:]) +
                            Text(f"  now {fh_pts[-1]:.0f}%  max {max(fh_pts):.0f}%", style="dim"))
            if len(sd_pts) >= 2:
                rows.append(Text("7d  ", style="bold") + sparkline(sd_pts[-60:]) +
                            Text(f"  now {sd_pts[-1]:.0f}%  max {max(sd_pts):.0f}%", style="dim"))
            items.append(Panel(Group(*rows), title="Utilization history (30d)", border_style="blue"))

    # Pace and projection in one panel, one row per window: they answer the same
    # question ("am I going to make it to the reset?") and each used to be its
    # own two-line panel, so half the tab was borders.
    pace = Table(box=None, show_header=True, header_style="dim", padding=(0, 2), expand=True)
    pace.add_column("Window", no_wrap=True, width=7)
    pace.add_column("Used", justify="right", no_wrap=True, width=6)
    pace.add_column("Elapsed", justify="right", no_wrap=True, width=8)
    pace.add_column("Verdict", no_wrap=True, ratio=1, min_width=12)
    pace.add_column("Projection", no_wrap=True, overflow="ellipsis", ratio=1, min_width=14)
    now = datetime.now(timezone.utc)
    rows_added = 0
    for label, pct, resets, hours in (
        ("5 hours", d.limits.five_hour_pct, d.limits.five_hour_resets_at, 5),
        ("7 days", d.limits.seven_day_pct, d.limits.seven_day_resets_at, 7 * 24),
    ):
        if pct is None or not resets:
            continue
        end_dt = _parse_iso(resets)
        if end_dt is None:
            continue
        start_dt = end_dt - timedelta(hours=hours)
        total = (end_dt - start_dt).total_seconds()
        elapsed = (now - start_dt).total_seconds()
        if total <= 0 or elapsed < 0:
            continue
        elapsed_pct = min(100.0, elapsed / total * 100)
        delta = pct - elapsed_pct
        if delta > 10:
            verdict, style = "over budget pace", "red"
        elif delta > 0:
            verdict, style = "slightly ahead", "yellow"
        else:
            verdict, style = "on track", "green"
        runout, note = _project_runout(pct, resets, hours, now)
        if runout:
            proj = Text(f"100% ~{runout.astimezone().strftime('%a %H:%M')}", style="bold red")
        elif note:
            proj = Text(note, style="green")
        else:
            proj = Text("\u2014", style="dim")
        pace.add_row(label, f"{pct:.0f}%", f"{elapsed_pct:.0f}%",
                     Text(verdict, style=style), proj)
        rows_added += 1
    if rows_added:
        body = [pace]
        if d.credits_7d:
            body.append(Text(
                f"Local estimate from the transcripts, last 7 days: "
                f"{fmt_int(round(d.credits_7d))} credits \u2014 the basis for the "
                f"percentages above. Exact values come from Anthropic.", style="dim"))
        items.append(Panel(Group(*body), title="Pace against the reset",
                           border_style="blue"))


    return Group(*items)


def render_tokens(d: aggregate.Dashboard, width=100):
    """Tokens tab: daily cost and token trends.

    One table, not three panels. Table, bar chart and sparkline showed the same
    thirty numbers three times over; folding the bar into the table as its own
    flexing column says it once, and the spare width goes to the bars instead
    of to padding.
    """
    items = []
    days = sorted(d.by_day_cost.keys())[-30:]
    if not days:
        items.append(Panel(Text("No data", style="dim"), title="Tokens"))
        return Group(*items)

    costs = [d.by_day_cost.get(day, 0) for day in days]
    maxc = max(costs) or 1.0

    tbl = Table(box=box.SIMPLE_HEAVY, expand=True)
    tbl.add_column("Date", no_wrap=True, width=10)
    tbl.add_column("Cost", justify="right", no_wrap=True, width=10)
    tbl.add_column("", ratio=1, min_width=6)  # the bar needs no header
    tbl.add_column("In", justify="right", no_wrap=True, width=11)
    tbl.add_column("Out", justify="right", no_wrap=True, width=12)
    for day, cost in zip(days, costs):
        i, o = d.by_day_tokens.get(day, (0, 0))
        # rich's Bar renders to the column's actual width, so the chart grows
        # with the terminal instead of stopping at a hardcoded character count
        tbl.add_row(day, fmt_cost(cost), Bar(size=maxc, begin=0, end=cost, color="green"),
                    fmt_int(i), fmt_int(o))

    trend = Table.grid(expand=True)
    trend.add_column(style="dim", no_wrap=True)
    trend.add_column(ratio=1)
    trend.add_row("30d trend  ", sparkline(costs))
    trend.add_row("", Text(f"min {fmt_cost(min(costs))}  \u00b7  max {fmt_cost(max(costs))}"
                           f"  \u00b7  last {fmt_cost(costs[-1])}", style="dim"))
    items.append(Panel(Group(tbl, trend), title="Daily cost & tokens (last 30 days)",
                       border_style="blue"))
    return Group(*items)


def render_rtk(d: aggregate.Dashboard, width=100):
    """rtk tab: savings analytics from rtk's SQLite DB."""
    items = []
    if not d.rtk.available:
        items.append(Panel(Group(*unavailable_lines("rtk", d.rtk, verbose=True)),
                           title="rtk savings", border_style="dim"))
        return Group(*items)

    k = Table.grid(expand=True)
    k.add_column(justify="left")
    k.add_column(justify="left")
    k.add_column(justify="left")
    k.add_column(justify="left")
    k.add_row(
        kpi("Commands", fmt_int(d.rtk.total_commands)),
        kpi("Saved tokens", fmt_int(d.rtk.total_saved_tokens), f"{d.rtk.avg_savings_pct:.1f}% avg"),
        kpi("Input tokens", fmt_int(d.rtk.total_input_tokens)),
        kpi("Output tokens", fmt_int(d.rtk.total_output_tokens)),
    )
    items.append(Panel(k, title="rtk totals (90-day retention)", border_style="green"))

    days = sorted(d.rtk.by_day.keys())[-30:]
    if days:
        tbl = Table(box=box.SIMPLE_HEAVY, expand=True)
        tbl.add_column("Date", no_wrap=True, width=10)
        tbl.add_column("Commands", justify="right", no_wrap=True, width=9)
        tbl.add_column("Saved", justify="right", no_wrap=True, width=10)
        tbl.add_column("", ratio=1, min_width=6)
        tbl.add_column("Savings %", justify="right", no_wrap=True, width=9)
        top = max(d.rtk.by_day[day][1] for day in days) or 1
        for day in days:
            cnt, saved, pct = d.rtk.by_day[day]
            tbl.add_row(day, fmt_int(cnt), fmt_int(saved),
                        Bar(size=top, begin=0, end=saved, color="cyan"), f"{pct:.1f}")
        items.append(Panel(tbl, title="Daily savings (last 30 days)", border_style="cyan"))

    if d.rtk.top_projects:
        tbl = Table(box=box.SIMPLE_HEAVY, expand=True)
        tbl.add_column("Project", overflow="ellipsis", no_wrap=True, ratio=1, min_width=10)
        tbl.add_column("Commands", justify="right", no_wrap=True, width=9)
        tbl.add_column("Saved", justify="right", no_wrap=True, width=11)
        tbl.add_column("", ratio=2, min_width=6)
        top = max((s for _, _, s in d.rtk.top_projects[:12]), default=1) or 1
        for proj, cnt, saved in d.rtk.top_projects[:12]:
            tbl.add_row(proj, fmt_int(cnt), fmt_int(saved),
                        Bar(size=top, begin=0, end=saved, color="magenta"))
        items.append(Panel(tbl, title="By project", border_style="magenta"))

    return Group(*items)


def render_caveman(d: aggregate.Dashboard, width=100):
    """caveman tab: output-compression savings."""
    items = []
    if not d.caveman.available:
        items.append(Panel(Group(*unavailable_lines("caveman", d.caveman, verbose=True)),
                           title="caveman savings", border_style="dim"))
        return Group(*items)

    k = Table.grid(expand=True)
    k.add_column(justify="left")
    k.add_column(justify="left")
    k.add_column(justify="left")
    k.add_column(justify="left")
    k.add_row(
        kpi("Sessions", fmt_int(d.caveman.sessions)),
        kpi("Saved output tokens", fmt_int(d.caveman.total_saved_tokens)),
        kpi("Output tokens", fmt_int(d.caveman.total_output_tokens)),
        kpi("Turns", fmt_int(d.caveman.total_turns)),
    )
    items.append(Panel(k, title="caveman lifetime savings", border_style="yellow"))

    days = sorted(d.caveman.by_day.keys())[-30:]
    if days:
        tbl = Table(box=box.SIMPLE_HEAVY, expand=True)
        tbl.add_column("Written", no_wrap=True, width=10)
        tbl.add_column("Sessions", justify="right", no_wrap=True, width=8)
        tbl.add_column("Saved", justify="right", no_wrap=True, width=10)
        tbl.add_column("Output", justify="right", no_wrap=True, width=10)
        tbl.add_column("", ratio=1, min_width=6)
        top = max(d.caveman.by_day[day][1] for day in days) or 1
        for day in days:
            s, sv, oo = d.caveman.by_day[day]
            tbl.add_row(day, fmt_int(s), fmt_int(sv), fmt_int(oo),
                        Bar(size=top, begin=0, end=sv, color="yellow"))
        # NOT a daily-savings chart, however much it looks like one. The history
        # carries only the timestamp of the snapshot, never the span it covers,
        # so a month of work lands on the day /caveman-stats happened to run —
        # here 33M output tokens on a single date. Dating it by consumption, as
        # the cost tables do, is impossible from this source.
        items.append(Panel(Group(tbl, Text(
            "Dated by when the snapshot was written, not by when the tokens were "
            "spent — the plugin's history records no time span. A row is one "
            "snapshot run, not one day of work.", style="dim")),
            title="Snapshots (last 30 written)", border_style="cyan"))

    rows = _caveman_session_rows(d)
    if rows:
        tbl = Table(box=box.SIMPLE_HEAVY, expand=True)
        # two flexing text columns, narrow fixed widths elsewhere — the sum of
        # fixed widths must stay under an 80-column terminal or rich drops the
        # trailing columns instead of shrinking them
        tbl.add_column("Session", overflow="ellipsis", no_wrap=True, ratio=2, min_width=10)
        tbl.add_column("Mode", no_wrap=True, width=5)
        tbl.add_column("Model", overflow="ellipsis", no_wrap=True, ratio=1, min_width=7)
        tbl.add_column("Turns", justify="right", no_wrap=True, width=6)
        tbl.add_column("Output", justify="right", no_wrap=True, width=8)
        tbl.add_column("Saved", justify="right", no_wrap=True, width=8)
        tbl.add_column("Cut", justify="right", no_wrap=True, width=4)
        tbl.add_column("Snapshot", justify="right", no_wrap=True, width=8)
        for r in rows:
            tbl.add_row(
                Text(r["label"], style="bold cyan" if r["newest"] else ""),
                r["mode"] or "?",
                r["model"],
                fmt_int(r["turns"]) if r["turns"] else "?",
                fmt_short(r["output"]),
                Text(fmt_short(r["saved"]), style="green"),
                f"{r['cut_pct']:.0f}%" if r["cut_pct"] is not None else "?",
                Text(r["age"], style="bold" if r["newest"] else "dim"),
            )
        items.append(Panel(Group(tbl, Text(
            "\"Cut\" is the plugin's own estimate: saved / (output + saved), i.e. how much "
            "shorter the answers got. A snapshot is only written when caveman-stats runs, "
            "so the ages differ per session. Turns and Output are the plugin's own counts, "
            "which sum every assistant line without deduplicating message.id — measured "
            "~2.6x above the tokens actually billed. The Model column is ours, taken from "
            "where each session's output really went; * marks a session that ran on more "
            "than one model.", style="dim")),
            title=f"Per session ({len(rows)})", border_style="magenta"))

    return Group(*items)


def _caveman_session_rows(d: aggregate.Dashboard):
    """Per-session caveman rows, newest snapshot first among equals.

    Replaces a raw dump of the newest snapshot's JSON keys, which showed one
    arbitrary session (whichever stopped last — usually not the one being
    looked at) with unformatted values and a meaningless UUID.
    """
    by_session = d.caveman.by_session or {}
    if not by_session:
        return []
    newest_ts = max((info.get("ts") or 0) for info in by_session.values())
    out = []
    for sid, info in by_session.items():
        saved = info.get("saved") or 0
        output = info.get("output") or 0
        base = output + saved
        ts = info.get("ts") or 0
        out.append({
            "label": d.session_name.get(sid) or d.session_project.get(sid) or sid[:8],
            "mode": info.get("mode"),
            # our own per-call attribution first: caveman's history records the
            # model of the *first* assistant line, which labelled a month-long
            # session "Fable 5" on the strength of its first day (4% of output)
            "model": d.session_model.get(sid) or display_name(info.get("model") or ""),
            "turns": info.get("turns") or 0,
            "output": output,
            "saved": saved,
            "cut_pct": (saved / base * 100) if base else None,
            "age": fmt_ago(time.time() - ts / 1000) if ts else "?",
            "newest": ts == newest_ts,
        })
    out.sort(key=lambda r: -r["saved"])
    return out[:20]


def render_projects(d: aggregate.Dashboard, width=100):
    """Projects tab: cost by project."""
    items = []
    if not d.project_list:
        items.append(Panel(Text("No sessions", style="dim"), title="Projects"))
        return Group(*items)
    tbl = Table(box=box.SIMPLE_HEAVY, expand=True)
    tbl.add_column("Project", overflow="ellipsis", no_wrap=True, ratio=1, min_width=10)
    tbl.add_column("Cost", justify="right", no_wrap=True, width=11)
    tbl.add_column("", ratio=2, min_width=6)
    tbl.add_column("Share", justify="right", no_wrap=True, width=7)
    total = d.total_cost or 1.0
    top = d.project_list[0][1] if d.project_list else 1.0
    for name, cost in d.project_list[:20]:
        tbl.add_row(name, fmt_cost(cost),
                    Bar(size=top or 1.0, begin=0, end=cost, color="blue"),
                    f"{cost / total * 100:.1f}%")
    items.append(Panel(tbl, title="Cost by project", border_style="blue"))
    return Group(*items)


_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def render_activity(d: aggregate.Dashboard, width=100):
    """Activity tab: weekday x hour heatmap of user messages."""
    items = []
    if not d.heatmap:
        items.append(Panel(Text("No activity data", style="dim"), title="Activity"))
        return Group(*items)

    max_val = max(d.heatmap.values()) or 1

    # build as plain text grid — a rich Table wraps/truncates at panel width
    shades = [" ", "░", "▒", "▓", "█"]
    grid_rows = []
    # hour header: mark every 3rd hour
    header = Text("     ")
    for h in range(24):
        if h % 3 == 0:
            header.append(f"{h:02d} ", style="bold")
        else:
            header.append("   ")
    grid_rows.append(header)
    for wd in range(7):
        row = Text(f"{_WEEKDAYS[wd]}  ")
        for h in range(24):
            v = d.heatmap.get(f"{wd},{h}", 0)
            if v == 0:
                row.append("·  ", style="dim")
            else:
                level = min(4, int(v / max_val * 4) + 1)
                style = {1: "blue", 2: "cyan", 3: "green", 4: "bold green"}[level]
                row.append(shades[level] + "  ", style=style)
        grid_rows.append(row)

    items.append(Panel(Group(*grid_rows), title="Messages by weekday x hour (local time)", border_style="blue"))

    # top hours summary
    hour_totals = {}
    wd_totals = {}
    for key, v in d.heatmap.items():
        wd, h = key.split(",")
        hour_totals[int(h)] = hour_totals.get(int(h), 0) + v
        wd_totals[int(wd)] = wd_totals.get(int(wd), 0) + v
    if hour_totals:
        peak_h = max(hour_totals, key=hour_totals.get)
        peak_d = max(wd_totals, key=wd_totals.get)
        items.append(Panel(
            Text(f"Peak hour: {peak_h:02d}:00  ({fmt_int(hour_totals[peak_h])} msgs)   "
                 f"Peak day: {_WEEKDAYS[peak_d]}  ({fmt_int(wd_totals[peak_d])} msgs)"),
            title="Peaks", border_style="green"))
    return Group(*items)


SESSION_SORT_KEYS = ("date", "cost", "output", "messages")
"""Sort keys of the Sessions tab, in the order the `s` key cycles them.

Deliberately the literal row-dict keys, so sorting needs no key translation
table that could drift away from the row shape.
"""

# the cap `aggregate` puts on session_rows; sorting can only ever rank within
# those rows, and the tab says so instead of hiding it
_SESSION_ROW_CAP = 200


def _sorted_session_rows(rows, sort="date", desc=True, name_filter=""):
    """Filter and sort session rows for the Sessions tab.

    Pure helper, so the tab's sort/filter behaviour is testable without a TUI.

    `sort` is one of SESSION_SORT_KEYS, i.e. a key of the row dicts. An unknown
    key falls back to "date" instead of raising: the value is persisted in the
    user config, and a stale or hand-edited entry must not break the tab.

    `name_filter` is a case-insensitive substring, matched against the session
    name, the project and the branch — everything the label can show, so what is
    visible is also findable. The fields are tested separately, never
    concatenated: joining them lets a needle straddle the boundary and match a
    row where no single field contains it (name "Fo" + project "obar" would match
    "oob"). An empty filter keeps every row.

    Ascending order is the exact reverse of descending order (the list is sorted
    once and reversed), so rows that tie on the sort key keep a predictable
    position instead of flipping with the sort direction.
    """
    key = sort if sort in SESSION_SORT_KEYS else "date"
    needle = (name_filter or "").strip().lower()
    out = [r for r in rows
           if not needle
           or needle in (r.get("name") or "").lower()
           or needle in (r.get("project") or "").lower()
           or needle in (r.get("branch") or "").lower()]
    out.sort(key=lambda r: r[key])
    if desc:
        out.reverse()
    return out


def render_sessions(d: aggregate.Dashboard, width=100, sort="date", desc=True,
                    name_filter="", limit=60):
    """Sessions tab: recent sessions with cost and model.

    Sort key, direction and filter are passed in by the app (which owns that
    state); the defaults reproduce the plain "newest first" listing. Stays a
    pure function of its arguments — no config reads, no state.

    The panel title spells out the current sort and filter; the keys that change
    them sit in the subtitle, because those bindings are hidden from the footer
    (they only work on this one tab).
    """
    items = []
    if not d.session_rows:
        items.append(Panel(Text("No sessions", style="dim"), title="Sessions"))
        return Group(*items)

    key = sort if sort in SESSION_SORT_KEYS else "date"
    rows = _sorted_session_rows(d.session_rows, key, desc, name_filter)
    shown = rows[:limit]

    title = f"Recent sessions ({len(shown)} of {len(rows)}) · sorted by {key} {'↓' if desc else '↑'}"
    if name_filter:
        title += f' · filter "{name_filter}"'
    # the key hint goes in the subtitle, not the title: a title carrying state
    # *and* hint outgrows an 80-column terminal as soon as a filter is set, and
    # rich then cuts the tail mid-word instead of shrinking it
    hint = "s/S sort · / filter"

    if not rows:
        items.append(Panel(
            Text(f'No session matches "{name_filter}" — press / to change the filter',
                 style="dim"),
            title=title, subtitle=hint, border_style="blue"))
        return Group(*items)

    tbl = Table(box=box.SIMPLE_HEAVY)
    tbl.add_column("Started", no_wrap=True)
    tbl.add_column("Name / Project", overflow="ellipsis")
    tbl.add_column("Model")
    tbl.add_column("Cost", justify="right")
    tbl.add_column("Output", justify="right")
    tbl.add_column("Msgs", justify="right")
    for r in shown:
        label = claude_parser.display_label(
            r.get("name"), r.get("project"), r.get("branch"), r.get("session"))
        tbl.add_row(r["date"], label, r["model"], fmt_cost(r["cost"]),
                    fmt_int(r["output"]), fmt_int(r["messages"]))

    body = [tbl]
    if len(d.session_rows) >= _SESSION_ROW_CAP:
        body.append(Text(
            f"Only the {_SESSION_ROW_CAP} most recent sessions are loaded, so this ranks "
            f"within those — e.g. the most expensive of the {_SESSION_ROW_CAP} newest, "
            "not of all time.", style="dim"))
    items.append(Panel(Group(*body), title=title, subtitle=hint, border_style="blue"))
    return Group(*items)


# ---------------------------------------------------------------------------
# Economics tab: what compacting actually did, and where the running sessions
# are heading. Everything here comes from measurements in the transcripts, not
# from the model in economics.py — that model is what these numbers calibrate.
# ---------------------------------------------------------------------------

def _compact_label(d, ev):
    """Session name for a compaction, falling back the same way as elsewhere."""
    sid = getattr(ev, "session_id", None) or ""
    return (d.session_name.get(sid)
            or d.slug_name.get(getattr(ev, "slug", "") or "")
            or d.session_project.get(sid)
            or getattr(ev, "project", None)
            or (sid[:8] if sid else "?"))


def render_compacts(d: aggregate.Dashboard):
    """Compaction history: how often, how deep, and whether it paid off.

    The dashboard's break-even advice rests on one number — how large a context
    is *after* a compaction. Claude Code reports that number itself, and it is
    wrong for billing purposes by a factor of ~3.7: it counts the preserved
    conversation only, while the system prompt, CLAUDE.md, tool definitions and
    MCP schemas are billed too. This panel shows the measured value and says so.
    """
    c = getattr(d, "compacts", None)
    if not c or not c.events:
        return None
    rates = economics.billing_rates(d)

    k = Table.grid(expand=True)
    for _ in range(4):
        k.add_column(justify="left")
    auto_pct = (c.auto / c.total * 100) if c.total else 0
    k.add_row(
        kpi("Compactions", fmt_int(c.total)),
        kpi("Automatic", f"{fmt_int(c.auto)}", f"{auto_pct:.0f}% of all"),
        kpi("Manual", fmt_int(c.manual)),
        kpi("Median duration", f"{c.duration_median_s:.0f}s" if c.duration_median_s else "?"),
    )

    lines = [k, Text("")]
    if c.auto:
        lines.append(Text(
            f"{c.auto} of {c.total} compactions were automatic — the window ran full "
            f"before you acted.", style="bold yellow"))
    fills = []
    if c.auto_fill_ratio:
        fills.append(f"automatic {c.auto_fill_ratio * 100:.1f}%")
    if c.manual_fill_ratio:
        fills.append(f"manual {c.manual_fill_ratio * 100:.1f}%")
    if len(fills) == 2:
        gap = (c.auto_fill_ratio - c.manual_fill_ratio) * 100
        lines.append(Text(
            f"Window fill when compacting: {', '.join(fills)} — your own compactions come "
            f"{gap:.1f} points earlier than the machine's.", style="dim"))

    if c.ctx_after_median and c.post_median:
        lines.append(Text(
            f"Context after a compaction measured at {fmt_short(c.ctx_after_median)} tokens "
            f"(median of {c.total}, {fmt_short(c.ctx_after_min)}–{fmt_short(c.ctx_after_max)}). "
            f"Claude Code's own figure ({fmt_short(c.post_median)}) counts only the preserved "
            f"conversation — system prompt, CLAUDE.md and tool definitions are billed too. "
            f"Every break-even number on this dashboard uses the measured value.",
            style="dim"))

    items = [Panel(Group(*lines), title="Compaction history (measured)",
                   border_style="cyan")]

    # per compaction, newest first
    tbl = Table(box=box.SIMPLE_HEAVY, expand=True)
    tbl.add_column("When", no_wrap=True, width=11)
    tbl.add_column("Trigger", no_wrap=True, width=7)
    tbl.add_column("Session", overflow="ellipsis", no_wrap=True, ratio=2, min_width=10)
    tbl.add_column("Context", justify="right", no_wrap=True, width=13)
    tbl.add_column("$/turn", justify="right", no_wrap=True, width=8)
    tbl.add_column("B/E", justify="right", no_wrap=True, width=5)
    tbl.add_column("After", justify="right", no_wrap=True, width=6)
    shown = sorted(c.events, key=lambda e: e.ts or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)[:30]
    for ev in shown:
        led = economics.compact_ledger(ev, rates)
        before = ev.ctx_before or ev.pre_tokens or 0
        after = ev.ctx_after or 0
        be = led["breakeven_turns"]
        tbl.add_row(
            ev.ts.astimezone().strftime("%m-%d %H:%M") if ev.ts else "?",
            Text(ev.trigger or "?", style="red" if ev.trigger == "auto" else "dim"),
            Text(_compact_label(d, ev), style="cyan"),
            f"{fmt_short(before)}→{fmt_short(after)}" if before else "?",
            Text(f"${led['saved_per_turn']:.3f}", style="green"),
            f"{be:.1f}" if be else "?",
            fmt_int(led["turns_after"]),
        )
    note = [tbl, Text(
        '"$/turn" is what the dropped tokens would cost to re-read on every later turn; '
        '"B/E" is how many turns that takes to repay the compaction; "After" is how many '
        "turns actually ran on the smaller context.", style="dim")]
    if c.turns_total:
        note.append(Text(
            f"Across all {c.total} compactions the context stayed small for "
            f"{fmt_int(c.turns_total)} turns. Pricing those turns at the pre-compact size "
            f"is an upper bound on avoided cost, not a saving — with a full window most of "
            f"them could not have run at all.", style="dim italic"))
    hidden = f"  (+{c.total - len(shown)} older)" if c.total > len(shown) else ""
    items.append(Panel(Group(*note), title=f"Per compaction ({c.total}){hidden}",
                       border_style="magenta"))
    return Group(*items)


def render_pace(d: aggregate.Dashboard):
    """How fast the active contexts grow, and how far to the automatic cut.

    Returns None when no active session has a measurable rate — a growth number
    invented from two samples would be worse than no number.
    """
    rows = [c for c in (d.context or []) if c.get("growth_per_turn")]
    if not rows:
        return None
    rates = economics.billing_rates(d)
    stats = getattr(d, "compacts", None)

    tbl = Table(box=box.SIMPLE_HEAVY, expand=True)
    tbl.add_column("Session", overflow="ellipsis", no_wrap=True, ratio=2, min_width=10)
    tbl.add_column("+/turn", justify="right", no_wrap=True, width=8)
    tbl.add_column("Turns left", justify="right", no_wrap=True, width=10)
    tbl.add_column("ETA", justify="right", no_wrap=True, width=7)
    tbl.add_column("$ to go", justify="right", no_wrap=True, width=8)
    for c in sorted(rows, key=lambda r: r.get("turns_to_auto") or 1e9):
        rw = economics.runway(c.get("tokens") or 0, c.get("growth_per_turn"),
                              c.get("auto_threshold") or 0, rates)
        turns = c.get("turns_to_auto")
        eta = c.get("eta_auto_s")
        tbl.add_row(
            Text(claude_parser.display_label(
                c.get("name"), c.get("project"), c.get("branch"), c.get("session")),
                style="cyan"),
            f"+{fmt_short(round(c['growth_per_turn']))}",
            f"~{fmt_int(round(turns))}" if turns else "—",
            fmt_ago(eta) if eta else "—",
            f"${rw['usd_until']:.2f}" if rw.get("usd_until") else "—",
        )

    measured = bool(stats and stats.measured and stats.auto)
    if measured:
        basis = (f'"Turns left" counts to the fill level at which automatic compaction '
                 f'really fired here: {stats.auto_fill_ratio * 100:.1f}% of the window, '
                 f'measured over {stats.auto} automatic compactions.')
    else:
        basis = ('"Turns left" assumes automatic compaction at 98% of the window — no '
                 "automatic compaction has been recorded here yet.")
    return Panel(Group(tbl, Text(
        "Growth is measured over the last API calls of each transcript, cut at the most "
        "recent compaction: the context drops by hundreds of thousands of tokens there, so "
        "a plain difference across it means nothing. " + basis, style="dim")),
        title="Context pace", border_style="yellow")




def _cheaper_models(d, n=2):
    """Cheaper models the user has actually run, cheapest first.

    Restricted to models present in the history on purpose. Repricing against
    the cheapest entry in the table produces a number for a model that may never
    have been tried on this kind of work; a model already in the history is one
    whose output the user has seen. If nothing cheaper was ever used, there is
    no honest comparison to make and the panel disappears.
    """
    from .pricing import _PRICING

    def blended(mid):
        i, o, cr, cw, _ = _PRICING[mid]
        return i + o + cr + cw

    used = [m for m in (d.model_tokens or {})
            if m in _PRICING and (d.model_tokens[m].get("cost") or 0) > 0]
    if len(used) < 2:
        return []
    ceiling = max(blended(m) for m in used)
    cheaper = sorted((m for m in used if blended(m) < ceiling), key=blended)
    return cheaper[:n]


def _reprice(bucket, model_id):
    """What one model's token counts would have cost on another price list."""
    from .pricing import _PRICING, cache_write_price
    i, o, cr, cw, _ = _PRICING[model_id]
    w5 = bucket.get("cache_write_5m", 0) or 0
    w1 = max(0, (bucket.get("cache_write", 0) or 0) - w5)
    return ((bucket.get("input", 0) or 0) * i
            + (bucket.get("output", 0) or 0) * o
            + (bucket.get("cache_read", 0) or 0) * cr
            + w1 * cw
            + w5 * cache_write_price(model_id, "5m")) / 1_000_000


def render_model_alt(d: aggregate.Dashboard):
    """The same tokens on a cheaper price list — a floor, not an estimate.

    Returns None without a per-model breakdown (an old cached snapshot), so the
    tab says nothing rather than showing zeros.
    """
    from .pricing import _PRICING
    buckets = {m: b for m, b in (d.model_tokens or {}).items()
               if m in _PRICING and (b.get("cost") or 0) > 0}
    if not buckets:
        return None
    alts = _cheaper_models(d)
    if not alts:
        return None

    tbl = Table(box=box.SIMPLE_HEAVY, expand=True)
    tbl.add_column("Model", overflow="ellipsis", no_wrap=True, ratio=1, min_width=9)
    tbl.add_column("Actual", justify="right", no_wrap=True, width=11)
    for a in alts:
        tbl.add_column(f"as {display_name(a)}", justify="right", no_wrap=True, width=13)
    tbl.add_column("Difference", justify="right", no_wrap=True, width=13)

    order = sorted(buckets.items(), key=lambda kv: -(kv[1].get("cost") or 0))
    totals = [0.0] * (len(alts) + 1)
    for mid, b in order:
        actual = b.get("cost") or 0.0
        totals[0] += actual
        cells = []
        for idx, a in enumerate(alts):
            alt = actual if a == mid else _reprice(b, a)
            totals[idx + 1] += alt
            cells.append(fmt_cost(alt) if a != mid else Text("=", style="dim"))
        best = min(_reprice(b, a) for a in alts if a != mid) if len(alts) > 1 or alts[0] != mid else actual
        tbl.add_row(display_name(mid), fmt_cost(actual), *cells,
                    Text(f"\u2264 -{fmt_cost(actual - best)}", style="green")
                    if best < actual else Text("\u2014", style="dim"))

    share = [Text(fmt_cost(t), style="bold") for t in totals[1:]]
    tbl.add_section()
    tbl.add_row(Text("Total", style="bold"), Text(fmt_cost(totals[0]), style="bold"), *share,
                Text(f"\u2264 -{fmt_cost(totals[0] - min(totals[1:]))}", style="bold green"))

    caveat = Text(
        "Same token counts, different price list \u2014 nothing else changes. That is the whole "
        "assumption, and it is the wrong one: a weaker model does not reach the same result "
        "with the same tokens. It needs more turns and more corrections, and every extra turn "
        "re-reads the whole context at the cache_read rate, which is already "
        f"{(d.cost_cache_read / d.total_cost * 100) if d.total_cost else 0:.0f}% of this bill. "
        "Read these numbers as the "
        "floor of what a switch could save, never as what it would.", style="dim")
    return Panel(Group(tbl, caveat),
                 title="If the same tokens had run on a cheaper model (hypothetical)",
                 border_style="dim")


def render_economics(d: aggregate.Dashboard):
    """Economics tab: compaction history, context pace, model counterfactual."""
    parts = [p for p in (render_compacts(d), render_pace(d), render_model_alt(d))
             if p is not None]
    if not parts:
        return Group(Panel(Text(
            "No compaction has been recorded yet, and no active session has enough "
            "samples to measure a growth rate. Both appear on their own once you keep "
            "working — nothing to configure.", style="dim"),
            title="Economics", border_style="dim"))
    return Group(*parts)
