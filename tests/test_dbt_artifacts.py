"""The dbt artifact reader parses models, semantic models, and metrics,
and fails loudly on a missing/empty semantic manifest."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.dbt.artifacts import (
    DbtArtifactError,
    load_artifacts,
    parse_artifacts,
)

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


def test_missing_semantic_manifest_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text('{"metadata": {}, "nodes": {}}')
    with pytest.raises(DbtArtifactError, match="semantic_manifest.json not found"):
        load_artifacts(tmp_path)


def test_empty_semantic_models_fails_loudly() -> None:
    manifest = {"metadata": {"project_name": "x"}, "nodes": {}}
    with pytest.raises(DbtArtifactError, match="no semantic_models"):
        parse_artifacts(manifest, {"semantic_models": [], "metrics": []})
