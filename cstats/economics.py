"""What the context window actually costs, and when compacting pays off.

Every turn re-sends the whole conversation. With prompt caching the prefix is
billed as cache_read, so the input cost of a turn is essentially

    context_tokens x cache_read_rate

which means a long-running session pays for its entire history again on every
single turn. That is why cache_read dominates a heavy user's bill (~69% of it
in the history this was written against) while the tokens a turn actually adds
are a rounding error.

Compacting resets the context to a summary, so the per-turn cost drops back to
near zero. It is not free — the compaction call reads the full context once,
generates the summary, and the new prefix has to be cached again at the
cache_write rate (~21x the read rate) — but the payback is a handful of turns:
measured against real rates, a 268k-token context breaks even after ~5 turns,
an 800k one after ~3.

All rates are derived from what was actually billed (cost / tokens), never
from a price list, so a mixed-model history prices itself correctly.
"""

# A fresh post-compaction context: the summary plus system prompt, CLAUDE.md,
# tool definitions, MCP schemas and the last few messages Claude Code keeps.
#
# Measured, not assumed: over 43 compactions in the reference history the first
# billed assistant call after a compact boundary carried a median of 62,942
# tokens (min 47,644, max 104,283). Rounded to 60k as the fallback when a
# history has too few compactions of its own to measure (see compacts.py).
#
# Note which number this is NOT. Claude Code's own `compactMetadata.postTokens`
# has a median of ~16.8k for the same events — it counts only the conversation
# that survived the compaction, while the system prompt, CLAUDE.md, the tool
# definitions and the MCP schemas are re-sent and re-billed on top of it. Using
# postTokens here understates the post-compact context by a factor of ~3.7 and
# makes every payback estimate look better than it is.
POST_COMPACT_TOKENS = 60_000

# Tokens the compaction call itself generates for the summary. Measured: the
# summary message has a median length of 18,563 characters, i.e. ~4.6k tokens
# at ~4 chars/token.
SUMMARY_OUTPUT_TOKENS = 5_000

# Characters per token when only a text length is known. Lives here because
# both this module and compacts.py need it, and compacts.py already imports
# this one — the dependency must not run both ways.
CHARS_PER_TOKEN = 4


def billing_rates(d) -> dict:
    """Per-token USD rates actually paid, plus the cache reuse factor.

    `reuse` = cache_read / cache_write is exactly how often an average cached
    token is read again — the multiplier on everything that sits in context.

    The compaction parameters ride along in the same dict so callers do not
    have to know whether this history has measured compactions or not:

    - `post_compact` / `summary_out`: measured medians when `d.compacts` says
      so, otherwise the documented constants above;
    - `post_measured`: whether those two are measured or assumed, so the UI can
      word it honestly;
    - `auto_fill`: context-window share at which auto-compaction triggered.

    A dashboard without a `.compacts` attribute is fine — the constants apply.
    """
    c = getattr(d, "compacts", None)
    return {
        "read": d.cost_cache_read / d.total_cache_read if d.total_cache_read else 0.0,
        "write": d.cost_cache_write / d.total_cache_write if d.total_cache_write else 0.0,
        "output": d.cost_output / d.total_output if d.total_output else 0.0,
        "reuse": d.total_cache_read / d.total_cache_write if d.total_cache_write else 0.0,
        "post_compact": int(getattr(c, "post_compact_tokens", None) or POST_COMPACT_TOKENS),
        "summary_out": int(getattr(c, "summary_output_tokens", None) or SUMMARY_OUTPUT_TOKENS),
        "post_measured": bool(getattr(c, "measured", False)),
        "auto_fill": float(getattr(c, "auto_fill_ratio", None) or 0.98),
    }


def session_economics(tokens, rates, post_compact=None) -> dict:
    """Cost of carrying `tokens` of context, and the case for compacting.

    Returns per_turn / after / compact_cost / saved_per_turn / breakeven_turns.
    `breakeven_turns` is None when compacting cannot pay off (the context is
    already at or below what a fresh one would cost).

    `post_compact` resolves in that order: the explicit argument, then the
    measured value in `rates`, then the module constant. Existing two-argument
    callers therefore pick up a measured history automatically.
    """
    read = rates.get("read", 0.0)
    if post_compact is None:
        post_compact = rates.get("post_compact") or POST_COMPACT_TOKENS
    summary_out = rates.get("summary_out") or SUMMARY_OUTPUT_TOKENS
    per_turn = tokens * read
    post = min(post_compact, tokens)
    after = post * read
    # reading the whole context once + writing the summary + re-caching the
    # new prefix (a cache write costs ~21x a read, so it is not negligible)
    compact_cost = (tokens * read
                    + summary_out * rates.get("output", 0.0)
                    + post * rates.get("write", 0.0))
    saved = per_turn - after
    breakeven = compact_cost / saved if saved > 0 else None
    return {
        "tokens": tokens,
        "per_turn": per_turn,
        "after": after,
        "compact_cost": compact_cost,
        "saved_per_turn": saved,
        "breakeven_turns": breakeven,
    }


def _field(ev, name, default=None):
    """Read `name` off an event object or dict, tolerating None values."""
    if isinstance(ev, dict):
        value = ev.get(name, default)
    else:
        value = getattr(ev, name, default)
    return default if value is None else value


def compact_ledger(ev, rates) -> dict:
    """What one compaction that really happened cost, and what it bought.

    Unlike `session_economics` (a forecast for a context still running), this
    is retrospective: `ev` is a `compacts.CompactEvent`, so the context before
    and after, the summary length and the number of turns that followed are all
    measured rather than assumed.

    `avoided_usd_upper` is named `_upper` on purpose. It prices the tokens the
    compaction dropped against every turn that followed, which is the *upper
    bound* of a counterfactual, not a saving: summed over the reference
    history's 43 compactions it comes to ~$8k against a total bill of $8.9k,
    and the scenario it prices was physically impossible — the window was full,
    so those turns could not have run at all without compacting. Report it as a
    ceiling on avoided cost, never as money saved.

    Tolerates missing fields (a boundary on the first line of a resumed session
    has no context before it) and never raises.
    """
    read = rates.get("read", 0.0)
    write = rates.get("write", 0.0)
    out_rate = rates.get("output", 0.0)

    ctx_before = _field(ev, "ctx_before", 0) or _field(ev, "pre_tokens", 0)
    # NOT `post_tokens` as the fallback: that is exactly the number documented
    # above as understating the post-compact context by ~3.7x. When the real
    # reading is missing, the measured median is the honest stand-in.
    ctx_after = _field(ev, "ctx_after", 0) or (rates.get("post_compact")
                                               or POST_COMPACT_TOKENS)
    dropped = _field(ev, "dropped_tokens", 0)
    if not dropped:
        dropped = max(0, ctx_before - ctx_after)
    chars = _field(ev, "summary_chars", 0)
    summary_out = (chars // CHARS_PER_TOKEN if chars
                   else (rates.get("summary_out") or SUMMARY_OUTPUT_TOKENS))
    turns_after = _field(ev, "turns_after", 0)

    saved_per_turn = dropped * read
    compact_cost = ctx_before * read + summary_out * out_rate + ctx_after * write
    breakeven = compact_cost / saved_per_turn if saved_per_turn > 0 else None
    payback = turns_after / breakeven if breakeven else None
    return {
        "dropped": dropped,
        "saved_per_turn": saved_per_turn,
        "compact_cost": compact_cost,
        "breakeven_turns": breakeven,
        "turns_after": turns_after,
        "payback_x": payback,
        "avoided_usd_upper": dropped * read * turns_after,
    }


def runway(tokens, growth_per_turn, threshold, rates) -> dict:
    """Turns and dollars left before a context hits `threshold`.

    The cost is an arithmetic series, not `tokens x turns`: the context grows
    with every turn, so each turn is billed on a larger prefix than the last.
    Averaging start and end is exact for a constant growth rate:

        usd_until = read x turns_left x (tokens + threshold) / 2

    `turns_left` / `usd_until` are None when there is no positive growth rate
    or the threshold is already behind us.
    """
    read = rates.get("read", 0.0)
    tokens = tokens or 0
    threshold = threshold or 0
    tokens_left = max(0, threshold - tokens)
    turns_left = None
    usd_until = None
    if tokens_left > 0 and growth_per_turn and growth_per_turn > 0:
        turns_left = tokens_left / growth_per_turn
        usd_until = read * turns_left * (tokens + threshold) / 2.0
    return {
        "tokens_left": tokens_left,
        "turns_left": turns_left,
        "usd_until": usd_until,
    }


def advice(econ, alert_usd, max_breakeven=25) -> str:
    """'compact' | 'watch' | '' — whether this session is worth acting on.

    'compact': expensive per turn AND the compaction pays for itself quickly.
    'watch':   getting expensive but not yet worth the interruption.

    `max_breakeven` was 15 while POST_COMPACT_TOKENS was 20k. Measuring the
    post-compact context at 60k tripled the floor a compaction can reach, so
    every break-even figure rose with it and the old bound silently pushed the
    advice later. Recalibrated against the reference bill (cache_read
    $0.52/MTok, write 21.5x read):

        post-compact  bound  verdict fires from
        20k           15     70,269 tokens
        60k           15    174,537 tokens   <- the silent shift
        60k           25    126,813 tokens   <- chosen

    Note which limit actually binds: with the default alert of $0.10/turn the
    verdict cannot fire below ~193,700 tokens anyway, where break-even is 13
    turns, so at the default this bound is slack. It binds for anyone who
    lowered `context_alert_usd` into the $0.065-$0.090 range — which is exactly
    the user who asked to be told earlier.

    Contexts at or below the post-compact size still return '' (nothing is
    saved by compacting them, `saved == 0`), and that is correct.
    """
    per_turn = econ["per_turn"]
    be = econ["breakeven_turns"]
    if be is None:
        return ""
    if per_turn >= alert_usd and be <= max_breakeven:
        return "compact"
    if per_turn >= alert_usd / 2:
        return "watch"
    return ""
