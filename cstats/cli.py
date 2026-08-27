"""CLI entry point: install/update/headless modes + TUI launch.

Modes:
    (no args)               start TUI (cache-first, background refresh)
    --install               symlink launcher into ~/.local/bin, print PATH hint
    --uninstall             remove the symlink
    --update                git pull + reinstall deps (from the repo checkout)
    --version               print version
    --json                  dump current dashboard as JSON, no TUI
    --line                  single-line status for tmux/prompt, no TUI
    --no-cache              ignore the disk cache
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from . import __version__
from . import config
from .service import DataService


BIN_DIR = os.path.expanduser("~/.local/bin")
LAUNCHER_NAME = "cstats"
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _launcher_link() -> str:
    return os.path.join(BIN_DIR, LAUNCHER_NAME)


def _print(msg=""):
    print(msg)


def _eprint(msg=""):
    print(msg, file=sys.stderr)


def cmd_install(args) -> int:
    # bin/cstats, not cstats/ — the launcher and the package share a name, so
    # the launcher lives one directory down
    src = os.path.join(REPO_DIR, "bin", LAUNCHER_NAME)
    target = _launcher_link()
    os.makedirs(BIN_DIR, exist_ok=True)
    if os.path.lexists(target):
        os.remove(target)
    os.symlink(src, target)
    _print(f"Installed: {target} -> {src}")
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if BIN_DIR not in path_entries:
        _print(f"Add to PATH: export PATH=\"{BIN_DIR}:$PATH\"  (in ~/.bashrc or ~/.zshrc)")
    else:
        _print("PATH ok. Run: cstats")
    return 0


def cmd_uninstall(args) -> int:
    target = _launcher_link()
    if os.path.lexists(target):
        os.remove(target)
        _print(f"Removed {target}")
    else:
        _print("Not installed.")
    return 0


def cmd_update(args) -> int:
    """Update from the git checkout: pull + pip install -r requirements."""
    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        _eprint("No git checkout found — nothing to update.")
        return 1
    # check a remote is configured at all
    r0 = subprocess.run(["git", "-C", REPO_DIR, "remote"], capture_output=True, text=True)
    if not r0.stdout.strip():
        _eprint("No git remote configured. Add one (git remote add origin <url>)")
        _eprint("to use --update, or update manually.")
        return 1
    _print(f"Updating {REPO_DIR} ...")
    r = subprocess.run(["git", "-C", REPO_DIR, "pull", "--ff-only"],
                       capture_output=True, text=True)
    _print(r.stdout.strip())
    if r.returncode != 0:
        _eprint(r.stderr.strip())
        _eprint("Update failed (maybe dirty working tree?).")
        return r.returncode
    # reinstall deps
    venv_py = os.path.join(REPO_DIR, ".venv", "bin", "python")
    pip = [venv_py, "-m", "pip", "install", "--quiet", "-r",
           os.path.join(REPO_DIR, "requirements.txt")] if os.path.exists(venv_py) \
        else [sys.executable, "-m", "pip", "install", "--quiet", "-r",
              os.path.join(REPO_DIR, "requirements.txt")]
    r2 = subprocess.run(pip, capture_output=True, text=True)
    if r2.returncode != 0:
        _eprint("Dependency install failed:")
        _eprint(r2.stderr.strip())
        return r2.returncode
    _print("Update complete.")
    return 0


def _build_service(args) -> DataService:
    svc = DataService(use_cache=not getattr(args, "no_cache", False))
    svc.refresh()  # always fresh for headless modes
    return svc


def cmd_json(args) -> int:
    from .aggregate import dashboard_to_json
    svc = _build_service(args)
    obj = dashboard_to_json(svc.data)
    obj["error"] = svc.error
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0


def cmd_line(args) -> int:
    """One-line status, e.g. for tmux status-right or a shell prompt:
    5h 38% | 7d 35% | $9,633 | 66 sessions | rtk +377k | caveman +98k
    """  # savings are the corrected figures, see views.savings_offset
    svc = _build_service(args)
    d = svc.data
    parts = []
    if d.limits.available:
        fh = d.limits.five_hour_pct
        sd = d.limits.seven_day_pct
        parts.append(f"5h {fh:.0f}%" if fh is not None else "5h ?")
        parts.append(f"7d {sd:.0f}%" if sd is not None else "7d ?")
    parts.append(f"${d.total_cost:,.0f}")
    parts.append(f"{d.total_sessions}s")
    if d.context:
        from .pricing import context_window
        # context is a list of active sessions (newest first) — show the newest
        ctx = d.context[0]
        tokens = ctx.get("tokens") or 0
        win = context_window(ctx.get("model"), tokens)
        pct = tokens / win * 100 if win else 0
        # what the next turn costs just to re-read this context
        from .economics import billing_rates, session_economics
        per_turn = session_economics(tokens, billing_rates(d))["per_turn"]
        parts.append(f"ctx {pct:.0f}% ${per_turn:.2f}/t")
    # Our corrected figures, not the tools' own. A one-liner has nowhere to put
    # the footnote that makes a foreign number honest, so it shows the number
    # we can stand behind; the panels show both side by side.
    if d.rtk.available:
        parts.append(f"rtk +{d.rtk.billable_saved_tokens:,}")
    if d.caveman.available:
        from .views import caveman_overcount
        cav = (d.caveman.total_saved_tokens or 0) / caveman_overcount(d)
        parts.append(f"cav +{cav:,.0f}")
    parts.append(f"@{d.generated_at.astimezone().strftime('%H:%M')}")
    print(" | ".join(parts))
    return 0


def cmd_version(args) -> int:
    print(f"cstats {__version__}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cstats",
        description="Claude Code + rtk + caveman usage dashboard",
    )
    parser.add_argument("--install", action="store_true", help="symlink launcher into ~/.local/bin")
    parser.add_argument("--uninstall", action="store_true", help="remove launcher symlink")
    parser.add_argument("--update", action="store_true", help="git pull + reinstall deps")
    parser.add_argument("--version", action="store_true", help="print version")
    parser.add_argument("--json", action="store_true", help="dump dashboard JSON, no TUI")
    parser.add_argument("--line", action="store_true", help="single-line status, no TUI")
    parser.add_argument("--no-cache", action="store_true", help="ignore disk cache")
    parser.add_argument("--theme", metavar="NAME", help="start with this theme (e.g. orange, nexat, nord)")
    parser.add_argument("--inline", action="store_true",
                        help="run inline without the alternate screen (fix flicker in tmux/WSL)")
    args = parser.parse_args(argv)

    # Before anything reads a path: carry the pre-rename directories over.
    # A no-op on a fresh install and after the first run.
    for old, new in config.migrate_legacy_dirs():
        _eprint(f"moved {old} -> {new}")

    if args.version:
        return cmd_version(args)
    if args.install:
        return cmd_install(args)
    if args.uninstall:
        return cmd_uninstall(args)
    if args.update:
        return cmd_update(args)
    if args.json:
        return cmd_json(args)
    if args.line:
        return cmd_line(args)

    # default: TUI
    from .app import main as tui_main
    return tui_main(use_cache=not args.no_cache, theme=args.theme, inline=args.inline)


if __name__ == "__main__":
    sys.exit(main())
