"""cstats — combined usage dashboard.

Sources:
  - Claude Code session transcripts (~/.claude/projects/*/*.jsonl)
  - Live plan limits (Anthropic OAuth /api/oauth/usage)
  - rtk savings (SQLite ~/.local/share/rtk/history.db)
  - caveman savings (~/.claude/.caveman-history.jsonl)

Run:  python -m cstats      (TUI)
      python -m cstats --install / --update / --json / --line / --version
"""

import os
import sys

if __package__ in (None, ""):
    # executed directly (python cstats/__main__.py): make the package importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cstats.cli import main  # noqa: E402

sys.exit(main())
