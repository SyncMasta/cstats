# ARCHITECTURE.md

## Overview

```
~/.claude/projects/*/*.jsonl ──┐
OAuth /api/oauth/usage ────────┤
~/.local/share/rtk/history.db ─┼─→ aggregate.build() ─→ Dashboard ─┬─→ TUI (app.py + views.py)
~/.claude/.caveman-history ────┘                                  ├─→ --json / --line
                                                                  └─→ ~/.cache/cstats/dashboard.json
```

A single `Dashboard` object is the interface between every data source and
every output (TUI, JSON, one-liner, disk cache).

## Why this project exists

Three tools all affect my Claude Code spend:

- **Claude Code** itself — its `/usage` has to be invoked by hand and shows
  only the current moment
- **rtk** (Rust Token Killer) — a shell proxy that compresses bash output
  before it reaches the model and logs the saving in a SQLite DB
- **caveman** — a plugin that compresses the agent's own wording and writes
  estimates into a JSONL file

Nothing showed all three together: none of the sources has a TUI, `/usage`
does not refresh itself, and the question "what do rtk and caveman actually
save me in money" is answered by none of them alongside the real spend.

## Requirements

1. **A terminal TUI** that refreshes itself (60s), no manual invocation.
2. **One overview**: real 5h/7d plan limits (live), tokens and cost, rtk
   savings, caveman savings, context fill of the running sessions.
3. **Everything readable locally** — no accounts, no cloud, no telemetry, and
   **no mandatory integrations**: rtk and caveman may be missing, either or
   both. The tool then says so instead of looking broken.
4. **Instant start** — cache-first: the last state is visible immediately, a
   background refresh follows.
5. **Local timezone** for every time shown.
6. Headless modes (`--line`, `--json`) for tmux, prompts and scripting.
7. **Labelled by session, not by folder.** I work in several worktrees of one
   repo at the same time. A folder name then does not say which work a number
   came from, so wherever possible the session name is shown instead.
8. **Measured rather than assumed, wherever the data allows it.** The
   transcripts record what actually happened — how large the context really
   was after a compaction, at what fill the automatic one triggered, which
   model paid for a call. Where an assumption can be replaced by a
   measurement it is, and the UI says which of the two it is showing.
9. **Every number carries its own visible age.** The sources age independently
   (the OAuth endpoint rate-limits, caveman only writes when prompted).
   Without an age, "frozen" cannot be told from "unchanged" — that is the
   first thing this tool got wrong. A *wrong* age is worse than none: the file
   time sat next to a two-day-old context reading and claimed "just now".
10. **Do not pass a foreign number through when we know better.** rtk and
    caveman count for their own purposes: caveman without `message.id` dedupe
    (2.6x above what was billed) and with the model of the *first* answer as
    the session label. Where the tool has the same quantity cleanly from the
    transcripts, its own wins; where it does not, the foreign one is shown
    with a footnote rather than looking right without one.

## The two questions behind the display

The tool does not only display; it answers two questions none of the sources
answers. Both hang on the same observation:

**`cache_read` dominates the bill** (69% here). Every turn sends the whole
conversation again and the prefix is billed as `cache_read` — a long session
pays for its entire history on *every* turn. A single token is therefore never
paid for "once" but `1 x write + N x read`, with N = `cache_read /
cache_write` (~68 here).

1. **What do rtk and caveman save in money?** A saved token does not save its
   list price but the whole chain. The first version calculated read-once and
   was off by ~70x.
2. **When should I compact?** When `context_tokens x read_rate` per turn
   hurts. Compacting costs once (a full read + the summary + a new cache write
   at ~21x the read price) and still pays back after ~3-10 turns. The tool
   says so using the rates actually billed, not list prices — including a
   desktop hint when a session crosses the threshold. The range is measured,
   not guessed: the context after a compaction is really ~71k tokens, not the
   ~17k Claude Code's own metadata reports (those count only the preserved
   conversation — the system prompt, CLAUDE.md, tool definitions and MCP
   schemas are billed too).

## Measured state of the history

As of **2026-08-20**, regenerated in ~5s by `tools/measure_facts.py`. These
numbers are the basis of the design decisions — **do not re-measure them by
hand**, a full scan over ~680 MB of JSONL costs minutes.

The model and cache figures in this measurement are `message.id`-deduplicated
for the first time (§1); the script previously summed every assistant line and
was off on output by the dedupe factor. The compaction figures now filter out
the compaction call itself (`COMPACT_CALL_SHARE`, §5d). The history has also
grown from 82 to 119 transcripts since the last measurement, so differences
against the old table come from both.

| Measure | Value | Why it matters |
|---|---|---|
| history | 119 transcripts, 682 MB | a refresh re-reads all of it, the reason for the incremental cache |
| dedupe factor | 91,316 lines / 47,121 `message.id` = **1.94x** | without the dedupe every token figure would be that much too high |
| sessions crossing day boundaries | **44 of 108**, longest span **1357h** | cost must not hang off the session start |
| spread of one session | one session touches up to **106 of 168** weekday/hour buckets | the heatmap must bucket per message, not per session |
| cache-write TTL | 216.9M tokens 1h vs 0.56M tokens 5m (**99.74% 1h**) | the 1h price (2x input) is almost always the right one here, the 5m price (1.25x) is still needed |
| compactions | **97** (15 automatic / 82 manual) | the basis for every compaction statement |
| context before compacting | median 567k | |
| context after | median **70.6k** (48k-104k), not the reported 17k | a factor of 4.1 — otherwise the break-even model is too optimistic |
| fill when compacting | automatic **99.91%** (n=14), manual **49.87%** (n=80) | manual compactions sat at 96.1% until 2026-08-08 (~4 points ahead of the machine); since the economics work they really do happen early |
| `gitBranch` | 139 distinct values | the label reserve when a session has no name |
| mtime ahead of content | **54 of 108** files by more than 10min | mtime is not activity — a resume *touches* the file, metadata lines carry no `timestamp` |
| `isSidechain: true` | **0 lines** in the whole history | subagents have no transcripts of their own here, which refutes every "that was a subagent" explanation |
| sessions spanning more than one model | **8** | "the model of a session" does not exist |
| worst model label | first-model-wins hits **0.6%** of the output, last-model-wins **0.2%** | both shortcuts are wrong, in opposite directions — the label comes from `by_model` |
| title lines per transcript | `custom-title` 69, `agent-name` 56, `ai-title` 60 of 119 | three kinds, all repeating per turn — precedence must not depend on file order |
| only `custom-title` present | **36 of 119** | these sessions showed the repo name instead of their own, because the typed kind was never read |

## Non-goals

- No distribution or packaging (a personal tool, the symlink install is
  enough).
- No real billing data — the basis is the standard price list, not an invoice.
  The derived rates (`cost_x / tokens_x`) are mixed from it rather than read
  off a bill; they are right relative to each other, not against your account.
- No interference with the data sources: the tool only reads, and never writes
  into `~/.claude` or the rtk DB. The Stop hook that keeps the caveman history
  current deliberately belongs in the user's Claude Code config, **not** in
  this tool.

## Approach

Python + Textual (rather than the Go fork of claude-code-usage-dashboard
originally considered), because iteration is faster, Rich comes with it, and
all four data sources are simple file formats (JSONL, SQLite, OAuth JSON) that
Python reads with the stdlib.

## The data sources in detail

| Source | Path | Format | What is special |
|---|---|---|---|
| session transcripts | `~/.claude/projects/*/*.jsonl` | JSONL | needs `message.id` dedupe (content-block lines); the folder name is the cwd slug, which is what maps rtk paths onto sessions |
| live limits | `GET api.anthropic.com/api/oauth/usage` | JSON via the OAuth token in `~/.claude/.credentials.json` | undocumented, rate-limits aggressively (429) — needs a backoff, otherwise the display freezes |
| rtk | `~/.local/share/rtk/history.db` | SQLite, table `commands` | 90-day retention; UTC timestamps, no `session_id` — only `project_path` |
| caveman | `~/.claude/.caveman-history.jsonl` | JSONL, snapshots per session | the newest per `session_id` counts; written **only** by `/caveman-stats` or a Stop hook of your own. `output`/`turns` carry no `message.id` dedupe (2.6x too high), `model` is the session's first answer, `ts` is the write time with no span |

## Modules (`cstats/`)

| File | Responsibility |
|---|---|
| `cli.py` | arg parsing, install/uninstall/update, `--json`/`--line`/`--version`, starting the TUI |
| `app.py` | Textual app: 9 tabs, key bindings, worker refresh, theme registration |
| `views.py` | pure render functions `render_X(dashboard) -> rich renderable`, one per tab |
| `screens.py` | modals: help (`?`), settings (`,`) |
| `service.py` | `DataService`: holds the current dashboard, cache-first start, refresh lock, alerts |
| `aggregate.py` | builds the `Dashboard` from every source; JSON serialization for the cache |
| `claude_parser.py` | JSONL parser (the dedupe!), `current_contexts()` for running sessions |
| `limits.py` | OAuth limits with a response cache, 429 backoff and age metadata |
| `limit_history.py` | utilization snapshots as JSONL (sparkline data), 30 days |
| `rtk.py` | rtk SQLite reader (read-only, aggregated by day and project) |
| `caveman.py` | caveman JSONL reader (newest snapshot per session_id) |
| `pricing.py` | model prices, credit weighting, context window sizes |
| `themes.py` | custom themes orange + nexat (NEXAT style guide colours) |
| `config.py` | `~/.config/cstats/config.json` (theme, refresh, compaction threshold), chmod 600 |
| `notify.py` | desktop notifications (notify-send/osascript), fire-and-forget thread |
| `economics.py` | cost per turn per session, compaction break-even, billing rates |
| `compacts.py` | compaction history from the transcripts (byte scan, its own file cache), growth rate of active sessions |
| `session_cache.py` | incremental per-file parse cache for `parse_sessions()` |

## Central design decisions

### 1. message.id dedupe (claude_parser.py)

One Claude API message writes **one JSONL line per content block**; every line
shares the `message.id`, and `output_tokens` grows monotonically. The naive
approach counts ~2.4x too much.

The rule: count `input`/`cache` tokens only on the **first** sighting of a
`message.id`; on later lines of the same message count only the **output
delta** (`ot - prev`). Cost follows: the input side once, the output side per
delta.

**The same rule across files.** Forking a session copies the inherited turns
into the new transcript verbatim, `message.id` included; those copies are not
new API calls and were never billed again. Per-file dedupe therefore counted
them twice — measured, 470 ids across 7 pairs of transcripts, $52 of $10,634.
`parse_sessions` keeps a claim map (id -> owning transcript), built in the
fixed path order the files are already parsed in, so the answer does not depend
on whether the cache was warm. A cached entry whose ids have since been claimed
elsewhere is rescanned. Which of two copies owns an id is arbitrary but
deterministic; the total is exact either way, only the attribution to a session
could differ.

### 2. Splitting cost into input and output

Per call the parser accumulates `cost_input` (input + cache_read +
cache_write), `cost_output`, and separately `cost_cache_read` and
`cost_cache_write`. The split exists for the savings-offset calculation: only
this way can a price per token class be formed against the *matching* token
count.

### 2b. Savings offset (`views.savings_offset`)

What would the same work have cost without rtk and caveman? A saved token is
**not** paid for once:

- It never enters the prompt cache (the `cache_write` rate).
- It is never re-read on all the later turns of the same session.
  `total_cache_read / total_cache_write` is exactly the average number of
  re-reads per written token (~68 here) and therefore the multiplier on the
  downstream cost. This share dominates.

From that:
- **rtk** keeps shell output out of the prompt: `write_rate + read_rate x reuse`
- **caveman** prevents output tokens: `out_rate` (generating them) `+
  write_rate + read_rate x reuse` (the text then joins the context like any
  other)

**Neither tool's own token figure may be used as it stands** (requirement 10).
Both are corrected against something measured here, and the panel says so
rather than applying the correction silently:

- **rtk** reports `raw command output - compressed output`, i.e. it prices a
  world in which the entire untruncated output would have reached the model.
  It would not have: across 25,639 real Bash results in this history the
  largest is 52,545 characters and the 99th percentile is 10,139 — Claude Code
  caps tool output. `rtk.BASH_OUTPUT_CEILING_TOKENS` (15k) caps each command's
  credited saving accordingly. This is not a rounding correction: 74 of 14,958
  rows (0.5%) carry 94.3% of the reported total, led by one `find / -name '*'`
  claiming 11.5M tokens. The total goes from 38.7M to 3.0M.
- **caveman** sums every content-block line without deduplicating
  `message.id`, so its output figure sits 2.15x above what these transcripts
  were billed for. `views.caveman_overcount(d)` divides its saving by exactly
  that measured ratio, and returns 1.0 whenever the comparison is impossible,
  so the correction can only shrink a claim.

Together this moved the offset from $5,365 (+50% on top of spend) to $1,875
(+18%). The formula was never the problem — the inputs were.

Every rate comes from what was actually billed (`cost_x / tokens_x`), not from
a price list — that is how a history with mixed models prices itself.

The old formula valued rtk at `cost_input / (input + cache_read)`. Wrong
twice: the numerator contained the cache_write cost while the denominator did
not contain the cache_write tokens (rate ~32% too high), and the model omitted
the re-reads entirely (result ~70x too low: $0.30 instead of $20.66).

### 3. Cache-first (service.py)

On start `DataService` loads the last state from
`~/.cache/cstats/dashboard.json` and renders immediately; a worker
thread then builds fresh and renders via `call_from_thread`. The cache carries
a `cache_version`, so a schema change discards old caches automatically.

### 4. The refresh model

- Auto-refresh every 60s (`refresh_ms` in config). **Careful: Textual's
  `set_interval` takes seconds, not milliseconds.**
- `@work(exclusive=True, thread=True)` — concurrent refreshes are dropped; the
  UI never blocks.
- Every refresh rebuilds the dashboard but reads the transcripts
  **incrementally** (`session_cache.py`): per file it stores `(size, mtime_ns,
  sha256 over the offset plus the first and last 4 KiB of the prefix)` together
  with the finished session aggregates, skips unchanged files and resumes
  grown ones at `commit_offset`. 1.8s cold, **0.015s warm** (400 MB, 67
  files). A cache miss only ever costs work, never different numbers: if
  anything does not match, the file is scanned in full.
- The parse cache has to store the `counted` dict (`message.id` -> last
  `output_tokens`) as well, otherwise a message continued across the cache
  boundary is counted twice. It is stored **whole**, not truncated: measured,
  26,558 of 26,559 ids are contiguous — the one exception reappears 59 ids
  later, so "keep the last N" would be a usually-true rule. The price is ~1 MB
  of cache.
- The cache is only written when something actually changed (`if fresh !=
  cache`); otherwise the TUI would write a megabyte every 60 seconds for
  nothing (39ms -> 14.7ms warm).
- **Two kinds of refresh.** `DataService.refresh(force=False)` (the timer)
  bails out when a build is already running. `refresh(force=True)` (the `r`
  key) waits on `_build_lock` for the running build and then builds again —
  before that the key silently returned the old state whenever it hit a
  running auto-refresh. `force=True` additionally bypasses the OAuth response
  TTL (see §6).
- **Start refreshes forcibly** (`on_mount` calls `refresh_data(force=True)`).
  The dashboard cache can be hours old, and the OAuth response TTL (60s) would
  serve a freshly started TUI a stale measurement. The min-interval and
  backoff limits from §6 still apply, so restarting repeatedly cannot trigger
  a 429 storm.
- While a build runs the subtitle shows `· refreshing…`; without a cache the
  panes show "Loading usage data …" instead of staying blank. The marker
  counts running workers (`_busy`), it is not a flag: `r` starts a second
  worker while the thread worker cancelled for exclusivity is still running
  its `finally`, and a flag would clear the marker too early.
- `_render_all()` always reads `service.data`, never a dashboard a worker
  still holds: otherwise an auto-refresh finishing late overwrites the result
  of a manual one.
- The panes are **not focusable** (`can_focus = False`). A click used to move
  focus into the `ScrollableContainer`, after which the arrow keys scrolled the
  panel instead of switching tabs — a mouse click silently changed what the
  keyboard does. Scrolling goes through app bindings anyway (`↑/↓`,
  `pgup/pgdn`, `home/end`) onto the visible pane, and the mouse wheel needs no
  focus.
- Every panel whose source can age independently (OAuth limits, rtk, caveman)
  shows its own age. Without that line "stale" cannot be told from
  "unchanged".

### 5. Active sessions (current_contexts)

"Active" means the last **timestamped** line is younger than 10 minutes. For
each active file only the **tail** (500KB) is read: the last `usage` block is
the context fill, titles as in §5a. The head (60KB) is the fallback for
transcripts whose titles sit before the tail window.

**mtime is not activity.** 42 of 58 transcripts with content have an mtime
more than ten minutes ahead of their newest content: Claude *touches* the file
on resume (`fs.utimes`, recognisable by the ms-rounded `st_mtime_ns`) and
appends metadata lines on open (`last-prompt`, `ai-title`, `mode`,
`permission-mode`, `agent-name`, `bridge-session`) — real writes with no
conversation in them. Sorted by mtime, sessions dormant for days therefore sat
in the panel with "Seen 0s" next to a two-day-old context reading, and the
compaction advice suggested them. mtime never lags *behind* the content, so it
stays as a cheap prefilter; filtering and sorting go by content. Metadata
lines carry no `timestamp`, so the filter catches them by itself.

Three ages, deliberately separate: `content_age_s` (activity, filters and
sorts), `age_s` (age of the `usage` block — the age of the *number being
shown*, displayed in the `Seen` column and deciding `frozen`), and
`touched_s` (mtime). The panel rule "every number with a visible age" is
otherwise violated: the column claims an age that does not belong to the
number beside it.

### 5a. The name of a session (`session_title`)

A transcript contains **three** kinds of title, all repeating once per turn
and interleaved with each other:

| Line | Field | Origin |
|---|---|---|
| `custom-title` | `customTitle` | typed by the user |
| `agent-name` | `agentName` | generated, current |
| `ai-title` | `aiTitle` | generated, survives a resume |

Precedence: **`custom-title` → `agent-name` → `ai-title`**. What was typed
beats what was generated; of the generated two, `agent-name` is the more
current, while `ai-title` drags along the title of the work a resumed session
was forked from.

`custom-title` was originally not read at all, and between the other two the
file order decided (`session.name` was overwritten on every title line).
Measured: **36 of 119** transcripts carry *only* a `custom-title` — those
showed the repo name in the active-sessions panel via the fallback
`ctx["name"] or ctx["project"]`, which is exactly the confusion requirement 7
("labelled by session, not by folder") exists to prevent. It shows up after a
`/clear`: the typed title survives, the generated ones start from nothing.

`Session.name` is therefore a **property** over the three slots
(`custom_title`, `agent_name`, `ai_title`), not a field — precedence lives in
one place instead of in the parse order. The parse cache serializes the slots
rather than `name` (`PARSE_VERSION` 3).

`session_title` decides **what** the name is; `display_label(name, project,
branch, session_id)` decides **what is shown** when there is none: name →
`project:branch` → project → the first eight characters of the session id →
`?`. That too was scattered across five call sites, each different, so the
same session carried a different name depending on the tab. The branch is the
documented reserve (139 distinct values) and at least tells two worktrees of
one repo apart.

### 5b. Session names instead of folder names (`_link_savings`)

Several sessions run in one repo at the same time (worktrees, several
workspaces). A folder name therefore does not say which work a saving came
from — the overview shows session names for rtk and caveman.

- **caveman** stores a `session_id` per snapshot, resolvable directly through
  `session_name` (see §5a).
- **rtk** stores only `project_path`. But Claude derives its transcript folder
  from exactly that path (every character outside `[A-Za-z0-9-]` becomes `-`,
  see `_path_to_slug`), so the path can be turned back into the slug and thus
  into the session.

Priority in `_build_slug_names()`: active sessions (`context`, the ones
producing the rtk commands right now) → the newest named session in the same
slug → the `slug_name` map persisted in the cache (so a cache-first render
keeps the names). No hit means the last path segment; the home directory
becomes `(home)`, because its last segment is the username.

**Order matters:** `_link_savings()` must run in `dashboard_from_json()`
*after* `rtk`/`caveman` have been restored — before that it labelled the empty
placeholders and the panels stayed blank after a cache load (that was a bug).

### 5c. Cost per turn and the compaction hint (`economics.py`)

Every turn sends the whole conversation again; with prompt caching the prefix
is billed as `cache_read`. The input cost of a turn is therefore essentially
`context_tokens x read_rate` — a long session pays for its entire history on
**every** turn. That is why the session table has a `$/turn` column; that is
the number that hurts.

Compaction break-even:

```
compact_cost = tokens x read + summary_out x out + post_compact x write
saved/turn   = (tokens - post_compact) x read
breakeven    = compact_cost / saved_per_turn
```

`post_compact` is **measured, not assumed** (see §5d): a median of 70.6k
tokens over 97 real compactions. The old assumption of 20k was off by a factor
of three because it followed Claude Code's own `postTokens` — which counts
only the preserved conversation, while the system prompt, CLAUDE.md, tool
definitions and MCP schemas are billed too. Break-even with that: 268k of
context after ~10 turns, 800k after ~4. The constants in `economics.py` stay
as the fallback for a history without compactions, now set to the measured
values (`POST_COMPACT_TOKENS = 60_000`, `SUMMARY_OUTPUT_TOKENS = 5_000`).

`billing_rates(d)` additionally returns `post_compact`, `summary_out`,
`auto_fill` and `post_measured` — the last of which drives the wording in the
UI ("measured" vs. "assumed"). The function has to survive a dashboard without
`.compacts`.

`max_breakeven` in `advice()` is 25. With `post_compact` tripled, every
break-even figure rises systematically; the old 15 was calibrated for the 20k
assumption and would have silently moved the warning threshold from 88k to
175k of context.

The threshold `context_alert_usd` (default $0.10/turn) drives both the panel
hint and the desktop notification. The notification fires **once per session**
and re-arms as soon as that session's context drops below half the threshold —
i.e. after a compaction or a restart. Context alerts do not depend on the
OAuth endpoint: `_check_alerts()` runs even when no limits are available.

### 5d. Compaction history and growth rate (`compacts.py`)

Claude Code logs every compaction in **two** lines: a boundary (`type ==
"system"`, `subtype == "compact_boundary"`, field `compactMetadata`) and a
summary (`type == "user"`, `isCompactSummary`). Guarding on `type` **and**
`subtype` **and** `isinstance(cm, dict)` is mandatory — the string
`compactMetadata` also appears inside quoted transcripts.

**The central trap**: `compactMetadata.postTokens` is not the context that
gets billed afterwards. Measured over the history: 17,177 reported, 70,590
actually billed (a factor of 4.1). The module therefore takes the first
deduplicated `assistant` usage line **after** the boundary as `ctx_after`.
`pre/post_tokens` are carried along as metadata but enter no calculation.

The scan is one byte pass per file with prefilters and `json.loads` only on
the few relevant lines: 0.8s cold over 400 MB, 0.003s from its own file cache
(`compacts.json`, with its own `SCAN_VERSION`). Two traps that a review found
and that are tested: the event list of a cache entry must **never** be
truncated (otherwise every resume loses events, monotonically decreasing with
no recovery), and a torn last line must not corrupt the committed state — the
scan stops at the first line without a `\n` so that state and offset cannot
drift apart.

**Growth rate** (`annotate_pace`): the context does not grow monotonically; at
every compaction it drops by several hundred thousand tokens. The samples are
therefore cut twice — at the last boundary and at the last negative delta
(which catches resume and `/clear`, neither of which writes a boundary). The
sample **at** the boundary stays in: it is the anchor of the new segment, and
the compaction itself is accounted for before it. Below three samples the
result is `None` rather than a number. A call reading >= 50% of `preTokens`
right after a boundary is the compaction call itself and is excluded — without
that guard a 950k value slips into `ctx_after`.

The forecast "auto-compact in ~N turns" uses the measured trigger point of the
automatic compaction (99.9% of the window, from 15 real auto-compactions),
not a guessed safety margin.

### 6. OAuth limits

The endpoint `/api/oauth/usage` is undocumented and rate-limits sharply (HTTP
429 after a few calls). `limits.json` therefore stores the last response
together with two timestamps: `_ts` (when the data was fetched) and
`_next_try` (the earliest we may ask again).

- `CACHE_TTL = 60s` — the cache is served without asking for that long.
- `MIN_FETCH_INTERVAL = 20s` — a floor even for `force=True`, so holding down
  `r` cannot provoke a 429.
- `BACKOFF_429 = 240s`, doubling per consecutive refusal up to
  `BACKOFF_429_MAX = 1800s`; `BACKOFF_ERROR = 60s`. After an error only
  `_next_try` (plus `_strikes`) moves forward, while `_ts` and the data stay
  where they are. A successful fetch resets `_strikes` to 0.

Why escalating: a fixed value against a longer server-side window runs in a
circle — expire, ask, 429, wait the same time again — and every refused
request keeps the limit alive. Doubling finds the actual window instead of
guessing it. `UsageLimits.retry_in` carries the remaining wait into the
display: "rate-limited" with no end in sight reads like a defect.

The cache entry doubles as the backoff marker. Without that, a 429 arriving
**before** any response ever existed wrote no `_next_try` at all — a fresh
machine then kept asking every 60s and stayed rate-limited.

The backoff is the actual point: before it, a 429 did return the stale
cache, but without a `_next_try` — so every 60s refresh asked again, collected
another 429, and the displayed percentages froze permanently on the last
successful fetch. `UsageLimits.fetched_at`, `.age_s`, `.stale` and
`.rate_limited` carry that state all the way into the views.

### 6b. Day and hour buckets are built while parsing

`Session.by_day` and `Session.by_hour` are filled **per line**, by the local
day of that line's timestamp; `Dashboard._compute()` only sums them.
Previously everything hung off `s.start`, which mis-booked 25 of 59
transcripts (the longest spanning 1046 hours): a single day showed $3,242
instead of $183, and the heatmap filled 36 instead of 123 of the 168 cells,
because one session touches up to 99 different weekday/hour cells and all of
them landed in one. The Activity tab was showing start times, not working
hours.

The buckets are filled in the **same** dedupe branches as the totals (§1),
never beside them — otherwise sum and buckets drift apart. The smoke test
checks `sum(by_day_cost) == total_cost`.

### 6c. Credits, cache TTL, branch

- `calc_credits` does **not** round per call. Credits are a scaling of the
  dollar amount (`x 2/15`); rounded per summand, 85% disappeared — measured,
  183 instead of 1,187 over the whole history, because almost every call is
  below $3.75. `credits_7d` is built from the daily buckets, not from sessions
  that *started* in the last 7 days (9.7 instead of 139.6).
- Cache writes are priced by TTL: `usage.cache_creation` distinguishes
  `ephemeral_5m` (1.25x input) from `ephemeral_1h` (2x input). If the field is
  missing the 1h rate applies — the previous behaviour, so no regression.
- **Prices can expire.** A call is billed at the price in force when it was
  made, so `price_for(model, when)` takes the line's own timestamp and
  `_DATED_PRICING` holds the introductory rates. Sonnet 5 launched at $2/$10
  against a $3/$15 list price; with an undated table, 2026-09-01 would have
  raised the cost of every Sonnet 5 call ever made by 50% overnight — a fifth
  of the output in this history.
- `Session.git_branch` (the last value, `HEAD` discarded) serves as the label
  reserve: a session without a name shows `project:branch` rather than just
  the folder name.
- `Session.by_model` accumulates **per API call** by raw model id.
  `Session.model` is last-model-wins and threw every session whose last line
  was `<synthetic>` into an "Unknown" bucket ($817).
- **A session's model label** (`_dominant_model`, `Dashboard.session_model`)
  comes exclusively from `by_model`, weighted by output, `<synthetic>`
  excluded; below a 90% share a `*` is appended. Both other available fields
  lie about long sessions in opposite directions: `Session.model` is
  last-model-wins, and caveman writes first-model-wins into its history
  (`caveman-stats.js`: `if (!model && entry.message.model)`). A session
  running from 07 July to 08 August therefore appeared in the Sessions tab as
  "Opus 5" (16.6% of its output) and in the caveman tab as "Fable 5" (4.2%) —
  when it was really 79.2% Opus 4.8. Subagents were not the cause: **0 lines**
  with `isSidechain: true` in the whole history. 8 sessions span more than one
  model; in the worst case first-model-wins names a session after 0.6% of its
  output.
- caveman counts `output` and `turns` **without a `message.id` dedupe** and is
  therefore measured at 2.6x the billed amount (5 sessions report more output
  than all our transcripts together). Not repairable from the foreign format —
  the columns are shown with a footnote. `caveman.by_day` is **dated by
  snapshot** for the same reason, not by consumption: the history knows no
  time span, so a month of work lands on the day `/caveman-stats` happened to
  run. That is why the panel is called "Snapshots", not "Daily savings".

### 7. Time zones

Everything is displayed in local time (`astimezone()`); storage and arithmetic
are UTC. `by_day` buckets are local days — **in every source**:

- rtk stores UTC RFC3339. The daily buckets are therefore no longer built with
  `substr(timestamp,1,10)` in SQL (those would be UTC days) but in Python via
  `_local_day()`; the query only fetches the raw rows from a UTC cutoff
  (`_utc_cutoff(44)`, index-friendly as a 19-character prefix). The old `WHERE
  substr(...) = date('now','localtime')` compared a UTC day against a local
  day — commands between local 00:00 and 02:00 (CEST) fell out of "today".
- caveman buckets its `ts` (epoch ms) through `.astimezone()` as well.

### 8. Themes

Textual's built-ins plus two custom themes, from the NEXAT style guide
(NEXred `#b41918`, anthracite `#383e42`, light grey `#cbd2d9`) and the orstats
orange palette respectively. Selected with `t`, persisted in config.json.

### 9. rtk and caveman are optional

Both integrations may be missing — neither, one, or both. The dashboard's own
numbers (limits, tokens, cost, context, compaction hint) depend on neither.

`RtkStats.status` / `CavemanStats.status` distinguish four cases instead of
just "there / not there":

| Status | Meaning | How it is detected |
|---|---|---|
| `ok` | data present | a DB with rows / a history with snapshots |
| `empty` | installed, nothing recorded yet | `shutil.which("rtk")` or the plugin cache directory / `.caveman-active` |
| `missing` | not installed | neither binary nor DB / no plugin |
| `error` | there, but unreadable | `sqlite3.Error`/`OSError` while reading, broken column types |

Plus a `hint` naming the concrete next step. The distinction is the point: a
bare "not found" reads like a broken dashboard when it is an uninstalled
add-on — and reporting `error` as `empty` says "wait for data" about a state
that never improves on its own. `compacts.CompactStats` knows `error` for the
same reason: a history without compactions is normal (the assumed
post-compaction value then applies), an aborted scan is not.

The same rule covers the limits: `UsageLimits.reason` says *why* nothing is
there (no OAuth token / 429 / endpoint unreachable) instead of folding three
causes into one empty object. And transcripts the parser could not read land
in `Dashboard.warnings` and are shown as an "Incomplete data" panel —
otherwise every total is quietly too small.

In the UI:
- Overview boxes: status + hint + "optional — everything else on this
  dashboard works without it", with a dimmed border rather than a coloured one.
- Tabs: the label becomes `rtk (not installed)`, `(no data)` or
  `(unreadable)` — `TabbedContent.get_tab(id).label` is settable. The tab stays
  reachable and explains itself rather than disappearing.
- Savings offset: a column per tool only when that tool is installed (a $0.00
  column looks like a broken calculation), plus a line saying which tool is
  being counted as zero and why. With neither installed the panel is dropped
  entirely.

`smoke_test.py` checks the tab labelling in both directions.

## Data flow when something fails

Every source fails on its own: rtk/caveman missing → the panel and the tab say
"not installed" (see §9), OAuth 429 → the stale cache with its age, corrupt
JSONL → the line is skipped and reported. `service.error` is shown as a red
banner in the overview; the last cached state stays visible.

## Tests

`smoke_test.py` — headless through Textual's Pilot: boots the app, waits for
the worker, checks every tab, tab jumps by number key, theme cycling, the
modals, scrolling, a manual refresh, the sort/filter logic of the Sessions tab
and `check_narrow_widths()`.

**Isolates config and cache** through `XDG_CONFIG_HOME=<tmpdir>` *and*
`XDG_CACHE_HOME=<tmpdir>` — a test may touch neither the real user config nor
the real cache. Both have happened: the user's theme was overwritten, and the
test refresh rewrote `dashboard.json`, `limit-history.jsonl` and
`alert-state.json`, because the cache paths were module constants — resolved
at import, i.e. before the test could redirect the environment. Through the
alert loop that could even fire a real desktop notification. Cache paths are
therefore **functions**. `check_cache_isolated()` asserts that no path leaves
the tempdir.

That list is not the real guard, though: it enumerates paths by hand and went
stale once — `limits.py` and `limit_history.py` were missing from it and wrote
real user files on every test run. `check_no_cache_constants()` therefore
parses every module in `cstats/` and rejects a top-level assignment that builds
a `~/.cache` or `~/.config` path. By the same pattern,
`check_views_do_no_io()` keeps the views off the disk and
`check_one_name_ladder()` keeps name selection inside `display_label()`.

All cache files are created through `config.open_private()` with 0600 — they
carry project paths, session names and spend.

`check_dominant_model()` and `check_ghost_sessions()` protect the two
shortcuts this history refutes (§5, §6c): a 79/17/4 split must not be named
after the 4% model, and a transcript that was *touched* without anything being
said is not an active session. The latter runs against a synthetic
`CLAUDE_CONFIG_DIR`, not against the real history.

`check_session_title()` checks the precedence from §5a directly and once
through both read paths (`current_contexts` and `parse_sessions`) against a
synthetic transcript in which the generated title comes **after** the typed
one — otherwise a regression to "last line wins" would pass unnoticed.

`check_billing_arithmetic()` and `check_savings_offset_arithmetic()` pin the
money functions by property rather than by dollar amount, so a price-list
update does not have to touch them: cache_creation is its own token class and
is not billed as input as well, a reported 5m share cannot exceed the reported
total, 5m is cheaper than 1h, credits are cost on another scale applied once,
and a saved token is worth the write plus every re-read.

`test_compacts.py` — the compaction scan against the real history (its figures
pinned against the "Measured state of the history" table), cache idempotence,
torn lines, and the pace cuts as a pure function.

`test_session_cache.py` — 23 cases, always the same question at heart: does
the cached path produce exactly the same numbers as a full scan? Including
growth onto a line boundary, a torn line, a shrunk file and a backdated mtime.

`tools/measure_facts.py` — regenerates the figures in "Measured state of the
history" in ~5s. Re-measuring them by hand burns minutes for numbers that are
already written down (AGENTS.md rule 10).

**`check_narrow_widths()`** guards against a trap that has struck three times:
a rich `Table` with too many fixed column widths drops the **trailing**
columns instead of shrinking them — invisible on a wide terminal. It hit
`$/turn`, caveman's `Snapshot`, and most recently a panel *title* that grew
past 80 characters once a filter was set. New tables belong in that list; text
columns get a `ratio`, fixed widths are for numbers only.

## File layout of the cache and state

```
~/.cache/cstats/     (or $XDG_CACHE_HOME/cstats/)
  dashboard.json       # last dashboard state (cache_version'd)
  sessions.json        # per-file parse cache, own parse_version (§4)
  compacts.json        # compaction scan cache, own SCAN_VERSION (§5d)
  limits.json          # last OAuth response + _ts/_next_try/_strikes (§6)
  limit-history.jsonl  # 5h/7d % snapshots (30 days, trimmed every 50 writes)
  alert-state.json     # {"fired": [...limit thresholds], "context_warned": [...session_ids]}
~/.config/cstats/
  config.json          # theme, refresh_ms, context_alert_usd
```

All of them are written 0600 through `config.open_private()`.

## External dependency: the caveman history

`~/.claude/.caveman-history.jsonl` is written **only** when
`caveman-stats.js` runs. The caveman plugin registers no hook for it (only
SessionStart + UserPromptSubmit), so the file never updates on its own — the
caveman panel then sits still for days while caveman is active. The remedy is
a Stop hook in `~/.claude/settings.json` calling
`~/.claude/hooks/caveman-history.sh` (async, so the end of a turn does not
block). That script lives outside this repo, which is why the panel shows the
age of the last snapshot — without that line you cannot tell whether the
numbers are old or simply unchanged.
