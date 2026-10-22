"""The figure a question asks for must be one the chosen metric measures.

An entity can carry columns for several measures while declaring a metric
for only one of them, a count. A question asking for another measure by a
business acronym ("GRV" for gross rental value) names no column and no
definition term, so the undeclared-measure rail stays quiet, the metric vote
has one confident option, and acting on it serves a count for a value
question. The quantity word now has to map to the chosen metric (its name,
label, aliases, description, the columns it reads, or an acronym one of those
or a definition spells out); a word that maps to nothing clarifies.
"""

from __future__ import annotations

from datetime import date

from services.contracts.protocols import Decision
from services.contracts.semantic_model import (
    Definition,
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.typed_resolver import TypedResolver, unconsumed_quantity

_RENTALS = Metric(name="rental_count", agg="count", expr="rentals.rental_id", label="rentals")


def _entity(*metrics: Metric) -> Entity:
    """A bike-rental chain's rentals: columns for value, minutes and rentals,
    and by default one metric, the rental count."""
    return Entity(
        name="rentals",
        source=EntitySource(connection="wh", table="rentals"),
        default_time_field="started_on",
        primary_key=["rental_id"],
        grain="one row per ride",
        fields=[
            Field(name="rental_id", type="string"),
            Field(name="started_on", type="date"),
            Field(name="station", type="string"),
            Field(name="gross_rental_value", type="number"),
            Field(name="ride_minutes", type="number"),
            Field(name="amount", type="number"),
        ],
        dimensions=[Dimension(name="station")],
        metrics=list(metrics) or [_RENTALS],
    )


def _model(*metrics: Metric, definitions: list[Definition] | None = None) -> SemanticModel:
    return SemanticModel(
        lens="rentals",
        dialect="duckdb",
        entities=[_entity(*metrics)],
        definitions=definitions or [],
    )


class _Chooser:
    """Picks the first preferred name among the options offered, else `none`
    when it is offered, else the first option: one confident vote per slot."""

    def __init__(self, *prefer: str) -> None:
        self.prefer = prefer

    def decide(self, *, question: str, context: str, options: list, allow_none: bool) -> Decision:  # type: ignore[type-arg]
        names = [o.name for o in options]
        chosen = next(
            (p for p in self.prefer if p in names), "none" if "none" in names else names[0]
        )
        return Decision(
            chosen=None if chosen == "none" else chosen, probs={chosen: 1.0}, provider="scripted"
        )


def _resolve(question: str, model: SemanticModel, *prefer: str):  # type: ignore[no-untyped-def]
    return TypedResolver(
        _Chooser("aggregate", *prefer),  # type: ignore[arg-type]
        domains={"station": ["harbour", "old town"]},
        today=date(2026, 10, 22),
    ).resolve(question, model)


# ── an acronym with only a count declared ────────────────────────────────────


def test_an_acronym_with_only_a_count_metric_clarifies() -> None:
    for question in ("What was GRV in September 2026?", "GRV by station last month"):
        res = _resolve(question, _model(), "rental_count")
        assert res.intent is None, question
        c = res.clarification
        assert c is not None and c.term == "grv", question
        assert "no metric for 'grv'; it measures rentals" in c.question
        assert c.options == ["rental_count"]


def test_a_definition_spelling_the_acronym_out_does_not_make_a_count_answer_it() -> None:
    grv = Definition(term="GRV", body="gross rental value: what riders paid, before refunds")
    res = _resolve("What was total GRV in September 2026?", _model(definitions=[grv]))
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "grv"


# ── an acronym a metric or a definition spells out ───────────────────────────


def test_an_acronym_in_a_metric_description_acts_on_that_metric() -> None:
    takings = Metric(
        name="takings",
        agg="sum",
        expr="rentals.amount",
        description="GRV: what riders paid, before refunds",
    )
    res = _resolve("What was GRV in September 2026?", _model(_RENTALS, takings), "takings")
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["takings"]


def test_an_acronym_a_metric_column_spells_acts() -> None:
    value = Metric(name="value_total", agg="sum", expr="rentals.gross_rental_value")
    res = _resolve("GRV by station", _model(_RENTALS, value), "value_total", "station")
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["value_total"]


def test_an_acronym_a_definition_spells_out_acts_on_the_metric_it_names() -> None:
    grv = Definition(term="GRV", body="gross rental value: what riders paid, before refunds")
    value = Metric(name="takings", agg="sum", expr="rentals.amount", label="gross rental takings")
    res = _resolve(
        "What was GRV in September 2026?", _model(_RENTALS, value, definitions=[grv]), "takings"
    )
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["takings"]


# ── counts ───────────────────────────────────────────────────────────────────


def test_a_plain_count_question_acts_on_the_count_metric() -> None:
    res = _resolve("How many rentals were there in September 2026?", _model(), "rental_count")
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["rental_count"]


def test_a_count_of_something_the_count_does_not_count_clarifies() -> None:
    res = _resolve("How many helmets were lent in September 2026?", _model(), "rental_count")
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "helmets"


def test_a_count_counts_its_grain_and_leaves_a_sum_to_the_count_rail() -> None:
    e = _entity()
    model = _model()
    # a row count counts what one row is ("one row per ride")
    assert unconsumed_quantity("How many rides in May?", e, ["rental_count"], model, {}) is None
    minutes = Metric(name="minutes_ridden", agg="sum", expr="rentals.ride_minutes")
    e = _entity(_RENTALS, minutes)
    # a sum answering "how many" is the count rail's judgment, not this one's
    assert (
        unconsumed_quantity("How many ride minutes in May?", e, ["minutes_ridden"], model, {})
        is None
    )


# ── a question naming the metric ─────────────────────────────────────────────


def test_a_question_naming_the_metric_label_acts() -> None:
    value = Metric(
        name="grv_total", agg="sum", expr="rentals.gross_rental_value", label="rental takings"
    )
    res = _resolve(
        "What were rental takings by station in September 2026?",
        _model(_RENTALS, value),
        "grv_total",
        "station",
    )
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["grv_total"]


def test_a_value_or_dimension_word_is_not_a_quantity_word() -> None:
    e = _entity()
    model = _model()
    domains = {"station": ["harbour", "old town"]}
    assert unconsumed_quantity("Harbour by month", e, ["rental_count"], model, domains) is None
    assert (
        unconsumed_quantity("What were stations in May", e, ["rental_count"], model, domains)
        is None
    )


def test_a_word_that_is_not_a_measure_stays_the_deciders_reading() -> None:
    e = _entity()
    model = _model()
    for question in (
        "What was the busiest station in May?",
        "What is the difference in rentals between May and June?",
        "What was turnover in May?",
    ):
        assert unconsumed_quantity(question, e, ["rental_count"], model, {}) is None, question
