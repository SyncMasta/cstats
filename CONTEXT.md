# CONTEXT.md

Where the work stands. Keep it short (**~50 lines max**): resolved items get
deleted, not accumulated. Durable knowledge — requirements, the measured state
of the history, design decisions — belongs in `ARCHITECTURE.md`, rules for
agents in `AGENTS.md`.

## Current state

- Branch: `claude/cstats-project-review-b9hmby`, pushed to
  `github.com/SyncMasta/cstats` (PR #1). The repo also lives at `NEXAT/cstats`
  on the company GitLab; `cstats --update` follows whichever remote the
  checkout has.
- Version 0.4.0, `CACHE_VERSION` 13.
- `test_compacts.py` and `test_session_cache.py` green. `smoke_test.py` needs a
  real local history — it asserts the Tokens pane scrolls, which a day or two
  of transcripts cannot fill at 120x40, and stops there on a fresh checkout.

## Last session

**Repo review**, then the small findings fixed. `bin/cstats` and
`scripts/setup_agent_worktree.sh` had lost their executable bit in the GitHub
web upload, so the documented entry point returned "Permission denied" for
anyone cloning. Dropped a 1-byte `test` artefact and the dead
`caveman.CAVEMAN_HISTORY` constant.

**More of the OAuth response is now read** (`ARCHITECTURE.md` §6). The endpoint
already returned `seven_day_opus` / `seven_day_sonnet` and the extra-usage
ceiling; the tool fetched them and threw them away, so the panel could read 35%
while Opus sat at 88%. `check_model_limit_windows()` covers both directions and
asserts on the pace *row*: the panel above prints the same labels, so a
document-wide assertion passes straight over a clipped column.

`limits.credentials_path()` honours `CLAUDE_CONFIG_DIR` like every other reader
(this module was the last hold-out) and the no-token reason names the macOS
keychain instead of a path that does not exist there.

## Open

- **macOS has no token to read.** Claude Code stores it in the login keychain
  (`Claude Code-credentials`). Reading it means `security find-generic-password`,
  which can raise a blocking system prompt from the refresh thread — deliberately
  not done; the reason line says so instead. Unresolved, not forgotten.
- **OpenTelemetry was built and removed again — do not rebuild it unasked.**
  A working `otel.py` (Prometheus scrape, four statuses, Economics panel,
  `check_otel_reader()`) was added and reverted on request: this tool is for
  the rtk and caveman savings, and a fourth source is scope nobody asked for.
  It is in the branch history if it is ever wanted (commit 6cb9a7b, reverted by
  eacc34f). What it alone could give: subagent cost — §6c records **0 lines**
  with `isSidechain: true`, so a subagent leaves no transcript and its spend is
  invisible to every other panel here. The Admin and Claude Code Analytics APIs
  report subscription usage per user and day but need an org and an admin key,
  so there is no path for a personal plan either way.
- The compaction hint fires at $0.10/turn and ordinary sessions now sit right
  there. If it gets noisy, raise the threshold in the TUI with `,`.
  Deliberately not changed in code.
