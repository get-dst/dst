"""The dbt artifact reader parses models, semantic models, and metrics; without a
semantic layer it imports the manifest's models as entities instead of refusing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from services.cli.main import _import_dbt
from services.dbt.artifacts import (
    DbtArtifactError,
    load_artifacts,
    parse_artifacts,
)
from services.dbt.compile import import_shared_assets

TARGET = Path(__file__).resolve().parent.parent / "fixtures" / "jaffle" / "target"


def test_load_jaffle_artifacts() -> None:
    art = load_artifacts(TARGET)
    assert art.project == "jaffle_shop"
    assert "v12" in art.manifest_version
    assert art.dbt_version == "1.8.0"

    # models (from manifest.json) keyed by alias, with typed columns
    assert set(art.models) == {"orders", "customers"}
    orders = art.models["orders"]
    assert orders.relation_name == '"jaffle_shop"."main"."orders"'
    cols = {c.name: c.data_type for c in orders.columns}
    assert cols["amount"] == "double"
    assert cols["order_id"] == "integer"

    # semantic models, entities, dimensions, measures
    sm = {s.name: s for s in art.semantic_models}
    assert set(sm) == {"orders", "customers"}
    o = sm["orders"]
    assert o.model_alias == "orders"
    assert [e.name for e in o.entities if e.type == "primary"] == ["order"]
    assert {m.name: m.agg for m in o.measures}["order_total"] == "sum"
    assert {d.name: d.type for d in o.dimensions}["order_date"] == "time"

    # metrics with backing measures / ratio params
    metrics = {m.name: m for m in art.metrics}
    assert metrics["revenue"].type == "simple"
    assert metrics["revenue"].measure == "order_total"
    assert metrics["average_order_value"].type == "ratio"
    assert metrics["average_order_value"].numerator == "revenue"
    assert metrics["average_order_value"].denominator == "order_count"


def _manifest() -> dict[str, object]:
    return {
        "metadata": {"project_name": "plain", "dbt_schema_version": "v12"},
        "nodes": {
            "model.plain.orders": {
                "resource_type": "model",
                "name": "orders",
                "alias": "orders",
                "database": "shop",
                "schema": "main",
                "relation_name": '"shop"."main"."orders"',
                "description": "One row per order.",
                "columns": {
                    "order_id": {"name": "order_id", "description": "Primary key."},
                    "amount": {"name": "amount", "data_type": "double"},
                    "status": {"name": "status"},
                },
            },
            "test.plain.unique_orders_order_id": {
                "resource_type": "test",
                "test_metadata": {"name": "unique"},
                "column_name": "order_id",
                "attached_node": "model.plain.orders",
            },
            "test.plain.not_null_orders_order_id": {
                "resource_type": "test",
                "test_metadata": {"name": "not_null"},
                "column_name": "order_id",
                "attached_node": "model.plain.orders",
            },
            "test.plain.not_null_orders_amount": {
                "resource_type": "test",
                "test_metadata": {"name": "not_null"},
                "column_name": "amount",
                "attached_node": "model.plain.orders",
            },
        },
    }


def test_no_semantic_layer_imports_manifest_models(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps(_manifest()))
    (tmp_path / "catalog.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.plain.orders": {
                        "columns": {
                            "order_id": {"type": "INTEGER"},
                            "status": {"type": "VARCHAR"},
                        }
                    }
                }
            }
        )
    )
    art = load_artifacts(tmp_path)
    assert art.manifest_only
    assert art.metrics == []
    sm = art.semantic_models
    assert [s.name for s in sm] == ["orders"]
    assert sm[0].model_alias == "orders"
    assert sm[0].description == "One row per order."
    # unique + not_null on one column is the grain; not_null alone is not
    assert [(e.name, e.type) for e in sm[0].entities] == [("order_id", "primary")]
    assert sm[0].measures == [] and sm[0].dimensions == []
    # catalog types fill in where the manifest declares none; declared types win
    cols = {c.name: c.data_type for c in art.models["orders"].columns}
    assert cols == {"order_id": "INTEGER", "amount": "double", "status": "VARCHAR"}

    result = import_shared_assets(art, connection="warehouse")
    assert [e.name for e in result.entities] == ["orders"]
    orders = result.entities[0]
    assert orders.source.table == "main.orders"  # the schema dbt built it in
    assert orders.primary_key == ["order_id"]
    assert orders.description == "One row per order."
    assert {f.name: f.type for f in orders.fields} == {
        "order_id": "integer",
        "amount": "number",
        "status": "string",
    }
    assert orders.metrics == [] and orders.dimensions == []
    assert result.skipped == []


def test_empty_semantic_models_falls_back_to_manifest() -> None:
    art = parse_artifacts(_manifest(), {"semantic_models": [], "metrics": []})
    assert art.manifest_only and [s.name for s in art.semantic_models] == ["orders"]


def test_semantic_layer_present_is_not_manifest_only() -> None:
    assert not load_artifacts(TARGET).manifest_only


def test_nothing_to_import_fails_loudly() -> None:
    with pytest.raises(DbtArtifactError, match="no semantic models and no models"):
        parse_artifacts({"metadata": {"project_name": "x"}, "nodes": {}}, None)


def test_import_dbt_cli_says_what_it_imported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(_manifest()))
    project = tmp_path / "proj"
    args = argparse.Namespace(target_dir=str(target), connection="warehouse", dir=str(project))
    assert _import_dbt(args) == 0
    out = capsys.readouterr().out
    assert "no semantic layer found" in out
    assert "entity orders: main.orders, 3 fields, key order_id" in out
    assert "metrics and dimensions still need authoring" in out
    assert (project / "semantic" / "entities" / "orders.yaml").exists()
