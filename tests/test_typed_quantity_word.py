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


# ── false positives are forbidden ────────────────────────────────────────────


def _replies(*metrics: Metric, fields: list[Field] | None = None) -> Entity:
    """A help desk's replies: one row per reply sent."""
    return Entity(
        name="replies",
        source=EntitySource(connection="wh", table="replies"),
        default_time_field="sent_on",
        primary_key=["reply_id"],
        grain="one row per reply sent",
        fields=[
            Field(name="reply_id", type="string"),
            Field(name="sent_on", type="date"),
            Field(name="channel", type="string"),
            Field(name="ai_suggested", type="boolean", description="a drafted reply was offered"),
            Field(name="first_response_minutes", type="number"),
            *(fields or []),
        ],
        metrics=list(metrics),
    )


_SENT = Metric(name="replies_sent", agg="count", expr="replies.reply_id", label="replies sent")


def _unconsumed(question: str, entity: Entity, chosen: str) -> str | None:
    model = SemanticModel(lens="desk", dialect="duckdb", entities=[entity])
    return unconsumed_quantity(question, entity, [chosen], model, {"channel": ["email", "chat"]})


def test_a_word_the_metric_carries_counts_though_a_field_name_carries_it_too() -> None:
    assisted = Metric(name="ai_assisted_replies", agg="count", expr="replies.reply_id")
    e = _replies(_SENT, assisted)
    # "ai" of `ai_suggested`, "sent" of `sent_on`: still the chosen metric's words
    assert _unconsumed("How many AI drafts in May 2026?", e, "ai_assisted_replies") is None
    assert _unconsumed("How many messages sent in May 2026?", e, "replies_sent") is None


def test_the_verb_a_count_is_named_for_is_evidence() -> None:
    e = _replies(Metric(name="sent_count", agg="count", expr="replies.reply_id"))
    assert _unconsumed("How many messages were sent in May 2026?", e, "sent_count") is None
    # a verb the metric is not named for proves nothing
    assert _unconsumed("How many tickets were opened in May 2026?", e, "sent_count") == "tickets"


def test_a_word_inside_another_metrics_name_is_the_votes_to_weigh() -> None:
    frt = Metric(
        name="frt", agg="avg", expr="replies.first_response_minutes", label="first response time"
    )
    e = _replies(_SENT, frt)
    assert _unconsumed("What were responses in May 2026?", e, "replies_sent") is None


def test_names_match_as_stemmed_whole_words() -> None:
    shipped = Metric(name="shipped_parcels", agg="sum", expr="replies.parcels")
    e = _replies(
        shipped,
        fields=[Field(name="parcels", type="number"), Field(name="shipment_weight", type="number")],
    )
    assert _unconsumed("What were shipments by channel?", e, "shipped_parcels") is None


def test_a_short_token_counts_only_where_the_lens_declares_it() -> None:
    e = _replies(_SENT)
    assert _unconsumed("What was the EU total in May 2026?", e, "replies_sent") is None
    value = Field(name="gross_value", type="number", description="GV: what the reply billed")
    e = _replies(_SENT, fields=[value])
    assert _unconsumed("What was the GV total in May 2026?", e, "replies_sent") == "gv"


def test_an_acronym_nothing_declares_still_clarifies() -> None:
    e = _replies(_SENT)
    assert _unconsumed("What was NPS in May 2026?", e, "replies_sent") == "nps"
    assert _unconsumed("NPS by channel in May 2026", e, "replies_sent") == "nps"
