"""Stored values resolve from the profile dictionary, the way they always have.

A value the question names (any case) or the caller binds (any case) is read
off the column's dictionary: the column that holds it is a filter candidate
whatever its name, a binding takes the stored spelling and applies whether or
not the column vote picked its field, and a grain word a column stores is that
value, not a series. The rails that keep key columns, dates and period flags
out of an open filter decision still hold where no value settles the column.
"""

from __future__ import annotations

from datetime import date

from services.contracts.protocols import Decision
from services.contracts.query_intent import QueryIntent
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime.compiler import compile_intent
from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver

TODAY = date(2026, 10, 22)
READERS = [f"Reader {a}{b}" for a in "ABCDEF" for b in "ABCDEF"]  # 36: past the decidable size
DOMAINS = {
    "branch": ["Harbour", "Old Town", "Hillside"],
    "branch_key": ["HBR", "OLD", "HIL"],
    "pickup_day": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
    "loan_term": ["Daily", "Weekly", "Monthly"],
    "reader": READERS,
}


def _loans(*extra: Field) -> Entity:
    """A library's loans: one row per item lent."""
    return Entity(
        name="loans",
        source=EntitySource(connection="wh", table="loans"),
        default_time_field="lent_on",
        primary_key=["loan_id"],
        grain="one row per loan",
        fields=[
            Field(name="loan_id", type="string"),
            Field(name="lent_on", type="date"),
            Field(name="branch", type="string"),
            Field(name="reader", type="string"),
            *extra,
        ],
        metrics=[Metric(name="loan_count", agg="count", expr="loans.loan_id", label="loans")],
    )


def _model(*extra: Field) -> SemanticModel:
    return SemanticModel(lens="library", dialect="duckdb", entities=[_loans(*extra)])


class _Chooser:
    """Picks the first preferred name among the options offered, else `none`
    when it is offered, else the first option; records every slot it saw."""

    def __init__(self, *prefer: str) -> None:
        self.prefer = ("aggregate", "loan_count", *prefer)
        self.seen: list[tuple[str, list[str]]] = []

    def decide(self, *, question: str, context: str, options: list, allow_none: bool) -> Decision:  # type: ignore[type-arg]
        names = [o.name for o in options]
        self.seen.append((context, names))
        chosen = next(
            (p for p in self.prefer if p in names), "none" if "none" in names else names[0]
        )
        return Decision(
            chosen=None if chosen == "none" else chosen, probs={chosen: 1.0}, provider="scripted"
        )


def _resolve(question: str, model: SemanticModel, chooser: _Chooser, **bindings: str):  # type: ignore[no-untyped-def]
    return TypedResolver(
        chooser,  # type: ignore[arg-type]
        domains=DOMAINS,
        bindings=bindings,
        today=TODAY,
    ).resolve(question, model)


def _filters(res) -> list[tuple[str, str, object]]:  # type: ignore[no-untyped-def]
    assert res.intent is not None, res.clarification
    return [(f.field, f.op, f.value) for f in res.intent.filters if f.field != "lent_on"]


def _filter_options(chooser: _Chooser) -> list[str]:
    return next(
        names for ctx, names in chooser.seen if ctx.startswith("Does the question restrict")
    )


# ── the column holding a named value is a candidate ──────────────────────────


def test_a_value_in_a_period_worded_column_resolves_from_the_text() -> None:
    model = _model(Field(name="pickup_day", type="string"))
    chooser = _Chooser("pickup_day")
    res = _resolve("How many loans were picked up on a Saturday in May 2026?", model, chooser)
    assert "pickup_day" in _filter_options(chooser)
    assert _filters(res) == [("pickup_day", "=", "Saturday")]


def test_a_code_in_a_key_named_column_resolves_from_the_text() -> None:
    model = _model(Field(name="branch_key", type="string"))
    chooser = _Chooser("branch_key")
    res = _resolve("How many loans at HBR in May 2026?", model, chooser)
    assert "branch_key" in _filter_options(chooser)
    assert _filters(res) == [("branch_key", "=", "HBR")]


def test_keys_and_period_flags_stay_out_where_no_value_names_them() -> None:
    model = _model(Field(name="branch_key", type="string"), Field(name="pickup_day", type="string"))
    chooser = _Chooser()
    _resolve("How many loans at Harbour in May 2026?", model, chooser)
    options = _filter_options(chooser)
    assert "branch" in options
    assert "branch_key" not in options and "pickup_day" not in options


# ── a binding is the caller's value, in the dictionary's spelling ────────────


def test_a_binding_resolves_whatever_its_case() -> None:
    res = _resolve("How many loans in May 2026?", _model(), _Chooser("branch"), branch="harbour")
    assert _filters(res) == [("branch", "=", "Harbour")]
    assert res.supplied == ["branch"]


def test_a_binding_applies_when_the_column_vote_does_not_pick_it() -> None:
    res = _resolve("How many loans in May 2026?", _model(), _Chooser(), branch="HARBOUR")
    assert _filters(res) == [("branch", "=", "Harbour")]


def test_a_binding_past_the_decidable_size_takes_the_stored_spelling() -> None:
    res = _resolve("How many loans in May 2026?", _model(), _Chooser("reader"), reader="reader cd")
    assert _filters(res) == [("reader", "=", "Reader CD")]


def test_a_binding_the_dictionary_lacks_is_asked_about_never_dropped() -> None:
    res = _resolve("How many loans in May 2026?", _model(), _Chooser(), branch="Seaside")
    assert res.intent is None and res.clarification is not None
    c = res.clarification
    assert c.kind == "unknown_value" and c.term == "branch"
    assert "Harbour, Old Town, Hillside" in c.question
    assert c.options == ["Harbour", "Old Town", "Hillside"]


# ── a grain word a column stores is that value ───────────────────────────────


def test_a_stored_grain_word_is_the_value_not_a_series() -> None:
    model = _model(Field(name="loan_term", type="string"))
    chooser = _Chooser("loan_term", "day")
    res = _resolve("Daily loans in May 2026", model, chooser)
    assert _filters(res) == [("loan_term", "=", "Daily")]
    assert res.intent is not None and res.intent.grain is None
    assert not any("broken down over time" in ctx for ctx, _n in chooser.seen)


def test_a_grain_word_no_column_stores_is_the_grain() -> None:
    chooser = _Chooser("day")
    res = _resolve("Daily loans in May 2026", _model(), chooser)
    assert res.intent is not None and res.intent.grain == "day"
    assert _filters(res) == []


# ── a pinned boolean is held by a binding or a bare boolean condition ────────


def _renewals_model(*definitions: Definition) -> SemanticModel:
    loans = _loans(Field(name="is_renewed", type="boolean")).model_copy(
        update={"pinned_dimensions": ["is_renewed"]}
    )
    return SemanticModel(
        lens="library", dialect="duckdb", entities=[loans], definitions=list(definitions)
    )


def test_a_bound_pinned_boolean_is_not_asked_back() -> None:
    resolver = TypedResolver(
        _Chooser(),  # type: ignore[arg-type]
        domains={**DOMAINS, "is_renewed": ["False", "True"]},
        bindings={"is_renewed": "true"},
        today=TODAY,
    )
    out = TypedIntentGenerator(resolver).generate(
        question="How many loans in May 2026?",
        semantic_model=_renewals_model(),
        prose_context=[],
        dialect="duckdb",
    )
    assert out.clarification is None, out.clarification
    assert out.intent is not None
    assert ("is_renewed", "=", "True") in [(f.field, f.op, f.value) for f in out.intent.filters]


def test_a_boolean_condition_pins_its_column() -> None:
    for expr in ("NOT loans.is_renewed", "loans.is_renewed IS TRUE"):
        renewed = Definition(term="renewed loan", body="a loan renewed once", sql_expr=expr)
        intent = QueryIntent(entity="loans", metrics=["loan_count"], definitions=["renewed loan"])
        assert "is_renewed" in compile_intent(intent, _renewals_model(renewed)), expr
