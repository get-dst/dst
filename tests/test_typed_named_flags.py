"""A true/false column the question names by its subject restricts the answer.

"Top docks by guided rentals" and "fee revenue on unpaid rentals" name a flag
(`is_guided`, `paid`) by what it flags. A flag's dictionary holds only true and
false, so the named-value net never saw these words, and when the general
filter decision answered "none" the ranking or total was served over every row.
The words now get a focused decision, and its "none" is a question back. A
kayak rental desk's rentals.
"""

from __future__ import annotations

from datetime import date

import duckdb

from services.contracts.protocols import Decision
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.compiler import compile_intent
from services.runtime.typed_resolver import TypedResolver, named_flags

TODAY = date(2026, 10, 22)
DOMAINS = {
    "dock": ["north dock", "south dock", "weir"],
    "is_guided": ["false", "true"],
    "paid": ["false", "true"],
}


def _rentals(*extra: Metric, population_filter: str | None = None) -> Entity:
    return Entity(
        name="rentals",
        source=EntitySource(connection="wh", table="rentals"),
        default_time_field="rented_on",
        primary_key=["rental_id"],
        grain="one row per rental",
        population_filter=population_filter,
        fields=[
            Field(name="rental_id", type="string"),
            Field(name="dock", type="string"),
            Field(name="is_guided", type="boolean"),
            Field(name="paid", type="boolean"),
            Field(name="rented_on", type="date"),
            Field(name="fee", type="number"),
        ],
        dimensions=[Dimension(name="dock"), Dimension(name="is_guided", type="boolean")],
        metrics=[
            Metric(name="rental_count", agg="count", label="rentals"),
            Metric(name="fee_revenue", agg="sum", expr="rentals.fee", label="fee revenue"),
            *extra,
        ],
    )


def _model(*extra: Metric, population_filter: str | None = None) -> SemanticModel:
    return SemanticModel(
        lens="kayaks",
        dialect="duckdb",
        entities=[_rentals(*extra, population_filter=population_filter)],
    )


class _Prefers:
    """The first preferred name on offer; else `none` where it is offered;
    else the first option. Records every option list it was shown."""

    def __init__(self, *prefer: str, none_when: str = "") -> None:
        self.prefer = prefer
        self.none_when = none_when
        self.offered: list[list[str]] = []
        self.asked: list[tuple[str, list[str]]] = []

    def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
        names = [o.name for o in options]
        self.offered.append(names)
        self.asked.append((context, names))
        if self.none_when and self.none_when in context and allow_none:
            return Decision(chosen=None, probs={"none": 1.0}, provider="scripted")
        pick = next((p for p in self.prefer if p in names), None)
        if pick is None:
            pick = None if "none" in names or allow_none else names[0]
        return Decision(chosen=pick, probs={pick or "none": 1.0}, provider="scripted")


def _resolve(question: str, model: SemanticModel, decider: _Prefers):  # type: ignore[no-untyped-def]
    return TypedResolver(decider, domains=DOMAINS, today=TODAY).resolve(question, model)  # type: ignore[arg-type]


def _run(sql: str) -> list[tuple[object, ...]]:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE rentals AS SELECT * FROM (VALUES "
        "('r1', 'north dock', TRUE, TRUE, DATE '2026-09-02', 40.0),"
        "('r2', 'north dock', TRUE, FALSE, DATE '2026-09-03', 40.0),"
        "('r3', 'south dock', FALSE, TRUE, DATE '2026-09-04', 15.0),"
        "('r4', 'south dock', FALSE, TRUE, DATE '2026-09-05', 15.0),"
        "('r5', 'south dock', FALSE, FALSE, DATE '2026-09-06', 15.0),"
        "('r6', 'weir', TRUE, TRUE, DATE '2026-09-07', 40.0)"
        ") AS t(rental_id, dock, is_guided, paid, rented_on, fee)"
    )
    return con.execute(sql).fetchall()


def _flag_offers(decider: _Prefers) -> list[list[str]]:
    return [o for o in decider.offered if o == ["true", "false", "none"]]


def test_named_flags_reads_the_subject_joined_and_negated() -> None:
    e = _rentals().model_copy(
        update={"fields": [*_rentals().fields, Field(name="is_ebike", type="boolean")]}
    )
    cols = ["is_guided", "paid", "is_ebike", "dock"]
    assert named_flags("Top docks by guided rentals", e, cols) == [("is_guided", "guided", False)]
    assert named_flags("fee revenue on unpaid rentals", e, cols) == [("paid", "unpaid", True)]
    assert named_flags("rentals not paid in May", e, cols) == [("paid", "not paid", True)]
    assert named_flags("e-bike rentals", e, cols) == [("is_ebike", "e bike", False)]
    assert named_flags("prepaid rentals at the weir", e, cols) == []


def test_a_ranking_keeps_the_flag_the_question_names() -> None:
    """Served over every rental, the top dock was south dock (3 rentals)."""
    model = _model()
    decider = _Prefers("ranking", "rental_count", "true")
    res = _resolve("Top 1 dock by guided rentals in September 2026", model, decider)
    assert res.intent is not None, res.clarification
    assert [(f.field, f.value) for f in res.intent.filters if f.field == "is_guided"] == [
        ("is_guided", "true")
    ]
    assert _run(compile_intent(res.intent, model)) == [("north dock", 2)]


def test_a_flag_the_decider_calls_incidental_is_asked_never_dropped() -> None:
    decider = _Prefers("ranking", "rental_count")
    res = _resolve("Top 1 dock by guided rentals in September 2026", _model(), decider)
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "is_guided"
    assert res.clarification.options == ["true", "false"]


def test_an_unpaid_total_is_the_false_rows() -> None:
    model = _model()
    decider = _Prefers("aggregate", "fee_revenue", "false")
    res = _resolve("What was fee revenue on unpaid rentals in September 2026?", model, decider)
    assert res.intent is not None, res.clarification
    assert [(f.field, f.value) for f in res.intent.filters if f.field == "paid"] == [
        ("paid", "false")
    ]
    assert _run(compile_intent(res.intent, model)) == [(55.0,)]


def test_a_grouped_flag_is_its_breakdown_and_not_asked() -> None:
    decider = _Prefers("aggregate", "rental_count", "is_guided", none_when="restrict the rows")
    res = _resolve("How many guided rentals were there in September 2026?", _model(), decider)
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions == ["is_guided"]
    assert _flag_offers(decider) == []


def test_a_flag_the_population_decides_is_not_asked() -> None:
    model = _model(population_filter="rentals.is_guided = TRUE")
    decider = _Prefers("aggregate", "rental_count")
    res = _resolve("How many guided rentals were there in September 2026?", model, decider)
    assert res.intent is not None, res.clarification
    assert _flag_offers(decider) == []


def test_a_flag_the_metric_counts_by_is_not_asked() -> None:
    guided = Metric(
        name="guided_rentals", agg="count", filters=["rentals.is_guided"], label="guided rentals"
    )
    decider = _Prefers("aggregate", "guided_rentals")
    res = _resolve("How many guided rentals in September 2026?", _model(guided), decider)
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["guided_rentals"]
    assert _flag_offers(decider) == []


class _Splits(_Prefers):
    """Splits its vote on restricting the rows by `is_guided`: offered that
    column, the filter column pick clarifies."""

    def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
        if "restrict the rows" in context and "is_guided" in [o.name for o in options]:
            return Decision(chosen="is_guided", probs={"is_guided": 0.5, "none": 0.5}, provider="s")
        return super().decide(
            question=question, context=context, options=options, allow_none=allow_none
        )


def test_a_bound_column_is_never_asked_back() -> None:
    """The caller answered "restricted by which field?" with a binding, and a
    split column vote asked the same question again."""
    model = _model()
    res = TypedResolver(
        _Splits("aggregate", "fee_revenue"),  # type: ignore[arg-type]
        domains=DOMAINS,
        today=TODAY,
        bindings={"is_guided": "true"},
    ).resolve("What was fee revenue on guided rentals in September 2026?", model)
    assert res.intent is not None, res.clarification
    assert _run(compile_intent(res.intent, model)) == [(120.0,)]


class _PicksTheColumn(_Prefers):
    """Picks `paid` as the field that restricts the rows, then answers its
    value with "unstated": the general value decision, shown only true,
    false and unstated, reads "unpaid" as no value at all."""

    def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
        names = [o.name for o in options]
        if "restrict the rows" in context and "paid" in names:
            return Decision(chosen="paid", probs={"paid": 1.0}, provider="scripted")
        if "Which stored value of 'paid'" in context:
            return Decision(chosen="unstated", probs={"unstated": 1.0}, provider="scripted")
        return super().decide(
            question=question, context=context, options=options, allow_none=allow_none
        )


def test_a_flag_column_picked_by_the_vote_takes_its_value_from_the_words() -> None:
    """Served as fee revenue on every rental: the column was picked and its
    value dropped as unstated."""
    model = _model()
    decider = _PicksTheColumn("aggregate", "fee_revenue", "false")
    res = _resolve("What was fee revenue on unpaid rentals in September 2026?", model, decider)
    assert res.intent is not None, res.clarification
    assert [(f.field, f.value) for f in res.intent.filters if f.field == "paid"] == [
        ("paid", "false")
    ]
    assert _run(compile_intent(res.intent, model)) == [(55.0,)]


def test_a_flag_the_population_holds_is_not_offered_to_the_filter_vote() -> None:
    model = _model(population_filter="rentals.is_guided = TRUE")
    decider = _Prefers("aggregate", "fee_revenue")
    res = _resolve("What was guided fee revenue in September 2026?", model, decider)
    assert res.intent is not None, res.clarification
    assert not any(
        "is_guided" in names for context, names in decider.asked if "restrict the rows" in context
    )


class _SplitsOnDock(_Prefers):
    """Splits its vote between restricting by `dock` and by nothing."""

    def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
        if "restrict the rows" in context:
            return Decision(chosen="dock", probs={"dock": 0.5, "none": 0.5}, provider="scripted")
        return super().decide(
            question=question, context=context, options=options, allow_none=allow_none
        )


def test_a_split_vote_on_a_column_the_words_settle_is_not_asked_back() -> None:
    """'weir' is a stored value of dock: the named-value net binds it from the
    words, so a split on whether to restrict by dock is no question."""
    model = _model()
    decider = _SplitsOnDock("aggregate", "fee_revenue", "dock")
    res = _resolve("What was fee revenue at the weir in September 2026?", model, decider)
    assert res.intent is not None, res.clarification
    assert _run(compile_intent(res.intent, model)) == [(40.0,)]
