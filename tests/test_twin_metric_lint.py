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
                    name="finance_kpis",
                    source=EntitySource(connection="wh", table="proj.marts.finance_kpis"),
                    fields=[
                        Field(name="arr_usd", type="number"),
                        Field(name="status_date", type="date"),
                        Field(name="is_end_of_month", type="boolean"),
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
                name="current_arr",
                agg="sum",
                expr="finance_kpis.arr_usd",
                filters=[
                    "finance_kpis.status_date = (SELECT MAX(finance_kpis.status_date) "
                    "FROM proj.marts.finance_kpis AS finance_kpis)"
                ],
            ),
            Metric(
                name="total_arr",
                agg="sum",
                expr="finance_kpis.arr_usd",
                filters=["finance_kpis.is_end_of_month = TRUE"],
            ),
        ]
    )
    assert "twin_metric_filters" in codes


def test_twins_with_identical_filters_do_not_warn() -> None:
    shared = ["finance_kpis.is_end_of_month = TRUE"]
    codes = _codes(
        [
            Metric(name="a_arr", agg="sum", expr="finance_kpis.arr_usd", filters=shared),
            Metric(name="b_arr", agg="sum", expr="finance_kpis.arr_usd", filters=shared),
        ]
    )
    assert "twin_metric_filters" not in codes


def test_metrics_over_distinct_expressions_do_not_warn() -> None:
    codes = _codes(
        [
            Metric(
                name="current_arr",
                agg="sum",
                expr="finance_kpis.arr_usd",
                filters=["finance_kpis.is_end_of_month = TRUE"],
            ),
            Metric(name="snapshot_days", agg="count", expr="finance_kpis.status_date"),
        ]
    )
    assert "twin_metric_filters" not in codes
