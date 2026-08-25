"""Cost attribution — configured prices, honest None for unpriced models."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.config import settings
from services.observability.cost import add_cost, ai_cost_usd, warehouse_cost_usd


def test_known_model_prices() -> None:
    assert ai_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000) == 18.0
    assert ai_cost_usd("deepseek-v4-flash", 1_000_000, 0) == 0.14


def test_unknown_model_is_unpriced_not_defaulted() -> None:
    assert ai_cost_usd("llama3-self-hosted", 1_000_000, 1_000_000) is None


def test_ai_pricing_setting_extends_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "ai_pricing", {"llama3-self-hosted": (0.0, 0.0), "claude-sonnet-4-6": (1.0, 1.0)}
    )
    assert ai_cost_usd("llama3-self-hosted", 5_000_000, 5_000_000) == 0.0
    assert ai_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000) == 2.0


def test_one_unpriced_call_makes_the_trace_unpriced() -> None:
    total: float | None = 0.0
    total = add_cost(total, 0.5)
    total = add_cost(total, None)  # an unpriced call taints the sum — no partial lies
    total = add_cost(total, 0.25)
    assert total is None


def test_warehouse_bytes_price_by_billing_model() -> None:
    # 1 TB at the BigQuery on-demand rate; the sql-probe path prices through the
    # same function as the lens-query path, so bytes are never priced at $0.00.
    assert warehouse_cost_usd(1_000_000_000_000, "bigquery") == 6.25
    assert warehouse_cost_usd(0, "bigquery") == 0.0  # a cache hit bills 0 bytes
    # In-process / self-hosted: $0.00 is the truth, not a gap.
    assert warehouse_cost_usd(None, "duckdb") == 0.0
    assert warehouse_cost_usd(None, "postgres") == 0.0


def test_unmeasurable_warehouse_cost_is_unpriced_never_zero() -> None:
    # Snowflake bills credit-seconds the cursor can't see; pricing its queries
    # at $0.00 (the pre-kind behavior) understated real spend exactly like the
    # unpriced-AI-model case. Same doctrine: unknown is None, observe shows it.
    assert warehouse_cost_usd(None, "snowflake") is None
    assert warehouse_cost_usd(123_456, "snowflake") is None  # no $/byte model exists
    assert warehouse_cost_usd(None, "bigquery") is None  # bytes lost -> unpriced
    assert warehouse_cost_usd(None, "someday-new-kind") is None  # unmodelled: no default rate


def test_sql_probe_trace_prices_its_bytes() -> None:
    # The seam itself: api/sql.py must never write wh_bytes without wh_cost_usd.
    source = (Path(__file__).resolve().parents[1] / "services" / "api" / "sql.py").read_text(
        encoding="utf-8"
    )
    assert "warehouse_cost_usd" in source, "probe trace records bytes it never prices"
