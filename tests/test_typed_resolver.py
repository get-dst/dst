"""The typed resolver: a question either types or it doesn't.

Driven by a ScriptedDecider so every slot path is pinned offline: act on every
slot compiles; a slot the policy cannot act on clarifies naming the slot; a
listing shape projects fields; the window is a deterministic parse; a dictionary
value is a decision, an open value comes from the caller's bindings or
clarifies; every decision rides the ledger.
"""

from __future__ import annotations

from datetime import date

from services.contracts.fakes import ScriptedDecider
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
from services.runtime.compiler import compile_intent
from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver


def _model() -> SemanticModel:
    return SemanticModel(
        lens="sales",
        dialect="duckdb",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="wh", table="orders"),
                default_time_field="order_date",
                fields=[
                    Field(name="order_id", type="integer"),
                    Field(name="amount", type="number"),
                    Field(name="status", type="string"),
                    Field(name="country", type="string"),
                    Field(name="customer_name", type="string"),
                    Field(name="order_date", type="date"),
                ],
                dimensions=[Dimension(name="country"), Dimension(name="status")],
                metrics=[
                    Metric(name="revenue", agg="sum", expr="orders.amount"),
                    Metric(name="order_count", agg="count"),
                ],
            )
        ],
        definitions=[
            Definition(
                term="large_order", body="an order over 1000", sql_expr="orders.amount > 1000"
            )
        ],
    )


DOMAINS = {"status": ["shipped", "open"], "country": ["FI", "DK"]}
TODAY = date(2026, 9, 16)


def _sure(chosen: str | None, *others: str) -> Decision:
    probs = {chosen or "none": 1.0, **{o: 0.0 for o in others}}
    return Decision(chosen=chosen, probs=probs, provider="scripted")


def _unsure(chosen: str, runner: str) -> Decision:
    return Decision(chosen=chosen, probs={chosen: 0.5, runner: 0.45}, provider="scripted")


def _resolver(decisions: list[Decision], **kw: object) -> TypedResolver:
    return TypedResolver(ScriptedDecider(decisions), domains=DOMAINS, today=TODAY, **kw)  # type: ignore[arg-type]


def test_every_slot_acts_and_the_intent_compiles() -> None:
    # shape, metric, another-metric(none), dimension, another(none),
    # filter column, filter value, another filter(none) — no grain: the
    # question asks for no breakdown over time, so none is posed
    decisions = [
        _sure("aggregate"),
        _sure("revenue"),
        _sure(None),
        _sure("country"),
        _sure(None),
        _sure("status"),
        _sure(None),
    ]
    res = _resolver(decisions).resolve("revenue by country for shipped orders last month", _model())
    assert res.typed and res.intent is not None and res.clarification is None
    i = res.intent
    assert i.entity == "orders" and i.metrics == ["revenue"] and i.dimensions == ["country"]
    assert [(f.field, f.op, f.value) for f in i.filters] == [
        ("status", "=", "shipped"),
        ("order_date", ">=", "2026-08-01"),
        ("order_date", "<=", "2026-08-31"),
    ]
    assert [d.slot for d in res.decisions] == [
        "shape",
        "metric",
        "metric",
        "dimension",
        "dimension",
        "filter",
        "filter_value",
        "filter",
    ]
    assert all(d.verdict == "act" for d in res.decisions)
    # "shipped" is a stored status value the question names: read off the
    # text, recorded as such, never decided
    value = res.decisions[6]
    assert value.chosen == "shipped" and value.provider == "question-text" and value.p is None
    assert res.matched == ["status"]
    sql = compile_intent(i, _model())
    assert "SUM(" in sql and "GROUP BY" in sql and "'shipped'" in sql and "2026-08-01" in sql


def test_an_unsure_slot_clarifies_naming_it_and_its_candidates() -> None:
    res = _resolver([_sure("aggregate"), _unsure("revenue", "order_count")]).resolve(
        "how much did we do", _model()
    )
    assert not res.typed and res.clarification is not None
    c = res.clarification
    assert c.kind == "unresolved_slot" and c.term == "metric"
    assert c.options[:2] == ["revenue", "order_count"]
    assert res.decisions[-1].verdict == "clarify"


def test_a_confident_none_on_shape_declines() -> None:
    res = _resolver([_sure(None, "aggregate", "listing")]).resolve("tell me a joke", _model())
    assert res.decline and res.intent is None and res.clarification is None


def test_listing_projects_fields_without_aggregation() -> None:
    decisions = [
        _sure("listing"),
        _sure("customer_name"),
        _sure("amount"),
        _sure(None),
        _sure(None),  # no filter
    ]
    res = _resolver(decisions).resolve("list customers and their order amounts", _model())
    assert res.intent is not None and res.intent.fields == ["customer_name", "amount"]
    sql = compile_intent(res.intent, _model())
    assert "GROUP BY" not in sql and "customer_name" in sql and "SUM(" not in sql


def test_a_ranking_with_no_measure_to_rank_on_clarifies_never_serves_a_bare_listing() -> None:
    """'Which customer has the highest revenue?' read as a listing carries no
    metric, so its order had nothing to attach to and a bare list of names was
    served. The ranking must fail loud (a clarify, which escalation hands to
    raw-SQL generation), never drop silently."""
    decisions = [_sure("listing"), _sure("customer_name"), _sure(None), _sure(None)]
    res = _resolver(decisions).resolve("Which customer has the highest revenue?", _model())
    assert res.intent is None and res.clarification is not None
    assert res.clarification.kind == "unresolved_slot" and res.clarification.term == "order"


def test_best_or_worst_never_types_its_direction_depends_on_the_measure() -> None:
    """'Best' means highest for revenue and lowest for deaths: the resolver cannot
    pick a direction, so it asks rather than serve an unordered figure."""
    decisions = [_sure("aggregate"), _sure("revenue"), _sure(None), _sure("country"), _sure(None)]
    res = _resolver(decisions).resolve("Which country has the best revenue?", _model())
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "order"


def test_a_ranking_is_one_figure_per_dimension_ordered_by_it() -> None:
    """'Which country has the highest revenue?' is a ranking: revenue per country,
    highest first — the shape the listing reading dropped to a bare list of names."""
    decisions = [_sure("ranking"), _sure("revenue"), _sure(None), _sure("country"), _sure(None)]
    res = _resolver(decisions).resolve("Which country has the highest revenue?", _model())
    assert res.intent is not None, res.clarification
    i = res.intent
    assert i.metrics == ["revenue"] and i.dimensions == ["country"]
    assert [(o.field, o.dir) for o in i.order_by] == [("revenue", "desc")]
    sql = compile_intent(i, _model())
    assert "GROUP BY" in sql and "ORDER BY" in sql and "DESC" in sql


def test_a_top_n_ranking_carries_its_limit() -> None:
    decisions = [_sure("ranking"), _sure("revenue"), _sure(None), _sure("country"), _sure(None)]
    res = _resolver(decisions).resolve("top 3 countries by revenue", _model())
    assert res.intent is not None and res.intent.limit == 3


def test_a_ranking_with_no_stated_direction_clarifies() -> None:
    decisions = [_sure("ranking"), _sure("revenue"), _sure(None), _sure("country"), _sure(None)]
    res = _resolver(decisions).resolve("rank the countries by revenue", _model())
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "order"


def test_a_ranking_needs_something_to_rank() -> None:
    decisions = [_sure("ranking"), _sure("revenue"), _sure(None), _sure(None)]
    res = _resolver(decisions).resolve("Which has the highest revenue?", _model())
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "dimension"


def test_a_derivation_definition_is_never_engaged_as_a_filter() -> None:
    """A definition the question names but whose sql_expr is a column, not a
    condition, is not offered as a filter at all — no decision, no WHERE clause."""
    model = _model()
    model.definitions[0].sql_expr = "orders.amount"
    decisions = [_sure("aggregate"), _sure("revenue"), *[_sure(None)] * 6]
    res = _resolver(decisions).resolve("revenue from every large order", model)
    assert res.intent is not None and res.intent.definitions == []
    assert not any(d.slot == "definition" for d in res.decisions)


def _ranked_model(better: str | None) -> SemanticModel:
    model = _model()
    model.entities[0].metrics[0].better = better  # type: ignore[assignment]
    return model


def test_best_and_worst_follow_the_declared_better_end() -> None:
    """'Best' orders toward the metric's declared better end, 'worst' away from
    it — the author's fact, never the resolver's guess."""
    steps = [_sure("ranking"), _sure("revenue"), _sure(None), _sure("country"), _sure(None)]
    best = _resolver(list(steps)).resolve(
        "Which country has the best revenue?", _ranked_model("higher")
    )
    assert best.intent is not None and [(o.field, o.dir) for o in best.intent.order_by] == [
        ("revenue", "desc")
    ]
    worst = _resolver(list(steps)).resolve(
        "Which country has the worst revenue?", _ranked_model("higher")
    )
    assert worst.intent is not None and worst.intent.order_by[0].dir == "asc"
    lower = _resolver(list(steps)).resolve(
        "Which country has the best revenue?", _ranked_model("lower")
    )
    assert lower.intent is not None and lower.intent.order_by[0].dir == "asc"


def test_a_named_value_the_filter_step_missed_gets_a_focused_decision() -> None:
    """'shipped' is a stored status the question names. When the general "any
    restriction?" step says none, a focused decision among the columns holding
    the value places it — the filter is kept, not asked back to the caller."""
    decisions = [
        _sure("listing"),
        _sure("customer_name"),
        _sure(None),
        _sure(None),
        _sure("status"),
    ]
    res = _resolver(decisions).resolve("list the customers with shipped orders", _model())
    assert res.intent is not None, res.clarification
    assert [(f.field, f.value) for f in res.intent.filters] == [("status", "shipped")]


def test_a_numeric_filter_value_is_never_picked_off_a_list() -> None:
    """A number is stated or it is not: a model choosing '3' from a list of ids
    for 'Legend' is a guess, so a numeric dictionary is never offered."""
    resolver = TypedResolver(
        ScriptedDecider(
            [
                _sure("aggregate"),
                _sure("revenue"),
                _sure(None),
                _sure(None),
                _sure("order_id"),
                _sure("3"),
                _sure(None),
                _sure(None),
            ]
        ),  # type: ignore[arg-type]
        domains={**DOMAINS, "order_id": ["1", "2", "3"]},
        today=TODAY,
    )
    res = resolver.resolve("revenue for the premium order", _model())
    assert not (res.intent and any(f.field == "order_id" for f in res.intent.filters))


def test_a_filter_outside_the_declared_population_declines() -> None:
    """A table counted over shipped orders only cannot answer about open ones:
    `status = 'open' AND (status = 'shipped')` matches no row. The contradiction
    is read off the declared population and declines before any query runs."""
    model = _model()
    model.entities[0].population_filter = "orders.status = 'shipped'"
    # the fifth "none" is the general filter step; the named value then gets its
    # focused decision, which places it on status
    steps = [_sure("aggregate"), _sure("revenue"), *[_sure(None)] * 3, _sure("status"), _sure(None)]
    res = _resolver(list(steps)).resolve("revenue from open orders", model)
    assert res.intent is None and res.decline and "open" in res.decline
    ok = _resolver(list(steps)).resolve("revenue from shipped orders", model)
    assert ok.intent is not None


def test_a_numeric_column_is_a_filter_candidate_only_with_a_stated_number() -> None:
    from services.runtime.typed_resolver import Option

    model = _model()
    entity = model.entities[0]
    members = [Option(f.name) for f in entity.fields]
    r = _resolver([])
    without = {
        o.name for o in r._filter_candidates("revenue for the premium order", entity, members)
    }
    with_number = {o.name for o in r._filter_candidates("revenue for order 7", entity, members)}
    assert "order_id" not in without and "amount" not in without
    assert "order_id" in with_number and "status" in without


def test_a_number_stated_once_restricts_one_column() -> None:
    """'15 minutes' once in the question compiled as minute = 15 AND gold_lead = 15,
    a filter on a value nobody gave. The second column reading the same literal
    asks which field it restricts."""
    steps = [
        _sure("aggregate"), _sure("revenue"), _sure(None), _sure(None),
        _sure("order_id"), _sure("="), _sure("amount"), _sure("="), _sure(None),
    ]  # fmt: skip
    res = _resolver(steps).resolve("revenue from order 7", _model())
    assert res.intent is None and res.clarification is not None
    assert set(res.clarification.options) == {"order_id", "amount"}
    one = [_sure("aggregate"), _sure("revenue"), _sure(None), _sure(None), _sure("order_id")]
    ok = _resolver([*one, _sure("="), _sure(None)]).resolve("revenue from order 7", _model())
    assert ok.intent is not None
    assert [(f.field, f.value) for f in ok.intent.filters] == [("order_id", 7)]


def _win_rate_model() -> SemanticModel:
    model = _model()
    orders = model.entities[0]
    orders.fields.append(Field(name="is_paid", type="boolean"))
    orders.metrics += [
        Metric(name="paid", agg="sum", expr="CASE WHEN orders.is_paid THEN 1 ELSE 0 END"),
        Metric(name="orders_n", agg="count", expr="orders.order_id"),
        Metric(name="paid_rate", type="ratio", numerator="paid", denominator="orders_n"),
    ]
    return model


def test_a_filter_on_a_ratios_numerator_column_is_refused() -> None:
    """`paid_rate WHERE is_paid` is 1.0 on every row: a filter on a column only the
    numerator reads decides the ratio. It asks rather than serve identical values."""
    resolver = TypedResolver(
        ScriptedDecider(
            [
                _sure("ranking"),
                _sure("paid_rate"),
                _sure(None),
                _sure("country"),
                _sure(None),
                _sure("is_paid"),
                _sure("true"),
                _sure(None),
                _sure(None),
            ]
        ),  # type: ignore[arg-type]
        domains={**DOMAINS, "is_paid": ["true", "false"]},
        today=TODAY,
    )
    res = resolver.resolve("Which country has the highest paid rate?", _win_rate_model())
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "is_paid"


def test_a_listing_without_ranking_words_still_types() -> None:
    decisions = [_sure("listing"), _sure("customer_name"), _sure(None), _sure(None)]
    res = _resolver(decisions).resolve("list the customers", _model())
    assert res.intent is not None and res.intent.order_by == []


def test_open_value_comes_from_bindings_or_clarifies() -> None:
    steps = [
        _sure("aggregate"),
        _sure("revenue"),
        _sure(None),
        _sure(None),
        _sure("customer_name"),
        _sure(None),
    ]
    clarified = _resolver(list(steps)).resolve("revenue for one customer", _model())
    assert clarified.clarification is not None
    assert clarified.clarification.kind == "unresolved_slot"
    assert clarified.clarification.term == "customer_name"
    assert clarified.clarification.options == ["the exact stored value"]

    bound = _resolver(list(steps), bindings={"customer_name": "Acme Oy"}).resolve(
        "revenue for one customer", _model()
    )
    assert bound.intent is not None and bound.supplied == ["customer_name"]
    assert [(f.field, f.value) for f in bound.intent.filters] == [("customer_name", "Acme Oy")]


def test_a_number_in_the_question_resolves_only_when_alone() -> None:
    steps = [
        _sure("aggregate"),
        _sure("order_count"),
        _sure(None),
        _sure(None),
        _sure("amount"),
        _sure(">"),
        _sure(None),
    ]
    res = _resolver(list(steps)).resolve("how many orders above 500 in 2026", _model())
    assert res.intent is not None
    assert ("amount", ">", 500) in [(f.field, f.op, f.value) for f in res.intent.filters]
    two = _resolver(list(steps[:5])).resolve("orders between 500 and 900", _model())
    assert two.clarification is not None and two.clarification.term == "amount"


def test_an_unparsable_window_clarifies_never_guesses() -> None:
    steps = [
        _sure("aggregate"),
        _sure("revenue"),
        _sure(None),
        _sure(None),
        _sure(None),
        _sure(None),
    ]
    res = _resolver(steps).resolve("revenue Q3 2025 vs Q3 2026", _model())
    assert res.clarification is not None and res.clarification.term == "order_date"
    assert "YYYY-Qn" in res.clarification.options[0]


def test_definition_engaged_by_decision() -> None:
    steps = [
        _sure("aggregate"),
        _sure("order_count"),
        _sure(None),
        _sure(None),
        _sure("engaged"),
        _sure(None),
    ]
    res = _resolver(steps).resolve("how many orders count as a large order", _model())
    assert res.intent is not None and res.intent.definitions == ["large_order"]
    assert "orders.amount > 1000" in compile_intent(res.intent, _model())


def test_generator_seam_carries_decisions_and_typed_flag() -> None:
    steps = [
        _sure("aggregate"),
        _sure("revenue"),
        _sure(None),
        _sure(None),
        _sure(None),
    ]
    gen = TypedIntentGenerator(_resolver(steps))
    out = gen.generate(
        question="total revenue", semantic_model=_model(), prose_context=[], dialect="duckdb"
    )
    assert out.sql and out.intent is not None and out.typed is True
    assert out.definition_used == "revenue" and len(out.decisions) == 5
    clar = TypedIntentGenerator(_resolver([_sure("aggregate"), _unsure("revenue", "order_count")]))
    out2 = clar.generate(
        question="how much", semantic_model=_model(), prose_context=[], dialect="duckdb"
    )
    assert out2.sql == "" and out2.clarification is not None and out2.decisions


class _BatchingDecider:
    """A decider that batches: every round's independent questions in ONE
    call, answered from a script keyed by question key."""

    provider = "batch"

    def __init__(self, answers: dict[str, str | None]) -> None:
        self._answers = answers
        self.calls: list[list[str]] = []

    def _one(self, key: str, chosen: str | None) -> Decision:
        return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider=self.provider)

    def decide(self, *, question: str, context: str, options: list, allow_none: bool) -> Decision:
        self.calls.append(["<single>"])
        # "another or none" rounds and values are asked one at a time
        for o in options:
            if o.name in self._answers.get("__sequential__", ()):
                return self._one("seq", o.name)
        return self._one("seq", None)

    def decide_many(self, question: str, questions: dict) -> dict[str, Decision]:
        self.calls.append(list(questions))
        return {k: self._one(k, self._answers.get(k)) for k in questions}


def test_a_batching_decider_types_a_typical_question_in_three_calls() -> None:
    """Round 1 asks shape (+ entity when there are several); round 2 the
    first metric, first dimension, grain, definitions and first filter; round
    3 the second of each and the first filter's value. A question with one
    metric, one dimension and one dictionary filter never asks a fourth time.
    Same intent as the sequential path — three calls."""
    decider = _BatchingDecider(
        {
            "shape": "aggregate",
            "metric:0": "revenue",
            "dimension:0": "country",
            "grain": None,
            "filter:0": "status",
            # round three: the second metric / dimension / filter say none, the
            # first filter's value is picked — all in the same call
            "metric:1": None,
            "dimension:1": None,
            "filter:1": None,
        }
    )
    res = TypedResolver(decider, domains=DOMAINS, today=TODAY).resolve(
        "revenue by country for shipped orders last month", _model()
    )
    assert res.intent is not None and res.typed
    assert res.intent.metrics == ["revenue"] and res.intent.dimensions == ["country"]
    assert ("status", "=", "shipped") in [(f.field, f.op, f.value) for f in res.intent.filters]
    batched = [c for c in decider.calls if c != ["<single>"]]
    assert set(batched[0]) == {"shape", "metric:0"}  # one entity → no entity question
    assert set(batched[1]) == {"metric:1", "dimension:0", "filter:0"}  # no grain asked
    # the value is named in the question: matched, so round three never asks it
    assert set(batched[2]) == {"dimension:1", "filter:1"}
    assert len(batched) == 3 and decider.calls.count(["<single>"]) == 0
    # Every decision is on the ledger, batched ones first in spec order.
    assert [d.slot for d in res.decisions][:4] == [
        "shape",
        "metric",
        "metric",
        "dimension",
    ]


def test_the_resolver_decides_an_ambiguous_reading_and_pins_it_for_the_metric_round() -> None:
    """Outside the pipeline (the slot lane), an ambiguous term the question
    does not name is one typed decision over its readings; act pins it and the
    reading rides the metric decision; a `none` clarifies on the term."""
    from services.runtime.reading import SLOT

    ambiguous = _model().model_copy(
        update={
            "definitions": [
                Definition(
                    term="sales",
                    body="ASK",
                    status="ambiguous",
                    possible_mappings=["net sales - orders.amount", "order volume - count"],
                )
            ]
        }
    )
    net = Decision(chosen="net sales", probs={"net sales": 0.9, "order volume": 0.1}, provider="v")
    steps = [
        net,
        _sure("aggregate"),
        _sure("revenue"),
        _sure(None),
        _sure(None),
        _sure(None),
        _sure(None),
    ]
    decider = ScriptedDecider(steps)
    resolver = TypedResolver(decider, domains=DOMAINS, today=TODAY)  # type: ignore[arg-type]
    res = resolver.resolve("total sales", ambiguous)
    assert res.intent is not None and res.intent.metrics == ["revenue"]
    assert res.decisions[0].slot == SLOT and res.decisions[0].verdict == "act"
    assert decider.calls[0]["options"] == ["net sales", "order volume"]
    assert "pinned for this question to: net sales - orders.amount" in resolver._notes

    unsure = Decision(chosen=None, probs={"none": 0.6, "net sales": 0.4}, provider="v")
    out = TypedResolver(ScriptedDecider([unsure]), domains=DOMAINS, today=TODAY).resolve(  # type: ignore[arg-type]
        "total sales", ambiguous
    )
    assert out.clarification is not None and out.clarification.kind == "ambiguous_term"
    assert out.clarification.term == "sales" and out.decisions[0].verdict != "act"


def test_the_window_owns_time_so_date_fields_are_not_filter_candidates() -> None:
    """ "in August 2026" parses to a window on the time field; a second date
    field (due_date) is never offered as a filter column for that question,
    while a question with no window still may filter on it."""
    with_due = _model()
    entity = with_due.entities[0].model_copy(
        update={"fields": [*with_due.entities[0].fields, Field(name="due_date", type="date")]}
    )
    with_due = with_due.model_copy(update={"entities": [entity]})
    decider = ScriptedDecider([_sure("aggregate"), _sure("revenue"), _sure(None)])
    TypedResolver(decider, domains=DOMAINS, today=TODAY).resolve(  # type: ignore[arg-type]
        "total revenue in August 2026", with_due
    )
    filter_calls = [c for c in decider.calls if "none" in c["options"] and "status" in c["options"]]
    assert filter_calls and all("due_date" not in c["options"] for c in filter_calls)
    decider2 = ScriptedDecider([_sure("aggregate"), _sure("revenue"), _sure(None)])
    TypedResolver(decider2, domains=DOMAINS, today=TODAY).resolve(  # type: ignore[arg-type]
        "total revenue", with_due
    )
    assert any("due_date" in c["options"] for c in decider2.calls)


def test_a_profiled_month_period_column_takes_the_window_instead_of_a_binding() -> None:
    """A string `month` whose profiled values are all YYYY-MM is the period the
    window compiles to — no time field declared, no value to bind; the field
    is not a filter candidate for a windowed question either."""
    from services.runtime.typed_resolver import period_field

    entity = Entity(
        name="budget",
        source=EntitySource(connection="wh", table="budget"),
        fields=[
            Field(name="month", type="string"),
            Field(name="entity", type="string"),
            Field(name="amount_eur", type="number"),
        ],
        dimensions=[Dimension(name="entity")],
        metrics=[Metric(name="budget", agg="sum", expr="budget.amount_eur")],
    )
    model = SemanticModel(lens="fin", dialect="duckdb", entities=[entity])
    domains = {"month": [f"2026-{m:02d}" for m in range(1, 13)], "entity": ["FI", "SE"]}
    assert period_field(entity, domains) == "month"
    assert period_field(entity, {"month": ["2026-01", "total"]}) is None
    decider = ScriptedDecider([_sure("aggregate"), _sure("budget"), _sure(None)])
    res = TypedResolver(decider, domains=domains, today=TODAY).resolve(  # type: ignore[arg-type]
        "total budget in August 2026", model
    )
    assert res.intent is not None, res.clarification
    window = [(f.field, f.op, f.value) for f in res.intent.filters]
    assert window == [("month", "=", "2026-08")]  # one month is an equality
    assert all("month" not in c["options"] for c in decider.calls if "none" in c["options"])
    sql = compile_intent(res.intent, model)
    assert "budget.month = '2026-08'" in sql
    q1 = TypedResolver(
        ScriptedDecider([_sure("aggregate"), _sure("budget"), _sure(None)]),  # type: ignore[arg-type]
        domains=domains,
        today=TODAY,
    ).resolve("total budget for Q1 2026", model)
    assert q1.intent is not None
    assert [(f.op, f.value) for f in q1.intent.filters] == [(">=", "2026-01"), ("<=", "2026-03")]


def test_the_entity_s_own_dictionary_serves_when_the_shared_one_dropped_the_column() -> None:
    """The model-wide dictionary has no `month` (two entities disagree); the
    entity's own does, so the period window still compiles and a `status`
    value pick still has its options."""
    entity = Entity(
        name="budget",
        source=EntitySource(connection="wh", table="budget"),
        fields=[
            Field(name="month", type="string"),
            Field(name="status", type="string"),
            Field(name="amount_eur", type="number"),
        ],
        metrics=[Metric(name="budget", agg="sum", expr="budget.amount_eur")],
    )
    model = SemanticModel(lens="fin", dialect="duckdb", entities=[entity])
    own = {"budget": {"month": ["2026-01", "2026-08"], "status": ["draft", "final"]}}

    class _ByName:
        """Picks the first preferred name on offer, else none — order-proof."""

        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
            names = [o.name for o in options]
            self.calls.append({"options": names})
            pick = next((p for p in ("aggregate", "budget", "status", "final") if p in names), None)
            return _sure(pick)

    decider = _ByName()
    res = TypedResolver(decider, domains={}, entity_domains=own, today=TODAY).resolve(  # type: ignore[arg-type]
        "final budget in August 2026", model
    )
    assert res.intent is not None, res.clarification
    got = {(f.field, f.op, f.value) for f in res.intent.filters}
    assert ("month", "=", "2026-08") in got and ("status", "=", "final") in got
    # "final" is matched against the entity's own dictionary (no shared one exists)
    assert res.matched == ["status"]


def test_a_month_span_compiles_and_an_unplaced_month_clarifies_never_drops() -> None:
    """ "January through August 2026" is one range; "January and August 2026"
    would otherwise serve August alone — it asks for the period instead."""
    steps = [
        _sure("aggregate"),
        _sure("revenue"),
        _sure(None),
        _sure(None),
        _sure(None),
        _sure(None),
    ]
    span = _resolver(list(steps)).resolve("revenue January through August 2026", _model())
    assert span.intent is not None, span.clarification
    assert [(f.op, f.value) for f in span.intent.filters] == [
        (">=", "2026-01-01"),
        ("<=", "2026-08-31"),
    ]
    dropped = _resolver(list(steps)).resolve("revenue for January and August 2026", _model())
    assert dropped.clarification is not None and dropped.clarification.term == "window"
    assert "january" in dropped.clarification.question
    bare = _resolver(list(steps)).resolve("revenue in January", _model())
    assert bare.clarification is not None and bare.clarification.term == "window"


def test_a_filter_column_the_question_states_no_value_for_is_dropped_not_clarified() -> None:
    """Two nones on a value: "the question states no value" drops the column
    (acted on, recorded); "a value, but not one of these" is the unknown-value
    clarification as before. Without a dictionary the same question is asked
    as a binary before the agent is asked to bind."""
    # dictionary column `status`: picked as a filter, then "unstated"
    steps = [
        _sure("aggregate"),
        _sure("revenue"),
        _sure(None),
        _sure(None),
        _sure("status"),
        _sure("unstated"),
        _sure(None),
    ]
    res = _resolver(list(steps)).resolve("total revenue", _model())
    assert res.intent is not None and res.intent.filters == []
    assert [d.slot for d in res.decisions][-3:] == ["filter", "filter_value", "filter"]
    # the same with "none": a stated value the dictionary lacks → unknown_value
    unknown = list(steps)
    unknown[5] = _sure(None)
    out = _resolver(unknown).resolve("total revenue for cancelled orders", _model())
    assert out.clarification is not None and out.clarification.kind == "unknown_value"
    # no dictionary (customer_name): "unstated" drops it, "stated" asks for a binding
    free = [
        _sure("aggregate"),
        _sure("revenue"),
        _sure(None),
        _sure(None),
        _sure("customer_name"),
        _sure("unstated"),
        _sure(None),
    ]
    dropped = _resolver(list(free)).resolve("total revenue", _model())
    assert dropped.intent is not None and dropped.intent.filters == []
    free[5] = _sure("stated")
    asked = _resolver(list(free)).resolve("revenue for one customer", _model())
    assert asked.clarification is not None and asked.clarification.term == "customer_name"


def test_a_prefetched_decision_the_resolution_never_used_is_not_on_the_ledger() -> None:
    """Round two asks the first metric, dimension, grain and filter at once;
    when the metric clarifies, the dimension / grain / filter answers were
    never consumed — they must not ride the ledger as decisions taken."""
    decider = _BatchingDecider(
        {
            "shape": "aggregate",
            "metric:0": None,  # a required slot with no pick: clarifies
            "dimension:0": None,
            "grain": None,
            "filter:0": None,
        }
    )
    res = TypedResolver(decider, domains=DOMAINS, today=TODAY).resolve("how much", _model())
    assert res.intent is None and res.decline  # no metric picked: the lens declines
    assert [d.slot for d in res.decisions] == ["shape", "metric"]


def test_a_declared_period_field_takes_the_window_without_a_dictionary() -> None:
    entity = Entity(
        name="monthly_revenue",
        source=EntitySource(connection="wh", table="monthly_revenue"),
        fields=[Field(name="month", type="string"), Field(name="revenue_eur", type="number")],
        metrics=[Metric(name="revenue", agg="sum", expr="monthly_revenue.revenue_eur")],
    )
    model = SemanticModel(lens="fin", dialect="duckdb", entities=[entity])
    decider = ScriptedDecider([_sure("aggregate"), _sure("revenue"), _sure(None)])
    res = TypedResolver(
        decider, domains={}, period_fields={"monthly_revenue": "month"}, today=TODAY
    ).resolve("revenue for August 2026", model)  # type: ignore[arg-type]
    assert res.intent is not None, res.clarification
    assert [(f.field, f.op, f.value) for f in res.intent.filters] == [("month", "=", "2026-08")]


def test_the_grain_is_asked_only_when_the_question_asks_for_a_breakdown_over_time() -> None:
    """ "revenue in August 2026" is a window, not a monthly series: no grain
    decision is posed, so a decider that would answer 'month' never gets to.
    "revenue by month in 2026" poses it."""
    from services.runtime.typed_resolver import asks_breakdown_over_time

    assert not asks_breakdown_over_time("revenue in August 2026")
    assert not asks_breakdown_over_time("work order count for synced batch B2026-08-31")
    for q in ("revenue by month in 2026", "monthly revenue", "revenue trend", "orders per week"):
        assert asks_breakdown_over_time(q), q

    class _Monthly:
        """Answers 'month' to any grain question — the failure the gate prevents."""

        def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
            names = [o.name for o in options]
            pick = next((p for p in ("aggregate", "revenue", "month") if p in names), None)
            return _sure(pick)

    single = (
        _resolver([])
        .__class__(_Monthly(), domains=DOMAINS, today=TODAY)
        .resolve(  # type: ignore[arg-type]
            "revenue in August 2026", _model()
        )
    )
    assert single.intent is not None and single.intent.grain is None
    assert all(d.slot != "grain" for d in single.decisions)
    series = TypedResolver(_Monthly(), domains=DOMAINS, today=TODAY).resolve(  # type: ignore[arg-type]
        "revenue by month in 2026", _model()
    )
    assert series.intent is not None and series.intent.grain == "month"


def test_the_metric_names_the_entity_across_a_lens() -> None:
    """Two entities, distinctive metric names: the first decision is the
    metric over every metric of the lens, and the entity is its owner — no
    entity decision is posed for a figure."""
    invoices = Entity(
        name="invoices",
        source=EntitySource(connection="wh", table="invoices"),
        fields=[Field(name="amount", type="number")],
        metrics=[Metric(name="billed", agg="sum", expr="invoices.amount")],
    )
    quotes = Entity(
        name="quotes",
        source=EntitySource(connection="wh", table="quotes"),
        fields=[Field(name="deal_id", type="string")],
        metrics=[Metric(name="won_count", agg="count")],
    )
    model = SemanticModel(lens="sales", dialect="duckdb", entities=[invoices, quotes])

    class _ByName:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
            names = [o.name for o in options]
            self.calls.append(names)
            pick = next((p for p in ("aggregate", "won_count") if p in names), None)
            return _sure(pick)

    dec = _ByName()
    res = TypedResolver(dec, domains={}, today=TODAY).resolve("what was won count", model)  # type: ignore[arg-type]
    assert res.intent is not None, res.clarification
    assert res.intent.entity == "quotes" and res.intent.metrics == ["won_count"]
    assert ["billed", "won_count"] in dec.calls  # every metric of the lens was on offer
    assert all(d.slot != "entity" for d in res.decisions)
