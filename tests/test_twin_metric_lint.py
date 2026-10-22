"""Apply warns when metrics share an expression with differing mandatory
filters: the snapshot pattern (current_* = latest date,
total_* = end-of-month) is legal and useful, but the serve-time guard can
only tell the twins apart through the question's wording — the author must
learn that contract at apply, not from a rejection three cuts later."""

from __future__ import annotations

from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.lenses.store import LensBundle
from services.validate.report import validate_bundle


def _bundle(metrics: list[Metric]) -> LensBundle:
    return LensBundle(
        config=LensConfig(name="t", display_name="T", connections=["wh"]),
        semantic_model=SemanticModel(
            lens="t",
            dialect="bigquery",
            entities=[
                Entity(
                    name="store_kpis",
                    source=EntitySource(connection="wh", table="proj.marts.store_kpis"),
                    fields=[
                        Field(name="revenue_eur", type="number"),
                        Field(name="snapshot_date", type="date"),
                        Field(name="is_month_end", type="boolean"),
                    ],
                    metrics=metrics,
                )
            ],
        ),
    )


def _codes(metrics: list[Metric]) -> set[str]:
    return {i.code for i in validate_bundle(_bundle(metrics), [], []).issues}


def test_twins_with_differing_filters_warn_at_apply() -> None:
    codes = _codes(
        [
            Metric(
                name="current_revenue",
                agg="sum",
                expr="store_kpis.revenue_eur",
                filters=[
                    "store_kpis.snapshot_date = (SELECT MAX(store_kpis.snapshot_date) "
                    "FROM proj.marts.store_kpis AS store_kpis)"
                ],
            ),
            Metric(
                name="total_revenue",
                agg="sum",
                expr="store_kpis.revenue_eur",
                filters=["store_kpis.is_month_end = TRUE"],
            ),
        ]
    )
    assert "twin_metric_filters" in codes


def test_twins_with_identical_filters_do_not_warn() -> None:
    shared = ["store_kpis.is_month_end = TRUE"]
    codes = _codes(
        [
            Metric(name="a_revenue", agg="sum", expr="store_kpis.revenue_eur", filters=shared),
            Metric(name="b_revenue", agg="sum", expr="store_kpis.revenue_eur", filters=shared),
        ]
    )
    assert "twin_metric_filters" not in codes


def test_metrics_over_distinct_expressions_do_not_warn() -> None:
    codes = _codes(
        [
            Metric(
                name="current_revenue",
                agg="sum",
                expr="store_kpis.revenue_eur",
                filters=["store_kpis.is_month_end = TRUE"],
            ),
            Metric(name="snapshot_days", agg="count", expr="store_kpis.snapshot_date"),
        ]
    )
    assert "twin_metric_filters" not in codes
