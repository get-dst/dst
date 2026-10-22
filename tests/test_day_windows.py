"""A day, or a span of days, written in words is a day window.

"on 28 August 2026" read as "August 2026" served the whole month for a one-day
question, and `window_applied` passed it because the SQL did carry a date
filter. The parse reads the day; the check fails a served window wider than
the days asked, read off the SQL's own date literals.
"""

from __future__ import annotations

from datetime import date

import pytest

from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.contracts.warehouse import QueryResult
from services.runtime.timewindow import (
    month_end_as_month,
    temporal_mismatch,
    temporal_terms,
    unplaced_days,
    unplaced_months,
    vague_days,
    window_ranges,
)
from services.runtime.typed_resolver import _Clarify, window_filters
from services.runtime.verification import build_report

TODAY = date(2026, 10, 22)


def _range(text: str) -> list[tuple[date, date]]:
    return window_ranges(temporal_terms(text), TODAY)


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("total lift-pass sales on 15 October 2026", date(2026, 10, 15), date(2026, 10, 15)),
        ("checkouts on 28 August 2026", date(2026, 8, 28), date(2026, 8, 28)),
        ("checkouts 28 Aug 2026", date(2026, 8, 28), date(2026, 8, 28)),
        ("checkouts on August 28, 2026", date(2026, 8, 28), date(2026, 8, 28)),
        ("on the 28th of August 2026", date(2026, 8, 28), date(2026, 8, 28)),
        ("between 5 and 12 October 2026", date(2026, 10, 5), date(2026, 10, 12)),
        ("between 20 and 28 March 2026", date(2026, 3, 20), date(2026, 3, 28)),
        ("from 5 to 12 October 2026", date(2026, 10, 5), date(2026, 10, 12)),
        ("5–12 Oct 2026", date(2026, 10, 5), date(2026, 10, 12)),
        ("October 5 to 12, 2026", date(2026, 10, 5), date(2026, 10, 12)),
        ("from 28 September to 3 October 2026", date(2026, 9, 28), date(2026, 10, 3)),
        ("between 28 December and 3 January 2027", date(2026, 12, 28), date(2027, 1, 3)),
        ("from 2026-03-20 to 2026-03-28", date(2026, 3, 20), date(2026, 3, 28)),
        ("the week of 5 October 2026", date(2026, 10, 5), date(2026, 10, 11)),
        ("the week of October 7, 2026", date(2026, 10, 5), date(2026, 10, 11)),
        ("the first half of August 2026", date(2026, 8, 1), date(2026, 8, 15)),
        ("the second half of February 2026", date(2026, 2, 16), date(2026, 2, 28)),
    ],
)
def test_a_day_written_in_words_is_that_day(text: str, start: date, end: date) -> None:
    assert _range(text) == [(start, end)]
    assert unplaced_months(text, temporal_terms(text)) == []


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        # on or before the lens clock: this year's
        ("lift-pass sales on 28 August", date(2026, 8, 28), date(2026, 8, 28)),
        ("between 5 and 12 October", date(2026, 10, 5), date(2026, 10, 12)),
        ("from 5 to 12 October", date(2026, 10, 5), date(2026, 10, 12)),
        ("5–12 Oct", date(2026, 10, 5), date(2026, 10, 12)),
        ("Oct 5-12", date(2026, 10, 5), date(2026, 10, 12)),
        ("the week of 5 October", date(2026, 10, 5), date(2026, 10, 11)),
        # after the lens clock: the most recent one is last year's
        ("on 28 December", date(2025, 12, 28), date(2025, 12, 28)),
        ("between 28 December and 3 January", date(2025, 12, 28), date(2026, 1, 3)),
    ],
)
def test_a_day_without_a_year_is_the_most_recent_one(text: str, start: date, end: date) -> None:
    assert _range(text) == [(start, end)]


def test_a_number_that_counts_is_not_a_day_and_may_stays_a_verb() -> None:
    month = [(date(2026, 8, 1), date(2026, 8, 31))]
    assert _range("top 10 August 2026 deals") == month
    assert _range("which 5 may churn in August 2026") == month
    assert _range("in August 2026") == month
    assert _range("on 5 May 2026") == [(date(2026, 5, 5), date(2026, 5, 5))]


def test_a_day_that_does_not_exist_resolves_to_nothing() -> None:
    # unresolved, so the typed path clarifies — never a nearby day
    assert _range("on 30 February 2026") == []
    assert _range("between 12 and 5 October 2026") == []


def test_a_day_is_not_the_month_a_certified_answer_was_approved_for() -> None:
    assert temporal_mismatch("lift-pass sales on 28 August 2026", "lift-pass sales in August 2026")
    assert not temporal_mismatch(
        "lift-pass sales on 28 August 2026", "lift-pass sales on 2026-08-28"
    )


# ── the typed window ─────────────────────────────────────────────────────────


def _entity(time_type: str) -> Entity:
    # a "string" time type is a month-form period column (`month = '2026-08'`),
    # which is never the entity's declared time field
    return Entity(
        name="lift_passes",
        source=EntitySource(connection="wh", table="lift_passes"),
        default_time_field=None if time_type == "string" else "day",
        fields=[Field(name="day", type=time_type), Field(name="amount", type="number")],
        metrics=[Metric(name="lift_pass_sales", agg="sum", expr="amount")],
    )


def _filters(question: str, time_type: str = "date", period: str | None = None):  # noqa: ANN202
    return [
        (f.field, f.op, f.value)
        for f in window_filters(
            question, _entity(time_type), ["lift_pass_sales"], period_field=period, today=TODAY
        )
    ]


def test_a_day_window_compiles_to_that_day() -> None:
    assert _filters("lift-pass sales on 15 October 2026") == [
        ("day", ">=", "2026-10-15"),
        ("day", "<=", "2026-10-15"),
    ]
    assert _filters("lift-pass sales between 5 and 12 October") == [
        ("day", ">=", "2026-10-05"),
        ("day", "<=", "2026-10-12"),
    ]
    # a timestamp closes the last day at the next midnight
    assert _filters("checkouts on 28 August 2026", "timestamp") == [
        ("day", ">=", "2026-08-28"),
        ("day", "<", "2026-08-29"),
    ]


def test_a_day_window_on_a_month_period_field_clarifies() -> None:
    # `month = '2026-08'` would be the month's figure for a one-day question
    with pytest.raises(_Clarify) as raised:
        _filters("lift-pass sales on 28 August 2026", "string", period="month")
    assert "holds months" in raised.value.request.question
    assert _filters("lift-pass sales in August 2026", "string", period="month") == [
        ("month", "=", "2026-08")
    ]


# ── window_applied ───────────────────────────────────────────────────────────


def _check(question: str, sql: str, dialect: str = "duckdb"):  # noqa: ANN202
    model = SemanticModel(lens="t", dialect=dialect, entities=[_entity("date")])
    return build_report(
        question=question,
        sql=sql,
        answer_text="",
        result=QueryResult(columns=["lift_pass_sales"], rows=[[1.0]]),
        semantic_model=model,
        definition_used=None,
        truncated=False,
        certification="none",
        composed=False,
        uncomposed_reason="rows-only test",
    )


MONTH_SQL = (
    "SELECT SUM(amount) FROM lift_passes "
    "WHERE lift_passes.day >= CAST('2026-10-01' AS DATE) "
    "AND lift_passes.day <= CAST('2026-10-31' AS DATE)"
)


def test_a_month_served_for_a_day_fails_the_window() -> None:
    report = _check("total lift-pass sales on 15 October 2026", MONTH_SQL)
    check = report.check("window_applied")
    assert check is not None and check.status == "fail"
    assert "2026-10-15 to 2026-10-15" in (check.reason or "")
    assert "day 2026-10-01 to 2026-10-31" in (check.reason or "")
    assert report.grade != "verified"
    ranged = _check("lift-pass sales between 5 and 12 October 2026", MONTH_SQL).check(
        "window_applied"
    )
    assert ranged is not None and ranged.status == "fail"


@pytest.mark.parametrize(
    "where",
    [
        "day BETWEEN '2026-08-01' AND '2026-08-31'",
        "month = '2026-08'",
        "DATE_TRUNC('month', day) = DATE '2026-08-01'",
        "created_at >= TIMESTAMP '2026-08-01' AND created_at < TIMESTAMP '2026-09-01'",
    ],
)
def test_every_spelling_of_the_month_fails_a_day_ask(where: str) -> None:
    check = _check("checkouts on 28 August 2026", f"SELECT COUNT(*) FROM t WHERE {where}").check(
        "window_applied"
    )
    assert check is not None and check.status == "fail", where


@pytest.mark.parametrize(
    "where",
    [
        "day >= CAST('2026-08-28' AS DATE) AND day <= CAST('2026-08-28' AS DATE)",
        "created_at >= CAST('2026-08-28' AS TIMESTAMP) AND created_at < "
        "CAST('2026-08-29' AS TIMESTAMP)",
        "day = '2026-08-28'",
        "day BETWEEN '2026-08-28' AND '2026-08-28'",
        # not literal bounds: not read, never guessed
        "day = CURRENT_DATE - INTERVAL 55 DAY",
        # under an OR the bounds are not the rows' bounds
        "(day >= '2026-08-01' OR day IS NULL) AND day <= '2026-08-31'",
        # a second column bounded wider does not hide the right one
        "day = '2026-08-28' AND signup_date BETWEEN '2026-01-01' AND '2026-12-31'",
    ],
)
def test_the_asked_day_or_an_unreadable_bound_passes(where: str) -> None:
    check = _check("checkouts on 28 August 2026", f"SELECT COUNT(*) FROM t WHERE {where}").check(
        "window_applied"
    )
    assert check is not None and check.status == "pass", (where, check.reason)


def test_a_month_ask_served_as_its_month_still_passes() -> None:
    check = _check("total lift-pass sales in October 2026", MONTH_SQL).check("window_applied")
    assert check is not None and check.status == "pass"


# ── parts of a month ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("the first 7 days of October 2026", date(2026, 10, 1), date(2026, 10, 7)),
        ("first 7 days of October", date(2026, 10, 1), date(2026, 10, 7)),
        ("the last 3 days of August", date(2026, 8, 29), date(2026, 8, 31)),
        ("the last 3 days of February 2026", date(2026, 2, 26), date(2026, 2, 28)),
        ("the first week of August 2026", date(2026, 8, 1), date(2026, 8, 7)),
        ("the last week of August 2026", date(2026, 8, 25), date(2026, 8, 31)),
        ("the last day of February 2026", date(2026, 2, 28), date(2026, 2, 28)),
        ("at the start of August 2026", date(2026, 8, 1), date(2026, 8, 1)),
        ("at the beginning of October", date(2026, 10, 1), date(2026, 10, 1)),
        ("at the end of August 2026", date(2026, 8, 31), date(2026, 8, 31)),
        ("at the end of 2026-08", date(2026, 8, 31), date(2026, 8, 31)),
        ("at the end of last month", date(2026, 9, 30), date(2026, 9, 30)),
        ("at the start of this month", date(2026, 10, 1), date(2026, 10, 1)),
        ("on 28 August in 2026", date(2026, 8, 28), date(2026, 8, 28)),
    ],
)
def test_a_definite_part_of_a_month_is_its_days(text: str, start: date, end: date) -> None:
    assert _range(text) == [(start, end)]
    assert unplaced_days(text, TODAY) == []


def test_a_level_metric_at_a_month_s_end_is_the_month_s_last_snapshot() -> None:
    assert window_ranges(month_end_as_month(temporal_terms("end of August 2026")), TODAY) == [
        (date(2026, 8, 1), date(2026, 8, 31))
    ]
    assert window_ranges(month_end_as_month(temporal_terms("end of last month")), TODAY) == [
        (date(2026, 9, 1), date(2026, 9, 30))
    ]


def test_a_vague_part_of_a_month_offers_its_days_and_is_never_a_window() -> None:
    assert [(a, b) for _p, a, b in vague_days("early August", TODAY)] == [
        (date(2026, 8, 1), date(2026, 8, 10))
    ]
    assert [(a, b) for _p, a, b in vague_days("mid-August 2026", TODAY)] == [
        (date(2026, 8, 11), date(2026, 8, 20))
    ]
    assert [(a, b) for _p, a, b in vague_days("late February 2026", TODAY)] == [
        (date(2026, 2, 21), date(2026, 2, 28))
    ]
    assert vague_days("the middle of August 2026", TODAY)


@pytest.mark.parametrize(
    "text",
    [
        "the first few days of August 2026",
        "the first days of August 2026",
        "the week of August 2026",
        "the 3rd week of August 2026",
        "early August",
        "late August 2026",
    ],
)
def test_a_day_qualifier_the_parser_could_not_place_is_unplaced(text: str) -> None:
    assert unplaced_days(text, TODAY)


@pytest.mark.parametrize(
    "text",
    [
        "revenue in August 2026",
        "accounts that start in August 2026",
        "deals that end in August 2026",
        "number of active days in August 2026",
        "customers at the end of every month",
        "last August",
    ],
)
def test_a_month_without_a_day_qualifier_is_not_unplaced(text: str) -> None:
    assert unplaced_days(text, TODAY) == []


def test_parts_of_a_month_compile_or_clarify_never_the_month() -> None:
    assert _filters("lift-pass sales in the first 7 days of October") == [
        ("day", ">=", "2026-10-01"),
        ("day", "<=", "2026-10-07"),
    ]
    assert _filters("lift-pass sales at the end of August 2026") == [
        ("day", ">=", "2026-08-31"),
        ("day", "<=", "2026-08-31"),
    ]
    with pytest.raises(_Clarify) as vague:
        _filters("lift-pass sales in mid-August 2026")
    assert vague.value.request.options == ["2026-08-11/2026-08-20"]
    assert "2026-08-11 to 2026-08-20" in vague.value.request.question
    with pytest.raises(_Clarify) as unplaced:
        _filters("lift-pass sales in the first few days of August 2026")
    assert "first few days of August 2026" in unplaced.value.request.question


def test_a_level_metric_at_the_end_of_a_month_keeps_the_month_window() -> None:
    entity = _entity("date")
    entity.metrics = [
        Metric(name="season_pass_holders", agg="sum", expr="amount", additivity="level")
    ]
    got = window_filters(
        "season-pass holders at the end of August 2026",
        entity,
        ["season_pass_holders"],
        period_field=None,
        today=TODAY,
    )
    assert [(f.op, f.value) for f in got] == [(">=", "2026-08-01"), ("<=", "2026-08-31")]


@pytest.mark.parametrize(
    "question",
    [
        "lift-pass sales in the first 7 days of October 2026",
        "lift-pass sales at the end of October 2026",
        "lift-pass sales in early October",
        "lift-pass sales in the first few days of October 2026",
    ],
)
def test_a_month_served_for_part_of_it_fails_the_window(question: str) -> None:
    check = _check(question, MONTH_SQL).check("window_applied")
    assert check is not None and check.status == "fail", question


def test_the_days_of_a_part_of_a_month_pass_the_window() -> None:
    sql = "SELECT SUM(amount) FROM lift_passes WHERE day >= '2026-10-01' AND day <= '2026-10-07'"
    check = _check("lift-pass sales in the first 7 days of October 2026", sql).check(
        "window_applied"
    )
    assert check is not None and check.status == "pass"
    early = _check("lift-pass sales in early October", sql).check("window_applied")
    assert early is not None and early.status != "fail"
