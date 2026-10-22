"""A figure asked over time on an entity with no clock is refused, not totalled.

A venue list has capacities and no date. "Total capacity by month" typed to
the plain total: the breakdown was asked only when the entity declared a time
field, so without one the words "by month" were dropped and one number came
back for a series. The lens covers the figure; it cannot split it, and says so.
"""

from __future__ import annotations

from datetime import date

import pytest

from services.contracts.protocols import Decision
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.runtime.typed_resolver import TypedResolver, split_over_time

TODAY = date(2026, 10, 22)


class _Chooser:
    def __init__(self, *prefer: str) -> None:
        self.prefer = prefer

    def decide(self, *, question: str, context: str, options: list, allow_none: bool) -> Decision:  # type: ignore[type-arg]
        names = [o.name for o in options]
        chosen = next((p for p in self.prefer if p in names), names[0])
        return Decision(
            chosen=None if chosen == "none" else chosen, probs={chosen: 1.0}, provider="scripted"
        )


def _venues(*fields: Field, time_field: str | None = None) -> SemanticModel:
    entity = Entity(
        name="venues",
        source=EntitySource(connection="wh", table="venues"),
        primary_key=["venue_id"],
        default_time_field=time_field,
        fields=[
            Field(name="venue_id", type="string"),
            Field(name="city", type="string"),
            Field(name="capacity", type="number"),
            *fields,
        ],
        metrics=[Metric(name="total_capacity", agg="sum", expr="venues.capacity")],
    )
    return SemanticModel(lens="venues", dialect="duckdb", entities=[entity])


def _resolve(question: str, model: SemanticModel, *prefer: str):  # type: ignore[no-untyped-def]
    chooser = _Chooser("aggregate", "total_capacity", *prefer, "none")
    return TypedResolver(chooser, today=TODAY).resolve(question, model)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("question", "split"),
    [
        ("total capacity by month", "by month"),
        ("monthly total capacity", "by month"),
        ("total capacity per week", "by week"),
        ("total capacity trend", "over time"),
    ],
)
def test_a_split_the_entity_cannot_make_is_refused(question: str, split: str) -> None:
    res = _resolve(question, _venues())
    assert res.intent is None
    assert res.decline == (
        f"this lens cannot split total_capacity {split}: venues declares no time field"
    )


def test_the_plain_figure_still_serves() -> None:
    res = _resolve("total capacity", _venues())
    assert res.intent is not None and res.intent.metrics == ["total_capacity"]


def test_a_date_breakdown_the_question_names_still_splits() -> None:
    model = _venues(Field(name="opened_on", type="date"))
    res = _resolve("total capacity by month by opened_on", model, "opened_on")
    assert res.intent is not None, res.decline


def test_an_entity_with_a_clock_buckets_as_before() -> None:
    model = _venues(Field(name="opened_on", type="date"), time_field="opened_on")
    res = _resolve("total capacity by month", model, "month")
    assert res.intent is not None, res.decline


def test_the_split_is_named_as_asked() -> None:
    assert split_over_time("revenue quarterly") == "by quarter"
    assert split_over_time("revenue week over week") == "by week"
    assert split_over_time("revenue over time") == "over time"
