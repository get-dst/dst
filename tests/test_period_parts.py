"""Quarters, halves and the end of a period, read as their own windows; a
window phrase is never a ranking or a grouping.

"the third quarter of 2026" and "the second half of 2026" were read as the
year, and `window_applied` passed the year's SQL. "the first 2 days of
October" was also read as a top 2, with an unasked grouping by day. The
parse reads quarters and halves in every spelling, a year-less one on the
lens clock, and the end of a period with none named as the last one that
has ended; the check fails a whole year served for part of one; the ranking
and grouping reads skip the words a window phrase consumed.
"""

from __future__ import annotations

from datetime import date

import pytest

from services.contracts.protocols import Decision, Option
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.contracts.warehouse import QueryResult
from services.runtime.timewindow import (
    beside_windows,
    month_end_as_month,
    temporal_terms,
    window_ranges,
)
from services.runtime.typed_resolver import TypedResolver, window_filters
from services.runtime.verification import build_report

TODAY = date(2026, 10, 22)


def _range(text: str, today: date = TODAY) -> list[tuple[date, date]]:
    return window_ranges(temporal_terms(text), today)


Q3, H2 = (date(2026, 7, 1), date(2026, 9, 30)), (date(2026, 7, 1), date(2026, 12, 31))


@pytest.mark.parametrize(
    ("text", "span"),
    [
        ("rides in the third quarter of 2026", Q3),
        ("rides in the 3rd quarter of 2026", Q3),
        ("rides in the third quarter, 2026", Q3),
        ("rides in Q3 2026", Q3),
        ("rides in Q3 of 2026", Q3),
        ("rides in 2026-Q3", Q3),
        ("revenue in the second half of 2026", H2),
        ("revenue in the 2nd half of 2026", H2),
        ("revenue in H2 2026", H2),
        ("revenue in H2 of 2026", H2),
        ("revenue in 2026-H2", H2),
        ("revenue in the first half of 2026", (date(2026, 1, 1), date(2026, 6, 30))),
        ("revenue in H1 2026", (date(2026, 1, 1), date(2026, 6, 30))),
        ("rides in the first quarter of 2026", (date(2026, 1, 1), date(2026, 3, 31))),
        ("rides in the fourth quarter of 2025", (date(2025, 10, 1), date(2025, 12, 31))),
        ("rides in the 4th quarter of 2025", (date(2025, 10, 1), date(2025, 12, 31))),
        ("rides in the last quarter of 2025", (date(2025, 10, 1), date(2025, 12, 31))),
        ("rides in the final quarter of 2025", (date(2025, 10, 1), date(2025, 12, 31))),
        ("rides in the last half of 2025", (date(2025, 7, 1), date(2025, 12, 31))),
        ("rides in the third quarter of this year", Q3),
        ("rides in the first half of last year", (date(2025, 1, 1), date(2025, 6, 30))),
    ],
)
def test_a_quarter_or_half_of_a_year_is_its_months(text: str, span: tuple[date, date]) -> None:
    assert _range(text) == [span]


@pytest.mark.parametrize(
    ("text", "today", "span"),
    [
        # started on or before the lens clock: this year's
        ("rides in the fourth quarter", TODAY, (date(2026, 10, 1), date(2026, 12, 31))),
        ("rides in the third quarter", TODAY, Q3),
        ("rides in the first half", TODAY, (date(2026, 1, 1), date(2026, 6, 30))),
        ("rides in the second half", TODAY, H2),
        ("rides in the first half of the year", TODAY, (date(2026, 1, 1), date(2026, 6, 30))),
        # not started yet: last year's
        ("rides in the fourth quarter", date(2026, 3, 10), (date(2025, 10, 1), date(2025, 12, 31))),
        ("rides in the second half", date(2026, 3, 10), (date(2025, 7, 1), date(2025, 12, 31))),
    ],
)
def test_a_quarter_or_half_without_a_year_is_the_most_recent_one(
    text: str, today: date, span: tuple[date, date]
) -> None:
    assert _range(text, today) == [span]


def test_what_is_not_a_quarter_or_half_of_a_year_stays_what_it_was() -> None:
    # the relative period, a half of a month, a half of something else
    assert _range("rides in the last quarter") == [Q3]
    assert _range("rides in the first half of August 2026") == [
        (date(2026, 8, 1), date(2026, 8, 15))
    ]
    assert temporal_terms("goals in the first half of the match") == frozenset()
    # a code without a year could be any year: still no range, so it asks
    assert _range("rides in Q3") == []
    assert _range("rides in H2") == []
    # both halves are the year
    assert sorted(_range("rides in H1 and H2 2026")) == [
        (date(2026, 1, 1), date(2026, 6, 30)),
        H2,
    ]


def _entity() -> Entity:
    return Entity(
        name="station_days",
        source=EntitySource(connection="wh", table="station_days"),
        default_time_field="day",
        primary_key=["day", "station_name"],
        fields=[
            Field(name="day", type="date"),
            Field(name="station_name", type="string"),
            Field(name="rentals", type="integer"),
            Field(name="balance", type="number"),
        ],
        metrics=[
            Metric(name="rides", agg="sum", expr="rentals"),
            Metric(name="bikes_docked", agg="sum", expr="balance", additivity="level"),
        ],
    )


def _filters(question: str, metric: str = "rides") -> list[tuple[str, str, object]]:
    got = window_filters(question, _entity(), [metric], period_field=None, today=TODAY)
    return [(f.field, f.op, f.value) for f in got]


def test_a_quarter_in_words_compiles_to_the_quarter() -> None:
    assert _filters("rides in the third quarter of 2026") == [
        ("day", ">=", "2026-07-01"),
        ("day", "<=", "2026-09-30"),
    ]
    assert _filters("rides in the second half of 2025") == [
        ("day", ">=", "2025-07-01"),
        ("day", "<=", "2025-12-31"),
    ]


# ── the end of a period with none named ─────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("bikes docked at the end of the month", date(2026, 9, 30)),
        ("bikes docked at end of month", date(2026, 9, 30)),
        ("bikes docked at month end", date(2026, 9, 30)),
        ("bikes docked at month-end", date(2026, 9, 30)),
        ("bikes docked at the close of the month", date(2026, 9, 30)),
        ("bikes docked at the end of the quarter", date(2026, 9, 30)),
        ("bikes docked at quarter-end", date(2026, 9, 30)),
        ("bikes docked at the end of the year", date(2025, 12, 31)),
        # a named period is unchanged
        ("bikes docked at the end of this month", date(2026, 10, 31)),
        ("bikes docked at the end of last month", date(2026, 9, 30)),
        ("bikes docked at the end of August 2026", date(2026, 8, 31)),
    ],
)
def test_the_end_of_a_period_with_none_named_is_the_last_completed_one(
    text: str, day: date
) -> None:
    assert _range(text) == [(day, day)]


def test_the_end_of_the_month_never_reads_the_current_month() -> None:
    # a level metric reads the last completed month's last snapshot
    terms = month_end_as_month(temporal_terms("bikes docked at the end of the month"))
    assert window_ranges(terms, TODAY) == [(date(2026, 9, 1), date(2026, 9, 30))]
    assert _filters("bikes docked at the end of the month", "bikes_docked") == [
        ("day", ">=", "2026-09-01"),
        ("day", "<=", "2026-09-30"),
    ]
    # the start of the month is the current one's first day
    assert _range("rides since the start of the month") == [(date(2026, 10, 1), date(2026, 10, 1))]


def test_month_end_beside_a_named_period_or_a_breakdown_is_no_day() -> None:
    assert _range("bikes docked at month end in August 2026") == [
        (date(2026, 8, 1), date(2026, 8, 31))
    ]
    assert temporal_terms("bikes docked per month end") == frozenset()
    assert temporal_terms("bikes docked at the end of each month") == frozenset()


# ── window_applied ───────────────────────────────────────────────────────────


def _check(question: str, sql: str, certification: str = "none"):  # noqa: ANN202
    model = SemanticModel(lens="bikes", dialect="duckdb", entities=[_entity()])
    return build_report(
        question=question,
        sql=sql,
        answer_text="",
        result=QueryResult(columns=["rides"], rows=[[1.0]]),
        semantic_model=model,
        definition_used=None,
        truncated=False,
        certification=certification,  # type: ignore[arg-type]
        composed=False,
        uncomposed_reason="rows-only test",
    ).check("window_applied")


def _sql(lo: str, hi: str) -> str:
    return (
        "SELECT SUM(rentals) FROM station_days WHERE station_days.day >= "
        f"CAST('{lo}' AS DATE) AND station_days.day <= CAST('{hi}' AS DATE)"
    )


YEAR_SQL = _sql("2026-01-01", "2026-12-31")


@pytest.mark.parametrize(
    "question",
    [
        "rides in the third quarter of 2026",
        "rides in Q3 2026",
        "rides in Q3",
        "rides in the second half of 2026",
        "rides in H2 2026",
        "rides in the last quarter of 2026",
        "rides in the fourth quarter",
        "rides in the first half",
        "rides in the third quarter of this year",
        "rides last quarter",
    ],
)
def test_a_whole_year_served_for_part_of_one_fails_the_window(question: str) -> None:
    check = _check(question, YEAR_SQL)
    assert check is not None and check.status == "fail", question
    assert "whole year" in (check.reason or "")
    every_spelling = (
        "SELECT SUM(rentals) FROM station_days WHERE day BETWEEN '2026-01-01' AND '2026-12-31'"
    )
    check = _check(question, every_spelling)
    assert check is not None and check.status == "fail", question


def test_the_part_asked_or_a_question_covering_the_year_passes() -> None:
    q3 = _check("rides in the third quarter of 2026", _sql("2026-07-01", "2026-09-30"))
    assert q3 is not None and q3.status == "pass"
    h2 = _check("rides in the second half of 2026", _sql("2026-07-01", "2026-12-31"))
    assert h2 is not None and h2.status == "pass"
    both = _check("rides in H1 and H2 2026", YEAR_SQL)
    assert both is not None and both.status == "pass"
    year = _check("rides in 2026", YEAR_SQL)
    assert year is not None and year.status == "pass"


# ── a window phrase is never a ranking nor a grouping ───────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "rides per station in the first 2 days of October 2026",
        "rides per station in the first 3 weeks of September 2026",
        "rides per station in the last 3 days of September 2026",
        "rides per station in the last 2 weeks of August",
        "rides per station in the first week of October 2026",
    ],
)
def test_the_number_in_a_window_phrase_is_no_limit(question: str) -> None:
    assert TypedResolver._order(question, ["rides"], []) == ([], None)
    assert "days" not in beside_windows(question) and "weeks" not in beside_windows(question)


def test_a_ranking_beside_a_window_keeps_its_own_number() -> None:
    order, limit = TypedResolver._order(
        "top 3 stations by rides in the first 2 days of October 2026", ["rides"], []
    )
    assert limit == 3 and [(o.field, o.dir) for o in order] == [("rides", "desc")]
    order, limit = TypedResolver._order("top 2 stations by rides", ["rides"], [])
    assert limit == 2 and [(o.field, o.dir) for o in order] == [("rides", "desc")]


class _Greedy:
    """A decider that groups by the station, then takes the day whenever it
    is offered as a further grouping — the lure the words "2 days" were."""

    def __init__(self, shape: str) -> None:
        self.shape = shape
        self.offered: list[list[str]] = []

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        names = [o.name for o in options]
        if "aggregate" in names:
            pick: str | None = self.shape
        elif "rides" in names:
            pick = "rides"
        elif "broken down by a dimension" in context:
            self.offered.append(names)
            if "Already chosen" in context:
                pick = next((n for n in ("day", "station_name") if n in names), None)
            else:
                pick = "station_name" if "station_name" in names else "day"
        else:
            pick = None if allow_none else names[0]
        return Decision(chosen=pick, probs={pick or "none": 1.0}, provider="scripted")


def _resolve(question: str, shape: str = "aggregate"):  # noqa: ANN202
    model = SemanticModel(lens="bikes", dialect="duckdb", entities=[_entity()])
    decider = _Greedy(shape)
    res = TypedResolver(decider, domains={}, today=TODAY).resolve(question, model)  # type: ignore[arg-type]
    return res, decider


@pytest.mark.parametrize(
    ("question", "shape", "limit"),
    [
        ("rides per station in the first 2 days of October 2026", "aggregate", None),
        ("rides per station in the last 3 days of September 2026", "aggregate", None),
        ("rides per station in the first 2 weeks of October 2026", "aggregate", None),
        ("top 3 stations by rides in the first 2 days of October 2026", "ranking", 3),
    ],
)
def test_a_breakdown_beside_a_day_window_sums_the_days(
    question: str, shape: str, limit: int | None
) -> None:
    res, decider = _resolve(question, shape)
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions == ["station_name"]
    assert res.intent.limit == limit
    assert all("day" not in names for names in decider.offered)


def test_a_grouping_by_day_asked_in_words_is_still_offered() -> None:
    res, decider = _resolve("rides per station per day in the first week of October 2026")
    assert res.intent is not None, res.clarification
    assert sorted(res.intent.dimensions) == ["day", "station_name"]
    # "which day" asks about days outside the window phrase: the day is offered
    _res, decider = _resolve(
        "Which day had the most rides in the first week of October 2026?", "ranking"
    )
    assert decider.offered and "day" in decider.offered[0]
