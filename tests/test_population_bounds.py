"""Entity population bounds ride every rail.

A table holding a scoped subset (an enrolled cohort, a channel allow-list, a
start date) used to document that scope in definition prose — which constrains
nothing, so "what share of ALL X" was answered from the subset and presented as
the whole population at `verified`: a modelling gap, not a generation gap. The
declared bound now rides the compile seam, both generation prompts, the
composer, the trust summary, and a serve-time check — a fact that reaches one
tier does not exist."""

from __future__ import annotations

from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.query_intent import QueryIntent
from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.contracts.warehouse import QueryResult
from services.runtime.compiler import compile_intent
from services.runtime.generator import FixedSQLGenerator, serialize_model
from services.runtime.intent_generator import serialize_layer
from services.runtime.pipeline import run_query
from services.runtime.verification import build_report


def _scoped_model() -> SemanticModel:
    return SemanticModel(
        lens="pilot",
        dialect="duckdb",
        entities=[
            Entity(
                name="pilot_orders",
                source=EntitySource(connection="wh", table="marts.pilot_orders"),
                population="enrolled accounts only; channels A and B; from 2026-06-01",
                population_filter="pilot_orders.channel_id IN ('A','B')",
                fields=[
                    Field(name="amount", type="number"),
                    Field(name="channel_id", type="string"),
                ],
                metrics=[Metric(name="order_count", agg="count", expr="pilot_orders.amount")],
            )
        ],
    )


def test_population_filter_is_anded_into_every_compiled_query() -> None:
    # The enforcing half: the metric compiler applies the bound mechanically,
    # the same mechanism metric filters use.
    sql = compile_intent(QueryIntent(metrics=["order_count"]), _scoped_model())
    assert "channel_id IN ('A', 'B')" in sql.replace('"', "").replace("','", "', '")


def test_population_reaches_both_generation_prompts() -> None:
    # One tier rendering it and one dropping it is
    # how a declared fact stays a suggestion.
    for prompt in (serialize_layer(_scoped_model()), serialize_model(_scoped_model())):
        assert "enrolled accounts only" in prompt
        assert "channel_id IN ('A','B')" in prompt


def test_raw_sql_missing_the_filter_fails_population_declared() -> None:
    report = build_report(
        question="What share of ALL orders used the new flow?",
        sql="SELECT COUNT(*) FROM marts.pilot_orders",
        answer_text="",
        result=QueryResult(columns=["n"], rows=[[42]]),
        semantic_model=_scoped_model(),
        definition_used=None,
        truncated=False,
        certification="none",
        composed=False,
        uncomposed_reason="rows-only test",
    )
    check = report.check("population_declared")
    assert check is not None and check.status == "fail"
    assert "enrolled accounts only" in (check.reason or "")
    assert report.grade == "partial"  # capped, never withheld


def test_sql_carrying_the_filter_passes() -> None:
    report = build_report(
        question="How many pilot orders?",
        sql="SELECT COUNT(*) FROM marts.pilot_orders WHERE pilot_orders.channel_id IN ('A','B')",
        answer_text="",
        result=QueryResult(columns=["n"], rows=[[42]]),
        semantic_model=_scoped_model(),
        definition_used=None,
        truncated=False,
        certification="none",
        composed=False,
        uncomposed_reason="rows-only test",
    )
    check = report.check("population_declared")
    assert check is not None and check.status == "pass"


def test_unscoped_queries_skip_the_check() -> None:
    report = build_report(
        question="How many things?",
        sql="SELECT COUNT(*) FROM other_table",
        answer_text="",
        result=QueryResult(columns=["n"], rows=[[1]]),
        semantic_model=_scoped_model(),
        definition_used=None,
        truncated=False,
        certification="none",
        composed=False,
        uncomposed_reason="rows-only test",
    )
    check = report.check("population_declared")
    assert check is not None and check.status == "skip"


def test_trust_summary_carries_the_scope() -> None:
    # The caveat travels with the number, on the field agents
    # are told to lead with — whether or not enforcement applied.
    res = run_query(
        question="What share of all orders used the new flow?",
        lens_name="pilot",
        org_id="o",
        caller="a",
        semantic_model=_scoped_model(),
        connector=FakeConnector(result=QueryResult(columns=["share"], rows=[[0.279]])),
        generator=FixedSQLGenerator(
            "SELECT 0.279 AS share FROM marts.pilot_orders "
            "WHERE pilot_orders.channel_id IN ('A','B')"
        ),
        composer=None,
    )
    assert res.response.trust_summary is not None
    assert "enrolled accounts only" in res.response.trust_summary


def test_composer_is_told_the_scope() -> None:
    from services.contracts.protocols import GeneratedQuery
    from services.runtime.answer import compose_prompt

    _, user = compose_prompt(
        question="what share of all orders used the new flow?",
        generated=GeneratedQuery(sql="SELECT 1 FROM marts.pilot_orders"),
        result=QueryResult(columns=["share"], rows=[[0.279]]),
        semantic_model=_scoped_model(),
    )
    assert "covers ONLY enrolled accounts only" in user
    # …and an unscoped query gets no such line.
    _, user = compose_prompt(
        question="how many things?",
        generated=GeneratedQuery(sql="SELECT 1 FROM elsewhere"),
        result=QueryResult(columns=["n"], rows=[[1]]),
        semantic_model=_scoped_model(),
    )
    assert "covers ONLY" not in user


def _date_scoped_model() -> SemanticModel:
    return SemanticModel(
        lens="snap",
        dialect="bigquery",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="wh", table="marts.orders"),
                population="daily snapshots from 2025-09-01 onward only",
                population_filter="status_date >= DATE '2025-09-01'",
                fields=[Field(name="status_date", type="date")],
            )
        ],
    )


def _pop_check(sql: str, model: SemanticModel):  # noqa: ANN202
    from services.runtime.verification import _population_declared

    return _population_declared(sql, model)


def test_the_compilers_own_rendering_satisfies_the_check() -> None:
    """The author writes DATE 'x', the compiler emits
    CAST('x' AS DATE) — the same predicate — and the text compare failed it,
    so declaring population_filter permanently capped answers at partial: a
    direct disincentive to adopt the lever that generalizes best."""
    sql = (
        "SELECT COUNT(*) FROM marts.orders AS orders "
        "WHERE orders.feature = TRUE AND (status_date >= CAST('2025-09-01' AS DATE))"
    )
    assert _pop_check(sql, _date_scoped_model()).status == "pass"


def test_equivalent_renderings_all_satisfy() -> None:
    for variant in (
        "status_date >= CAST('2025-09-01' AS DATE)",
        "status_date >= '2025-09-01'",
        "orders.status_date >= DATE '2025-09-01'",
    ):
        sql = f"SELECT 1 FROM marts.orders AS orders WHERE {variant}"
        assert _pop_check(sql, _date_scoped_model()).status == "pass", variant


def test_a_genuinely_missing_population_filter_still_fails() -> None:
    assert _pop_check("SELECT 1 FROM marts.orders", _date_scoped_model()).status == "fail"


def test_a_weaker_predicate_still_fails() -> None:
    # Reaches past the declared bound — a DIFFERENT predicate, must not pass.
    sql = "SELECT 1 FROM marts.orders WHERE status_date >= DATE '2020-01-01'"
    assert _pop_check(sql, _date_scoped_model()).status == "fail"


# ── aggregation scope ────────────────────────────────────────────────────────


def _currency_model() -> SemanticModel:
    return SemanticModel(
        lens="gl",
        dialect="bigquery",
        entities=[
            Entity(
                name="revrec",
                source=EntitySource(connection="wh", table="finance_marts.revrec_all_accounts"),
                pinned_dimensions=["currency"],
                fields=[
                    Field(name="ns_credit", type="number"),
                    Field(name="ns_debit", type="number"),
                    Field(name="currency", type="string"),
                ],
            )
        ],
    )


def _agg_check(sql: str):  # noqa: ANN202
    from services.runtime.verification import _aggregation_scope

    return _aggregation_scope(sql, _currency_model())


def test_a_cross_currency_sum_fails_aggregation_scope() -> None:
    """SUM(ns_credit - ns_debit) with no currency reference anywhere: the
    arithmetic sum of three currencies is a number denominated in nothing,
    overstated when read as one of them, and it used to serve with no
    disclosure."""
    sql = (
        "SELECT SUM(revrec.ns_credit - revrec.ns_debit) AS revenue_recognized "
        "FROM finance_marts.revrec_all_accounts AS revrec "
        "WHERE revrec.accounting_period = '2026-07-01'"
    )
    check = _agg_check(sql)
    assert check.status == "fail"
    assert "currency" in (check.reason or "")
    assert "denominated in nothing" in (check.reason or "")


def test_grouping_by_the_pinned_dimension_passes() -> None:
    sql = (
        "SELECT revrec.currency, SUM(revrec.ns_credit) AS credit "
        "FROM finance_marts.revrec_all_accounts AS revrec GROUP BY revrec.currency"
    )
    assert _agg_check(sql).status == "pass"


def test_pinning_to_one_value_passes() -> None:
    sql = (
        "SELECT SUM(revrec.ns_credit) AS credit "
        "FROM finance_marts.revrec_all_accounts AS revrec WHERE revrec.currency = 'USD'"
    )
    assert _agg_check(sql).status == "pass"


def test_non_aggregate_queries_skip() -> None:
    sql = "SELECT revrec.ns_credit FROM finance_marts.revrec_all_accounts AS revrec LIMIT 5"
    assert _agg_check(sql).status == "skip"


def test_pinned_dimensions_reach_both_generation_prompts() -> None:
    for prompt in (serialize_layer(_currency_model()), serialize_model(_currency_model())):
        assert "currency" in prompt
        assert "denominated in nothing" in prompt or "meaningless" in prompt


def test_imperative_prose_constraints_are_flagged_as_decorative() -> None:
    """The class mechanism: prose that TRIES to be a rule is told at
    apply that it is decorative, and which structured field enforces it."""
    from services.contracts.lens_config import LensConfig
    from services.lenses.store import LensBundle
    from services.validate.report import validate_bundle

    def _issues(description: str):  # noqa: ANN202
        bundle = LensBundle(
            config=LensConfig(name="t", display_name="T", connections=["wh"]),
            semantic_model=SemanticModel(
                lens="t",
                dialect="duckdb",
                entities=[
                    Entity(
                        name="gl",
                        source=EntitySource(connection="wh", table="gl"),
                        description=description,
                        fields=[Field(name="x", type="number")],
                    )
                ],
            ),
        )
        return validate_bundle(bundle, [], []).issues

    flagged = _issues("Amounts are per-currency — never sum money columns across currencies.")
    assert any(i.code == "constraint_in_prose" for i in flagged)
    # Benign prose with the same words in a non-imperative shape must not fire.
    clean = _issues("Customers never churn twice; amounts are stored per currency.")
    assert not any(i.code == "constraint_in_prose" for i in clean)


def test_population_round_trips_through_semantic_files() -> None:
    from services.contracts.shared_semantic import SharedEntity
    from services.semantic.files import parse_semantic_files, render_semantic_files

    entity = SharedEntity.model_validate(
        {
            "name": "pilot_orders",
            "source": {"connection": "wh", "table": "marts.pilot_orders"},
            "population": "enrolled accounts only",
            "population_filter": "channel_id IN ('A','B')",
        }
    )
    files = render_semantic_files([entity], [])
    entities, _ = parse_semantic_files(files)
    back = entities["pilot_orders"]
    assert back.population == "enrolled accounts only"
    assert back.population_filter == "channel_id IN ('A','B')"


_ = ScriptedLLM  # imported for parity with sibling suites; composer=None above
