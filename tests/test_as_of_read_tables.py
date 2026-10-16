"""The data's age is the age of what the answer READ.

`data_as_of` was the oldest table in the whole lens, so a question over
yesterday's orders was dated to a reference table loaded last spring. Now the
pipeline dates an answer from the
tables the guard resolved in the served SQL — a time-series table decides, a
dimension (no declared clock) only when nothing else was read — and names the
table on the stamp, so the date is attributable at a glance.
"""

from __future__ import annotations

from datetime import UTC, datetime

from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.profile import TableProfile
from services.contracts.warehouse import QueryResult
from services.lenses import profile_enrich
from services.lenses.demo import jaffle_customer_value
from services.lenses.profile_enrich import ReadTable, scope_as_of
from services.runtime.answer import AnswerComposer
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import run_query

STAMPS = {"customers": "2026-06-01", "orders": "2026-06-08", "payments": "2026-06-05"}
ORDERS = ReadTable(table="orders", name="orders", dated=True)
PAYMENTS = ReadTable(table="payments", name="payments", dated=True)
CUSTOMERS = ReadTable(table="customers", name="customers", dated=False)


def test_scope_as_of_is_the_oldest_table_read_and_names_it() -> None:
    assert scope_as_of(STAMPS, [ORDERS]) == ("2026-06-08", "orders")
    # Several time-series tables: the oldest of THEM, named.
    assert scope_as_of(STAMPS, [ORDERS, PAYMENTS]) == ("2026-06-05", "payments")
    # Nothing read, or nothing profiled: no claim, never the lens-wide floor.
    assert scope_as_of(STAMPS, []) == (None, None)
    assert scope_as_of(STAMPS, [ReadTable(table="other", name="other", dated=True)]) == (
        None,
        None,
    )


def test_a_dimension_never_dates_an_answer_unless_it_is_all_that_was_read() -> None:
    # customers is older than orders and is ignored next to a time series…
    assert scope_as_of(STAMPS, [ORDERS, CUSTOMERS]) == ("2026-06-08", "orders")
    # …and dates the answer only when it is the only table read.
    assert scope_as_of(STAMPS, [CUSTOMERS]) == ("2026-06-01", "customers")


def test_table_as_of_applies_the_time_field_rule_per_table() -> None:
    profiles = [
        TableProfile(
            connection="w",
            table="orders",
            last_updated_logical=datetime(2026, 6, 8, tzinfo=UTC),
            last_updated_physical=datetime(2026, 6, 9, tzinfo=UTC),
        ),
        TableProfile(
            connection="w",
            table="customers",
            last_updated_logical=datetime(2026, 3, 1, tzinfo=UTC),  # a signup date, not a load
            last_updated_physical=datetime(2026, 6, 9, tzinfo=UTC),
        ),
    ]
    stamps = profile_enrich.table_as_of(profiles, {"orders": "order_date", "customers": None})
    assert stamps == {"orders": "2026-06-08", "customers": "2026-06-09"}


def _serve(sql: str, prose: str) -> str:
    res = run_query(
        question="how many?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),  # customers: no clock · orders: order_date
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[42]])),
        generator=FixedSQLGenerator(sql),
        composer=AnswerComposer(ScriptedLLM([prose])),
        data_as_of="2026-06-01",  # the lens-wide floor: customers, the slowest table
        table_as_of={"customers": "2026-06-01", "orders": "2026-06-08"},
    )
    assert res.response.answer is not None
    return res.response.answer


def test_the_answer_is_dated_from_the_table_it_read_and_names_it() -> None:
    answer = _serve("SELECT count(*) AS n FROM orders", "There are 42 orders.")
    assert "(data as of 2026-06-08, orders)" in answer
    assert "2026-06-01" not in answer  # the lens-wide floor is not this answer's date


def test_a_join_to_a_dimension_keeps_the_time_series_date() -> None:
    answer = _serve(
        "SELECT count(*) AS n FROM orders o JOIN customers c ON c.customer_id = o.customer_id",
        "There are 42 orders.",
    )
    assert "(data as of 2026-06-08, orders)" in answer


def test_a_dimension_only_read_is_dated_from_the_dimension() -> None:
    answer = _serve("SELECT count(*) AS n FROM customers", "There are 42 customers.")
    assert "(data as of 2026-06-01, customers)" in answer


def test_the_freshness_check_names_the_table() -> None:
    from services.runtime.verification import freshness_check

    model = jaffle_customer_value().model_copy(update={"stale_after_days": 1})
    check = freshness_check("2020-01-01", model, table="orders")
    assert check.status == "fail" and check.reason is not None
    assert check.reason.startswith("data in orders is as of 2020-01-01")


def test_the_served_freshness_reason_names_the_table_the_stamp_names() -> None:
    """The check reads the same table scope_as_of chose for the stamp: a
    stale orders read fails freshness naming orders, not the lens floor."""
    model = jaffle_customer_value().model_copy(update={"stale_after_days": 1})
    res = run_query(
        question="how many?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=model,
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[42]])),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM orders"),
        composer=AnswerComposer(ScriptedLLM(["There are 42 orders."])),
        data_as_of="2020-01-01",
        table_as_of={"customers": "2020-01-01", "orders": "2020-01-08"},
    )
    assert res.response.verification is not None
    check = res.response.verification.check("freshness")
    assert check is not None and check.status == "fail" and check.reason is not None
    assert check.reason.startswith("data in orders is as of 2020-01-08")
    # the in-band note carries the same table and date; the stamp is not
    # repeated after it
    assert "Note: data in orders is as of 2020-01-08" in res.response.answer


def test_a_future_newest_time_value_dates_the_answer_to_today() -> None:
    """A bookings table whose newest row lies ahead of the clock is "as of"
    today, never the future date: the stamp, the freshness read and the
    prose all clamp, and the hidden horizon is said beside the stamp."""
    from services.lenses.profile_enrich import clamp_as_of, lens_today

    model = jaffle_customer_value().model_copy(update={"stale_after_days": 3})
    today = lens_today(model.timezone).isoformat()
    ahead = "2099-11-30"
    assert clamp_as_of(ahead, lens_today(model.timezone)) == (today, ahead)
    assert clamp_as_of("2026-06-08", lens_today(model.timezone)) == ("2026-06-08", None)
    res = run_query(
        question="how many?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=model,
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[42]])),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM orders"),
        composer=AnswerComposer(ScriptedLLM(["There are 42 orders."])),
        data_as_of=ahead,
        table_as_of={"customers": "2026-06-01", "orders": ahead},
    )
    assert res.response.answer is not None
    assert f"(data as of {today}, orders; rows dated to {ahead})" in res.response.answer
    assert res.response.data_as_of == today
    assert res.response.verification is not None
    check = res.response.verification.check("freshness")
    assert check is not None and check.status == "pass"
