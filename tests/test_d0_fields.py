"""The entity meta fields reach prompts, and metric filters reach SQL."""

from __future__ import annotations

import pytest

from services.contracts.query_intent import QueryIntent
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Join,
    Metric,
    SemanticModel,
)
from services.runtime.compiler import compile_intent
from services.runtime.generator import serialize_model
from services.runtime.intent_generator import serialize_layer


def _model() -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity(
                name="orders",
                grain="one row per order",
                use_cases=["Use for revenue reporting", "Avoid for forecasts"],
                common_questions=["how many orders last month?"],
                source=EntitySource(connection="wh", table="analytics.orders"),
                default_time_field="order_date",
                primary_key=["order_id"],
                fields=[],
                dimensions=[
                    Dimension(
                        name="purchase_year",
                        expr="EXTRACT(YEAR FROM orders.order_date)",
                        type="integer",
                        description="calendar year the order was placed",
                    ),
                    Dimension(name="status", description="order lifecycle state"),
                ],
                metrics=[
                    Metric(
                        name="revenue",
                        agg="sum",
                        expr="orders.amount",
                        filters=["orders.status = 'completed'"],
                        format="currency",
                    ),
                    Metric(
                        name="order_count",
                        agg="count",
                        expr="orders.order_id",
                        agg_time_field="shipped_at",
                    ),
                ],
            )
        ],
        joins=[
            Join(
                left="orders",
                right="customers",
                on="orders.customer_id = customers.id",
                relationship="many_to_one",
            )
        ],
    )


def test_serialize_model_renders_d0_fields() -> None:
    prompt = serialize_model(_model())
    assert "grain: one row per order" in prompt
    assert "use: Avoid for forecasts" in prompt
    assert "answers: how many orders last month?" in prompt
    assert "metric revenue REQUIRES filters: orders.status = 'completed'" in prompt
    assert "(left, many_to_one)" in prompt
    assert "REQUIRES filters" in prompt and "order_count REQUIRES" not in prompt


def test_serialize_model_renders_dimensions() -> None:
    """Authored dimensions parse, validate and compile; if the raw-SQL
    generation prompt never shows them, a value-shape rule written into a
    dimension description is a silent no-op. The expression, the type and the
    description all reach the model."""
    prompt = serialize_model(_model())
    year = "dimension orders.purchase_year integer = EXTRACT(YEAR FROM orders.order_date)"
    assert year in prompt
    assert "calendar year the order was placed" in prompt
    # No expr → the dimension stands for the field of the same name, exactly as
    # the intent compiler resolves it.
    assert "dimension orders.status string = orders.status — order lifecycle state" in prompt
    # …and the rule that tells the model what to do with them.
    assert "embed\n  the dimension's listed expression EXACTLY" in prompt


def test_serialize_layer_renders_grain_use_and_filters() -> None:
    prompt = serialize_layer(_model())
    assert "Grain: one row per order" in prompt
    assert "Use: Use for revenue reporting" in prompt
    assert "revenue: sum of orders.amount [only where orders.status = 'completed']" in prompt


def test_serialize_model_renders_time_hints() -> None:
    prompt = serialize_model(_model())
    assert "time: order_date" in prompt  # entity-level canonical event date
    assert "[time: shipped_at]" in prompt  # metric-level override annotation


def test_serialize_layer_renders_time_field() -> None:
    assert "Time field: order_date" in serialize_layer(_model())


def test_a_fields_value_domain_reaches_both_prompts() -> None:
    """A field's description is where the VALUE DOMAIN lives — both tiers must see it.

    A question like "customers in Denmark and Finland" returns zero
    rows when the column holds 'DK'/'FI' and the author's
    `description: "ISO country code (NO, SE, DK, FI, IS)"` was rendered by the raw-SQL
    prompt but DROPPED by the intent prompt — which is the default door for any lens
    with metrics. A confidently wrong empty answer carrying no signal: the exact
    silent-wrong this product exists to prevent.

    Pinned on BOTH tiers deliberately. The bug was not that a prompt lacked a line; it
    was that two tiers disagreed about whether authored meaning is worth sending, and
    only the non-default one got it right.
    """
    m = _model()
    m.entities[0].fields = [
        Field(name="country", type="string", description="ISO country code (NO, SE, DK, FI, IS)")
    ]

    for prompt in (serialize_layer(m), serialize_model(m)):
        assert "ISO country code (NO, SE, DK, FI, IS)" in prompt


def test_compile_intent_ands_metric_filters() -> None:
    sql = compile_intent(QueryIntent(entity="orders", metrics=["revenue"]), _model())
    assert "WHERE (orders.status = 'completed')" in sql


def test_unfiltered_metric_adds_no_where() -> None:
    sql = compile_intent(QueryIntent(entity="orders", metrics=["order_count"]), _model())
    assert "WHERE" not in sql


def test_same_filter_metrics_share_the_where() -> None:
    m = _model()
    m.entities[0].metrics.append(
        Metric(
            name="completed_count",
            agg="count",
            expr="orders.order_id",
            filters=["orders.status = 'completed'"],
        )
    )
    sql = compile_intent(QueryIntent(entity="orders", metrics=["revenue", "completed_count"]), m)
    assert sql.count("orders.status = 'completed'") == 1  # one shared WHERE
    assert "CASE WHEN" not in sql


def test_mixed_filter_metrics_never_intersect() -> None:
    """completed_revenue + refund_total in one query must NOT
    produce WHERE completed AND returned (zero rows) — each metric filters
    inside its own aggregation."""
    m = _model()
    m.entities[0].metrics.append(
        Metric(
            name="refund_total",
            agg="sum",
            expr="orders.amount",
            filters=["orders.status = 'returned'"],
        )
    )
    sql = compile_intent(QueryIntent(entity="orders", metrics=["revenue", "refund_total"]), m)
    assert "WHERE" not in sql  # no shared metric-filter WHERE
    assert "CASE WHEN orders.status = 'completed' THEN orders.amount" in sql
    assert "CASE WHEN orders.status = 'returned' THEN orders.amount" in sql


def test_mixed_with_unfiltered_metric_inlines_only_the_filtered_one() -> None:
    m = _model()
    sql = compile_intent(QueryIntent(entity="orders", metrics=["revenue", "order_count"]), m)
    assert "WHERE" not in sql
    assert "CASE WHEN orders.status = 'completed' THEN orders.amount" in sql
    # the unfiltered metric aggregates every row
    assert "COUNT(orders.order_id)" in sql.replace("count(", "COUNT(")


def test_validate_rejects_unparseable_metric_filter() -> None:
    from services.contracts.lens_config import LensConfig
    from services.lenses.store import LensBundle
    from services.validate.report import validate_bundle

    m = _model()
    m.entities[0].metrics[0].filters = ["((("]
    bundle = LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]),
        semantic_model=m,
    )
    report = validate_bundle(bundle, [], [])
    codes = {i.code for i in report.issues}
    assert "metric_filter_invalid" in codes


@pytest.mark.parametrize("frag", ["orders.status = 'completed'", "orders.amount > 100"])
def test_validate_accepts_parseable_filters(frag: str) -> None:
    from services.contracts.lens_config import LensConfig
    from services.lenses.store import LensBundle
    from services.validate.report import validate_bundle

    m = _model()
    m.entities[0].metrics[0].filters = [frag]
    bundle = LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]),
        semantic_model=m,
    )
    report = validate_bundle(bundle, [], [])
    assert "metric_filter_invalid" not in {i.code for i in report.issues}


def test_router_anchors_include_entity_meta() -> None:
    from services.contracts.lens_config import LensConfig
    from services.lenses.store import LensBundle
    from services.router.profiles import coverage_profile

    bundle = LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]),
        semantic_model=_model(),
    )
    anchors = coverage_profile(bundle).anchors
    assert "how many orders last month?" in anchors
    assert "Use for revenue reporting" in anchors


def test_ambiguous_definitions_do_not_warn_unenforceable() -> None:
    """'Can't be enforced in SQL' is an ambiguous term's JOB, not a gap."""
    from services.contracts.lens_config import LensConfig
    from services.contracts.semantic_model import Definition
    from services.lenses.store import LensBundle
    from services.validate.report import validate_bundle

    m = _model()
    m.definitions = [
        Definition(term="value", body="ASK.", status="ambiguous", possible_mappings=["a", "b"]),
        Definition(term="ltv", body="prose only"),
    ]
    bundle = LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]),
        semantic_model=m,
    )
    warned = [
        i.message
        for i in validate_bundle(bundle, [], []).issues
        if i.code == "definition_not_enforceable"
    ]
    assert len(warned) == 1 and "'ltv'" in warned[0]
