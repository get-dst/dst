"""A restriction on a joined entity's field resolves through the declared join.

"Crossings of in-service vessels" asked of crossings joined to vessels was
asked back ("vessels has it; crossings does not, which?") although the lens
declares the join and the compiler reads a joined field. A stored value or a
flag the question names on an entity the declared joins reach without
fanning the figure's own rows out is a filter through that join; an
undeclared or fanning relationship keeps the question back.
"""

from __future__ import annotations

import duckdb
import pytest

from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    Join,
    Metric,
    SemanticModel,
)
from services.runtime.compiler import compile_intent
from services.runtime.typed_resolver import TypedResolver
from tests.test_spoken_period_shapes import TODAY
from tests.test_typed_question_shapes import _Prefers

DOMAINS = {"status": ["in service", "laid up"], "home_port": ["Kelmar", "Ostvik"]}


def _model(relationship: str | None = "many_to_one") -> SemanticModel:
    crossings = Entity(
        name="crossings",
        source=EntitySource(connection="wh", table="crossings"),
        default_time_field="sailed_on",
        primary_key=["crossing_id"],
        fields=[
            Field(name="crossing_id", type="string"),
            Field(name="vessel_id", type="string"),
            Field(name="sailed_on", type="date"),
            Field(name="passengers", type="integer"),
        ],
        metrics=[
            Metric(
                name="passenger_total", agg="sum", expr="crossings.passengers", label="passengers"
            )
        ],
    )
    vessels = Entity(
        name="vessels",
        source=EntitySource(connection="wh", table="vessels"),
        primary_key=["vessel_id"],
        fields=[
            Field(name="vessel_id", type="string"),
            Field(name="vessel_name", type="string"),
            Field(name="status", type="string"),
            Field(name="home_port", type="string"),
            Field(name="is_chartered", type="boolean"),
        ],
        metrics=[Metric(name="vessel_count", agg="count", label="vessels")],
    )
    join = Join(
        left="crossings",
        right="vessels",
        on="crossings.vessel_id = vessels.vessel_id",
        relationship=relationship,  # type: ignore[arg-type]
    )
    return SemanticModel(
        lens="ferries", dialect="duckdb", entities=[crossings, vessels], joins=[join]
    )


def _run(sql: str) -> list[tuple[object, ...]]:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE crossings AS SELECT * FROM (VALUES "
        "('c1', 'v1', DATE '2026-09-03', 100), ('c2', 'v2', DATE '2026-09-04', 40),"
        "('c3', 'v3', DATE '2026-09-05', 7), ('c4', 'v1', DATE '2026-08-05', 60)"
        ") AS t(crossing_id, vessel_id, sailed_on, passengers)"
    )
    con.execute(
        "CREATE TABLE vessels AS SELECT * FROM (VALUES "
        "('v1', 'Tern', 'in service', 'Kelmar', false),"
        "('v2', 'Skua', 'laid up', 'Ostvik', true),"
        "('v3', 'Gull', 'in service', 'Ostvik', true)"
        ") AS t(vessel_id, vessel_name, status, home_port, is_chartered)"
    )
    return con.execute(sql).fetchall()


def _resolve(question: str, model: SemanticModel, *prefer: str):  # type: ignore[no-untyped-def]
    return TypedResolver(
        _Prefers("aggregate", "passenger_total", *prefer),  # type: ignore[arg-type]
        domains=DOMAINS,
        today=TODAY,
    ).resolve(question, model)


@pytest.mark.parametrize(
    ("question", "field", "value", "total"),
    [
        ("Passengers on in service vessels in September 2026", "vessels.status", "in service", 107),
        ("Passengers on Ostvik vessels in September 2026", "vessels.home_port", "Ostvik", 47),
    ],
)
def test_a_joined_entitys_value_filters_through_the_join(
    question: str, field: str, value: str, total: int
) -> None:
    model = _model()
    res = _resolve(question, model, field)
    assert res.intent is not None, res.clarification
    assert res.intent.entity == "crossings"
    assert [(f.field, f.value) for f in res.intent.filters if f.field == field] == [(field, value)]
    assert _run(compile_intent(res.intent, model)) == [(total,)]


def test_a_joined_flag_named_by_its_subject_filters_through_the_join() -> None:
    model = _model()
    res = _resolve("Passengers on chartered vessels in September 2026", model, "true")
    assert res.intent is not None, res.clarification
    assert [(f.field, f.value) for f in res.intent.filters][0] == ("vessels.is_chartered", "true")


def test_a_comparison_carries_the_joined_filter() -> None:
    model = _model()
    res = _resolve(
        "How did passengers on in service vessels change in September 2026?",
        model,
        "vessels.status",
    )
    assert res.intent is not None, res.clarification
    assert res.intent.compare is not None
    assert ("vessels.status", "in service") in [(f.field, f.value) for f in res.intent.filters]
    # September 107 against August 60
    assert _run(compile_intent(res.intent, model))[0][:3] == (107, 60, 47)


def test_the_decisions_none_asks_never_drops() -> None:
    res = _resolve("Passengers on in service vessels in September 2026", _model())
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "in service"


@pytest.mark.parametrize("relationship", [None, "one_to_many"])
def test_a_join_that_may_fan_out_keeps_the_question_back(relationship: str | None) -> None:
    res = _resolve(
        "Passengers on in service vessels in September 2026",
        _model(relationship),
        "vessels.status",
    )
    assert res.intent is None and res.clarification is not None
    assert "vessels has it; crossings does not" in res.clarification.question
