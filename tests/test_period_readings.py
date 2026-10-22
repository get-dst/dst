"""Which period, and which snapshot of it, a figure reads when the question
leaves it open or names an edge. A marina: charters (events) and a daily
snapshot of its berths.

- A ratio asked over no period reads the last complete month and says so, on
  the typed, structured, sole-metric and generated-intent doors; "all time"
  reads the whole history. A ratio over levels reads the latest snapshot.
- A level asked "at the start of" a period reads its first snapshot, "at the
  end of" its last.
- A flag declared ``window_reading: any_in_window`` counts what it flagged at
  any snapshot of a stated period; ``snapshot`` reads one. A certified answer
  is held to the same declaration.
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb
import pytest

from services.contracts.protocols import Decision
from services.contracts.query_intent import IntentFilter, QueryIntent
from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime import growth, verification
from services.runtime.compiler import compile_intent
from services.runtime.generator import FixedSQLGenerator
from services.runtime.typed_resolver import (
    TypedIntentGenerator,
    TypedResolver,
    pin_intent,
    sole_metric_intent,
)

TODAY = date(2026, 10, 22)


def _charters() -> Entity:
    return Entity(
        name="charters",
        source=EntitySource(connection="wh", table="charters"),
        default_time_field="sailed_on",
        primary_key=["charter_id"],
        fields=[
            Field(name="charter_id", type="string"),
            Field(name="sailed_on", type="date"),
            Field(name="cancelled", type="boolean"),
        ],
        metrics=[
            Metric(name="charter_count", agg="count", label="charters"),
            Metric(name="cancelled_charters", agg="count", filters=["charters.cancelled"]),
            Metric(
                name="cancellation_rate",
                type="ratio",
                numerator="cancelled_charters",
                denominator="charter_count",
                label="cancellation rate",
            ),
        ],
    )


def _berths(overdue_reading: str = "any_in_window") -> Entity:
    return Entity(
        name="berth_days",
        source=EntitySource(connection="wh", table="berth_days"),
        default_time_field="snap_day",
        primary_key=["berth_id", "snap_day"],
        fields=[
            Field(name="berth_id", type="string"),
            Field(name="snap_day", type="date"),
            Field(name="boat_id", type="string"),
            Field(name="occupied", type="boolean"),
            Field(name="overdue", type="boolean"),
        ],
        metrics=[
            Metric(
                name="occupied_berths",
                agg="count",
                filters=["berth_days.occupied"],
                additivity="level",
                label="occupied berths",
            ),
            Metric(name="berth_total", agg="count", additivity="level"),
            Metric(
                name="occupancy",
                type="ratio",
                numerator="occupied_berths",
                denominator="berth_total",
                label="occupancy",
            ),
            Metric(
                name="overdue_boats",
                agg="count_distinct",
                expr="berth_days.boat_id",
                filters=["berth_days.overdue"],
                additivity="level",
                window_reading=overdue_reading,  # type: ignore[arg-type]
                label="overdue boats",
            ),
        ],
    )


def _model(*entities: Entity) -> SemanticModel:
    return SemanticModel(lens="marina", dialect="duckdb", entities=list(entities))


def _db(path: str | None = None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(path or ":memory:")
    con.execute(
        "CREATE TABLE charters AS SELECT * FROM (VALUES "
        "('c1', DATE '2026-08-03', true), ('c2', DATE '2026-08-09', false),"
        "('c3', DATE '2026-09-02', true), ('c4', DATE '2026-09-12', false),"
        "('c5', DATE '2026-09-19', false), ('c6', DATE '2026-09-28', false),"
        "('c7', DATE '2026-10-04', true)"
        ") AS t(charter_id, sailed_on, cancelled)"
    )
    # Q3 opens with 3 of 4 berths occupied and closes with 1; boat b1 is
    # overdue mid-July only, b2 at the end of September; today, b3.
    con.execute(
        "CREATE TABLE berth_days AS SELECT * FROM (VALUES "
        "('p1', DATE '2026-07-01', 'b1', true, false),"
        "('p2', DATE '2026-07-01', 'b2', true, false),"
        "('p3', DATE '2026-07-01', 'b3', true, false),"
        "('p4', DATE '2026-07-01', NULL, false, false),"
        "('p1', DATE '2026-07-15', 'b1', true, true),"
        "('p2', DATE '2026-07-15', 'b2', true, false),"
        "('p3', DATE '2026-07-15', NULL, false, false),"
        "('p4', DATE '2026-07-15', NULL, false, false),"
        "('p1', DATE '2026-09-30', NULL, false, false),"
        "('p2', DATE '2026-09-30', 'b2', true, true),"
        "('p3', DATE '2026-09-30', NULL, false, false),"
        "('p4', DATE '2026-09-30', NULL, false, false),"
        "('p1', DATE '2026-10-20', 'b3', true, true),"
        "('p2', DATE '2026-10-20', 'b2', true, false),"
        "('p3', DATE '2026-10-20', 'b4', true, false),"
        "('p4', DATE '2026-10-20', NULL, false, false)"
        ") AS t(berth_id, snap_day, boat_id, occupied, overdue)"
    )
    return con


def _rows(sql: str) -> list[tuple[object, ...]]:
    return _db().execute(sql).fetchall()


class _Picks:
    """The first preferred option on offer, else none / unstated / the first."""

    def __init__(self, *prefer: str) -> None:
        self.prefer = prefer

    def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
        names = [o.name for o in options]
        pick = next((p for p in self.prefer if p in names), None)
        if pick is None:
            pick = "unstated" if "unstated" in names else None if allow_none else names[0]
        return Decision(chosen=pick, probs={pick or "none": 1.0}, provider="scripted")


def _resolver(*prefer: str) -> TypedResolver:
    return TypedResolver(_Picks(*prefer), today=TODAY)  # type: ignore[arg-type]


# ── 2.2 a ratio over no period reads the last complete month ────────────────


def test_a_ratio_over_no_period_reads_the_last_complete_month_and_says_so(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from services.connectors.duckdb import DuckDBConnector
    from services.runtime.pipeline import run_query

    path = str(tmp_path / "marina.duckdb")
    _db(path).close()
    model = _model(_charters())
    res = run_query(
        question="What is the cancellation rate?",
        lens_name="marina",
        org_id="org-1",
        caller="harbourmaster",
        semantic_model=model,
        connector=DuckDBConnector(path),
        generator=TypedIntentGenerator(_resolver("aggregate", "cancellation_rate")),
        composer=None,
    )
    assert res.response.status == "ok", res.response.answer
    assert res.response.data is not None
    # September 2026: 1 of 4 cancelled — not 3 of 7 over every month
    assert res.response.data.rows == [[0.25]]
    assert "last complete month, September 2026" in res.response.answer


def test_all_time_wording_still_reads_the_whole_history() -> None:
    res = _resolver("aggregate", "cancellation_rate").resolve(
        "What is the all-time cancellation rate?", _model(_charters())
    )
    assert res.intent is not None, res.clarification
    assert res.intent.filters == []
    assert not [d for d in res.decisions if d.provider == growth.PERIOD_DEFAULT]
    assert _rows(compile_intent(res.intent, _model(_charters()))) == [(3 / 7,)]


def test_the_structured_and_sole_metric_doors_pin_the_same_month(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from services.connectors.duckdb import DuckDBConnector
    from services.runtime.pipeline import run_query

    model = _model(_charters())
    bare = QueryIntent(entity="charters", metrics=["cancellation_rate"])
    pin = pin_intent("", bare, model, TODAY)
    assert isinstance(pin, tuple)
    intent, record = pin
    assert record.chosen == "2026-09-01/2026-09-30"
    sole = sole_metric_intent("cancellation rate", model, today=TODAY)
    assert sole is not None
    assert pin_intent("cancellation rate", sole, model, TODAY) == pin
    path = str(tmp_path / "marina.duckdb")
    _db(path).close()
    res = run_query(
        question="cancellation_rate",
        lens_name="marina",
        org_id="org-1",
        caller="dashboard",
        semantic_model=model,
        connector=DuckDBConnector(path),
        generator=FixedSQLGenerator(
            compile_intent(intent, model), intent=intent, decisions=[record]
        ),
        composer=None,
    )
    assert res.response.data is not None and res.response.data.rows == [[0.25]]
    assert "September 2026" in res.response.answer
    # a structured caller's own window is never replaced
    windowed = bare.model_copy(
        update={"filters": [IntentFilter(field="sailed_on", op=">=", value="2026-08-01")]}
    )
    assert pin_intent("", windowed, model, TODAY) is None


def test_a_ratio_over_levels_reads_the_latest_snapshot_not_a_month() -> None:
    model = _model(_berths())
    res = _resolver("aggregate", "occupancy").resolve("What is the occupancy?", model)
    assert res.intent is not None, res.clarification
    assert res.intent.filters == []
    # 2026-10-20: 3 of 4 berths, not the average over every snapshot
    assert _rows(compile_intent(res.intent, model)) == [(0.75,)]
    from services.runtime.resolution import level_disclosure

    line = level_disclosure(res.intent, model)
    assert line is not None and "latest snapshot in the data" in line


# ── 70 the start of a period is its first snapshot ──────────────────────────


@pytest.mark.parametrize(
    ("question", "edge", "value"),
    [
        ("How many occupied berths at the start of Q3 2026?", "first", 3),
        ("occupied berths at the beginning of Q3 2026", "first", 3),
        ("How many occupied berths at the end of Q3 2026?", "last", 1),
    ],
)
def test_a_level_at_a_period_edge_reads_that_snapshot(question: str, edge: str, value: int) -> None:
    model = _model(_berths())
    res = _resolver("aggregate", "occupied_berths").resolve(question, model)
    assert res.intent is not None, res.clarification
    assert res.intent.snapshot == edge
    assert [f.value for f in res.intent.filters] == ["2026-07-01", "2026-09-30"]
    assert _rows(compile_intent(res.intent, model)) == [(value,)]


def test_the_sole_metric_door_reads_the_start_too() -> None:
    model = _model(_berths())
    sole = sole_metric_intent("occupied berths at the beginning of Q3 2026", model, today=TODAY)
    assert sole is not None and sole.snapshot == "first"
    assert _rows(compile_intent(sole, model)) == [(3,)]


# ── 68 a declared window reading for flags ──────────────────────────────────


def _q3(metric: str, **kw: object) -> QueryIntent:
    return QueryIntent(
        entity="berth_days",
        metrics=[metric],
        filters=[
            IntentFilter(field="snap_day", op=">=", value="2026-07-01"),
            IntentFilter(field="snap_day", op="<=", value="2026-09-30"),
        ],
        **kw,  # type: ignore[arg-type]
    )


def test_any_in_window_counts_every_boat_flagged_in_the_period() -> None:
    model = _model(_berths("any_in_window"))
    assert _rows(compile_intent(_q3("overdue_boats"), model)) == [(2,)]  # b1 in July, b2
    # at a named edge, and over no period, it is a snapshot
    assert _rows(compile_intent(_q3("overdue_boats", snapshot="last"), model)) == [(1,)]
    bare = QueryIntent(entity="berth_days", metrics=["overdue_boats"])
    assert _rows(compile_intent(bare, model)) == [(1,)]  # b3 today
    by_month = QueryIntent(entity="berth_days", metrics=["overdue_boats"], grain="month")
    assert _rows(compile_intent(by_month, model) + " ORDER BY 1") == [
        (datetime(2026, 7, 1), 1),
        (datetime(2026, 9, 1), 1),
        (datetime(2026, 10, 1), 1),
    ]


def test_snapshot_reads_the_period_end() -> None:
    model = _model(_berths("snapshot"))
    assert _rows(compile_intent(_q3("overdue_boats"), model)) == [(1,)]


def test_typed_serving_reads_the_declaration() -> None:
    for reading, value in (("any_in_window", 2), ("snapshot", 1)):
        model = _model(_berths(reading))
        res = _resolver("aggregate", "overdue_boats").resolve(
            "How many overdue boats in Q3 2026?", model
        )
        assert res.intent is not None, res.clarification
        assert _rows(compile_intent(res.intent, model)) == [(value,)], reading


_WHOLE_WINDOW = (
    "SELECT COUNT(DISTINCT boat_id) FROM berth_days WHERE overdue "
    "AND snap_day >= DATE '2026-07-01' AND snap_day <= DATE '2026-09-30'"
)
_PERIOD_END = (
    "SELECT COUNT(DISTINCT boat_id) FROM berth_days WHERE overdue "
    "AND snap_day >= DATE '2026-07-01' AND snap_day <= DATE '2026-09-30' "
    "AND snap_day = (SELECT MAX(snap_day) FROM berth_days "
    "WHERE snap_day >= DATE '2026-07-01' AND snap_day <= DATE '2026-09-30')"
)


def test_a_certified_answer_is_held_to_the_declared_reading() -> None:
    q = "How many overdue boats in Q3 2026?"
    any_in = _model(_berths("any_in_window"))
    snap = _model(_berths("snapshot"))
    assert verification.window_reading_conflicts(_WHOLE_WINDOW, any_in, q) == []
    assert verification.window_reading_conflicts(_PERIOD_END, snap, q) == []
    assert verification.window_reading_conflicts(_PERIOD_END, any_in, q)
    assert verification.window_reading_conflicts(_WHOLE_WINDOW, snap, q)
    # a question naming the period's end reads the snapshot under either
    edge = "How many overdue boats at the end of Q3 2026?"
    assert verification.window_reading_conflicts(_PERIOD_END, any_in, edge) == []
    # one day named outright is one snapshot; an undeclared level is not read
    one_day = "SELECT COUNT(DISTINCT boat_id) FROM berth_days WHERE snap_day = '2026-09-30'"
    assert verification.window_reading_conflicts(one_day, snap, q) == []
    plain = _model(_berths())
    plain.entities[0].metrics[3].window_reading = None
    assert verification.window_reading_conflicts(_WHOLE_WINDOW, plain, q) == []
    check = verification.window_reading_check(
        verification.window_reading_conflicts(_PERIOD_END, any_in, q)
    )
    assert check.status == "fail" and "any_in_window" in (check.reason or "")


def test_plan_refuses_a_window_reading_it_cannot_honour() -> None:
    from services.semantic.resolve import resolve_model

    berths = _berths()
    flow = berths.metrics[0].model_copy(
        update={"name": "flagged", "additivity": None, "window_reading": "snapshot"}
    )
    counted = berths.metrics[0].model_copy(
        update={"name": "counted", "window_reading": "any_in_window"}
    )
    berths.metrics += [flow, counted]
    _out, errors, _warnings = resolve_model(_model(berths))
    assert any("flagged" in e and "additivity: level" in e for e in errors), errors
    assert any("counted" in e and "count_distinct" in e for e in errors), errors
    assert not any("overdue_boats" in e for e in errors), errors


def test_a_certified_answer_is_held_only_to_the_metric_it_reads() -> None:
    # Two flags counted over the same boat_id: the SQL reading `overdue` over
    # the window is the any_in_window metric's, and the snapshot metric whose
    # flag it never reads has no say in it.
    berths = _berths("any_in_window")
    berths.metrics.append(
        Metric(
            name="moored_boats",
            agg="count_distinct",
            expr="berth_days.boat_id",
            filters=["berth_days.occupied"],
            additivity="level",
            window_reading="snapshot",
            label="moored boats",
        )
    )
    model = _model(berths)
    q = "How many overdue boats in Q3 2026?"
    assert verification.window_reading_conflicts(_WHOLE_WINDOW, model, q) == []
    # the snapshot flag read over the whole window still conflicts
    moored = _WHOLE_WINDOW.replace("WHERE overdue", "WHERE occupied")
    assert verification.window_reading_conflicts(moored, model, "How many moored boats in Q3 2026?")
