"""`dst import dbt`: artifacts compile one-shot into dst-owned
shared-layer assets; unsupported constructs are reported, not silently dropped. The
end-to-end test proves an imported metric runs on jaffle through select-all lens
compilation + the runtime compiler + SQL guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.lens_config import LensConfig
from services.contracts.query_intent import IntentFilter, QueryIntent
from services.contracts.shared_semantic import SelectSpec
from services.dbt.artifacts import load_artifacts, parse_artifacts
from services.dbt.compile import import_shared_assets
from services.project.compile import compile_lens_model
from services.runtime.compiler import compile_intent
from services.runtime.sql_guard import check as guard_check

TARGET = Path(__file__).resolve().parent.parent / "fixtures" / "jaffle" / "target"


def _import():
    return import_shared_assets(load_artifacts(TARGET), connection="jaffle")


def _model():
    """Select-all lens compile over the imported assets — the real consumption path."""
    res = _import()
    config = LensConfig(
        name="jaffle_sales",
        display_name="Jaffle",
        connections=["jaffle"],
        select=SelectSpec.model_validate({"entities": ["*"], "definitions": ["*"]}),
    )
    model, _warnings = compile_lens_model(
        config=config,
        shared_entities={e.name: e for e in res.entities},
        shared_definitions={d.term: d for d in res.definitions},
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
    )
    return model


def test_imports_tables_columns_and_grain_from_dbt() -> None:
    res = _import()
    by_name = {e.name: e for e in res.entities}
    assert set(by_name) == {"orders", "customers"}
    orders = by_name["orders"]
    assert orders.source.table == "orders" and orders.primary_key == ["order_id"]
    assert orders.grain == "one row per order"
    assert any(f.name == "amount" for f in orders.fields)
    # FK-side join, cardinality declared
    assert len(orders.joins) == 1
    join = orders.joins[0]
    assert (join.right, join.relationship) == ("customers", "many_to_one")
    assert join.on == "orders.customer_id = customers.customer_id"
    assert by_name["customers"].joins == []


def test_supported_measures_and_business_metrics() -> None:
    orders = next(e for e in _import().entities if e.name == "orders")
    metrics = {m.name: m for m in orders.metrics}
    # raw measures, mapped aggs (average -> avg), expr qualified by entity alias
    assert metrics["order_total"].agg == "sum"
    assert metrics["order_total"].expr == "orders.amount"
    assert metrics["average_order_amount"].agg == "avg"
    assert metrics["distinct_customers"].agg == "count_distinct"
    assert metrics["order_count"].expr == "1"  # literal expr left unqualified
    # simple dbt metric surfaced under its business name
    assert "revenue" in metrics
    assert metrics["revenue"].expr == "orders.amount"
    # unsupported (median) is NOT a queryable metric
    assert "median_order_amount" not in metrics


def test_compound_metrics_import_as_queryable() -> None:
    """Ratio/derived metrics whose inputs all resolve on one entity become compound
    Metric entries (the average_order_value credibility gap, closed)."""
    orders = next(e for e in _import().entities if e.name == "orders")
    metrics = {m.name: m for m in orders.metrics}
    aov = metrics["average_order_value"]
    assert (aov.type, aov.numerator, aov.denominator) == ("ratio", "revenue", "order_count")
    open_rev = metrics["open_revenue"]
    assert open_rev.type == "derived"
    assert open_rev.expr == "{revenue} - {completed_revenue}"
    # cross-entity derived stays out of the metric list — an honest skip, below
    assert "clv_per_order" not in metrics


def test_definitions_are_dst_owned_and_skips_reported() -> None:
    res = _import()
    defs = {d.term: d for d in res.definitions}
    # dst-owned from the moment they land — no dbt read-only tag
    assert defs["revenue"].source == "authored"
    assert defs["revenue"].sql_expr == "SUM(orders.amount)"
    # queryable compound metrics carry their EXPANDED aggregate as the enforceable SQL
    assert defs["average_order_value"].sql_expr == (
        "CAST(SUM(orders.amount) AS DOUBLE) / NULLIF(COUNT(1), 0)"
    )
    # cross-entity derived stays prose-only
    assert defs["clv_per_order"].sql_expr is None
    # nothing dropped silently
    skipped = {(s.kind, s.name) for s in res.skipped}
    assert ("measure", "median_order_amount") in skipped
    assert ("metric", "average_order_value") not in skipped  # it imported — no skip
    assert ("metric", "clv_per_order") in skipped
    reason = next(s.reason for s in res.skipped if s.name == "clv_per_order")
    assert "span entities" in reason


def test_imported_metric_runs_on_jaffle() -> None:
    """The payoff: import -> select-all compile -> runtime compiler + guard -> real rows."""
    sm = _model()
    assert sm.dialect == "duckdb"
    assert sm.allowed_tables() == {"orders", "customers"}
    intent = QueryIntent(
        entity="orders",
        metrics=["revenue"],
        dimensions=["status"],
        filters=[IntentFilter(field="status", op="=", value="completed")],
    )
    sql = compile_intent(intent, sm)
    verdict = guard_check(sql, sm)
    assert verdict.ok, verdict.reason
    result = DuckDBConnector(settings.duckdb_jaffle_path).execute(sql, read_only=True)
    assert result.rows
    assert "revenue" in [c.lower() for c in result.columns]
    # the compiled model carries the flattened many_to_one join
    assert sm.joins[0].relationship == "many_to_one"


def test_cli_import_writes_reviewable_files(tmp_path: Path) -> None:
    import argparse

    from services.cli.main import _import_dbt
    from services.semantic.files import parse_semantic_files

    ns = argparse.Namespace(target_dir=str(TARGET), connection="jaffle", dir=str(tmp_path))
    assert _import_dbt(ns) == 0
    written = {
        p.relative_to(tmp_path).as_posix(): p.read_text(encoding="utf-8")
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    orders = written["semantic/entities/orders.yaml"]
    assert orders.startswith("# imported from dbt project")  # provenance stamp
    assert all(ord(c) < 128 for c in orders)  # scaffold stays ASCII-clean
    entities, definitions = parse_semantic_files(written)  # header doesn't break parsing
    assert entities["orders"].grain == "one row per order"
    assert definitions["revenue"].source == "authored"


def test_agg_time_dimension_becomes_time_fields() -> None:
    """agg_time_dimension is wired, not discarded: the model-level default lands as
    Entity.default_time_field; measures that agree with it carry no override."""
    by_name = {e.name: e for e in _import().entities}
    assert by_name["orders"].default_time_field == "order_date"
    assert all(m.agg_time_field is None for m in by_name["orders"].metrics)
    assert by_name["customers"].default_time_field is None  # no time-anchored measures


def test_measure_time_dimension_differing_from_default_becomes_override() -> None:
    manifest = {"metadata": {"project_name": "t"}, "nodes": {}}
    sem = {
        "semantic_models": [
            {
                "name": "orders",
                "node_relation": {"alias": "orders"},
                "defaults": {"agg_time_dimension": "order_date"},
                "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
                "dimensions": [],
                "measures": [
                    {
                        "name": "order_total",
                        "agg": "sum",
                        "expr": "amount",
                        "agg_time_dimension": "order_date",
                    },
                    {
                        "name": "shipped_total",
                        "agg": "sum",
                        "expr": "amount",
                        "agg_time_dimension": "shipped_at",
                    },
                ],
            }
        ],
        "metrics": [],
    }
    orders = import_shared_assets(parse_artifacts(manifest, sem), connection="c").entities[0]
    assert orders.default_time_field == "order_date"
    by = {m.name: m for m in orders.metrics}
    assert by["order_total"].agg_time_field is None  # matches the default
    assert by["shipped_total"].agg_time_field == "shipped_at"


def test_metric_filters_survive_import() -> None:
    """A filtered dbt metric must keep its filter — as
    Metric.filters on the entity AND inside the definition's sql_expr."""
    res = _import()
    orders = next(e for e in res.entities if e.name == "orders")
    completed = next(m for m in orders.metrics if m.name == "completed_revenue")
    assert completed.filters == ["orders.status = 'completed'"]
    defs = {d.term: d for d in res.definitions}
    assert defs["completed_revenue"].sql_expr == (
        "SUM(CASE WHEN orders.status = 'completed' THEN orders.amount END)"
    )


def test_untranslatable_filter_is_reported_never_unfiltered() -> None:
    """An unfiltered stand-in would be the wrong number: no queryable metric,
    no sql_expr, and an honest skipped entry with the raw filter preserved."""
    res = _import()
    orders = next(e for e in res.entities if e.name == "orders")
    assert "weird_filtered" not in {m.name for m in orders.metrics}
    defs = {d.term: d for d in res.definitions}
    assert defs["weird_filtered"].sql_expr is None
    assert "TimeDimension" in defs["weird_filtered"].body  # raw filter preserved in prose
    skipped = {(s.kind, s.name) for s in res.skipped}
    assert ("metric", "weird_filtered") in skipped


def test_imported_definitions_bind_about_to_their_metric() -> None:
    """Meaning attaches to structure: an imported metric's definition points at
    the entity metric that computes it; non-queryable ones stay unbound."""
    defs = {d.term: d for d in _import().definitions}
    assert defs["revenue"].about == "orders.revenue"
    assert defs["completed_revenue"].about == "orders.completed_revenue"
    assert defs["average_order_value"].about == "orders.average_order_value"  # ratio, queryable
    assert defs["clv_per_order"].about is None  # cross-entity: no queryable metric
    assert defs["weird_filtered"].about is None  # untranslatable filter


def test_imported_ratio_runs_on_jaffle() -> None:
    """The credibility gap, closed: average_order_value compiles through the intent
    path and returns SUM(amount)/COUNT(*) from the real warehouse."""
    sm = _model()
    sql = compile_intent(QueryIntent(entity="orders", metrics=["average_order_value"]), sm)
    verdict = guard_check(sql, sm)
    assert verdict.ok, verdict.reason
    conn = DuckDBConnector(settings.duckdb_jaffle_path)
    got = conn.execute(sql, read_only=True).rows[0][0]
    want = conn.execute("SELECT SUM(amount) * 1.0 / COUNT(*) FROM orders", read_only=True).rows[0][
        0
    ]
    assert got == pytest.approx(want)
