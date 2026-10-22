"""Scope rails hold on every compiled query.

`population_filter` rides every door that compiles an intent (typed serving,
the sole-metric structured door, a caller's intent, a level metric's
snapshot pin); `pinned_dimensions` refuse an aggregate that neither pins nor
groups them, naming the dimension and its values; and a window literal is
cast to the column's declared type in every dialect.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from services.contracts.fakes import ScriptedDecider
from services.contracts.protocols import Decision
from services.contracts.query_intent import IntentFilter, QueryIntent
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.compiler import CompileError, UnpinnedDimension, compile_intent
from services.runtime.typed_resolver import (
    TypedIntentGenerator,
    TypedResolver,
    sole_metric_intent,
)

GUARD = "locker_events.count_basis = 'Daily Bay Tally'"
TODAY = date(2026, 10, 21)
DOMAINS = {
    "event_status": ["re-routed", "collected", "expired"],
    "count_basis": ["Daily Bay Tally", "Weekly Site Tally", "Monthly Route Tally"],
}


def _lockers(dialect: str = "duckdb", *, level: bool = False) -> SemanticModel:
    """One row per event, status and counting basis: the same event appears
    three times, once per basis, and only the per-bay tally is this entity's
    population."""
    return SemanticModel(
        lens="lockers",
        dialect=dialect,  # type: ignore[arg-type]
        entities=[
            Entity(
                name="locker_events",
                source=EntitySource(connection="wh", table="locker_events"),
                default_time_field="event_date",
                population_filter=GUARD,
                fields=[
                    Field(name="event_date", type="date"),
                    Field(name="event_status", type="string"),
                    Field(name="count_basis", type="string"),
                    Field(name="slot_occupancy_delta", type="number"),
                ],
                dimensions=[Dimension(name="event_status")],
                metrics=[
                    Metric(
                        name="re_routed_slot_occupancy_changes",
                        label="re-routed slot occupancy change",
                        agg="sum",
                        expr="locker_events.slot_occupancy_delta",
                        filters=["locker_events.event_status = 're-routed'"],
                    ),
                    Metric(
                        name="slot_occupancy",
                        agg="sum",
                        expr="locker_events.slot_occupancy_delta",
                        additivity="level" if level else None,
                    ),
                ],
            )
        ],
    )


@pytest.fixture
def wh() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE locker_events AS SELECT * FROM (VALUES "
        "(DATE '2026-09-08', 're-routed', 'Daily Bay Tally', -742.0), "
        "(DATE '2026-09-08', 're-routed', 'Weekly Site Tally', -315.0), "
        "(DATE '2026-09-08', 're-routed', 'Monthly Route Tally', -128.0), "
        "(DATE '2026-09-14', 'collected', 'Daily Bay Tally', 260.0), "
        "(DATE '2026-09-29', 'collected', 'Monthly Route Tally', 57.0)"
        ") t(event_date, event_status, count_basis, slot_occupancy_delta)"
    )
    return con


UNGUARDED = -1185.0
PER_BAY = -742.0


def _value(con: duckdb.DuckDBPyConnection, sql: str) -> float:
    return float(con.execute(sql).fetchone()[0])  # type: ignore[index]


def test_the_unguarded_sum_is_the_wrong_number(wh: duckdb.DuckDBPyConnection) -> None:
    raw = (
        "SELECT SUM(slot_occupancy_delta) FROM locker_events WHERE event_date >= '2026-09-01' "
        "AND event_date <= '2026-09-30' AND event_status = 're-routed'"
    )
    assert _value(wh, raw) == UNGUARDED


def test_the_typed_path_carries_the_population_filter(wh: duckdb.DuckDBPyConnection) -> None:
    def sure(chosen: str | None) -> Decision:
        return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="scripted")

    resolver = TypedResolver(
        ScriptedDecider([sure("aggregate"), sure("re_routed_slot_occupancy_changes"), sure(None)]),
        domains=DOMAINS,
        today=TODAY,
    )
    gq = TypedIntentGenerator(resolver).generate(
        question="What was the re-routed slot occupancy change in September 2026?",
        semantic_model=_lockers(),
        prose_context=[],
        dialect="duckdb",
    )
    assert gq.typed and gq.sql
    assert "count_basis = 'Daily Bay Tally'" in gq.sql
    assert _value(wh, gq.sql) == PER_BAY != UNGUARDED


def test_the_structured_door_carries_the_population_filter(wh: duckdb.DuckDBPyConnection) -> None:
    intent = sole_metric_intent(
        "What was the re-routed slot occupancy change in September 2026?",
        _lockers(),
        domains=DOMAINS,
        today=TODAY,
    )
    assert intent is not None
    sql = compile_intent(intent, _lockers())
    assert "count_basis = 'Daily Bay Tally'" in sql
    assert _value(wh, sql) == PER_BAY


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery", "snowflake", "mysql"])
@pytest.mark.parametrize(
    "intent",
    [
        QueryIntent(metrics=["re_routed_slot_occupancy_changes"]),
        QueryIntent(metrics=["re_routed_slot_occupancy_changes"], dimensions=["event_status"]),
        QueryIntent(metrics=["re_routed_slot_occupancy_changes", "slot_occupancy"], grain="month"),
        QueryIntent(fields=["event_status", "slot_occupancy_delta"]),
    ],
)
def test_every_compiled_shape_carries_it_on_every_dialect(
    dialect: str, intent: QueryIntent
) -> None:
    sql = compile_intent(intent.model_copy(update={"entity": "locker_events"}), _lockers(dialect))
    assert "count_basis = 'Daily Bay Tally'" in sql


def test_a_level_snapshot_is_the_latest_date_inside_the_population(
    wh: duckdb.DuckDBPyConnection,
) -> None:
    """The last September row is a per-route one: read over the whole
    table, the snapshot date is 2026-09-29 and the guarded WHERE keeps
    nothing of it. Inside the population the latest date is 2026-09-14."""
    sql = compile_intent(
        QueryIntent(
            entity="locker_events",
            metrics=["slot_occupancy"],
            filters=[
                IntentFilter(field="event_date", op=">=", value="2026-09-01"),
                IntentFilter(field="event_date", op="<=", value="2026-09-30"),
            ],
        ),
        _lockers(level=True),
    )
    assert sql.count("count_basis = 'Daily Bay Tally'") == 2
    assert _value(wh, sql) == 260.0


# ── pinned dimensions ──────────────────────────────────────────────────────


def _workspace(dialect: str = "duckdb") -> SemanticModel:
    """Four record kinds stacked into one table, told apart by record_kind."""
    return SemanticModel(
        lens="locker_ops",
        dialect=dialect,  # type: ignore[arg-type]
        entities=[
            Entity(
                name="locker_ops_workspace",
                source=EntitySource(connection="wh", table="locker_ops_workspace"),
                default_time_field="created_at",
                pinned_dimensions=["record_kind"],
                fields=[
                    Field(name="created_at", type="timestamp"),
                    Field(name="record_kind", type="string"),
                    Field(name="parcel_id", type="string"),
                ],
                dimensions=[Dimension(name="record_kind")],
                metrics=[
                    Metric(
                        name="parcels",
                        agg="count_distinct",
                        expr="locker_ops_workspace.parcel_id",
                    ),
                    Metric(
                        name="c2ls",
                        agg="count_distinct",
                        expr="locker_ops_workspace.parcel_id",
                        filters=["locker_ops_workspace.record_kind = 'C2L'"],
                    ),
                    Metric(
                        name="l2cs",
                        agg="count_distinct",
                        expr="locker_ops_workspace.parcel_id",
                        filters=["locker_ops_workspace.record_kind = 'L2C'"],
                    ),
                ],
            )
        ],
    )


KINDS = {"record_kind": ["booking", "C2L", "L2C", "parcel"]}
AUGUST = [
    IntentFilter(field="created_at", op=">=", value="2026-08-01"),
    IntentFilter(field="created_at", op="<", value="2026-09-01"),
]


def test_an_unpinned_aggregate_refuses_naming_the_dimension_and_its_values() -> None:
    intent = QueryIntent(
        entity="locker_ops_workspace", metrics=["parcels"], grain="week", filters=AUGUST
    )
    with pytest.raises(UnpinnedDimension) as err:
        compile_intent(intent, _workspace(), domains=KINDS)
    assert err.value.dimension == "record_kind"
    assert err.value.values == KINDS["record_kind"]
    assert "record_kind (booking, C2L, L2C, parcel)" in str(err.value)
    assert isinstance(err.value, CompileError)


@pytest.mark.parametrize(
    "intent",
    [
        QueryIntent(
            metrics=["parcels"],
            filters=[*AUGUST, IntentFilter(field="record_kind", op="=", value="C2L")],
        ),
        QueryIntent(
            metrics=["parcels"],
            filters=[*AUGUST, IntentFilter(field="record_kind", op="in", value=["L2C"])],
        ),
        QueryIntent(metrics=["parcels"], dimensions=["record_kind"], filters=AUGUST),
        QueryIntent(metrics=["c2ls", "l2cs"], grain="week", filters=AUGUST),
        QueryIntent(fields=["parcel_id"], filters=AUGUST),
    ],
)
def test_a_pin_a_group_or_the_metrics_own_filter_compiles(intent: QueryIntent) -> None:
    compile_intent(intent.model_copy(update={"entity": "locker_ops_workspace"}), _workspace())


def test_a_many_valued_in_is_not_a_pin() -> None:
    intent = QueryIntent(
        entity="locker_ops_workspace",
        metrics=["parcels"],
        filters=[IntentFilter(field="record_kind", op="in", value=["C2L", "L2C"])],
    )
    with pytest.raises(UnpinnedDimension):
        compile_intent(intent, _workspace())


def test_two_kind_metrics_per_week_count_each_kind(tmp_path: object) -> None:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE locker_ops_workspace AS SELECT * FROM (VALUES "
        "(TIMESTAMP '2026-08-03 08:30:00', 'C2L', 'p1'), "
        "(TIMESTAMP '2026-08-04 17:10:00', 'L2C', 'p1'), "
        "(TIMESTAMP '2026-08-05 09:00:00', 'booking', 'p2'), "
        "(TIMESTAMP '2026-08-06 12:45:00', 'L2C', 'p5'), "
        "(TIMESTAMP '2026-08-11 07:20:00', 'C2L', 'p3'), "
        "(TIMESTAMP '2026-08-12 18:05:00', 'C2L', 'p4')"
        ") t(created_at, record_kind, parcel_id)"
    )
    sql = compile_intent(
        QueryIntent(
            entity="locker_ops_workspace",
            metrics=["c2ls", "l2cs"],
            grain="week",
            filters=AUGUST,
        ),
        _workspace(),
    )
    rows = con.execute(sql + " ORDER BY 1").fetchall()
    assert [(r[1], r[2]) for r in rows] == [(1, 2), (2, 0)]


def _workspace_without_kind_metrics() -> SemanticModel:
    """The workspace with only the kind-blind count: "C2Ls and L2Cs per week"
    has no metric of its own to land on, so the reading sums every kind."""
    model = _workspace()
    entity = model.entities[0]
    only = [m for m in entity.metrics if m.name == "parcels"]
    return model.model_copy(update={"entities": [entity.model_copy(update={"metrics": only})]})


TWO_KINDS = "How many C2Ls and L2Cs per week in August 2026?"
ASKED = (
    "The locker_ops_workspace table holds several kinds of record in `record_kind` "
    "(booking, C2L, L2C, parcel): which one, or one row per kind?"
)


def test_the_typed_path_asks_which_kind_instead_of_declining() -> None:
    def sure(chosen: str | None) -> Decision:
        return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="scripted")

    resolver = TypedResolver(
        ScriptedDecider([sure("aggregate"), sure("parcels"), sure(None)]),
        entity_domains={"locker_ops_workspace": KINDS},
        today=TODAY,
    )
    gq = TypedIntentGenerator(resolver).generate(
        question=TWO_KINDS,
        semantic_model=_workspace_without_kind_metrics(),
        prose_context=[],
        dialect="duckdb",
    )
    assert not gq.sql and gq.no_answer_reason is None
    assert gq.clarification is not None
    assert gq.clarification.kind == "unresolved_slot"
    assert gq.clarification.term == "record_kind"
    assert gq.clarification.options == KINDS["record_kind"]
    assert gq.clarification.question == ASKED
    assert gq.decisions  # the decisions that typed ride the clarification


def test_the_model_wide_dictionary_names_the_kinds_too() -> None:
    def sure(chosen: str | None) -> Decision:
        return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="scripted")

    resolver = TypedResolver(
        ScriptedDecider([sure("aggregate"), sure("parcels"), sure(None)]),
        domains=KINDS,
        today=TODAY,
    )
    gq = TypedIntentGenerator(resolver).generate(
        question=TWO_KINDS,
        semantic_model=_workspace_without_kind_metrics(),
        prose_context=[],
        dialect="duckdb",
    )
    assert gq.clarification is not None and gq.clarification.options == KINDS["record_kind"]


_BLIND = (
    '{"entity": "locker_ops_workspace", "metrics": ["parcels"], "grain": "week", '
    '"filters": [{"field": "created_at", "op": ">=", "value": "2026-08-01"}]}'
)


def test_the_intent_door_asks_which_kind_when_its_repair_still_sums_them() -> None:
    """The kinds are listed on the intent door too: it compiles with the
    entity's own dictionaries, as the typed path does. Before, it compiled
    with none and asked which kind offering no options at all."""
    from services.contracts.fakes import ScriptedLLM
    from services.runtime.intent_generator import IntentSQLGenerator

    gq = IntentSQLGenerator(
        ScriptedLLM([_BLIND, _BLIND]), entity_domains={"locker_ops_workspace": KINDS}
    ).generate(
        question=TWO_KINDS,
        semantic_model=_workspace_without_kind_metrics(),
        prose_context=[],
        dialect="duckdb",
    )
    assert not gq.sql
    assert gq.clarification is not None and gq.clarification.term == "record_kind"
    assert gq.clarification.options == KINDS["record_kind"]
    assert gq.clarification.question == ASKED


def test_the_served_intent_door_carries_the_entitys_dictionaries() -> None:
    """Serving builds the intent door with the assembled dictionaries: a kind
    column two tables share is dropped from the model-wide ones, and the
    entity's own still lists it."""
    from services.contracts.fakes import ScriptedLLM
    from services.contracts.lens_config import LensConfig
    from services.llm.registry import ResolvedModel
    from services.runtime.assembly import AssembledInputs, select_generators
    from services.runtime.intent_generator import IntentSQLGenerator

    model = _workspace_without_kind_metrics()
    assembled = AssembledInputs(
        model=model,
        prose=[],
        certified=None,
        certification="none",
        certified_sql=None,
        data_as_of=None,
        counts={},
        entity_domains={"locker_ops_workspace": KINDS},
    )
    config = LensConfig.model_validate(
        {"name": "locker_ops", "connections": ["wh"], "model": {"provider": "anthropic"}}
    )
    resolved = ResolvedModel(ScriptedLLM([_BLIND, _BLIND]), "anthropic/claude-sonnet-4-6", "m")
    generator, _esc, _mode, _band = select_generators(assembled, resolved, config=config)
    assert isinstance(generator, IntentSQLGenerator)
    gq = generator.generate(
        question=TWO_KINDS, semantic_model=model, prose_context=[], dialect="duckdb"
    )
    assert gq.clarification is not None
    assert gq.clarification.options == KINDS["record_kind"]


def test_a_dictionary_is_found_whatever_the_case_of_the_pinned_name() -> None:
    model = _workspace()
    entity = model.entities[0].model_copy(update={"pinned_dimensions": ["Record_Kind"]})
    with pytest.raises(UnpinnedDimension) as err:
        compile_intent(
            QueryIntent(entity="locker_ops_workspace", metrics=["parcels"], filters=AUGUST),
            model.model_copy(update={"entities": [entity]}),
            domains=KINDS,
        )
    assert err.value.values == KINDS["record_kind"]


# ── window literals carry the column's declared type ───────────────────────


@pytest.mark.parametrize(
    ("dialect", "date_lit", "ts_lit"),
    [
        ("duckdb", "CAST('2026-08-01' AS DATE)", "CAST('2026-08-01' AS TIMESTAMP)"),
        ("postgres", "CAST('2026-08-01' AS DATE)", "CAST('2026-08-01' AS TIMESTAMP)"),
        # A declared timestamp may be a DATETIME there: the bare string reads as either.
        ("bigquery", "CAST('2026-08-01' AS DATE)", "'2026-08-01'"),
        ("snowflake", "CAST('2026-08-01' AS DATE)", "CAST('2026-08-01' AS TIMESTAMP)"),
        ("mysql", "CAST('2026-08-01' AS DATE)", "CAST('2026-08-01' AS DATETIME)"),
    ],
)
def test_a_window_literal_is_cast_to_the_declared_type(
    dialect: str, date_lit: str, ts_lit: str
) -> None:
    on_date = compile_intent(
        QueryIntent(
            entity="locker_events",
            metrics=["re_routed_slot_occupancy_changes"],
            filters=[IntentFilter(field="event_date", op=">=", value="2026-08-01")],
        ),
        _lockers(dialect),
    )
    assert f">= {date_lit}" in on_date
    on_ts = compile_intent(
        QueryIntent(
            entity="locker_ops_workspace",
            metrics=["c2ls"],
            filters=[IntentFilter(field="created_at", op=">=", value="2026-08-01")],
        ),
        _workspace(dialect),
    )
    assert f">= {ts_lit}" in on_ts


@pytest.mark.parametrize(
    ("field", "physical", "bound"),
    [
        ("created_at", "DATETIME", "CAST('2026-08-01' AS DATETIME)"),
        ("created_at", "TIMESTAMP", "CAST('2026-08-01' AS TIMESTAMP)"),
        ("created_at", None, "'2026-08-01'"),
        ("created_on", None, "CAST('2026-08-01' AS DATE)"),
        ("created_on", "DATE", "CAST('2026-08-01' AS DATE)"),
        ("created_on", "TIMESTAMP", "CAST('2026-08-01' AS TIMESTAMP)"),
        ("created_on", "STRING", "'2026-08-01'"),
    ],
)
def test_a_bigquery_bound_is_cast_to_the_profiled_type(
    field: str, physical: str | None, bound: str
) -> None:
    """On BigQuery a bound is cast to the column's own type when the stored
    profile recorded it; without one, a declared date is a DATE and a
    declared timestamp stays a bare string BigQuery reads as either moment
    type."""
    model = _workspace("bigquery")
    entity = model.entities[0]
    entity = entity.model_copy(
        update={"fields": [*entity.fields, Field(name="created_on", type="date")]}
    )
    model = model.model_copy(update={"entities": [entity]})
    sql = compile_intent(
        QueryIntent(
            entity="locker_ops_workspace",
            metrics=["c2ls"],
            filters=[IntentFilter(field=field, op=">=", value="2026-08-01")],
        ),
        model,
        column_types={"locker_ops_workspace": {field: physical}} if physical else None,
    )
    assert f"{field} >= {bound}" in sql


@pytest.mark.parametrize(
    ("dialect", "physical", "upper"),
    [
        ("bigquery", "TIMESTAMP", "created_on < CAST('2026-09-01' AS TIMESTAMP)"),
        ("bigquery", "DATETIME", "created_on < CAST('2026-09-01' AS DATETIME)"),
        ("duckdb", "TIMESTAMP", "created_on < CAST('2026-09-01' AS DATE)"),
        ("snowflake", "TIMESTAMP_NTZ", "created_on < CAST('2026-09-01' AS DATE)"),
        ("postgres", "timestamp without time zone", "created_on < CAST('2026-09-01' AS DATE)"),
        ("bigquery", "DATE", "created_on <= CAST('2026-08-31' AS DATE)"),
        ("duckdb", None, "created_on <= CAST('2026-08-31' AS DATE)"),
    ],
)
def test_a_day_bound_on_a_stored_moment_closes_at_the_next_midnight(
    dialect: str, physical: str | None, upper: str
) -> None:
    """A column declared `date` the profile records as a moment: `<= '08-31'`
    is midnight of the 31st and drops that day's events, so the window ends
    `< '09-01'` instead, on every dialect. A real DATE, or no profile, keeps
    the inclusive day."""
    model = _workspace(dialect)
    entity = model.entities[0]
    entity = entity.model_copy(
        update={"fields": [*entity.fields, Field(name="created_on", type="date")]}
    )
    model = model.model_copy(update={"entities": [entity]})
    sql = compile_intent(
        QueryIntent(
            entity="locker_ops_workspace",
            metrics=["c2ls"],
            filters=[
                IntentFilter(field="created_on", op=">=", value="2026-08-01"),
                IntentFilter(field="created_on", op="<=", value="2026-08-31"),
            ],
        ),
        model,
        column_types={"locker_ops_workspace": {"created_on": physical}} if physical else None,
    )
    assert upper in sql
    if physical and physical != "DATE":
        assert "2026-08-31" not in sql
        one_day = compile_intent(
            QueryIntent(
                entity="locker_ops_workspace",
                metrics=["c2ls"],
                filters=[IntentFilter(field="created_on", op="=", value="2026-08-31")],
            ),
            model,
            column_types={"locker_ops_workspace": {"created_on": physical}},
        )
        assert "created_on >= " in one_day and "created_on < " in one_day


@pytest.mark.parametrize(
    ("dialect", "physical", "upper"),
    [
        ("bigquery", "TIMESTAMP", "created_at < CAST('2026-09-01' AS TIMESTAMP)"),
        ("bigquery", "DATETIME", "created_at < CAST('2026-09-01' AS DATETIME)"),
        ("bigquery", None, "created_at < '2026-09-01'"),
        ("duckdb", "TIMESTAMP", "created_at < CAST('2026-09-01' AS TIMESTAMP)"),
        ("duckdb", None, "created_at < CAST('2026-09-01' AS TIMESTAMP)"),
        ("postgres", None, "created_at < CAST('2026-09-01' AS TIMESTAMP)"),
    ],
)
def test_a_day_bound_on_a_declared_timestamp_closes_at_the_next_midnight(
    dialect: str, physical: str | None, upper: str
) -> None:
    """A caller's `<= '2026-08-31'` on a column declared `timestamp` (the
    metrics and batch doors take the caller's operator as written) is
    midnight of the 31st and dropped that day's events, while the question
    door read the same day whole. It closes at the next midnight, exclusive,
    on every door and dialect, with or without a profile."""
    model = _workspace(dialect)
    filters = [
        IntentFilter(field="created_at", op=">=", value="2026-08-01"),
        IntentFilter(field="created_at", op="<=", value="2026-08-31"),
    ]
    sql = compile_intent(
        QueryIntent(entity="locker_ops_workspace", metrics=["c2ls"], filters=filters),
        model,
        column_types={"locker_ops_workspace": {"created_at": physical}} if physical else None,
    )
    assert upper in sql and "2026-08-31" not in sql
    after = compile_intent(
        QueryIntent(
            entity="locker_ops_workspace",
            metrics=["c2ls"],
            filters=[IntentFilter(field="created_at", op=">", value="2026-08-31")],
        ),
        model,
    )
    assert "created_at >= " in after and "2026-09-01" in after


def test_a_declared_timestamp_day_bound_counts_the_last_days_rows() -> None:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE locker_ops_workspace "
        "(created_at TIMESTAMP, record_kind VARCHAR, parcel_id VARCHAR)"
    )
    con.execute(
        "INSERT INTO locker_ops_workspace VALUES "
        "('2026-08-30 09:00', 'C2L', 'p1'), ('2026-08-31 00:00', 'C2L', 'p2'), "
        "('2026-08-31 17:45', 'C2L', 'p3'), ('2026-09-01 00:00', 'C2L', 'p4')"
    )
    sql = compile_intent(
        QueryIntent(
            entity="locker_ops_workspace",
            metrics=["c2ls"],
            filters=[
                IntentFilter(field="created_at", op=">=", value="2026-08-01"),
                IntentFilter(field="created_at", op="<=", value="2026-08-31"),
            ],
        ),
        _workspace(),
    )
    assert con.execute(sql).fetchone() == (3,)


def _shelf_counts(dialect: str = "duckdb") -> SemanticModel:
    """A stock snapshot declared by its day and stored as moments: one
    snapshot's rows carry different times on the same day."""
    return SemanticModel(
        lens="shelves",
        dialect=dialect,  # type: ignore[arg-type]
        entities=[
            Entity(
                name="shelf_counts",
                source=EntitySource(connection="wh", table="shelf_counts"),
                default_time_field="counted_on",
                fields=[
                    Field(name="counted_on", type="date"),
                    Field(name="shelf", type="string"),
                    Field(name="units", type="number"),
                ],
                metrics=[
                    Metric(
                        name="units_on_hand",
                        agg="sum",
                        expr="shelf_counts.units",
                        additivity="level",
                    )
                ],
            )
        ],
    )


_STOCK_ROWS = (
    "('2026-07-31 02:00:00', 'a', 5), ('2026-07-31 02:00:07', 'b', 6), "
    "('2026-08-31 02:00:00', 'a', 7), ('2026-08-31 02:00:09', 'b', 8)"
)


@pytest.mark.parametrize(
    ("filters", "grain", "expected"),
    [
        (
            [
                IntentFilter(field="counted_on", op=">=", value="2026-08-01"),
                IntentFilter(field="counted_on", op="<=", value="2026-08-31"),
            ],
            None,
            [(15,)],
        ),
        ([], None, [(15,)]),
        ([], "month", [(11,), (15,)]),
    ],
)
def test_a_level_snapshot_declared_by_day_reads_the_whole_latest_day(
    filters: list[IntentFilter], grain: str | None, expected: list[tuple[int]]
) -> None:
    """The latest-snapshot pin on a date-declared column the profile records
    as a moment was an equality on the latest moment, which kept the last
    row's instant and dropped the rest of that day's snapshot (8 for 15). It
    reads the latest day, from its midnight to the next one, exclusive."""
    con = duckdb.connect()
    con.execute("CREATE TABLE shelf_counts (counted_on TIMESTAMP, shelf VARCHAR, units INTEGER)")
    con.execute(f"INSERT INTO shelf_counts VALUES {_STOCK_ROWS}")
    sql = compile_intent(
        QueryIntent(entity="shelf_counts", metrics=["units_on_hand"], filters=filters, grain=grain),
        _shelf_counts(),
        column_types={"shelf_counts": {"counted_on": "TIMESTAMP"}},
    )
    values = [r[-1:] for r in con.execute(sql + (" ORDER BY 1" if grain else "")).fetchall()]
    assert values == expected


@pytest.mark.parametrize(
    ("dialect", "physical", "cast"),
    [
        ("bigquery", "TIMESTAMP", "AS TIMESTAMP)"),
        ("bigquery", "DATETIME", "AS DATETIME)"),
        ("snowflake", "TIMESTAMP_NTZ", "AS TIMESTAMP"),
        ("postgres", "timestamp without time zone", "AS TIMESTAMP)"),
    ],
)
def test_the_latest_day_pin_parses_and_casts_back_on_every_dialect(
    dialect: str, physical: str, cast: str
) -> None:
    import sqlglot

    sql = compile_intent(
        QueryIntent(entity="shelf_counts", metrics=["units_on_hand"]),
        _shelf_counts(dialect),
        column_types={"shelf_counts": {"counted_on": physical}},
    )
    sqlglot.parse_one(sql, read=dialect)
    assert "= (SELECT MAX(" not in sql and cast in sql


def test_a_real_date_snapshot_keeps_the_equality_pin() -> None:
    sql = compile_intent(
        QueryIntent(entity="shelf_counts", metrics=["units_on_hand"]),
        _shelf_counts(),
        column_types={"shelf_counts": {"counted_on": "DATE"}},
    )
    assert "shelf_counts.counted_on = (SELECT MAX(" in sql


def test_the_profiled_type_reaches_the_typed_path() -> None:
    """The stored profile's column type rides assembly into typed serving's
    compile: a DATETIME column on BigQuery is bounded by a DATETIME."""
    from services.contracts.profile import ColumnProfile, TableProfile
    from services.runtime.value_guard import entity_column_types

    model = _workspace("bigquery")
    profile = TableProfile(
        connection="wh",
        table="locker_ops_workspace",
        columns=[
            ColumnProfile(name="created_at", type="DATETIME"),
            ColumnProfile(name="record_kind", type="STRING"),
        ],
    )
    types = entity_column_types(model, [profile])
    assert types == {"locker_ops_workspace": {"created_at": "DATETIME", "record_kind": "STRING"}}

    def sure(chosen: str | None) -> Decision:
        return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="scripted")

    resolver = TypedResolver(
        ScriptedDecider([sure("aggregate"), sure("c2ls"), sure(None)]), today=TODAY
    )
    gq = TypedIntentGenerator(resolver, column_types=types).generate(
        question="How many C2Ls in August 2026?",
        semantic_model=model,
        prose_context=[],
        dialect="bigquery",
    )
    assert gq.sql, gq.no_answer_reason or gq.clarification
    assert "AS DATETIME)" in gq.sql and "AS TIMESTAMP)" not in gq.sql


def test_a_month_label_on_a_string_column_stays_a_string() -> None:
    sql = compile_intent(
        QueryIntent(
            entity="locker_events",
            metrics=["re_routed_slot_occupancy_changes"],
            filters=[IntentFilter(field="count_basis", op="=", value="Monthly Route Tally")],
        ),
        _lockers(),
    )
    assert "CAST(" not in sql


def test_plan_compiles_a_pinned_entity_grouped_by_its_pin() -> None:
    """Plan's compile lint asks for the base and by-month shapes of the first
    metric; an unpinned aggregate is the rail refusing, not the entity broken."""
    from services.contracts.lens_config import LensConfig
    from services.lenses.store import LensBundle
    from services.validate.report import validate_bundle

    model = _workspace()
    bundle = LensBundle(
        config=LensConfig(name="locker_ops", display_name="Locker ops", connections=["wh"]),
        semantic_model=model,
    )
    codes = {i.code for i in validate_bundle(bundle, [], []).issues}
    assert "entity_does_not_compile" not in codes


def test_plan_rejects_a_distinct_on_metric_without_its_key() -> None:
    from pydantic import ValidationError

    from services.contracts.shared_semantic import SharedEntity

    with pytest.raises(ValidationError, match="distinct_on"):
        SharedEntity.model_validate(
            {
                "name": "bay_capacity",
                "source": {"connection": "wh", "table": "bay_capacity"},
                "fields": [{"name": "slots", "type": "number"}],
                "metrics": [
                    {"name": "slots", "agg": "sum_distinct_on", "expr": "bay_capacity.slots"}
                ],
            }
        )
