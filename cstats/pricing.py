"""Model pricing table ($ per MTok) and cost calculation.

Costs are hypothetical API-equivalent values derived from the standard
Anthropic price list, matching the reference claude-code-usage-dashboard.
"""

# model id -> (input, output, cache_read, cache_write_1h, display)
_PRICING = {
    "claude-fable-5": (10.00, 50.00, 1.00, 20.00, "Fable 5"),
    "claude-mythos-5": (10.00, 50.00, 1.00, 20.00, "Mythos 5"),
    "claude-opus-5": (5.00, 25.00, 0.50, 10.00, "Opus 5"),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 10.00, "Opus 4.8"),
    "claude-opus-4-7": (5.00, 25.00, 0.50, 10.00, "Opus 4.7"),
    "claude-opus-4-6": (5.00, 25.00, 0.50, 10.00, "Opus 4.6"),
    "claude-opus-4-5-20251101": (5.00, 25.00, 0.50, 10.00, "Opus 4.5"),
    "claude-opus-4-1-20250805": (15.00, 75.00, 1.50, 30.00, "Opus 4.1"),
    "claude-opus-4-20250514": (15.00, 75.00, 1.50, 30.00, "Opus 4"),
    # Sonnet 5 launched on introductory pricing; see _DATED_PRICING below —
    # the entry here is the list price that applies once it ends.
    "claude-sonnet-5": (3.00, 15.00, 0.30, 6.00, "Sonnet 5"),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 6.00, "Sonnet 4.6"),
    "claude-sonnet-4-5-20250929": (3.00, 15.00, 0.30, 6.00, "Sonnet 4.5"),
    "claude-sonnet-4-20250514": (3.00, 15.00, 0.30, 6.00, "Sonnet 4"),
    "claude-haiku-4-5-20251001": (1.00, 5.00, 0.10, 2.00, "Haiku 4.5"),
    "claude-3-5-haiku-20241022": (0.80, 4.00, 0.08, 1.60, "Haiku 3.5"),
    "claude-3-7-sonnet-20250219": (3.00, 15.00, 0.30, 6.00, "Sonnet 3.7"),
    "claude-3-5-sonnet-20241022": (3.00, 15.00, 0.30, 6.00, "Sonnet 3.5"),
}

# Prices that only applied for a period. A call is billed at the price in
# force when it was made, so the parser passes each line's timestamp in; a
# table without dates silently re-prices the whole history the day an
# introductory rate ends (Sonnet 5 would have jumped 50% overnight, on a
# history where it accounts for a fifth of the output).
#
#   model -> ((start, end_exclusive), (input, output, cache_read, cache_write_1h))
_DATED_PRICING = {
    "claude-sonnet-5": ((None, "2026-09-01"), (2.00, 10.00, 0.20, 4.00)),
}

# default fallback when model unknown
_FALLBACK = (3.00, 15.00, 0.30, 6.00, "Unknown")

# Anthropic credit weighting: credit = USD / MTok * 2/15 (matches reference tool)
CREDIT_FACTOR = 2.0 / 15.0

# context window sizes per model family; newer "-5" models have 1M windows
_CONTEXT_WINDOWS = {
    "claude-fable-5": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
}
_DEFAULT_WINDOW = 200_000


_WINDOW_STEPS = (200_000, 1_000_000)


def context_window(model_id, observed_tokens=0):
    """Context window size for a model. Env CSTATS_CONTEXT_WINDOW wins.

    If observed_tokens exceeds the table value (e.g. a model got a bigger
    window after our table was written), bump to the next known step so the
    percentage never exceeds 100.
    """
    import os
    env = os.environ.get("CSTATS_CONTEXT_WINDOW")
    if env and env.isdigit():
        return int(env)
    size = _DEFAULT_WINDOW
    if model_id:
        for prefix, s in _CONTEXT_WINDOWS.items():
            if model_id.startswith(prefix):
                size = s
                break
    if observed_tokens > size:
        for step in _WINDOW_STEPS:
            if step >= observed_tokens:
                return step
        return observed_tokens  # bigger than any known window
    return size


def price_for(model_id, when=None):
    """Return (input, output, cache_read, cache_write_1h, display) for a model.

    The 5-tuple shape is load-bearing: callers unpack it positionally and
    `display_name` indexes [4]. A cache-write price for another TTL comes from
    `cache_write_price`, not from a wider tuple.

    `when` is the moment the call was made (a datetime or an ISO date string).
    It selects an introductory price that was in force then. Omitted, the
    current date applies — right for a live quote, wrong for a historical
    call, which is why the parser passes the line's own timestamp.
    """
    base = _PRICING.get(model_id, _FALLBACK)
    dated = _DATED_PRICING.get(model_id)
    if not dated:
        return base
    (start, end), prices = dated
    day = _as_day(when)
    if (start is None or day >= start) and (end is None or day < end):
        return prices + (base[4],)
    return base


def _as_day(when):
    """A YYYY-MM-DD string for a datetime, a string, or None (= today)."""
    if when is None:
        from datetime import datetime as _dt
        return _dt.now().astimezone().strftime("%Y-%m-%d")
    if isinstance(when, str):
        return when[:10]
    return when.astimezone().strftime("%Y-%m-%d")


def display_name(model_id):
    """Return a human display name, falling back to the raw id."""
    return _PRICING.get(model_id, _FALLBACK)[4]


# prompt-cache writes are billed as a multiple of the input price; the table
# above stores the 1h value, which is exactly 2.0x input for every entry
CACHE_WRITE_MULTIPLIER_5M = 1.25


def cache_write_price(model_id, ttl="1h", when=None):
    """Price per MTok for a prompt-cache write with the given TTL.

    Anthropic bills a cache write as a multiple of the model's input price:
    1.25x for the 5-minute TTL, 2.0x for the 1-hour TTL. Transcripts report the
    split in `message.usage.cache_creation`. Only the 5m case is derived here;
    the 1h price comes straight from the table, so a hand-corrected table entry
    keeps winning over the multiplier.

    Any ttl other than "5m" is treated as 1h — that is the conservative
    (more expensive) side, and matches transcripts that carry no breakdown.
    """
    i, _o, _cr, cw_1h, _ = price_for(model_id, when)
    if ttl == "5m":
        return i * CACHE_WRITE_MULTIPLIER_5M
    return cw_1h


def calc_cost(model_id, input_tokens, output_tokens, cache_read, cache_write,
              cache_write_5m=0, when=None):
    """Compute hypothetical API cost in USD for a single call.

    `cache_write` is always the total number of cache-creation tokens (i.e.
    `cache_creation_input_tokens`). `cache_write_5m` is the share of that total
    billed at the cheaper 5-minute TTL rate; the remainder is billed at the
    1-hour rate. Callers that do not know the breakdown omit the argument and
    get the previous, 1h-only behaviour.
    """
    i, o, cr, cw_1h, _ = price_for(model_id, when)
    w5 = cache_write_5m if cache_write_5m > 0 else 0
    if w5 > cache_write:
        w5 = cache_write  # a reported split must never exceed the reported total
    w1 = cache_write - w5
    return (input_tokens * i + output_tokens * o + cache_read * cr
            + w1 * cw_1h + w5 * cache_write_price(model_id, "5m", when)) / 1_000_000


def calc_credits(model_id, input_tokens, output_tokens, cache_read, cache_write,
                 cache_write_5m=0, when=None):
    """Credit consumption of a single call (basis for the 5h/7d limit windows).

    Returns a float on purpose. A credit is nothing but the dollar amount on a
    different scale, and a scale factor must not be applied-and-rounded per
    summand: almost every single call costs far less than the $7.50 that make
    one credit, so rounding here turned ~1180 credits of history into 183.
    Round once, at display time.
    """
    usd = calc_cost(model_id, input_tokens, output_tokens, cache_read, cache_write,
                    cache_write_5m, when)
    return usd * CREDIT_FACTOR
