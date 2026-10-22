"""Cost computation: AI tokens -> USD, warehouse bytes -> USD.

Built-in list prices are a DATED FALLBACK (``AI_PRICES_AS_OF``), extended and
overridden by `ai_pricing` in dst.yaml or DST_AI_PRICING (JSON in the env/.env:
{"model": [usd_per_mtok_in, usd_per_mtok_out]}, optionally followed by the
prompt-cache read, write and long-lived write rates) — which is the authority, and
the only thing that can be right about a negotiated rate or a model newer than the
release. An unknown model's cost is None ("unpriced") — never silently billed at
some default price; the trace stores NULL and Observe renders the gap instead of
a made-up number. The same holds for a call that reports prompt-cache tokens the
model has no cache rate for: the call is unpriced, never priced as if the cache
tokens were free.

Input tokens are the UNCACHED input throughout: a provider client records cache
reads and writes apart from them (Anthropic reports it that way; an OpenAI-style
client subtracts them from ``prompt_tokens``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from services.config import settings

if TYPE_CHECKING:
    from services.contracts.protocols import LLMResult

# The date this table was checked against the vendors' own pricing pages. It is
# part of the data, not a comment: a price list inside a shipped package is a
# FALLBACK with a shelf life, and dst cannot keep up with vendor pricing —
# `claude-opus-4-8` sat here at the previous generation's $15/$75 (3x the real
# rate) long enough to make every Opus trace and every Observe cost KPI overstate
# spend, and it read exactly like a correct number the whole time.
#
# Declaring `ai_pricing` in dst.yaml (or DST_AI_PRICING) is the authority, and it
# is the right home for a rate that is negotiated, discounted, or newer than the
# release. This list only keeps a keyless install's traces from reading NULL.
AI_PRICES_AS_OF = "2026-10-22"

# USD per 1M tokens: (input, output), the standard (not batch, not cached) rate.
_BUILTIN_AI_PRICES: dict[str, tuple[float, float]] = {
    # Anthropic, from https://platform.claude.com/docs/en/about-claude/pricing
    # ("Model pricing", read 2026-10-22); API ids from
    # https://platform.claude.com/docs/en/about-claude/models/overview.
    # Sonnet 5's launch rate of $2/$10 is now its standard price (same page).
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    # Prompts up to 100,000 tokens; longer ones are priced from _LONG_PROMPT.
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
    # OpenAI, from https://developers.openai.com/api/docs/pricing (standard
    # tier, read 2026-10-22).
    "gpt-6-astra": (10.0, 50.0),
    "gpt-6.1-sol": (2.0, 10.0),
    "gpt-6-sol": (2.0, 10.0),
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.4": (2.50, 15.0),
    "gpt-5.4-pro": (30.0, 180.0),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.2": (1.75, 14.0),
    "gpt-5.2-pro": (21.0, 168.0),
    "gpt-5.1": (1.25, 10.0),
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-pro": (15.0, 120.0),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "o3": (2.0, 8.0),
    "o3-pro": (20.0, 80.0),
    "o4-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),
    # DeepSeek (in-app product AI + benchmark workhorse), per api-docs 2026-06.
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (0.435, 0.87),
    # typesafe.ai's typed-decision model, per its usage page 2026-09-17: input
    # only, output free. A decision's tokens are its option set.
    "jev-latest": (0.042, 0.0),
    # Over chat completions (the OpenAI page above). Its Decisions API rate
    # is an endpoint price, _ENDPOINT_PRICES below.
    "gpt-6-luna": (0.10, 0.50),
}

# USD per 1M prompt-cache tokens: (cache read, write, long-lived write), each
# None where the page lists no rate, so a call reporting such tokens is
# unpriced. Anthropic's write is the 5-minute cache, its long-lived write the
# 1-hour one ("Cache hits and refreshes", "5m cache writes", "1h cache writes"
# columns of the Anthropic page above, read 2026-10-22). OpenAI's read is the
# "Cached input" column of its page above; its write is the 30-minute cache,
# the long-lived one the 12-hour cache, listed only for gpt-6-luna (read
# 2026-10-22). A model with no entry has no cache price.
_BUILTIN_CACHE_PRICES: dict[str, tuple[float | None, float | None, float | None]] = {
    "claude-fable-5-1": (0.25, 12.50, 20.0),
    "claude-fable-5": (1.0, 12.50, 20.0),
    "claude-opus-5-5": (0.20, 5.0, 8.0),
    "claude-opus-5": (0.50, 6.25, 10.0),
    "claude-opus-4-8": (0.50, 6.25, 10.0),
    "claude-opus-4-7": (0.50, 6.25, 10.0),
    "claude-opus-4-6": (0.50, 6.25, 10.0),
    "claude-opus-4-5": (0.50, 6.25, 10.0),
    "claude-sonnet-5-5": (0.10, 2.50, 4.0),
    "claude-sonnet-5": (0.20, 2.50, 4.0),
    "claude-sonnet-4-6": (0.30, 3.75, 6.0),
    "claude-sonnet-4-5": (0.30, 3.75, 6.0),
    "claude-haiku-5-5": (0.01, 0.125, 0.20),
    "claude-haiku-4-5": (0.10, 1.25, 2.0),
    "gpt-6-astra": (1.0, None, None),
    "gpt-6.1-sol": (0.10, None, None),
    "gpt-6-luna": (0.01, 0.125, 0.25),
    "gpt-6-sol": (0.20, None, None),
    "gpt-5.6-sol": (0.40, None, None),
    "gpt-5.6-terra": (0.20, None, None),
    "gpt-5.6-luna": (0.02, None, None),
    "gpt-5.5": (0.50, None, None),
    "gpt-5.4": (0.25, None, None),
    "gpt-5.4-mini": (0.075, None, None),
    "gpt-5.4-nano": (0.02, None, None),
    "gpt-5.2": (0.175, None, None),
    "gpt-5.1": (0.125, None, None),
    "gpt-5": (0.125, None, None),
    "gpt-5-mini": (0.025, None, None),
    "gpt-5-nano": (0.005, None, None),
    "gpt-4.1": (0.50, None, None),
    "gpt-4.1-mini": (0.10, None, None),
    "gpt-4.1-nano": (0.025, None, None),
    "gpt-4o": (1.25, None, None),
    "gpt-4o-mini": (0.075, None, None),
    "o3": (0.50, None, None),
    "o4-mini": (0.275, None, None),
    "o3-mini": (0.55, None, None),
    # DeepSeek reports its cache hits (prompt_tokens_details.cached_tokens),
    # but its page prices them in CNY by time of day, which this table does
    # not model. No rate here: a DeepSeek call that reports cache hits is
    # unpriced and named, never priced at a guessed rate. An install that
    # knows its rate declares it in `ai_pricing`.
}

# Models whose rate steps up past a prompt size: model -> (prompt tokens above
# which the call is long, (input, output), (cache read, write, long-lived
# write) for a long call). A prompt is its input and cache tokens together.
# Same pages: Claude Haiku 5.5 over 100,000 tokens; gpt-6-luna over 272K.
_LONG_PROMPT: dict[
    str, tuple[int, tuple[float, float], tuple[float | None, float | None, float | None]]
] = {
    "claude-haiku-5-5": (100_000, (0.50, 2.50), (0.05, 0.625, 1.0)),
    "gpt-6-luna": (272_000, (0.20, 0.75), (0.02, 0.25, 0.50)),
}

# Prices that depend on the endpoint a model is called through, not the model
# alone: (endpoint, model) -> (input, output). The endpoint is the head of a
# decider's provider id (``openai-decisions:gpt-6-luna``). OpenAI's Decisions
# API guide (https://developers.openai.com/api/docs/guides/decisions, read
# 2026-10-22): gpt-6-luna input at $0.10/1M, "no cache-read, cache-write, or
# output-token charges", long-context input pricing applies (the pricing
# page's $0.20 over 272K). Over chat completions the same model is priced
# from the tables above.
_ENDPOINT_PRICES: dict[tuple[str, str], tuple[float, float]] = {
    ("openai-decisions", "gpt-6-luna"): (0.10, 0.0),
}
_ENDPOINT_CACHE_PRICES: dict[tuple[str, str], tuple[float | None, float | None, float | None]] = {
    ("openai-decisions", "gpt-6-luna"): (0.0, 0.0, 0.0),
}
_ENDPOINT_LONG_PROMPT: dict[
    tuple[str, str],
    tuple[int, tuple[float, float], tuple[float | None, float | None, float | None]],
] = {
    ("openai-decisions", "gpt-6-luna"): (272_000, (0.20, 0.0), (0.0, 0.0, 0.0)),
}

# A pinned snapshot id carries a date the price list does not
# (`claude-haiku-4-5-20251001`): priced as its model.
_SNAPSHOT = re.compile(r"-\d{8}$")

# BigQuery on-demand analysis pricing (USD per TB scanned).
BQ_USD_PER_TB = 6.25

# Connector kinds with no per-query cloud bill: in-process or self-hosted, so
# $0.00 is the truth of their marginal warehouse cost, not a measurement gap.
_FREE_WAREHOUSES = frozenset({"duckdb", "postgres", "mysql"})


Cache = tuple[float | None, float | None, float | None]


def _declared() -> dict[str, tuple[float, ...]]:
    """The prices this install declares (`ai_pricing` in dst.yaml, then
    DST_AI_PRICING, which wins): ``[input, output]``, optionally followed by
    ``cache read, cache write, long-lived cache write``."""
    from services.project.source import project_config

    out: dict[str, tuple[float, ...]] = {}
    proj = project_config()
    if proj is not None:
        for model, rates in proj.ai_pricing.items():
            out[model] = tuple(float(r) for r in rates)
    for model, rates in settings.ai_pricing.items():  # env wins over the project file
        out[model] = tuple(float(r) for r in rates)
    return out


def _base(model: str) -> str:
    """*model* without a dated snapshot suffix (``claude-haiku-4-5-20251001``)."""
    return _SNAPSHOT.sub("", model)


def price_of(model: str) -> tuple[float, float] | None:
    """``(input, output)`` USD per 1M tokens for *model*, or None: declared
    first, then the built-in list, a dated snapshot id as its model."""
    declared = _declared()
    for name in (model, _base(model)):
        if name in declared:
            return declared[name][0], declared[name][1]
        if name in _BUILTIN_AI_PRICES:
            return _BUILTIN_AI_PRICES[name]
    return None


def cache_price_of(model: str) -> Cache | None:
    """``(cache read, write, long-lived write)`` USD per 1M tokens for *model*
    (each None where no rate is known), or None when it has none at all: the
    rates its `ai_pricing` entry declares, else the built-in ones."""
    declared = _declared()
    for name in (model, _base(model)):
        rates = declared.get(name)
        if rates is not None and len(rates) > 2:
            extra = (*rates[2:], None, None)
            return extra[0], extra[1], extra[2]
        if name in _BUILTIN_CACHE_PRICES:
            return _BUILTIN_CACHE_PRICES[name]
    return None


def unpriced(models: list[str]) -> list[str]:
    """The models in *models* with no price: each call to one records a NULL
    cost, and every total it is part of is a floor."""
    return sorted({m for m in models if m and price_of(m) is None})


def _rates(
    model: str, endpoint: str | None, prompt_tokens: int
) -> tuple[tuple[float, float], Cache | None] | None:
    """The ``(input, output)`` and cache rates one call is billed at: an
    `ai_pricing` entry for ``<endpoint>:<model>``, then the endpoint's own
    price, then the model's."""
    declared = _declared()
    rates = declared.get(f"{endpoint}:{model}") if endpoint else None
    if rates is not None:
        extra = (*rates[2:], None, None, None)
        return (rates[0], rates[1]), (extra[0], extra[1], extra[2]) if len(rates) > 2 else None
    key = (endpoint or "", _base(model))
    long = _ENDPOINT_LONG_PROMPT.get(key)
    if key in _ENDPOINT_PRICES:
        if long is not None and prompt_tokens > long[0]:
            return long[1], long[2]
        return _ENDPOINT_PRICES[key], _ENDPOINT_CACHE_PRICES.get(key)
    price = price_of(model)
    if price is None:
        return None
    long = _LONG_PROMPT.get(_base(model))
    # A declared price is the install's own rate: no list long-prompt step on it.
    own = model in declared or _base(model) in declared
    if long is not None and prompt_tokens > long[0] and not own:
        return long[1], long[2]
    return price, cache_price_of(model)


def ai_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_long_tokens: int = 0,
    endpoint: str | None = None,
) -> float | None:
    """USD for one call, or None when the model has no configured price.

    *input_tokens* is the uncached input; *cache_write_tokens* every token
    written to the prompt cache, *cache_write_long_tokens* the part of them
    written to the long-lived cache (Anthropic's 1-hour, OpenAI's 12-hour).
    A call whose cache tokens have no rate is unpriced, like one whose model
    has none. *endpoint* names what the model was called through, where that
    changes its price (``openai-decisions``).

    Zero tokens is $0 at ANY price — no lookup. This is what lets a fully
    deterministic serve (FixedSQLGenerator's "certified" pseudo-model,
    stored prose, the template frame) trace ``ai_cost_usd: 0.0`` instead of
    "unpriced": a request that made no LLM call has a known cost, and None
    there would hide the certified door's headline property."""
    if not (input_tokens or output_tokens or cache_read_tokens or cache_write_tokens):
        return 0.0
    prompt = input_tokens + cache_read_tokens + cache_write_tokens
    found = _rates(model, endpoint, prompt)
    if found is None:
        return None
    (price_in, price_out), cache = found
    read, write, write_long = cache or (None, None, None)
    long_writes = min(cache_write_long_tokens, cache_write_tokens)
    billed = [
        (input_tokens, price_in),
        (output_tokens, price_out),
        (cache_read_tokens, read),
        (cache_write_tokens - long_writes, write),
        (long_writes, write_long),
    ]
    if any(tokens and rate is None for tokens, rate in billed):
        return None
    return round(sum(tokens / 1e6 * (rate or 0.0) for tokens, rate in billed if tokens), 6)


def result_cost_usd(model: str, result: LLMResult) -> float | None:
    """USD for the call that returned *result*, its prompt-cache tokens included."""
    return ai_cost_usd(
        model,
        result.input_tokens,
        result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_creation_tokens,
        cache_write_long_tokens=result.cache_write_long_tokens,
    )


_PARAM = re.compile(r"^[a-z_]+=")


def decision_endpoint(provider: str) -> str:
    """The head of a decider's provider id: ``openai-decisions`` in
    ``openai-decisions:gpt-6-luna``, ``vote`` in ``vote:<model>:k=5``."""
    return provider.split(":", 1)[0]


def decision_model(provider: str) -> str:
    """The priceable model name inside a decider's provider id:
    ``vote:deepseek-v4-flash:k=5`` → deepseek-v4-flash, ``typesafe:jev-latest``
    → jev-latest, ``openai-compatible:llama3:8b`` → llama3:8b. The id is the
    endpoint, the model and the decider's own ``name=value`` settings, and a
    model id may itself contain a colon, so only the head and those trailing
    settings are cut."""
    head, sep, rest = provider.partition(":")
    if not sep:
        return provider
    parts = rest.split(":")
    while len(parts) > 1 and _PARAM.match(parts[-1]):
        parts.pop()
    return ":".join(parts)


def decisions_cost_usd(usage_by_provider: dict[str, tuple[float, float]]) -> float | None:
    """USD for a set of decisions, given per-provider (input, output) token
    sums. None when any provider is unpriced — never a partial sum shown as
    the total. No tokens at all is $0."""
    total: float | None = 0.0
    for provider, (tokens_in, tokens_out) in usage_by_provider.items():
        total = add_cost(
            total,
            ai_cost_usd(
                decision_model(provider),
                int(tokens_in),
                int(tokens_out),
                endpoint=decision_endpoint(provider),
            ),
        )
    return total


def add_cost(total: float | None, delta: float | None) -> float | None:
    """Accumulate call costs; one unpriced call makes the whole trace unpriced
    (a partial sum shown as the total would be a silent lie)."""
    return None if total is None or delta is None else total + delta


def warehouse_cost_usd(bytes_scanned: int | None, kind: str) -> float | None:
    """USD for one query's warehouse scan, or None when it cannot be priced.

    Pricing is a property of the warehouse's billing model, so the connector
    kind is REQUIRED — pricing every dialect's bytes at BigQuery's $/TB priced
    Snowflake at $0.00 (its connector reports no bytes, and its bill is
    credit-seconds, not bytes). Same doctrine as ai_cost_usd: unknown is None
    ("unpriced"), never a made-up number — observe renders the gap.
    """
    if kind in _FREE_WAREHOUSES:
        return 0.0
    if kind == "bigquery":
        if bytes_scanned is None:
            return None
        return round(bytes_scanned / 1e12 * BQ_USD_PER_TB, 6)
    return None  # snowflake (credit-seconds) and any unmodelled kind: unpriced
