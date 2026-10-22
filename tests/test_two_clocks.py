"""An entity with two clocks: a period goes on a date the question or the
figure names, never on one picked for it.

A performance is booked on one day and played on another. "How many
performances last month" counts either; the entity's default answered it by
the date played, and nothing said so. A figure declares its clock (its own
``agg_time_field``, else the default it inherits); a question with no such
figure names the date or is asked which. A question that dates a figure by a
clock it is not counted by is asked too.
"""

from __future__ import annotations

from datetime import date

import pytest

from services.contracts.protocols import Decision
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.runtime.typed_resolver import (
    TypedResolver,
    _Clarify,
    named_clocks,
    window_filters,
)

TODAY = date(2026, 10, 22)


def _entity(*, two_clocks: bool = True) -> Entity:
    metrics = [Metric(name="seats_sold", agg="sum", expr="performances.seats")]
    if two_clocks:
        metrics.append(
            Metric(
                name="seats_booked",
                agg="sum",
                expr="performances.seats",
                agg_time_field="booked_at",
            )
        )
    return Entity(
        name="performances",
        source=EntitySource(connection="wh", table="performances"),
        default_time_field="performed_at",
        primary_key=["performance_id"],
        fields=[
            Field(name="performance_id", type="string"),
            Field(name="performed_at", type="date"),
            Field(name="booked_at", type="date"),
            Field(name="seats", type="number"),
        ],
        metrics=metrics,
    )


def _field(question: str, metrics: list[str], entity: Entity | None = None) -> str:
    filters = window_filters(question, entity or _entity(), metrics, period_field=None, today=TODAY)
    assert filters
    return {f.field for f in filters}.pop()


def test_a_question_with_no_figure_is_asked_which_date() -> None:
    with pytest.raises(_Clarify) as exc:
        _field("how many performances last month", [])
    c = exc.value.request
    assert c.term == "time_field"
    assert c.options == ["performed_at", "booked_at"]


@pytest.mark.parametrize(
    ("question", "clock"),
    [
        ("how many performances were booked last month", "booked_at"),
        ("performances by booking date in September 2026", "booked_at"),
        ("how many performances were performed last month", "performed_at"),
    ],
)
def test_a_question_naming_the_date_uses_it(question: str, clock: str) -> None:
    assert _field(question, []) == clock


@pytest.mark.parametrize(
    ("question", "metric", "clock"),
    [
        # The metric's own clock; "booked" in its name names the figure, not a date.
        ("seats booked last month", "seats_booked", "booked_at"),
        # The default it inherits is its declared clock.
        ("seats sold last month", "seats_sold", "performed_at"),
    ],
)
def test_a_figure_s_declared_clock_settles_it(question: str, metric: str, clock: str) -> None:
    assert _field(question, [metric]) == clock


def test_a_figure_dated_by_another_clock_is_asked() -> None:
    with pytest.raises(_Clarify) as exc:
        _field("seats sold by booking date last month", ["seats_sold"])
    assert exc.value.request.term == "time_field"
    assert "performed_at" in exc.value.request.question


def test_one_clock_is_unchanged() -> None:
    assert _field("how many performances last month", [], _entity(two_clocks=False)) == (
        "performed_at"
    )


def test_metric_words_never_name_a_clock() -> None:
    assert named_clocks("seats booked last month", _entity(), ["seats_booked"]) == []
    assert named_clocks("seats sold, booked last month", _entity(), ["seats_sold"]) == ["booked_at"]


class _Chooser:
    def __init__(self, *prefer: str) -> None:
        self.prefer = prefer

    def decide(self, *, question: str, context: str, options: list, allow_none: bool) -> Decision:  # type: ignore[type-arg]
        names = [o.name for o in options]
        chosen = next((p for p in self.prefer if p in names), names[0])
        return Decision(
            chosen=None if chosen == "none" else chosen,
            probs={chosen: 1.0},
            provider="scripted",
        )


def _resolve(question: str, *prefer: str):  # type: ignore[no-untyped-def]
    model = SemanticModel(lens="shows", dialect="duckdb", entities=[_entity()])
    return TypedResolver(_Chooser(*prefer), today=TODAY).resolve(question, model)  # type: ignore[arg-type]


def test_the_typed_door_asks_which_date_for_a_listing() -> None:
    res = _resolve("List the performances last month", "listing", "performance_id", "none")
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "time_field"


def test_the_typed_door_serves_a_figure_on_its_own_clock() -> None:
    res = _resolve("seats booked last month", "aggregate", "seats_booked", "none")
    assert res.intent is not None, res.clarification
    assert {f.field for f in res.intent.filters} == {"booked_at"}
