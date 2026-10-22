"""Clarification quality on the typed path: what a typed answer asks, and when
a deterministic rail asks instead of acting.

Each fixture mirrors a shape seen in use — a snapshot table with key columns,
sibling metrics one parameter apart, a level metric over a window, a lens that
measures one unit asked for another — driven by a ScriptedDecider so the rail,
not the decider, is what is pinned.
"""

from __future__ import annotations

from datetime import date

from services.contracts.fakes import ScriptedDecider
from services.contracts.protocols import Decision
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
from services.runtime.intent_generator import gate_dimensions
from services.runtime.typed_resolver import TypedResolver

TODAY = date(2026, 9, 16)


def _sure(chosen: str | None) -> Decision:
    return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="scripted")


def _unsure(probs: dict[str, float]) -> Decision:
    top = max(probs, key=lambda k: probs[k])
    return Decision(chosen=top, probs=probs, provider="scripted")


def _model(*entities: Entity) -> SemanticModel:
    return SemanticModel(lens="l", dialect="duckdb", entities=list(entities))


def _resolve(
    question: str,
    model: SemanticModel,
    decisions: list[Decision],
    domains: dict[str, list[str]] | None = None,
) -> tuple[object, ScriptedDecider]:
    decider = ScriptedDecider(decisions)
    res = TypedResolver(decider, domains=domains or {}, today=TODAY).resolve(question, model)  # type: ignore[arg-type]
    return res, decider


def _src(table: str) -> EntitySource:
    return EntitySource(connection="wh", table=table)


# ── key columns are never candidates ─────────────────────────────────────────


def _fuel_levels() -> Entity:
    return Entity(
        name="fuel_levels",
        source=_src("fuel_levels"),
        default_time_field="reading_date",
        primary_key=["unique_key"],
        fields=[
            Field(name="unique_key", type="string"),
            Field(name="vessel_id", type="string"),
            Field(name="tank_status", type="string"),
            Field(name="reading_date", type="date"),
            Field(name="fuel", type="number"),
        ],
        dimensions=[Dimension(name="vessel_id"), Dimension(name="tank_status")],
        metrics=[Metric(name="fuel", agg="sum", expr="fuel_levels.fuel")],
    )


def test_key_columns_and_unasked_dates_are_not_offered() -> None:
    res, decider = _resolve(
        "Show me fuel levels",
        _model(_fuel_levels()),
        [_sure("aggregate"), _sure("fuel"), _sure(None)],
        {"tank_status": ["full", "low"]},
    )
    offered = [o for c in decider.calls for o in c["options"]]
    assert "unique_key" not in offered and "vessel_id" not in offered
    assert "reading_date" not in offered
    assert res.intent is not None and res.intent.metrics == ["fuel"]  # type: ignore[attr-defined]


def test_a_listing_clarifies_with_no_key_candidates() -> None:
    res, _ = _resolve(
        "Show me fuel levels",
        _model(_fuel_levels()),
        [_sure("listing"), _unsure({"unique_key": 0.4, "tank_status": 0.35, "fuel": 0.25})],
    )
    c = res.clarification  # type: ignore[attr-defined]
    assert c is not None and "unique_key" not in c.options and "vessel_id" not in c.options


def test_a_listing_with_only_keys_left_clarifies_on_the_metric() -> None:
    entity = Entity(
        name="bookings",
        source=_src("bookings"),
        fields=[
            Field(name="booking_id", type="string"),
            Field(name="passenger_id", type="string"),
            Field(name="booked_at", type="timestamp"),
        ],
        metrics=[Metric(name="booking_count", agg="count", expr="bookings.booking_id")],
    )
    res, _ = _resolve("Which ports do travellers book?", _model(entity), [_sure("listing")])
    c = res.clarification  # type: ignore[attr-defined]
    assert c is not None and c.term == "metric" and c.options == ["booking_count"]


def test_a_key_the_question_names_is_still_offered() -> None:
    res, _ = _resolve(
        "Fuel by vessel id",
        _model(_fuel_levels()),
        [_sure("aggregate"), _sure("fuel"), _sure(None)],
    )
    assert res.intent is not None and res.intent.dimensions == ["vessel_id"]  # type: ignore[attr-defined]


# ── an anchored question clarifies, never declines ───────────────────────────


def _goals() -> Entity:
    return Entity(
        name="crew_goals",
        source=_src("crew_goals"),
        default_time_field="month",
        use_cases=[
            "Use when: Is a deckhand on track for their monthly drill target?",
            "Avoid for: vessel-level fuel cost",
        ],
        fields=[Field(name="crew_member", type="string"), Field(name="month", type="date")],
        metrics=[Metric(name="drills", agg="count", expr="crew_goals.crew_member")],
    )


def test_a_question_the_entity_declares_clarifies_on_shape() -> None:
    res, _ = _resolve(
        "Is a deckhand on track for their monthly drill target?", _model(_goals()), [_sure(None)]
    )
    assert res.decline is None  # type: ignore[attr-defined]
    c = res.clarification  # type: ignore[attr-defined]
    assert c is not None and c.term == "shape"
    assert c.options == ["aggregate", "ranking", "listing"]


def test_an_unanchored_question_still_declines_and_avoid_lines_never_anchor() -> None:
    for q in ("tell me a joke", "vessel-level fuel cost"):
        res, _ = _resolve(q, _model(_goals()), [_sure(None)])
        assert res.decline and res.clarification is None  # type: ignore[attr-defined]


# ── siblings one parameter apart ─────────────────────────────────────────────


def _boarding() -> Entity:
    windows = ("5m", "15m", "1h", "24h")
    return Entity(
        name="boarding_weekly",
        source=_src("boarding_weekly"),
        default_time_field="week",
        fields=[
            Field(name="week", type="date"),
            Field(name="passengers", type="integer"),
            *(Field(name=f"boarded_{w}", type="integer") for w in windows),
        ],
        metrics=[
            Metric(name="passenger_total", agg="sum", expr="boarding_weekly.passengers"),
            *(
                Metric(
                    name=f"total_boarded_within_{w}", agg="sum", expr=f"boarding_weekly.boarded_{w}"
                )
                for w in windows
            ),
        ],
    )


def test_siblings_differing_by_a_parameter_the_question_lacks_clarify() -> None:
    res, _ = _resolve(
        "What share of passengers get boarded within the target time?",
        _model(_boarding()),
        [_sure("aggregate"), _sure("total_boarded_within_5m")],
    )
    c = res.clarification  # type: ignore[attr-defined]
    assert c is not None and c.term == "metric"
    assert set(c.options) == {f"total_boarded_within_{w}" for w in ("5m", "15m", "1h", "24h")}
    assert res.decisions[-1].slot == "parameter" and res.decisions[-1].verdict == "clarify"  # type: ignore[attr-defined]


def test_a_stated_parameter_picks_the_sibling() -> None:
    res, _ = _resolve(
        "How many passengers were boarded within 15 minutes?",
        _model(_boarding()),
        [_sure("aggregate"), _sure("total_boarded_within_15m"), _sure(None)],
    )
    assert res.intent is not None and res.intent.metrics == ["total_boarded_within_15m"]  # type: ignore[attr-defined]


# ── the current period resolves on the lens clock ────────────────────────────


def test_this_month_resolves_to_date_and_is_disclosed() -> None:
    entity = Entity(
        name="fares_daily",
        source=_src("fares_daily"),
        default_time_field="day",
        fields=[
            Field(name="day", type="date"),
            Field(name="route_name", type="string"),
            Field(name="is_current_month", type="boolean"),
            Field(name="fare_income", type="number"),
        ],
        dimensions=[Dimension(name="route_name")],
        metrics=[Metric(name="fare_income", agg="sum", expr="fares_daily.fare_income")],
    )
    res, decider = _resolve(
        "Which routes have the highest fare income this month?",
        _model(entity),
        [_sure("ranking"), _sure("fare_income"), _sure("route_name"), _sure(None)],
    )
    assert all("is_current_month" not in c["options"] for c in decider.calls)
    i = res.intent  # type: ignore[attr-defined]
    assert i is not None and res.clarification is None  # type: ignore[attr-defined]
    assert [(f.field, f.op, f.value) for f in i.filters] == [
        ("day", ">=", "2026-09-01"),
        ("day", "<=", "2026-09-16"),
    ]
    window = [d for d in res.decisions if d.slot == "window"]  # type: ignore[attr-defined]
    assert window and "this month: 2026-09-01 to 2026-09-16" in (window[0].chosen or "")


# ── a level over a window asks no filter ─────────────────────────────────────


def test_a_level_metric_over_a_window_asks_no_filter() -> None:
    entity = Entity(
        name="fleet_month_close",
        source=_src("fleet_month_close"),
        default_time_field="month_close",
        primary_key=["unique_key"],
        fields=[
            Field(name="unique_key", type="string"),
            Field(name="hull_id", type="string"),
            Field(name="is_month_close", type="boolean"),
            Field(name="month_close", type="date"),
        ],
        metrics=[
            Metric(
                name="vessels",
                agg="count_distinct",
                expr="fleet_month_close.hull_id",
                additivity="level",
            )
        ],
    )
    model = _model(entity)
    res, decider = _resolve(
        "How many vessels did we run at the close of 2026-08?",
        model,
        [_sure("aggregate"), _sure("vessels"), _unsure({"is_month_close": 0.5, "none": 0.5})],
    )
    assert res.clarification is None  # type: ignore[attr-defined]
    assert not any("is_month_close" in c["options"] for c in decider.calls)
    i = res.intent  # type: ignore[attr-defined]
    assert i is not None and [f.field for f in i.filters] == ["month_close", "month_close"]
    compile_intent(i, model)


# ── sibling ratios both type ─────────────────────────────────────────────────


def _retention() -> Entity:
    return Entity(
        name="pass_retention_monthly",
        source=_src("pass_retention_monthly"),
        default_time_field="month",
        fields=[
            Field(name="month", type="date"),
            Field(name="opening", type="number"),
            Field(name="kept", type="number"),
            Field(name="kept_with_upgrades", type="number"),
        ],
        metrics=[
            Metric(name="opening_total", agg="sum", expr="pass_retention_monthly.opening"),
            Metric(name="kept_total", agg="sum", expr="pass_retention_monthly.kept"),
            Metric(
                name="net_kept_total", agg="sum", expr="pass_retention_monthly.kept_with_upgrades"
            ),
            Metric(
                name="commuter_nhr",
                type="ratio",
                numerator="net_kept_total",
                denominator="opening_total",
            ),
            Metric(
                name="commuter_ghr",
                type="ratio",
                numerator="kept_total",
                denominator="opening_total",
            ),
        ],
    )


def test_a_named_ratio_types_when_the_shape_vote_says_none() -> None:
    for name in ("commuter_ghr", "commuter_nhr"):
        q = f"What was commuter {name[-3:].upper()} in 2026-08?"
        res, _ = _resolve(q, _model(_retention()), [_sure(None), _sure(name), _sure(None)])
        i = res.intent  # type: ignore[attr-defined]
        assert i is not None and i.metrics == [name], (q, res)
        shape = [d for d in res.decisions if d.slot == "shape"]  # type: ignore[attr-defined]
        assert shape[-1].chosen == "aggregate" and shape[-1].provider == "question-text"


# ── "by <dimension>" reaches the intent on the generated path ────────────────


def test_by_dimension_is_recorded_in_a_generated_intent() -> None:
    entity = Entity(
        name="cargo_month_close",
        source=_src("cargo_month_close"),
        fields=[Field(name="tonnage", type="number"), Field(name="cargo_class", type="string")],
        dimensions=[Dimension(name="cargo_class")],
        metrics=[Metric(name="tonnage", agg="sum", expr="cargo_month_close.tonnage")],
    )
    model = _model(entity)
    gated = gate_dimensions(
        "What is tonnage by cargo class?",
        QueryIntent(entity="cargo_month_close", metrics=["tonnage"]),
        model,
    )
    assert gated.dimensions == ["cargo_class"]
    res, _ = _resolve(
        "What is tonnage by cargo class?",
        model,
        [_sure("aggregate"), _sure("tonnage"), _sure(None)],
    )
    assert res.intent is not None and res.intent.dimensions == ["cargo_class"]  # type: ignore[attr-defined]


# ── a named value no column holds, an undeclared measure, a bare row count ───


def _movements() -> Entity:
    return Entity(
        name="pass_movements",
        source=_src("pass_movements"),
        grain="one row per pass movement",
        default_time_field="status_date",
        primary_key=["unique_key"],
        fields=[
            Field(name="unique_key", type="string"),
            Field(name="holder_id", type="string"),
            Field(name="pass_status", type="string"),
            Field(name="status_date", type="date"),
            Field(name="trips", type="number"),
            Field(name="fare_delta", type="number"),
        ],
        metrics=[
            Metric(name="count", agg="count"),
            Metric(name="trips_movement", agg="sum", expr="pass_movements.trips"),
            Metric(
                name="lapsed_trips",
                agg="sum",
                expr="pass_movements.trips",
                filters=["pass_movements.pass_status = 'lapse'"],
            ),
        ],
    )


_STATUS = {"pass_status": ["new", "step-down", "lapse"]}


def test_a_named_value_no_field_holds_clarifies() -> None:
    q = "What was the step-down trips movement in September 2026"
    script = [_sure("aggregate"), _sure("trips_movement"), _sure("pass_status"), _sure(None)]
    res, _ = _resolve(q + " for ISL?", _model(_movements()), script, _STATUS)
    c = res.clarification  # type: ignore[attr-defined]
    assert c is not None and c.term == "ISL" and "no field for ISL" in c.question
    res, _ = _resolve(q + "?", _model(_movements()), script, _STATUS)
    i = res.intent  # type: ignore[attr-defined]
    assert i is not None and ("pass_status", "=", "step-down") in [
        (f.field, f.op, f.value) for f in i.filters
    ]


def test_an_undeclared_measure_clarifies_instead_of_the_nearest_metric() -> None:
    res, _ = _resolve(
        "What was lapsed fare in September 2026?",
        _model(_movements()),
        [_sure("aggregate"), _sure("lapsed_trips")],
        _STATUS,
    )
    c = res.clarification  # type: ignore[attr-defined]
    assert c is not None and c.term == "fare" and "not declared as a metric" in c.question
    res, _ = _resolve(
        "What were lapsed trips in September 2026?",
        _model(_movements()),
        [_sure("aggregate"), _sure("lapsed_trips"), _sure(None)],
        _STATUS,
    )
    assert res.intent is not None, res  # type: ignore[attr-defined]


def test_a_bare_row_count_is_offered_only_for_the_entitys_own_rows() -> None:
    _res, decider = _resolve(
        "holder or trip counts", _model(_movements()), [_sure("aggregate"), _sure(None)]
    )
    metric_calls = [c for c in decider.calls if "trips_movement" in c["options"]]
    assert metric_calls and all("count" not in c["options"] for c in metric_calls)
    res, decider = _resolve(
        "How many pass movements were there in September 2026?",
        _model(_movements()),
        [_sure("aggregate"), _sure("count"), _sure(None)],
        _STATUS,
    )
    assert any("count" in c["options"] for c in decider.calls)
    assert res.intent is not None and res.intent.metrics == ["count"]  # type: ignore[attr-defined]
