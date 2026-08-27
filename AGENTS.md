# AGENTS.md

Rules for AI agents working on this repo.

## Golden rules

1. **No packaging pressure.** This is a personal tool, not a PyPI release.
   `cstats` is a bash launcher symlinked into `~/.local/bin`. Do not
   introduce `pip install -e .` or anything like it.
2. **Read-only towards the sources.** Never write into `~/.claude/`, the rtk
   DB or caveman's files. Our own data belongs in `~/.cache/cstats/`
   and `~/.config/cstats/` and nowhere else.
3. **Tests isolate config *and* cache.** `smoke_test.py` points both
   `XDG_CONFIG_HOME` **and `XDG_CACHE_HOME`** at a tempdir — no test and no
   code path may write the real user config or the real cache (theme cycling
   in a test once overwrote the user's theme; the test refresh overwrote
   `dashboard.json`, `limit-history.jsonl` and `alert-state.json`, and via the
   alert loop could fire a real desktop notification). That is why every cache
   path is a **function, not a module constant**: a constant is resolved at
   import, i.e. before a test can redirect the environment.
   `check_no_cache_constants()` enforces this by parsing the source — the
   hand-written path list next to it went stale once and missed two modules.
4. **Textual's `set_interval` takes seconds**, not milliseconds. (This was a
   bug once: the refresh ran every 16h instead of every 60s.)
5. **Python's `weekday()` is Mon=0** — do not port Go-style remapping
   formulas (`(wd+6)%7`).
6. **The JSONL dedupe is sacred** (see ARCHITECTURE.md §1): input and cache
   count only on first sight of a `message.id`, after that only the output
   delta. Never "just sum every usage line".
7. **Never count a cost twice**: cache_creation belongs to the write rate, not
   additionally to the input rate.
8. **Local time for display** (`astimezone()`), UTC internally.
9. **Changing the cache schema means bumping `CACHE_VERSION`** in
   `aggregate.py` (and `PARSE_VERSION` in `session_cache.py` for the parse
   cache).
10. **Do not re-measure the history by hand.** The measured state of the local
    history lives in `ARCHITECTURE.md` ("Measured state of the history") and
    is regenerated in ~5s by `tools/measure_facts.py`. A hand-written full
    scan over ~680 MB of JSONL costs minutes: that already destroyed 24
    minutes of analysis once, when an agent died on an API error and the
    numbers existed only in its context.
11. **Write long analyses to disk as you go.** Anyone researching, planning or
    measuring for more than a few minutes puts intermediate results in a file
    (a scratch directory, not the repo) instead of holding them in the answer
    text. Code on disk survives an abort — findings in context do not.
12. **mtime is not activity, and "the model of a session" does not exist.**
    Both are shortcuts that are measurably wrong in this history: 54 of 108
    transcripts have an mtime ahead of their newest content (resume touches
    the file, metadata lines carry no `timestamp`), and sessions run for weeks
    across several models. Activity comes from the last timestamped entry, the
    model label from `by_model`, weighted by output (ARCHITECTURE.md §5, §6c).
13. **A session name has three sources and a precedence.** `custom-title`
    (typed) → `agent-name` → `ai-title`, decided in
    `claude_parser.session_title` (ARCHITECTURE.md §5a). All three repeat per
    turn and appear interleaved, so "last line wins" is not a rule but an
    accident. `Session.name` is a property over the three slots — do not
    assign it, and do not build a fourth fallback into a view; extend the
    function instead. What to show when there is no name at all is
    `display_label()`, also in `claude_parser` — one ladder, not one per call
    site.

## Commands

```bash
./bin/cstats                     # start the TUI (venv bootstraps itself)
.venv/bin/python smoke_test.py     # headless smoke test
.venv/bin/python test_compacts.py
.venv/bin/python test_session_cache.py
.venv/bin/python -m cstats --line   # one-line mode
.venv/bin/python -m cstats --json   # JSON dump
.venv/bin/python tools/measure_facts.py   # re-measure the history (~5s)
```

## Workflows (`.claude/workflows/`)

Multi-agent runs, only to be started when explicitly asked.

| Workflow | For | Agents |
|---|---|---|
| `assembly-line` | a real code change: architect designs, 1-3 implementers build, a reviewer verifies live, then the test commands run | 3-6 |
| `repo-audit` | a read-only sweep (security, architecture drift, correctness) plus synthesis; changes nothing | 4 |
| `brainstorm` | before `assembly-line`, when the approach is still open: scout, 3 proposals, judge | 5 |

None of them commits or pushes — that stays with the caller. `assembly-line`
verifies with `smoke_test.py`, `test_compacts.py` and `test_session_cache.py`
by default; pass something else via `args.verify_cmd`.

## Conventions

- Views are **pure functions**: `render_X(dashboard) -> renderable`, no state,
  no I/O. `check_views_do_no_io()` enforces it.
- A failing source means an empty panel, never an exception escaping upwards.
  Loaders catch everything and return empty stats — but a failure must stay
  distinguishable from an empty result (status `error` vs. `empty`), and
  anything dropped along the way is reported rather than silently subtracted.
- Rich for everything that is displayed, Textual only for the app shell and
  the modals.
- No new runtime dependencies without a good reason (currently: textual,
  rich — HTTP goes through urllib from the stdlib).
- Cache files are written through `config.open_private()` (0600).

## Background

Three documents, three roles:

- `CONTEXT.md` — **where we stand**: current branch/commit, what the last
  session did, what is open. Update it after any meaningful commit, keep it to
  ~50 lines, delete what is resolved instead of appending.
- `ARCHITECTURE.md` — **why and how**: requirements, non-goals, the measured
  state of the history, modules, data flow, design decisions. If you change
  the data flow, the dedupe logic, caching or the refresh model, update it in
  the same commit.
- `AGENTS.md` — this file: the rules themselves.

Documentation and commits are written in English.
