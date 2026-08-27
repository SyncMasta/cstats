# CONTEXT.md

Where the work stands. Keep it short (**~50 lines max**): resolved items get
deleted, not accumulated. Durable knowledge — requirements, the measured state
of the history, design decisions — belongs in `ARCHITECTURE.md`, rules for
agents in `AGENTS.md`.

## Current state

- Branch: `main`, clean, no remote yet
- Version 0.4.0. All three suites green: `smoke_test.py`, `test_compacts.py`,
  `test_session_cache.py`

## Last session

**Renamed to cstats** (was `claude-usage`). The launcher moved to `bin/cstats`
— a file and a package directory cannot share a name — so the old
`~/.local/bin/claude-usage` symlink is dead and `--install` was re-run here.
`config.migrate_legacy_dirs()` carries the old `~/.cache` and `~/.config`
directories over on first run; theme, threshold, 30 days of limit history and
the parse caches all survived.

**Audited every displayed number** against its source. The tool's own figures
held up; four did not, and are fixed (details in `ARCHITECTURE.md` §1, §2b, §6c):

- forked sessions were billed twice — dedupe was per file, $52 of $10,634. A
  global claim map fixes it; an independent reimplementation now agrees to
  0.002%, was 0.33%.
- rtk was ~13x overstated (it prices untruncated command output that Claude
  Code caps anyway), caveman 2.15x (no `message.id` dedupe). Both corrected
  against measurements, both named in the panel. Offset: $1,875, was $5,365.
- Sonnet 5's introductory price had no end date; prices are dated now.

Before that: docs split into two roles and translated to English,
`.claude/workflows/` brought over and retargeted, and a `repo-audit` round
(cache isolation, failure-vs-empty, view purity, security, dead code).

## Open

- Nothing blocking. The repo lives at `NEXAT/cstats` on the company GitLab and
  `cstats --update` works. The project is private because the NEXAT group is —
  GitLab refuses `internal` inside a private namespace — so colleagues reach it
  through their NEXAT group membership rather than by browsing.
- The compaction hint fires at $0.10/turn and ordinary sessions now sit right
  there. If it gets noisy, raise the threshold in the TUI with `,`.
  Deliberately not changed in code.
