"""Cost computation: AI tokens -> USD, warehouse bytes -> USD.

Built-in list prices are a DATED FALLBACK (``AI_PRICES_AS_OF``), extended and
overridden by `ai_pricing` in dst.yaml or DST_AI_PRICING (JSON in the env/.env:
{"model": [usd_per_mtok_in, usd_per_mtok_out]}) — which is the authority, and the
only thing that can be right about a negotiated rate or a model newer than the
release. An unknown model's cost is None ("unpriced") — never silently billed at
some default price; the trace stores NULL and Observe renders the gap instead of
a made-up number.
"""

from __future__ import annotations

from services.config import settings

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
AI_PRICES_AS_OF = "2026-08-25"

# USD per 1M tokens: (input, output). Standard rates only — an introductory or
# promotional rate expires without the package knowing (Sonnet 5 runs $2/$10
# through 2026-08-31), and a rate that silently becomes wrong is the defect this
# table already had once.
_BUILTIN_AI_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    # DeepSeek (in-app product AI + benchmark workhorse), per api-docs 2026-06.
    "deepseek-v4-flash": (0.14, 0.28),
    # cache-miss rate; DeepSeek's cached-input tier (~$0.004/M) is not modeled
    "deepseek-v4-pro": (0.435, 0.87),
}

# BigQuery on-demand analysis pricing (USD per TB scanned).
BQ_USD_PER_TB = 6.25

# Connector kinds with no per-query cloud bill: in-process or self-hosted, so
# $0.00 is the truth of their marginal warehouse cost, not a measurement gap.
_FREE_WAREHOUSES = frozenset({"duckdb", "postgres", "mysql"})


def _prices() -> dict[str, tuple[float, float]]:
    merged = dict(_BUILTIN_AI_PRICES)
    from services.project.source import project_config

    proj = project_config()
    if proj is not None:
        for model, pair in proj.ai_pricing.items():
            merged[model] = (float(pair[0]), float(pair[1]))
    for model, pair in settings.ai_pricing.items():  # env wins over the project file
        merged[model] = (float(pair[0]), float(pair[1]))
    return merged


def ai_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """USD for one call, or None when the model has no configured price.

    Zero tokens is $0 at ANY price — no lookup. This is what lets a fully
    deterministic serve (FixedSQLGenerator's "certified" pseudo-model,
    stored prose, the template frame) trace ``ai_cost_usd: 0.0`` instead of
    "unpriced": a request that made no LLM call has a known cost, and None
    there would hide the certified door's headline property."""
    if input_tokens == 0 and output_tokens == 0:
        return 0.0
    price = _prices().get(model)
    if price is None:
        return None
    price_in, price_out = price
    return round(input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out, 6)


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
