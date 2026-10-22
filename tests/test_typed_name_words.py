"""Words of the chosen metric's own name are the metric's, not other slots'.

A grain word inside a metric's name or label ("daily target" of
`daily_target_loaves`) names the figure; it does not ask for a daily series.
A parenthesised code after a noun ("server errors (5xx)") is a stored value of
the column whose dictionary holds it, or part of the metric's name; its digits
are never a number a filter takes, and it never asks for a binding.
"""

from __future__ import annotations

from datetime import date

from services.contracts.protocols import Decision
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.runtime.typed_resolver import TypedResolver, sole_metric_intent

TODAY = date(2026, 10, 22)


class _Eager:
    """Picks the first preferred option offered, else `none` when offered,
    else the first: told to, it picks a grain or says a value is stated
    wherever it is asked."""

    def __init__(self, *prefer: str) -> None:
        self.prefer = ("aggregate", *prefer)

    def decide(self, *, question: str, context: str, options: list, allow_none: bool) -> Decision:  # type: ignore[type-arg]
        names = [o.name for o in options]
        if "stated" in names:
            # A value is stated where the question still carries the code.
            chosen = "stated" if "5xx" in question else "unstated"
            return Decision(chosen=chosen, probs={chosen: 1.0}, provider="scripted")
        chosen = next(
            (p for p in self.prefer if p in names), "none" if "none" in names else names[0]
        )
        return Decision(
            chosen=None if chosen == "none" else chosen, probs={chosen: 1.0}, provider="scripted"
        )


def _bakery() -> SemanticModel:
    """A bakery's daily plan: how many loaves each day was meant to bake."""
    plan = Entity(
        name="bake_plan",
        source=EntitySource(connection="wh", table="bake_plan"),
        default_time_field="plan_day",
        primary_key=["plan_day"],
        fields=[
            Field(name="plan_day", type="date"),
            Field(name="daily_target_loaves", type="integer"),
        ],
        metrics=[
            Metric(
                name="daily_target_loaves",
                agg="sum",
                expr="bake_plan.daily_target_loaves",
                label="daily target loaves",
            )
        ],
    )
    return SemanticModel(lens="bakery", dialect="duckdb", entities=[plan])


def test_a_grain_word_in_the_metrics_name_is_not_a_series() -> None:
    res = TypedResolver(_Eager("daily_target_loaves", "day"), today=TODAY).resolve(  # type: ignore[arg-type]
        "What was the daily target loaves in May 2026?", _bakery()
    )
    assert res.intent is not None, res.clarification
    assert res.intent.grain is None
    assert not any(r.slot == "grain" for r in res.decisions)


def test_a_grain_word_beside_the_name_still_asks_a_series() -> None:
    res = TypedResolver(_Eager("daily_target_loaves", "week"), today=TODAY).resolve(  # type: ignore[arg-type]
        "Daily target loaves by week in May 2026", _bakery()
    )
    assert res.intent is not None, res.clarification
    assert res.intent.grain == "week"


def test_the_sole_metric_door_reads_the_name_as_the_figure() -> None:
    intent = sole_metric_intent("daily target loaves in May 2026", _bakery(), today=TODAY)
    assert intent is not None
    assert intent.grain is None and intent.metrics == ["daily_target_loaves"]


def _requests(*, status_dictionary: bool) -> tuple[SemanticModel, dict[str, list[str]]]:
    """A web service's request log."""
    log = Entity(
        name="requests",
        source=EntitySource(connection="wh", table="requests"),
        default_time_field="served_on",
        primary_key=["request_id"],
        grain="one row per request",
        fields=[
            Field(name="request_id", type="string"),
            Field(name="served_on", type="date"),
            Field(name="status_code", type="integer"),
            Field(name="status_class", type="string"),
            Field(name="route", type="string"),
        ],
        metrics=[
            Metric(
                name="server_errors",
                agg="count",
                expr="requests.request_id",
                filters=["requests.status_code >= 500"],
                label="server errors (5xx)",
            ),
            Metric(name="requests_served", agg="count", expr="requests.request_id"),
        ],
    )
    domains = {"status_class": ["2xx", "4xx", "5xx"]} if status_dictionary else {}
    return SemanticModel(lens="web", dialect="duckdb", entities=[log]), domains


def test_a_code_the_metrics_name_carries_is_not_a_filter_or_a_binding() -> None:
    model, domains = _requests(status_dictionary=False)
    res = TypedResolver(
        _Eager("server_errors", "status_class"), domains=domains, today=TODAY
    ).resolve(  # type: ignore[arg-type]
        "How many server errors (5xx) in May 2026?", model
    )
    assert res.intent is not None, res.clarification
    assert [f.field for f in res.intent.filters] == ["served_on", "served_on"]


def test_a_code_a_dictionary_holds_is_that_columns_value() -> None:
    model, domains = _requests(status_dictionary=True)
    res = TypedResolver(
        _Eager("requests_served", "status_code", "status_class", "="), domains=domains, today=TODAY
    ).resolve(  # type: ignore[arg-type]
        "How many requests (5xx) in May 2026?", model
    )
    assert res.intent is not None, res.clarification
    assert ("status_class", "5xx") in [(f.field, f.value) for f in res.intent.filters]
    assert "status_code" not in [f.field for f in res.intent.filters]


def _ovens() -> SemanticModel:
    """A bakery's ovens, one snapshot row per oven per day; no dimensions
    declared, so a breakdown is offered only when the question asks one."""
    days = Entity(
        name="oven_days",
        source=EntitySource(connection="wh", table="oven_days"),
        default_time_field="day",
        primary_key=["day", "oven_id"],
        fields=[
            Field(name="day", type="date"),
            Field(name="oven_id", type="string"),
            Field(name="trays", type="integer", description="trays the oven holds that day"),
            Field(name="bakes", type="integer"),
            Field(name="oven_model", type="string"),
        ],
        metrics=[
            Metric(name="bakes", agg="sum", expr="oven_days.bakes"),
            Metric(name="trays", agg="sum", expr="oven_days.trays", additivity="level"),
            Metric(
                name="bakes_per_tray_day",
                type="ratio",
                numerator="bakes",
                denominator="trays",
                period_weight="days",
                label="bakes per tray per day",
            ),
        ],
    )
    return SemanticModel(lens="ovens", dialect="duckdb", entities=[days])


def test_a_per_phrase_inside_the_metrics_label_is_not_a_breakdown() -> None:
    # "per tray" is the figure's own name: a decider reading it as "group by
    # the oven model" served one ratio per model nobody asked for.
    res = TypedResolver(_Eager("bakes_per_tray_day", "oven_model"), today=TODAY).resolve(  # type: ignore[arg-type]
        "What were bakes per tray per day in May 2026?", _ovens()
    )
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions == []


def test_a_per_phrase_beside_the_label_still_breaks_down() -> None:
    res = TypedResolver(_Eager("bakes_per_tray_day", "oven_model"), today=TODAY).resolve(  # type: ignore[arg-type]
        "Bakes per tray per day per oven model in May 2026", _ovens()
    )
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions == ["oven_model"]
