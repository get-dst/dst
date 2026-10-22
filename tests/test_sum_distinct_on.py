"""`agg: sum_distinct_on` sums one value per `distinct_on` key.

A monthly quota joined onto daily sales rows repeats on every day of the
month; a plain SUM adds it once per day. The distinct-on metric keeps one row
per key (rep, month) of the rows the query keeps, and sums those.
"""

from __future__ import annotations

import duckdb
import pytest
from pydantic import ValidationError

from services.contracts.query_intent import IntentFilter, IntentOrder, QueryIntent
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Join,
    Metric,
    SemanticModel,
)
from services.runtime.compiler import CompileError, compile_intent, metric_sql


def _model(dialect: str = "duckdb") -> SemanticModel:
    return SemanticModel(
        lens="sales",
        dialect=dialect,  # type: ignore[arg-type]
        entities=[
            Entity(
                name="daily_sales",
                source=EntitySource(connection="wh", table="daily_sales"),
                default_time_field="day",
                fields=[
                    Field(name="day", type="date"),
                    Field(name="rep_id", type="string"),
                    Field(name="month", type="string"),
                    Field(name="amount", type="number"),
                ],
                dimensions=[Dimension(name="rep_id")],
                metrics=[
                    Metric(name="sales", agg="sum", expr="daily_sales.amount"),
                    Metric(
                        name="quota",
                        agg="sum_distinct_on",
                        expr="quotas.quota",
                        distinct_on=["quotas.rep_id", "quotas.month"],
                    ),
                ],
            ),
            Entity(
                name="quotas",
                source=EntitySource(connection="wh", table="quotas"),
                primary_key=["rep_id", "month"],
                fields=[
                    Field(name="rep_id", type="string"),
                    Field(name="month", type="string"),
                    Field(name="quota", type="number"),
                ],
            ),
        ],
        joins=[
            Join(
                left="daily_sales",
                right="quotas",
                on="daily_sales.rep_id = quotas.rep_id AND daily_sales.month = quotas.month",
                relationship="many_to_one",
            )
        ],
    )


@pytest.fixture
def wh() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE daily_sales AS SELECT * FROM (VALUES "
        "(DATE '2026-09-01', 'a', '2026-09', 10.0), (DATE '2026-09-02', 'a', '2026-09', 20.0), "
        "(DATE '2026-09-03', 'a', '2026-09', 5.0), (DATE '2026-09-01', 'b', '2026-09', 7.0), "
        "(DATE '2026-10-01', 'a', '2026-10', 1.0)) t(day, rep_id, month, amount)"
    )
    con.execute(
        "CREATE TABLE quotas AS SELECT * FROM (VALUES "
        "('a', '2026-09', 100.0), ('b', '2026-09', 50.0), ('a', '2026-10', 120.0)"
        ") t(rep_id, month, quota)"
    )
    return con


def _rows(con: duckdb.DuckDBPyConnection, intent: QueryIntent) -> list[tuple[object, ...]]:
    return con.execute(compile_intent(intent, _model())).fetchall()


def test_a_monthly_quota_on_daily_rows_sums_once_per_rep_month(
    wh: duckdb.DuckDBPyConnection,
) -> None:
    plain = wh.execute(
        "SELECT SUM(q.quota) FROM daily_sales d JOIN quotas q "
        "ON d.rep_id = q.rep_id AND d.month = q.month"
    ).fetchone()
    assert plain == (470.0,)  # the quota added once per day
    assert _rows(wh, QueryIntent(entity="daily_sales", metrics=["quota", "sales"])) == [
        (270.0, 43.0)
    ]


def test_by_rep_and_by_month(wh: duckdb.DuckDBPyConnection) -> None:
    by_rep = QueryIntent(
        entity="daily_sales",
        metrics=["quota"],
        dimensions=["rep_id"],
        order_by=[IntentOrder(field="rep_id", dir="asc")],
    )
    assert _rows(wh, by_rep) == [("a", 220.0), ("b", 50.0)]
    by_month = _rows(wh, QueryIntent(entity="daily_sales", metrics=["quota"], grain="month"))
    assert sorted(r[1] for r in by_month) == [120.0, 150.0]


def test_a_window_keeping_part_of_a_month_counts_its_quota_once(
    wh: duckdb.DuckDBPyConnection,
) -> None:
    intent = QueryIntent(
        entity="daily_sales",
        metrics=["quota"],
        filters=[
            IntentFilter(field="day", op=">=", value="2026-09-02"),
            IntentFilter(field="day", op="<=", value="2026-09-30"),
        ],
    )
    assert _rows(wh, intent) == [(100.0,)]


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery", "snowflake", "mysql"])
def test_every_dialect_compiles_the_deduplicated_form(dialect: str) -> None:
    sql = compile_intent(
        QueryIntent(entity="daily_sales", metrics=["quota"], dimensions=["rep_id"]),
        _model(dialect),
    )
    assert "ROW_NUMBER() OVER (PARTITION BY" in sql
    assert "_dst_n0 = 1" in sql


def test_the_key_is_required() -> None:
    with pytest.raises(ValidationError, match="distinct_on"):
        Metric(name="quota", agg="sum_distinct_on", expr="quotas.quota")


def test_a_key_on_another_aggregate_is_rejected() -> None:
    with pytest.raises(ValidationError, match="sum_distinct_on"):
        Metric(name="quota", agg="sum", expr="quotas.quota", distinct_on=["quotas.rep_id"])


def test_it_never_appears_as_a_plain_aggregate() -> None:
    model = _model()
    entity = model.entities[0]
    with pytest.raises(CompileError, match="only the metric compiler"):
        metric_sql(entity.metrics[1], entity)


def test_a_ratio_over_it_is_refused() -> None:
    model = _model()
    entity = model.entities[0]
    ratio = Metric(name="attainment", type="ratio", numerator="sales", denominator="quota")
    entity.metrics.append(ratio)
    with pytest.raises(CompileError):
        compile_intent(QueryIntent(entity="daily_sales", metrics=["attainment"]), model)
