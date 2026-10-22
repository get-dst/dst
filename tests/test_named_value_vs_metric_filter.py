"""A stored value the question names word for word against a metric whose own
filter pins that column to another value.

A question naming a hyphenated value word for word could be served with a
sibling metric pinned to another value: the decider preferred it, the
question's literal was then dropped at compile as "governed by" that metric,
and the other value's figure went out as the named one's. Such a metric can
never answer the question, so the resolver does not offer it, and the compiler
refuses the pair outright instead of dropping the literal.
"""

from __future__ import annotations

from datetime import date

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
from services.runtime.compiler import CompileError, compile_intent, govern_literals, metric_pins
from services.runtime.typed_resolver import TypedResolver

TRIP_TYPES = ["standard", "group", "one-way", "overnight"]


def _model() -> SemanticModel:
    return SemanticModel(
        lens="rentals",
        dialect="duckdb",
        entities=[
            Entity(
                name="rental_trips",
                source=EntitySource(connection="wh", table="rental_trips"),
                default_time_field="trip_date",
                fields=[
                    Field(name="trip_date", type="date"),
                    Field(name="trip_type", type="string"),
                    Field(name="ride_minutes", type="number"),
                ],
                dimensions=[Dimension(name="trip_type")],
                metrics=[
                    Metric(name="total_ride_minutes", agg="sum", expr="rental_trips.ride_minutes"),
                    Metric(
                        name="overnight_ride_minutes",
                        agg="sum",
                        expr="rental_trips.ride_minutes",
                        filters=["rental_trips.trip_type = 'overnight'"],
                    ),
                    Metric(
                        name="group_ride_minutes",
                        agg="sum",
                        expr="CASE WHEN rental_trips.trip_type = 'group' "
                        "THEN rental_trips.ride_minutes END",
                    ),
                ],
            )
        ],
    )


def _sure(chosen: str | None) -> Decision:
    return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="scripted")


class _Chooser:
    """Picks the first preferred name among the options offered, else the
    first option: a decider that prefers a pinned metric whenever it is on
    offer, the way a live one can."""

    def __init__(self, prefer: list[str]) -> None:
        self.prefer = prefer
        self.offered: list[list[str]] = []

    def decide(self, *, question: str, context: str, options: list, allow_none: bool) -> Decision:  # type: ignore[type-arg]
        names = [o.name for o in options]
        self.offered.append(names)
        chosen = next((p for p in self.prefer if p in names), names[0])
        return _sure(None if chosen == "none" else chosen)


def test_metric_pins_read_filters_and_a_selecting_case() -> None:
    by = {m.name: m for m in _model().entities[0].metrics}
    assert metric_pins(by["overnight_ride_minutes"], "duckdb") == {"trip_type": {"overnight"}}
    assert metric_pins(by["group_ride_minutes"], "duckdb") == {"trip_type": {"group"}}
    assert metric_pins(by["total_ride_minutes"], "duckdb") == {}
    # a CASE with an ELSE that counts the other rows selects nothing
    signed = Metric(
        name="signed",
        agg="sum",
        expr="CASE WHEN t.trip_type = 'overnight' THEN -t.x ELSE t.x END",
    )
    assert metric_pins(signed, "duckdb") == {}
    either = Metric(
        name="lost", agg="sum", expr="t.x", filters=["t.trip_type IN ('overnight', 'one-way')"]
    )
    assert metric_pins(either, "duckdb") == {"trip_type": {"overnight", "one-way"}}


def test_a_metric_pinned_to_another_value_is_never_offered() -> None:
    decider = _Chooser(["aggregate", "overnight_ride_minutes", "group_ride_minutes"])
    res = TypedResolver(
        decider,  # type: ignore[arg-type]
        domains={"trip_type": TRIP_TYPES},
        today=date(2026, 10, 22),
    ).resolve("What were the one-way ride minutes in September 2026?", _model())
    metric_round = next(o for o in decider.offered if "total_ride_minutes" in o)
    assert "overnight_ride_minutes" not in metric_round
    assert "group_ride_minutes" not in metric_round
    assert res.intent is not None, res.clarification or res.decline
    assert res.intent.metrics == ["total_ride_minutes"]
    assert ("trip_type", "=", "one-way") in [(f.field, f.op, f.value) for f in res.intent.filters]
    sql = compile_intent(res.intent, _model())
    assert "'one-way'" in sql and "'overnight'" not in sql


def test_a_metric_pinned_to_the_named_value_stays_on_offer() -> None:
    decider = _Chooser(["aggregate", "overnight_ride_minutes"])
    res = TypedResolver(
        decider,  # type: ignore[arg-type]
        domains={"trip_type": TRIP_TYPES},
        today=date(2026, 10, 22),
    ).resolve("What were the overnight ride minutes in September 2026?", _model())
    assert res.intent is not None and res.intent.metrics == ["overnight_ride_minutes"]
    # the metric's own value: not a second predicate
    assert all(f.field != "trip_type" for f in res.intent.filters)


def test_every_metric_pinned_elsewhere_clarifies() -> None:
    model = _model()
    entity = model.entities[0]
    entity.metrics = [m for m in entity.metrics if m.name != "total_ride_minutes"]
    res = TypedResolver(
        ScriptedDecider([_sure("aggregate")]),
        domains={"trip_type": TRIP_TYPES},
        today=date(2026, 10, 22),
    ).resolve("What were the one-way ride minutes in September 2026?", model)
    assert res.intent is None and res.clarification is not None
    assert "one-way" in res.clarification.question


def test_compile_refuses_a_literal_the_metric_excludes() -> None:
    """The tripwire on every door: a literal the metric's filter rules out is
    a contradiction, never a restatement to drop."""
    intent = QueryIntent(
        entity="rental_trips",
        metrics=["overnight_ride_minutes"],
        filters=[IntentFilter(field="trip_type", value="one-way")],
    )
    with pytest.raises(CompileError, match="counts only trip_type = 'overnight'"):
        govern_literals(intent, _model())
    with pytest.raises(CompileError):
        compile_intent(intent, _model())
    # the same value as the metric's own is still a restatement, dropped
    same = intent.model_copy(
        update={"filters": [IntentFilter(field="trip_type", value="overnight")]}
    )
    assert govern_literals(same, _model()).literals_dropped == [
        "trip_type = 'overnight' (governed by overnight_ride_minutes)"
    ]
    by_case = QueryIntent(
        entity="rental_trips",
        metrics=["group_ride_minutes"],
        filters=[IntentFilter(field="trip_type", op="in", value=["group", "overnight"])],
    )
    with pytest.raises(CompileError, match="group"):
        govern_literals(by_case, _model())
