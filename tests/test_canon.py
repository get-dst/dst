"""One canonical reading of a result cell, on every rail that compares a
stored value with a live one.

The eval gate learned to read "82.59" and ``Decimal("82.59")`` as one number
and "2026-09-30T00:00:00" and "2026-09-30 00:00:00" as one day; the serve-time
``restated`` check kept its own comparison and reported a DuckDB TIMESTAMP cell
as a restatement on every certified serve (grade capped at partial, a RESTATED
line over nothing moved). Both rails read through ``services.runtime.canon``
now, and ``--refresh-samples`` compares the same way so a stored form that
differs only in key order never prints as a change.
"""

from __future__ import annotations

import datetime as dt
import decimal

from services.contracts.warehouse import QueryResult
from services.runtime import verification
from services.runtime.canon import canon_cell, canon_summary
from services.runtime.timewindow import breakdown_unit


def test_timestamp_spellings_and_exact_numerics_are_not_restatements() -> None:
    stored = {
        "columns": ["month", "net_revenue"],
        "rows": [["2026-09-30T00:00:00", 82.59], ["2026-08-31T00:00:00", "26.5649377153854187"]],
    }
    live = QueryResult(
        columns=["month", "net_revenue"],
        rows=[
            [dt.datetime(2026, 8, 31), decimal.Decimal("26.5649377153854187")],
            [dt.datetime(2026, 9, 30), decimal.Decimal("82.59")],
        ],
    )
    assert verification.restatement(stored, live) is None
    check, moved = verification.restated_check(stored, live)
    assert check.status == "pass" and moved is None
    # the other spelling, and a DATE against its midnight timestamp
    text_form = QueryResult(
        columns=["month", "net_revenue"],
        rows=[["2026-08-31 00:00:00", 26.5649377153854187], ["2026-09-30 00:00:00", 82.59]],
    )
    assert verification.restatement(stored, text_form) is None
    assert (
        verification.restatement(
            {"value": "2026-09-30T00:00:00"},
            QueryResult(columns=["d"], rows=[[dt.date(2026, 9, 30)]]),
        )
        is None
    )


def test_a_moved_timestamp_cell_is_still_a_restatement() -> None:
    stored = {"columns": ["month", "v"], "rows": [["2026-09-30T00:00:00", 1]]}
    live = QueryResult(columns=["month", "v"], rows=[[dt.datetime(2026, 10, 31), 1]])
    moved = verification.restatement(stored, live)
    assert moved == ("month 2026-09-30T00:00:00", "month 2026-10-31 00:00:00")
    # a number past its printed precision still moves
    assert verification.restatement(
        {"value": 82.59}, QueryResult(columns=["v"], rows=[[decimal.Decimal("82.61")]])
    ) == ("82.59", "82.61")


def test_canon_summary_ignores_key_order_row_order_and_stored_text_forms() -> None:
    before = {"rows": [["2026-09-30T00:00:00", "82.59"]], "columns": ["Month", "v"]}
    after = {"columns": ["month", "v"], "rows": [[dt.datetime(2026, 9, 30), 82.59]]}
    assert canon_summary(before) == canon_summary(after)
    assert canon_summary({"value": "7"}) == canon_summary({"value": 7})
    assert canon_summary({"value": 7}) != canon_summary({"value": 8})
    assert canon_summary(None) is None and canon_summary({}) is None
    assert canon_cell(True) is True and canon_cell(None) is None


def test_month_end_phrasings_read_as_a_month_bucket() -> None:
    for q in (
        "what were weekly active users at the end of each month of 2026",
        "MRR at the end of each month of 2026, one row per month",
        "open AR per month-end",
        "show MRR monthly for 2026",
        "what is ARR monthly?",
    ):
        assert breakdown_unit(q) == "month", q
    assert breakdown_unit("headcount at the end of each week in Q3") == "week"
    assert breakdown_unit("ARR each quarter") == "quarter"
    # the adverb before a noun names a metric, not a series
    assert breakdown_unit("monthly recurring revenue in 2026") is None
    assert breakdown_unit("weekly active users in 2026") is None
