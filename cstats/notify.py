"""Best-effort desktop notifications. Never raises, never blocks.

Linux: notify-send (libnotify). macOS: osascript. Otherwise: silent no-op.
Everything runs in a fire-and-forget daemon thread so the TUI is never held
up by a missing/short-lived notification daemon.
"""

import shutil
import subprocess
import threading


def _send(title: str, message: str) -> None:
    proc = None
    try:
        if shutil.which("notify-send"):
            proc = subprocess.Popen(
                ["notify-send", "-u", "normal", "-t", "8000", title, message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif shutil.which("osascript"):
            # The title comes from the transcript (a typed or model-generated
            # session name), so it must never be pasted into AppleScript
            # source: a name containing a quote could close the string and run
            # its own `do shell script`. Pass both as arguments instead and let
            # osascript bind them, so nothing in them can be code.
            script = ('on run argv\n'
                      '  display notification (item 1 of argv) '
                      'with title (item 2 of argv)\n'
                      'end run')
            proc = subprocess.Popen(
                ["osascript", "-e", script, message, title],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except OSError:
        pass
    if proc is not None:
        try:
            proc.wait(timeout=5)  # reap the child, avoid zombies
        except (subprocess.TimeoutExpired, OSError):
            pass


def notify(title: str, message: str) -> None:
    """Fire-and-forget desktop notification."""
    threading.Thread(target=_send, args=(title, message), daemon=True).start()
