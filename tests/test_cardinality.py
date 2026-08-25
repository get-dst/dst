"""Measured join cardinality — the check that keeps the compiler's trust honest."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import duckdb
import pytest

from services.semantic.cardinality import Measured, measure, verdict


@pytest.fixture
def execute() -> Iterator[Any]:
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id INT, customer_id INT, region_id INT)")
    con.execute("INSERT INTO orders VALUES (1,10,1),(2,10,1),(3,11,2)")
    con.execute("CREATE TABLE customers (customer_id INT, name TEXT)")
    con.execute("INSERT INTO customers VALUES (10,'a'),(11,'b')")
    # A composite key: unique on (ticker, day), duplicated on either column alone.
    con.execute("CREATE TABLE prices (ticker TEXT, day TEXT, price DOUBLE)")
    con.execute("INSERT INTO prices VALUES ('BTC','d1',1),('ETH','d1',2),('BTC','d2',3)")
    con.execute("CREATE TABLE empty_t (k INT)")
    yield lambda sql: con.sql(sql).fetchall()
    con.close()


def test_many_orders_to_one_customer(execute: Any) -> None:
    m = measure(
        execute,
        left_table="orders",
        left_columns=["customer_id"],
        right_table="customers",
        right_columns=["customer_id"],
    )
    assert m.relationship == "many_to_one"
    assert (m.left_max, m.right_max) == (2, 1)
    assert not m.fans_out_left


def test_the_reverse_direction_is_one_to_many(execute: Any) -> None:
    m = measure(
        execute,
        left_table="customers",
        left_columns=["customer_id"],
        right_table="orders",
        right_columns=["customer_id"],
    )
    assert m.relationship == "one_to_many"
    assert m.fans_out_left  # joining orders onto customers duplicates customers


def test_a_composite_key_is_measured_whole(execute: Any) -> None:
    """Regression: measuring ONE column of a composite key called a correct
    many_to_one declaration many-to-many on a shipped lens. `day` alone repeats;
    (ticker, day) does not."""
    single = measure(
        execute,
        left_table="orders",
        left_columns=["region_id"],
        right_table="prices",
        right_columns=["day"],
    )
    assert single.relationship == "many_to_many"  # what the wrong reading saw

    both = measure(
        execute,
        left_table="orders",
        left_columns=["region_id"],
        right_table="prices",
        right_columns=["ticker", "day"],
    )
    assert both.relationship == "many_to_one"
    assert both.right_max == 1


def test_an_empty_table_is_not_a_fan_out(execute: Any) -> None:
    m = measure(
        execute,
        left_table="orders",
        left_columns=["order_id"],
        right_table="empty_t",
        right_columns=["k"],
    )
    assert m.right_max == 0 and not m.fans_out_left


def test_an_unreadable_table_reports_rather_than_raises(execute: Any) -> None:
    m = measure(
        execute,
        left_table="orders",
        left_columns=["order_id"],
        right_table="no_such_table",
        right_columns=["k"],
    )
    assert m.relationship is None and m.error


# ── verdicts ─────────────────────────────────────────────────────────────────


def _m(rel: str, left_max: int, right_max: int) -> Measured:
    return Measured("l", "r", rel, left_max, right_max)


def test_a_confirmed_declaration_is_ok() -> None:
    status, message = verdict("many_to_one", _m("many_to_one", 5, 1))
    assert status == "ok" and "confirmed" in message


def test_claiming_safety_the_data_denies_is_wrong() -> None:
    """The dangerous direction: declared many_to_one, actually fans out — the
    compiler emits a direct join and every additive aggregate is multiplied."""
    status, message = verdict("many_to_one", _m("one_to_many", 1, 7))
    assert status == "wrong" and "MULTIPLIES" in message


def test_being_more_conservative_than_reality_costs_coverage_not_correctness() -> None:
    status, _message = verdict("one_to_many", _m("one_to_one", 1, 1))
    assert status == "ok"


def test_many_to_many_is_unsafe_and_unrepresentable() -> None:
    status, message = verdict("many_to_one", _m("many_to_many", 4, 3))
    assert status == "unsafe" and "no direct join is safe" in message


def test_an_undeclared_relationship_is_reported() -> None:
    status, message = verdict(None, _m("many_to_one", 3, 1))
    assert status == "wrong" and "no relationship declared" in message
