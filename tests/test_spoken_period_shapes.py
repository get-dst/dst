"""Periods and comparisons as analysts say them. A ferry line: crossings (an
event per sailing) and a daily snapshot of its fleet.

- A comparison carries every binding the plain question carries: the port,
  the snapshot edge, a caller's binding. "vs a year earlier" is a comparison
  (it was served as the plain figure), and "<a month> vs last year" compares
  the same month a year back, never the whole year beside one month.
- "growth per year, 2021 to 2025" is one row per year with its change against
  the year before, never one comparison of the range's two ends.
- Spoken ranges read as one window: "January to September" (the year from the
  question or the clock), "between March and May", "from 1 January to 25
  October", "Jan–Sep 2026", "so far this month (October)".
- "today", "now" or "currently" on a level reads its latest snapshot and the
  answer names that snapshot's day; a flow asked for today before today's rows
  are loaded keeps the data-end refusal.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from services.connectors.duckdb import DuckDBConnector
from services.contracts.query_intent import QueryIntent
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.compiler import compile_intent
from services.runtime.pipeline import run_query
from services.runtime.timewindow import temporal_terms, unplaced_months, window_ranges
from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver
from tests.test_typed_question_shapes import _Prefers

TODAY = date(2026, 10, 27)
DOMAINS = {"home_port": ["Kelmar", "Ostvik"]}


def _crossings() -> Entity:
    return Entity(
        name="crossings",
        source=EntitySource(connection="wh", table="crossings"),
        default_time_field="sailed_on",
        primary_key=["crossing_id"],
        fields=[
            Field(name="crossing_id", type="string"),
            Field(name="sailed_on", type="date"),
            Field(name="home_port", type="string"),
            Field(name="passengers", type="integer"),
        ],
        dimensions=[Dimension(name="home_port")],
        metrics=[
            Metric(
                name="passenger_total", agg="sum", expr="crossings.passengers", label="passengers"
            )
        ],
    )


def _fleet() -> Entity:
    return Entity(
        name="fleet_days",
        source=EntitySource(connection="wh", table="fleet_days"),
        default_time_field="snap_day",
        primary_key=["vessel_id", "snap_day"],
        fields=[
            Field(name="vessel_id", type="string"),
            Field(name="snap_day", type="date"),
            Field(name="home_port", type="string"),
            Field(name="seaworthy", type="boolean"),
        ],
        dimensions=[Dimension(name="home_port")],
        metrics=[
            Metric(
                name="seaworthy_vessels",
                agg="count",
                filters=["fleet_days.seaworthy"],
                additivity="level",
                label="seaworthy vessels",
            )
        ],
    )


def _model() -> SemanticModel:
    return SemanticModel(lens="ferries", dialect="duckdb", entities=[_crossings(), _fleet()])


def _db(path: str | None = None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(path or ":memory:")
    con.execute(
        "CREATE TABLE crossings AS SELECT * FROM (VALUES "
        "('c1', DATE '2021-05-02', 'Kelmar', 100), ('c2', DATE '2022-05-02', 'Kelmar', 150),"
        "('c3', DATE '2023-05-02', 'Kelmar', 120), ('c4', DATE '2024-05-02', 'Kelmar', 200),"
        "('c5', DATE '2025-05-02', 'Kelmar', 260), ('c6', DATE '2025-09-12', 'Kelmar', 40),"
        "('c7', DATE '2025-09-14', 'Ostvik', 900), ('c8', DATE '2026-09-03', 'Kelmar', 70),"
        "('c9', DATE '2026-09-20', 'Ostvik', 500), ('c10', DATE '2020-05-02', 'Kelmar', 80)"
        ") AS t(crossing_id, sailed_on, home_port, passengers)"
    )
    # The newest snapshot is 2026-10-25: today's (27 October) is not loaded.
    con.execute(
        "CREATE TABLE fleet_days AS SELECT * FROM (VALUES "
        "('v1', DATE '2025-08-31', 'Kelmar', true), ('v2', DATE '2025-08-31', 'Kelmar', true),"
        "('v3', DATE '2025-08-31', 'Ostvik', true), ('v4', DATE '2025-08-31', 'Kelmar', false),"
        "('v1', DATE '2026-08-31', 'Kelmar', true), ('v2', DATE '2026-08-31', 'Kelmar', false),"
        "('v3', DATE '2026-08-31', 'Ostvik', true), ('v4', DATE '2026-08-31', 'Ostvik', true),"
        "('v1', DATE '2026-10-25', 'Kelmar', true), ('v2', DATE '2026-10-25', 'Kelmar', true),"
        "('v3', DATE '2026-10-25', 'Ostvik', false)"
        ") AS t(vessel_id, snap_day, home_port, seaworthy)"
    )
    return con


def _resolve(question: str, *prefer: str, **kw: object):  # type: ignore[no-untyped-def]
    return TypedResolver(
        _Prefers("aggregate", *prefer),  # type: ignore[arg-type]
        domains=DOMAINS,
        today=TODAY,
        **kw,  # type: ignore[arg-type]
    ).resolve(question, _model())


def _run(intent: QueryIntent) -> list[tuple[object, ...]]:
    return _db().execute(compile_intent(intent, _model())).fetchall()


def _bindings(intent: QueryIntent, clock: str) -> list[tuple[str, str, object]]:
    return sorted((f.field, f.op, f.value) for f in intent.filters if f.field != clock)


# ── a comparison carries every binding ──────────────────────────────────────


@pytest.mark.parametrize(
    ("plain", "compared", "to", "row"),
    [
        (
            "Seaworthy vessels in Kelmar at the end of August 2026",
            "Seaworthy vessels in Kelmar at the end of August 2026 vs a year earlier",
            "previous_year",
            (1, 2),
        ),
        (
            "Seaworthy vessels in Kelmar at the end of August 2026",
            "Seaworthy vessels in Kelmar at the end of August 2026 compared with a year ago",
            "previous_year",
            (1, 2),
        ),
        (
            "Seaworthy vessels in Kelmar at the end of August 2026",
            "Seaworthy vessels in Kelmar at the end of August 2026 vs the end of August 2025",
            "2025-08-01/2025-08-31",
            (1, 2),
        ),
        (
            "Seaworthy vessels in Kelmar at the end of August 2026",
            "Seaworthy vessels in Kelmar at the end of August 2026 vs 2025",
            "2025-08-01/2025-08-31",
            (1, 2),
        ),
        (
            "Passengers from Kelmar in September 2026",
            "Passengers from Kelmar in September 2026 vs a year earlier",
            "previous_year",
            (70, 40),
        ),
        (
            "Passengers from Kelmar in September 2026",
            "Passengers from Kelmar in September 2026 vs last year",
            "2025-09-01/2025-09-30",
            (70, 40),
        ),
    ],
)
def test_a_comparison_carries_the_plain_questions_bindings(
    plain: str, compared: str, to: str, row: tuple[int, int]
) -> None:
    metric = "seaworthy_vessels" if "vessels" in plain else "passenger_total"
    clock = "snap_day" if "vessels" in plain else "sailed_on"
    base = _resolve(plain, metric, "home_port")
    res = _resolve(compared, metric, "home_port")
    assert base.intent is not None and res.intent is not None, res.clarification
    assert res.intent.compare is not None and res.intent.compare.to == to, compared
    assert _bindings(res.intent, clock) == _bindings(base.intent, clock) != []
    assert res.intent.snapshot == base.intent.snapshot
    assert [f for f in res.intent.filters if f.field == clock] == [
        f for f in base.intent.filters if f.field == clock
    ]
    # Kelmar only, in both windows
    assert _run(res.intent)[0][:2] == row


def test_a_callers_binding_rides_the_comparison_through_the_pipeline(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = str(tmp_path / "ferries.duckdb")
    _db(path).close()
    resolver = TypedResolver(
        _Prefers("aggregate", "passenger_total"),  # type: ignore[arg-type]
        domains=DOMAINS,
        today=TODAY,
        bindings={"home_port": "Kelmar"},
    )
    res = run_query(
        question="How many passengers in September 2026 vs a year earlier?",
        lens_name="ferries",
        org_id="org-1",
        caller="planner",
        semantic_model=_model(),
        connector=DuckDBConnector(path),
        generator=TypedIntentGenerator(resolver),
        composer=None,
    )
    assert res.response.status == "ok", res.response.answer
    assert res.response.data is not None
    assert res.response.data.rows[0][:3] == [70, 40, 30]


# ── growth per year over a range of years ───────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "Passenger growth per year, 2021 to 2025",
        "Yearly passenger growth 2021–2025",
        "How did passengers grow each year between 2021 and 2025?",
        "YoY passenger growth from 2021 to 2025",
    ],
)
def test_growth_per_year_over_a_range_is_each_years_change(question: str) -> None:
    res = _resolve(question, "passenger_total")
    assert res.intent is not None, res.clarification
    assert res.intent.grain == "year"
    assert [(f.op, f.value) for f in res.intent.filters] == [
        (">=", "2021-01-01"),
        ("<=", "2025-12-31"),
    ]
    assert res.intent.compare is not None
    assert res.intent.compare.to == "2020-01-01/2024-12-31"
    rows = sorted(_run(res.intent), key=lambda r: str(r[0]))
    # one row per year: the year, its total, the year before's, the change
    assert [(str(r[0])[:4], r[1], r[2], r[3]) for r in rows] == [
        ("2021", 100, 80, 20),
        ("2022", 150, 100, 50),
        ("2023", 120, 150, -30),
        ("2024", 200, 120, 80),
        ("2025", 1200, 200, 1000),
    ]


def test_growth_per_year_broken_down_by_month_asks() -> None:
    res = _resolve("Passenger growth per year, 2021 to 2025", "passenger_total", "month")
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "grain"


def test_a_change_between_two_years_compares_the_two() -> None:
    res = _resolve("How did passengers change from 2024 to 2025?", "passenger_total")
    assert res.intent is not None, res.clarification
    assert res.intent.compare is not None
    assert res.intent.compare.to == "2024-01-01/2024-12-31"
    assert [f.value for f in res.intent.filters] == ["2025-01-01", "2025-12-31"]


def test_a_range_of_years_with_no_change_is_one_window() -> None:
    res = _resolve("Passengers 2021 to 2025", "passenger_total")
    assert res.intent is not None, res.clarification
    assert res.intent.compare is None
    assert [f.value for f in res.intent.filters] == ["2021-01-01", "2025-12-31"]


# ── spoken ranges ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "today", "start", "end"),
    [
        ("from January to September", TODAY, date(2026, 1, 1), date(2026, 9, 30)),
        ("June to September", date(2026, 3, 10), date(2025, 6, 1), date(2025, 9, 30)),
        ("between March and May", TODAY, date(2026, 3, 1), date(2026, 5, 31)),
        ("in 2025, January to September", TODAY, date(2025, 1, 1), date(2025, 9, 30)),
        ("January to September last year", TODAY, date(2025, 1, 1), date(2025, 9, 30)),
        ("from 1 January to 25 October", TODAY, date(2026, 1, 1), date(2026, 10, 25)),
        ("Jan–Sep 2026", TODAY, date(2026, 1, 1), date(2026, 9, 30)),
        ("2021 to 2025", TODAY, date(2021, 1, 1), date(2025, 12, 31)),
        ("between 2021 and 2025", TODAY, date(2021, 1, 1), date(2025, 12, 31)),
    ],
)
def test_a_spoken_range_is_one_window(text: str, today: date, start: date, end: date) -> None:
    assert window_ranges(temporal_terms(text), today) == [(start, end)]


def test_a_modal_may_is_no_span_and_a_backwards_span_places_nothing() -> None:
    assert temporal_terms("crews may - june permitting") == frozenset()
    assert window_ranges(temporal_terms("November to February"), TODAY) == []


def test_a_month_restating_the_window_is_carried_and_another_is_not() -> None:
    terms = temporal_terms("so far this month (October)")
    assert unplaced_months("so far this month (October)", terms, TODAY) == []
    assert unplaced_months("so far this month (September)", terms, TODAY) == ["september"]


@pytest.mark.parametrize(
    ("question", "start", "end"),
    [
        ("Passengers from January to September", "2026-01-01", "2026-09-30"),
        ("Passengers between March and May", "2026-03-01", "2026-05-31"),
        ("How many passengers from 1 January to 25 October?", "2026-01-01", "2026-10-25"),
        ("Passengers Jan–Sep 2026", "2026-01-01", "2026-09-30"),
        ("Passengers so far this month (October)", "2026-10-01", "2026-10-27"),
    ],
)
def test_a_spoken_range_types(question: str, start: str, end: str) -> None:
    res = _resolve(question, "passenger_total")
    assert res.intent is not None, res.clarification
    assert [f.value for f in res.intent.filters] == [start, end]


# ── today on a level is its latest snapshot ─────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "How many seaworthy vessels are there today?",
        "Seaworthy vessels now",
        "How many vessels are currently seaworthy?",
    ],
)
def test_today_on_a_level_reads_the_latest_snapshot(question: str) -> None:
    res = _resolve(question, "seaworthy_vessels")
    assert res.intent is not None, res.clarification
    assert res.intent.filters == []
    assert _run(res.intent) == [(2,)]


def test_today_on_a_flow_keeps_its_window() -> None:
    res = _resolve("How many passengers today?", "passenger_total")
    assert res.intent is not None, res.clarification
    assert [f.value for f in res.intent.filters] == ["2026-10-27", "2026-10-27"]


def _serve(tmp_path, question: str, metric: str):  # type: ignore[no-untyped-def]
    path = str(tmp_path / "ferries.duckdb")
    _db(path).close()
    resolver = TypedResolver(
        _Prefers("aggregate", metric),  # type: ignore[arg-type]
        domains=DOMAINS,
        today=TODAY,
    )
    return run_query(
        question=question,
        lens_name="ferries",
        org_id="org-1",
        caller="planner",
        semantic_model=_model(),
        connector=DuckDBConnector(path),
        generator=TypedIntentGenerator(resolver),
        composer=None,
    ).response


def test_a_level_asked_for_today_says_which_snapshot_it_read(tmp_path) -> None:  # type: ignore[no-untyped-def]
    resp = _serve(tmp_path, "How many seaworthy vessels are there today?", "seaworthy_vessels")
    assert resp.status == "ok", resp.answer
    assert resp.data is not None and resp.data.rows == [[2]]
    assert "latest snapshot in the data, 2026-10-25 on `snap_day`" in resp.answer


def test_a_flow_asked_for_today_before_its_rows_keeps_the_data_end_refusal(tmp_path) -> None:  # type: ignore[no-untyped-def]
    resp = _serve(tmp_path, "How many passengers today?", "passenger_total")
    assert resp.status == "refused"
    assert "runs to 2026-09-20" in resp.answer
