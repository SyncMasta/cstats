# CONTEXT.md

Where the work stands. Keep it short (**~50 lines max**): resolved items get
deleted, not accumulated. Durable knowledge — requirements, the measured state
of the history, design decisions — belongs in `ARCHITECTURE.md`, rules for
agents in `AGENTS.md`.

## Current state

- Branch `claude/cstats-project-review-b9hmby`, pushed to
  `github.com/SyncMasta/cstats` (PR #1). The repo also lives at `NEXAT/cstats`
  on the company GitLab; `cstats --update` follows whichever remote is set.
- Version 0.4.0, `CACHE_VERSION` 14.
- `test_compacts.py` and `test_session_cache.py` green. `smoke_test.py` needs a
  real local history — it asserts the Tokens pane scrolls, which a day or two
  of transcripts cannot fill at 120x40, and stops there on a fresh checkout.

## Last session

**Repo review**, then its findings. `bin/cstats` and the worktree script had
lost their executable bit in the GitHub web upload, so the documented entry
point returned "Permission denied" for anyone cloning. `limits.py` was the last
reader still hard-coding `~/.claude` instead of honouring `CLAUDE_CONFIG_DIR`,
and its no-token message named a path that does not exist on macOS, where the
token lives in the login keychain.

**The OAuth response is read in full** (§6): `seven_day_opus` /
`seven_day_sonnet` and the extra-usage ceiling were fetched and discarded, so
the panel could read 35% while Opus sat at 88%.

**Added `otel.py`** (§10), a fourth optional source. Claude Code exports
telemetry to no file, so it scrapes the Prometheus endpoint the CLI serves. It
exists for the one thing the transcripts cannot give: what subagents cost — 0
lines in the whole history carry `isSidechain`. Shown in the Economics tab; the
digit keys 1-9 are full, and a cost question belongs there anyway.

## Open

- **The telemetry reader is untested end to end.** Built against the documented
  format and pinned by `check_otel_reader()` (names, parsing, the four
  statuses, cache round trip, panel) over a real HTTP round trip — but no
  Claude Code with telemetry on has ever answered it. `unknown_metrics` exists
  so a drifted name shows up in the panel instead of as a plausible zero.
- **macOS still has no token to read.** That needs
  `security find-generic-password`, which can raise a blocking system prompt
  from the refresh thread — deliberately not done; the reason line says so.
- **Cross-checking `pricing.py` against `claude_code.cost.usage`** is tempting
  and currently unsound: those counters cover one process since it started,
  every other total here covers the whole history, and the metrics carry no
  process start time to align them.
- The Admin and Claude Code Analytics APIs report subscription usage per user
  and day, but need an organisation and an admin key — no path for a personal
  plan.
- The compaction hint fires at $0.10/turn and ordinary sessions sit right
  there. If it gets noisy, raise the threshold with `,`. Not changed in code.
