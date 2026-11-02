"""The checks an outside-lens answer carries beyond its grain line: row
conservation across a join, the clock named per time column read, and
reconciliation against a certified number over the same tables and window.
Each runs live over DuckDB; a check that cannot run says "could not close".
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from services.connectors.duckdb import DuckDBConnector
from services.contracts.semantic_model import SemanticModel
from services.contracts.verification import VerificationReport
from services.runtime import explore_checks as ec


@pytest.fixture
def shop(tmp_path: Path) -> str:
    path = str(tmp_path / "world.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE SCHEMA shop")
    con.execute(
        "CREATE TABLE shop.orders AS SELECT * FROM (VALUES "
        "(1, DATE '2026-03-01', 10.0), (2, DATE '2026-03-02', 20.0), (3, DATE '2026-03-03', 30.0)"
        ") AS t(order_id, ordered_on, amount)"
    )
    con.execute(
        "CREATE TABLE shop.items AS SELECT * FROM (VALUES "
        "(1, 'a'), (1, 'b'), (2, 'c'), (3, 'd'), (3, 'e')) AS t(order_id, sku)"
    )
    con.execute(
        "CREATE TABLE shop.customers AS SELECT * FROM (VALUES (1, 'x')) AS t(order_id, name)"
    )
    con.close()
    return path


def _model(**blueprint: object) -> SemanticModel:
    return SemanticModel.model_validate(
        {
            "lens": "shop",
            "dialect": "duckdb",
            "entities": [
                {
                    "name": "orders",
                    "source": {"connection": "wh", "table": "shop.orders"},
                    "exposed": True,
                    "fields": [
                        {"name": "order_id", "type": "number"},
                        {"name": "ordered_on", "type": "date"},
                        {"name": "amount", "type": "number"},
                    ],
                },
                {
                    "name": "items",
                    "source": {"connection": "wh", "table": "shop.items"},
                    "exposed": True,
                    "fields": [
                        {"name": "order_id", "type": "number"},
                        {"name": "sku", "type": "string"},
                    ],
                },
            ],
            "blueprints": {
                "shop.orders": {
                    "connection": "wh",
                    "table": "shop.orders",
                    "grain": "one order",
                    "key": ["order_id"],
                    **blueprint,
                }
            },
        }
    )


def test_a_join_that_multiplies_rows_is_disclosed_with_its_counts(shop: str) -> None:
    sql = "SELECT SUM(o.amount) FROM shop.orders o JOIN shop.items i ON i.order_id = o.order_id"
    check = ec.join_check(sql, _model(), DuckDBConnector(shop), connection="wh")
    assert check is not None and check.status == "disclose", check
    assert check.mode == "wrong_grain"
    assert "5 rows from 3 rows of shop.orders" in check.detail
    assert check.line.startswith("join:disclose (wrong_grain): ")


def test_a_join_that_keeps_the_base_rows_passes(shop: str) -> None:
    sql = "SELECT COUNT(*) FROM shop.orders o LEFT JOIN shop.customers c ON c.order_id = o.order_id"
    check = ec.join_check(sql, _model(), DuckDBConnector(shop), connection="wh")
    assert check is not None and check.status == "pass", check
    assert "one row per row of shop.orders (3)" in check.detail


def test_a_statement_without_a_join_has_no_conservation_to_check(shop: str) -> None:
    assert (
        ec.join_check("SELECT COUNT(*) FROM shop.orders", _model(), None, connection="wh") is None
    )


def test_a_join_over_a_subquery_cannot_close(shop: str) -> None:
    sql = (
        "SELECT COUNT(*) FROM shop.orders o JOIN (SELECT order_id FROM shop.items) i "
        "ON i.order_id = o.order_id"
    )
    check = ec.join_check(sql, _model(), DuckDBConnector(shop), connection="wh")
    assert check is not None and check.status == "skip"
    assert check.detail.startswith("could not close: ") and check.mode == "unverifiable"
    with_cte = (
        "WITH i AS (SELECT order_id FROM shop.items) SELECT COUNT(*) FROM shop.orders o "
        "JOIN i ON i.order_id = o.order_id"
    )
    cte = ec.join_check(with_cte, _model(), DuckDBConnector(shop), connection="wh")
    assert cte is not None and cte.status == "skip" and "CTE" in cte.detail


def test_the_clock_is_named_from_the_blueprint_or_said_to_be_missing() -> None:
    sql = "SELECT COUNT(*) FROM shop.orders WHERE ordered_on >= DATE '2026-03-01'"
    declared = _model(
        clocks={"ordered_on": {"meaning": "when the order was placed", "timezone": "UTC"}}
    )
    (check,) = ec.clock_checks(sql, declared, ["shop.orders"])
    assert check.status == "pass"
    assert check.detail == "shop.orders.ordered_on records when the order was placed, stored in UTC"
    (missing,) = ec.clock_checks(sql, _model(), ["shop.orders"])
    assert missing.status == "skip" and "no clock is declared" in missing.detail
    # A time column the statement does not read is not a clock it needs.
    assert ec.clock_checks("SELECT COUNT(*) FROM shop.orders", declared, ["shop.orders"]) == []


def test_reconcile_computes_the_certified_number_and_discloses_the_delta(shop: str) -> None:
    model, connector = _model(), DuckDBConnector(shop)
    mine = "SELECT SUM(amount) FROM shop.orders WHERE ordered_on <= DATE '2026-03-03'"
    certified = [
        (
            "revenue in march",
            "SELECT SUM(amount) AS revenue FROM shop.orders WHERE ordered_on <= DATE '2026-03-03'",
        ),
    ]
    same = ec.reconcile_check(mine, [[60.0]], certified, model, connector, connection="wh")
    assert (
        same.status == "pass"
        and "matches the certified answer to 'revenue in march'" in same.detail
    )
    off = ec.reconcile_check(mine, [[66.0]], certified, model, connector, connection="wh")
    assert off.status == "disclose" and off.mode == "unverifiable"
    assert "this statement 66, certified 60, delta +6 (+10.0%)" in off.detail


def test_reconcile_says_when_no_governed_number_exists(shop: str) -> None:
    model = _model()
    other_window = [
        (
            "revenue in april",
            "SELECT SUM(amount) FROM shop.orders WHERE ordered_on >= DATE '2026-04-01'",
        )
    ]
    check = ec.reconcile_check(
        "SELECT SUM(amount) FROM shop.orders", [[60.0]], other_window, model, None, connection="wh"
    )
    assert check.status == "skip"
    assert (
        check.detail
        == "could not close: no certified answer of this lens reads shop.orders with no window"
    )
    none = ec.reconcile_check(
        "SELECT sku FROM shop.items", [["a"]], [], model, None, connection="wh"
    )
    assert "returns no number" in none.detail


def test_folding_caps_a_verified_grade_and_never_raises_one() -> None:
    report = VerificationReport(grade="verified", checks=[])
    disclosed = ec.ExploreCheck("join", "disclose", "x", mode="wrong_grain")  # type: ignore[arg-type]
    folded = ec.fold(report, [disclosed])
    assert folded.grade == "partial" and folded.checks[-1].status == "fail"
    assert (
        ec.fold(
            VerificationReport(grade="unverified"), [ec.ExploreCheck("clock", "pass", "y")]
        ).grade
        == "unverified"
    )
    assert ec.lines([disclosed]) == ["JOIN: x"] and ec.mode_of([disclosed]) == "wrong_grain"
