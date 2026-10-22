"""A question that names what it measures is not answered with another shape
or another unit.

A question naming a measure ("Show me sales levels") on a table of movements
must not be served as raw rows of the money-delta column: a listing, for a
question naming a measure. A question saying the word count ("shop or
crate counts") must not be served as a sum of a quantity column: a count asked
for, a sum given. Both clarify on the measure instead, deterministically.
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
from services.runtime.typed_resolver import TypedResolver, measure_noun, uncounted_noun


def _entity(*extra: Metric) -> Entity:
    return Entity(
        name="order_movements",
        source=EntitySource(connection="wh", table="order_movements"),
        default_time_field="order_date",
        primary_key=["unique_key"],
        fields=[
            Field(name="unique_key", type="string"),
            Field(name="cafe_id", type="string"),
            Field(name="order_date", type="date"),
            Field(name="order_status", type="string"),
            Field(name="crates", type="number"),
            Field(name="sales_delta", type="number"),
        ],
        dimensions=[Dimension(name="order_status")],
        metrics=[
            Metric(name="total_crates_movements", agg="sum", expr="order_movements.crates"),
            *extra,
        ],
    )


def _model(*extra: Metric) -> SemanticModel:
    return SemanticModel(lens="orders", dialect="duckdb", entities=[_entity(*extra)])


class _Chooser:
    """Picks the first preferred name among the options offered, else the
    first option."""

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


def _resolve(question: str, model: SemanticModel, *prefer: str):  # type: ignore[no-untyped-def]
    return TypedResolver(
        _Chooser(*prefer),  # type: ignore[arg-type]
        domains={"order_status": ["new", "lapse"]},
        today=date(2026, 10, 22),
    ).resolve(question, model)


# ── a listing for a measure ──────────────────────────────────────────────────


def test_measure_noun_reads_the_question() -> None:
    model = _model()
    assert measure_noun("Show me sales levels", model) == "levels"
    assert measure_noun("Show me sales for September", model) == "sales"
    assert measure_noun("how much did we sell?", model) == "how much"
    assert measure_noun("total crates", model) is not None
    # a listing word lets the listing stand
    assert measure_noun("List the sales movements in September", model) is None
    assert measure_noun("Show me the rows with sales deltas", model) is None
    assert measure_noun("Which cafes lapsed?", model) is None
    # nothing measured: a listing is fine
    assert measure_noun("Show me the movement statuses", model) is None


def test_a_listing_for_a_named_measure_clarifies() -> None:
    res = _resolve("Show me sales levels", _model(), "listing", "order_status", "sales_delta")
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "metric"
    assert "list of rows would not answer" in res.clarification.question
    assert res.clarification.options == ["total_crates_movements"]


def test_a_listing_asked_for_still_lists() -> None:
    res = _resolve(
        "List the sales movements in September 2026",
        _model(),
        "listing",
        "sales_delta",
        "none",
    )
    assert res.intent is not None, res.clarification
    assert res.intent.fields and not res.intent.metrics


# ── a sum for a count ────────────────────────────────────────────────────────


def test_a_count_asked_with_the_word_count_needs_a_count() -> None:
    e = _entity()
    assert uncounted_noun("shop or crate counts", e, ["total_crates_movements"]) in (
        "shop",
        "crate",
    )
    assert uncounted_noun("count of cafes", e, ["total_crates_movements"]) == "cafes"
    assert uncounted_noun("How many cafes lapsed?", e, ["total_crates_movements"])
    # a COUNT DISTINCT over the noun's key answers it
    cafes = Metric(name="cafes", agg="count_distinct", expr="order_movements.cafe_id")
    assert uncounted_noun("cafe counts", _entity(cafes), ["cafes"]) is None
    # a row count answers a count of the entity's own rows
    rows = Metric(name="movement_count", agg="count", expr="order_movements.unique_key")
    assert (
        uncounted_noun("How many order movements were there?", _entity(rows), ["movement_count"])
        is None
    )


def test_how_many_is_held_to_a_count_only_over_a_key() -> None:
    e = _entity()
    # `cafe_id` is a key: a sum of crates does not count cafes
    assert uncounted_noun("How many cafes lapsed?", e, ["total_crates_movements"])
    # a quantity column's sum is its count: "how many crates"
    assert uncounted_noun("How many crates did we lose?", e, ["total_crates_movements"]) is None
    # no key names the thing: the decider's sum stands
    assert uncounted_noun("How many loaves were baked?", e, ["total_crates_movements"]) is None
    assert uncounted_noun("How many times did it lapse?", e, ["total_crates_movements"]) is None


def test_a_count_of_an_uncounted_noun_clarifies_instead_of_a_sum() -> None:
    res = _resolve("shop or crate counts", _model(), "aggregate", "total_crates_movements")
    assert res.intent is None and res.clarification is not None
    assert "does not count them" in res.clarification.question


def test_a_count_metric_over_the_key_answers() -> None:
    cafes = Metric(name="cafes", agg="count_distinct", expr="order_movements.cafe_id")
    res = _resolve(
        "How many cafes had a movement in September 2026?",
        _model(cafes),
        "aggregate",
        "cafes",
        "none",
    )
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["cafes"]


# ── a breakdown the answer would drop ────────────────────────────────────────


def _driver_model() -> SemanticModel:
    return SemanticModel(
        lens="delivery",
        dialect="duckdb",
        entities=[
            Entity(
                name="driver_daily",
                source=EntitySource(connection="wh", table="driver_daily"),
                default_time_field="delivery_date",
                # the driver's name is half the key, so it is never offered as
                # a dimension the decider could pick
                primary_key=["driver_name", "delivery_date"],
                fields=[
                    Field(name="driver_name", type="string"),
                    Field(name="delivery_date", type="date"),
                    Field(name="drops_made", type="integer"),
                ],
                dimensions=[Dimension(name="driver_name")],
                metrics=[Metric(name="drops_made", agg="sum", expr="driver_daily.drops_made")],
            )
        ],
    )


def test_a_per_x_breakdown_the_answer_lacks_clarifies() -> None:
    """ "per driver" must not be served as one total: a breakdown the question
    asks in words that the answer does not group by clarifies."""
    from services.runtime.typed_resolver import unplaced_breakdown

    res = TypedResolver(
        _Chooser("aggregate", "drops_made", "none"),  # type: ignore[arg-type]
        today=date(2026, 10, 22),
    ).resolve("Drops made per driver in August 2026", _driver_model())
    assert res.intent is None and res.clarification is not None
    assert "breakdown by driver" in res.clarification.question
    entity = _driver_model().entities[0]

    def missing(q: str, metrics: list[str], dims: list[str], filters: list) -> str | None:  # type: ignore[type-arg]
        return unplaced_breakdown(q, entity, metrics, dims, None, filters)

    # grouped by it, named by the metric, or a filter's value: nothing missing
    assert missing("made per driver", ["drops_made"], ["driver_name"], []) is None
    assert missing("drops made per route", ["drops_made_per_route"], [], []) is None
    from services.contracts.query_intent import IntentFilter

    by_aino = [IntentFilter(field="driver_name", value="Aino")]
    assert missing("made by Aino", ["drops_made"], [], by_aino) is None
    # a period is the grain's to check, and a bare "every" asks for all
    assert missing("made per month", ["drops_made"], [], []) is None
    assert missing("made across every driver", ["drops_made"], [], []) is None
    assert missing("made per driver", ["drops_made"], [], []) == "driver"


def test_a_metric_names_qualifiers_are_not_measures() -> None:
    """`new_crates` declares "new" as a qualifier, not a measure: "Show me
    new cafes" is still a listing; a one-word metric name is a measure."""
    model = _model(
        Metric(
            name="new_crates",
            agg="sum",
            expr="order_movements.crates",
            filters=["order_movements.order_status = 'new'"],
        ),
        Metric(name="revenue", agg="sum", expr="order_movements.sales_delta"),
    )
    assert measure_noun("Show me new cafes", model) is None
    assert measure_noun("Show me revenue for September", model) == "revenue"
