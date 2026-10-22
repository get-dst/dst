"""Words that ask for a shape a plain aggregate does not have.

A listing names its rows; a superlative ranks; a share is a ratio; a ranking
groups by what the question names and keeps the caller's bindings; a count of
distinct keys per that key is no figure. Each shape is read off the question
at typing time and either shapes the intent or clarifies — never the plain
aggregate served in its place. A climbing-gym chain's visits and members.
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
from services.runtime.typed_resolver import TypedResolver, listing_identity

TODAY = date(2026, 10, 22)
DOMAINS = {
    "gym": ["harbour wall", "old mill"],
    "pass_type": ["day pass", "ten-visit card", "monthly pass"],
    "tier": ["basic", "plus"],
    "status": ["active", "lapsed"],
}


def _visits(*extra: Metric) -> Entity:
    return Entity(
        name="visits",
        source=EntitySource(connection="wh", table="visits"),
        default_time_field="visited_on",
        primary_key=["visit_id"],
        grain="one row per visit",
        fields=[
            Field(name="visit_id", type="string"),
            Field(name="member_id", type="string"),
            Field(name="member_name", type="string"),
            Field(name="gym", type="string"),
            Field(name="pass_type", type="string"),
            Field(name="visited_on", type="date"),
            Field(name="fee", type="number"),
        ],
        dimensions=[Dimension(name="gym"), Dimension(name="pass_type")],
        metrics=[
            Metric(name="visit_count", agg="count", expr="visits.visit_id", label="visits"),
            Metric(name="fee_revenue", agg="sum", expr="visits.fee", label="fee revenue"),
            Metric(
                name="unique_members",
                agg="count_distinct",
                expr="visits.member_id",
                label="unique members",
            ),
            *extra,
        ],
    )


def _members() -> Entity:
    return Entity(
        name="members",
        source=EntitySource(connection="wh", table="members"),
        default_time_field="joined_on",
        primary_key=["member_id"],
        grain="one row per member",
        fields=[
            Field(name="member_id", type="string"),
            Field(name="member_name", type="string"),
            Field(name="tier", type="string"),
            Field(name="status", type="string"),
            Field(name="joined_on", type="date"),
            Field(name="lapsed_on", type="date"),
        ],
        metrics=[Metric(name="member_count", agg="count", label="members")],
    )


def _model(*extra: Metric, members: bool = False) -> SemanticModel:
    entities = [_visits(*extra), *([_members()] if members else [])]
    return SemanticModel(lens="climbing", dialect="duckdb", entities=entities)


class _Prefers:
    """The first preferred name on offer; else `none` where it is offered;
    else `unstated` (no value stated); else the first option."""

    def __init__(self, *prefer: str) -> None:
        self.prefer = prefer
        self.offered: list[list[str]] = []

    def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
        names = [o.name for o in options]
        self.offered.append(names)
        pick = next((p for p in self.prefer if p in names), None)
        if pick is None:
            pick = (
                "unstated"
                if "unstated" in names
                else None
                if "none" in names or allow_none
                else names[0]
            )
        return Decision(chosen=pick, probs={pick or "none": 1.0}, provider="scripted")


def _resolve(question: str, model: SemanticModel, *prefer: str, **kw: object):  # type: ignore[no-untyped-def]
    return TypedResolver(
        _Prefers(*prefer),  # type: ignore[arg-type]
        domains=DOMAINS,
        today=TODAY,
        **kw,  # type: ignore[arg-type]
    ).resolve(question, model)


def _run(sql: str) -> list[tuple[object, ...]]:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE visits AS SELECT * FROM (VALUES "
        "('v1', 'm1', 'Aino', 'harbour wall', 'day pass', DATE '2026-09-03', 12.0),"
        "('v2', 'm1', 'Aino', 'harbour wall', 'day pass', DATE '2026-09-10', 12.0),"
        "('v3', 'm2', 'Bruno', 'old mill', 'monthly pass', DATE '2026-09-11', 45.0),"
        "('v4', 'm3', 'Chidi', 'old mill', 'ten-visit card', DATE '2026-09-20', 90.0),"
        "('v5', 'm2', 'Bruno', 'old mill', 'monthly pass', DATE '2026-08-14', 45.0),"
        "('v6', 'm3', 'Chidi', 'harbour wall', 'day pass', DATE '2026-08-21', 12.0)"
        ") AS t(visit_id, member_id, member_name, gym, pass_type, visited_on, fee)"
    )
    con.execute(
        "CREATE TABLE members AS SELECT * FROM (VALUES "
        "('m1', 'Aino', 'basic', 'active', DATE '2026-01-05', NULL),"
        "('m2', 'Bruno', 'plus', 'lapsed', DATE '2026-02-11', DATE '2026-09-30'),"
        "('m3', 'Chidi', 'basic', 'lapsed', DATE '2026-03-17', DATE '2026-09-02')"
        ") AS t(member_id, member_name, tier, status, joined_on, lapsed_on)"
    )
    return con.execute(sql).fetchall()


# ── a listing names its rows ────────────────────────────────────────────────


def test_a_listing_leads_with_the_key_and_its_label() -> None:
    """ "Which members lapsed?" picked the status and the date to show: rows
    nobody can tell apart. The key and its label lead the listing."""
    model = _model(members=True)
    res = _resolve("Which members lapsed?", model, "listing", "members", "status")
    assert res.intent is not None, res.clarification
    assert res.intent.fields == ["member_name", "member_id", "status"]
    rows = _run(compile_intent(res.intent, model))
    assert {r[0] for r in rows} == {"Bruno", "Chidi"}


def test_a_listing_that_shows_the_label_is_left_alone() -> None:
    model = _model(members=True)
    res = _resolve(
        "List the members and their tier", model, "listing", "members", "member_name", "tier"
    )
    assert res.intent is not None, res.clarification
    assert res.intent.fields == ["member_name", "tier"]


def test_listing_identity_is_the_key_and_one_label_never_a_hidden_column() -> None:
    assert listing_identity(_members()) == ["member_name", "member_id"]
    hidden = _members().model_copy(update={"hide": ["member_name"]})
    assert listing_identity(hidden) == ["member_id"]
    keyless = _members().model_copy(update={"primary_key": []})
    assert listing_identity(keyless) == []


# ── a superlative ranks ─────────────────────────────────────────────────────


def test_the_largest_visit_is_a_ranking_of_visits_not_the_total() -> None:
    """Typed as a figure, "the largest visit" was the sum of every visit's fee."""
    model = _model()
    res = _resolve(
        "What was the largest visit in September 2026?", model, "aggregate", "fee_revenue"
    )
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions == ["visit_id"]
    assert [(o.field, o.dir) for o in res.intent.order_by] == [("fee_revenue", "desc")]
    assert res.intent.limit == 1
    assert _run(compile_intent(res.intent, model)) == [("v4", 90.0)]


def test_a_superlative_with_nothing_named_to_rank_clarifies() -> None:
    res = _resolve(
        "What was the highest fee revenue in September 2026?",
        _model(),
        "aggregate",
        "fee_revenue",
    )
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "dimension"
    assert "Ranked across what?" in res.clarification.question


def test_a_max_metric_carries_the_superlative() -> None:
    top_fee = Metric(name="top_fee", agg="max", expr="visits.fee", label="largest fee")
    res = _resolve(
        "What was the largest fee in September 2026?", _model(top_fee), "aggregate", "top_fee"
    )
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["top_fee"] and res.intent.dimensions == []


def test_a_series_orders_its_buckets_and_stays_a_figure() -> None:
    res = _resolve(
        "Which month had the highest fee revenue by month in 2026?",
        _model(),
        "aggregate",
        "fee_revenue",
        "month",
    )
    assert res.intent is not None, res.clarification
    assert res.intent.grain == "month"


# ── a ranking groups by what the question names ─────────────────────────────


def test_which_members_ranks_members_whatever_the_decider_groups_by() -> None:
    """A decider grouping "which members visit most" by gym ranks gyms."""
    model = _model()
    res = _resolve(
        "Which members had the most visits in September 2026?",
        model,
        "ranking",
        "visit_count",
        "gym",
    )
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions[0] == "member_name"
    assert _run(compile_intent(res.intent, model))[0] == ("Aino", 2)


def test_the_top_n_noun_is_the_ranking_and_its_limit_stays() -> None:
    res = _resolve(
        "Top 2 gyms by fee revenue in September 2026", _model(), "ranking", "fee_revenue"
    )
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions == ["gym"] and res.intent.limit == 2


def test_a_count_of_the_key_it_groups_by_clarifies() -> None:
    """Distinct members per member is 1 on every row."""
    res = _resolve(
        "Which members have the most unique members in September 2026?",
        _model(),
        "ranking",
        "unique_members",
    )
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "metric"
    assert "every row would read 1" in res.clarification.question
    # the key itself, grouped where no label is declared, likewise
    model = _model()
    model.entities[0].fields = [f for f in model.entities[0].fields if f.name != "member_name"]
    res = _resolve(
        "Which members have the most unique members in September 2026?",
        model,
        "ranking",
        "unique_members",
    )
    assert res.intent is None and res.clarification is not None
    assert "counts distinct member_id" in res.clarification.question


def test_a_binding_on_the_entity_restricts_the_ranking_it_was_not_picked_for() -> None:
    """The caller bound the gym after a clarification; the re-asked ranking's
    filter decision chose no column, and every gym was served."""
    model = _model()
    res = _resolve(
        "Which members had the most visits in September 2026?",
        model,
        "ranking",
        "visit_count",
        bindings={"gym": "old mill"},
    )
    assert res.intent is not None, res.clarification
    assert ("gym", "=", "old mill") in [(f.field, f.op, f.value) for f in res.intent.filters]
    assert res.supplied == ["gym"]
    assert _run(compile_intent(res.intent, model)) == [("Bruno", 1), ("Chidi", 1)] or sorted(
        _run(compile_intent(res.intent, model))
    ) == [("Bruno", 1), ("Chidi", 1)]


def test_a_binding_a_column_does_not_hold_clarifies_never_drops() -> None:
    res = _resolve(
        "Which members had the most visits in September 2026?",
        _model(),
        "ranking",
        "visit_count",
        bindings={"gym": "the old mill"},
    )
    assert res.intent is None and res.clarification is not None
    assert res.clarification.kind == "unknown_value" and res.clarification.term == "gym"


# ── a share word is a ratio ─────────────────────────────────────────────────


def test_a_share_asked_of_a_count_clarifies() -> None:
    """The count of day-pass visits is not their share of all visits."""
    res = _resolve(
        "What share of visits used a day pass in September 2026?",
        _model(),
        "aggregate",
        "visit_count",
        "pass_type",
    )
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "share"
    assert "declares no ratio" in res.clarification.question


def test_a_share_asked_of_a_ratio_acts() -> None:
    day_pass = Metric(
        name="day_pass_visits", agg="count", filters=["visits.pass_type = 'day pass'"]
    )
    share = Metric(
        name="day_pass_share",
        type="ratio",
        numerator="day_pass_visits",
        denominator="visit_count",
        label="day pass share",
    )
    model = _model(day_pass, share)
    res = _resolve(
        "What share of visits used a day pass in September 2026?",
        model,
        "aggregate",
        "day_pass_share",
    )
    assert res.intent is not None, res.clarification
    assert res.intent.metrics == ["day_pass_share"]
    assert _run(compile_intent(res.intent, model)) == [(0.5,)]


def test_a_share_word_that_names_a_value_is_a_name() -> None:
    model = _model()
    domains = {**DOMAINS, "pass_type": ["day pass", "rate card"]}
    res = TypedResolver(
        _Prefers("aggregate", "visit_count", "pass_type"),  # type: ignore[arg-type]
        domains=domains,
        today=TODAY,
    ).resolve("How many rate card visits in September 2026?", model)
    assert res.intent is not None, res.clarification


# ── an acronym is a figure wherever it stands ───────────────────────────────


def test_an_acronym_before_a_period_or_alone_must_be_measured() -> None:
    """ "ARPV last month" names no column; the visit count was served for it."""
    for question in ("ARPV last month", "Show ARPV for September 2026", "ARPV?"):
        res = _resolve(question, _model(), "aggregate", "visit_count")
        assert res.intent is None and res.clarification is not None, question
        assert res.clarification.term == "arpv", question


def test_a_superlative_inside_a_declared_term_is_the_terms() -> None:
    from services.contracts.semantic_model import Definition

    model = _model().model_copy(
        update={
            "definitions": [
                Definition(
                    term="biggest spenders",
                    body="visits paying over 50",
                    sql_expr="visits.fee > 50",
                )
            ]
        }
    )
    res = _resolve(
        "How many visits did the biggest spenders make in September 2026?",
        model,
        "aggregate",
        "visit_count",
        "engaged",
    )
    assert res.intent is not None, res.clarification
    assert res.intent.dimensions == [] and res.intent.definitions == ["biggest spenders"]


def test_a_count_of_rows_grouped_by_their_own_label_clarifies() -> None:
    """ "Which is the largest member?" ranked members by members per member
    name: 1 on every row, and the first row served as the largest."""
    model = SemanticModel(lens="climbing", dialect="duckdb", entities=[_members()])
    res = _resolve("Which is the largest member?", model, "aggregate", "member_count")
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "metric"


def test_a_ranking_noun_only_another_entity_holds_clarifies() -> None:
    """ "Which tier had the most visits": tier lives on members, the visits
    are counted on visits, and the decider's grouping ranked gyms instead."""
    res = _resolve(
        "Which tier had the most visits in September 2026?",
        _model(members=True),
        "ranking",
        "visit_count",
        "gym",
    )
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "dimension"
    assert "members holds tier" in res.clarification.question


def test_a_listing_reads_the_entity_it_names_and_the_date_it_names() -> None:
    """ "Which members lapsed in September 2026?" on a lens beside visits: the
    rows are members, and the window goes on `lapsed_on`, the date the
    question names, not on `joined_on`, the declared clock. The decider
    picking visits listed every visit of September."""
    model = _model(members=True)
    res = _resolve("Which members lapsed in September 2026?", model, "listing", "visits", "status")
    assert res.intent is not None, res.clarification
    assert res.intent.entity == "members"
    assert {f.field for f in res.intent.filters} == {"lapsed_on", "status"}
    rows = _run(compile_intent(res.intent, model))
    assert {r[0] for r in rows} == {"Bruno", "Chidi"}


def test_a_figure_dated_by_a_date_it_is_not_counted_by_is_asked() -> None:
    model = SemanticModel(lens="climbing", dialect="duckdb", entities=[_members()])
    res = _resolve(
        "How many members lapsed in September 2026?", model, "aggregate", "member_count", "status"
    )
    assert res.intent is None and res.clarification is not None
    assert res.clarification.term == "time_field"
    assert res.clarification.options == ["joined_on", "lapsed_on"]


def test_a_listing_naming_its_entity_is_not_the_deciders_to_move() -> None:
    """The decider read "which members joined" as visits: every visit of
    September, listed as the answer."""
    model = _model(members=True)
    res = _resolve(
        "Which members joined in September 2026?", model, "listing", "visits", "member_name"
    )
    assert res.intent is not None, res.clarification
    assert res.intent.entity == "members"
    assert {f.field for f in res.intent.filters} == {"joined_on"}
