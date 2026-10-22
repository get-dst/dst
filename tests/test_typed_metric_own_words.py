"""What the chosen metric's own name carries is the metric's, not another slot's.

A number in a metric's name is its setting (`answered_within_15m`): "within 15
minutes" picks that metric and is never a literal a numeric filter takes. The
word a metric is named for ("branch visits" of `branch_visits`) says what it
measures: it is not a breakdown by `branch` unless the question asks one ("by
branch", "per branch", a ranking), so the dimension vote is not asked over it.
"""

from __future__ import annotations

from datetime import date

from services.contracts.protocols import Decision
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.typed_resolver import TypedResolver

TODAY = date(2026, 10, 22)


class _Chooser:
    """Picks the first preferred name among the options offered, else `none`
    when it is offered, else the first option; records every slot it saw.
    With ``filters=False`` it restricts by nothing."""

    def __init__(self, *prefer: str, filters: bool = True) -> None:
        self.prefer = ("aggregate", *prefer)
        self.filters = filters
        self.seen: list[tuple[str, list[str]]] = []

    def decide(self, *, question: str, context: str, options: list, allow_none: bool) -> Decision:  # type: ignore[type-arg]
        names = [o.name for o in options]
        self.seen.append((context, names))
        prefer = self.prefer if self.filters or "restrict" not in context else ()
        chosen = next((p for p in prefer if p in names), "none" if "none" in names else names[0])
        return Decision(
            chosen=None if chosen == "none" else chosen, probs={chosen: 1.0}, provider="scripted"
        )


def _resolve(question: str, model: SemanticModel, chooser: _Chooser):  # type: ignore[no-untyped-def]
    res = TypedResolver(chooser, domains={"queue": ["billing", "repairs"]}, today=TODAY).resolve(  # type: ignore[arg-type]
        question, model
    )
    assert res.intent is not None, res.clarification
    return res.intent


def _offered(chooser: _Chooser, prefix: str) -> list[str]:
    return [n for ctx, names in chooser.seen if ctx.startswith(prefix) for n in names]


# ── a number in the metric's name is its setting ─────────────────────────────


def _calls() -> SemanticModel:
    """A help line's calls: one row per call, with how long it waited."""
    calls = Entity(
        name="calls",
        source=EntitySource(connection="wh", table="calls"),
        default_time_field="called_on",
        primary_key=["call_id"],
        grain="one row per call",
        fields=[
            Field(name="call_id", type="string"),
            Field(name="called_on", type="date"),
            Field(name="queue", type="string"),
            Field(name="wait_minutes", type="integer"),
        ],
        metrics=[
            Metric(
                name="answered_within_15m",
                agg="count",
                expr="calls.call_id",
                filters=["calls.wait_minutes <= 15"],
                label="calls answered within 15 minutes",
            )
        ],
    )
    return SemanticModel(lens="helpline", dialect="duckdb", entities=[calls])


def test_a_number_the_metric_is_named_for_never_becomes_a_filter() -> None:
    chooser = _Chooser("answered_within_15m", "wait_minutes")
    intent = _resolve(
        "How many calls were answered within 15 minutes in May 2026?", _calls(), chooser
    )
    assert [f.field for f in intent.filters] == ["called_on", "called_on"]
    assert "wait_minutes" not in _offered(chooser, "Does the question restrict")


def test_another_number_the_question_states_still_filters() -> None:
    chooser = _Chooser("answered_within_15m", "wait_minutes", ">")
    intent = _resolve(
        "How many calls answered within 15 minutes had waited over 5 minutes in May 2026?",
        _calls(),
        chooser,
    )
    assert ("wait_minutes", ">", 5) in [(f.field, f.op, f.value) for f in intent.filters]


# ── the word the metric is named for is not a breakdown ──────────────────────


def _visits() -> SemanticModel:
    """A library's daily door counts, per branch."""
    visits = Entity(
        name="visits",
        source=EntitySource(connection="wh", table="visits"),
        default_time_field="visited_on",
        primary_key=["visited_on", "branch"],
        fields=[
            Field(name="visited_on", type="date"),
            Field(name="branch", type="string"),
            Field(name="visitors", type="integer"),
        ],
        dimensions=[Dimension(name="branch")],
        metrics=[
            Metric(name="branch_visits", agg="sum", expr="visits.visitors", label="branch visits")
        ],
    )
    return SemanticModel(lens="library", dialect="duckdb", entities=[visits])


def test_the_metrics_subject_word_is_not_put_to_the_dimension_vote() -> None:
    chooser = _Chooser("branch_visits", "branch", filters=False)
    intent = _resolve("What were branch visits in May 2026?", _visits(), chooser)
    assert intent.dimensions == []
    assert "branch" not in _offered(chooser, "Is the figure broken down")


def test_a_breakdown_the_question_asks_by_the_subject_still_groups() -> None:
    chooser = _Chooser("branch_visits", "branch", "highest", filters=False)
    for question in (
        "Branch visits by branch in May 2026",
        "Which branch had the most branch visits in May 2026?",
        "Branch visits per branch in May 2026",
    ):
        assert _resolve(question, _visits(), chooser).dimensions == ["branch"], question
