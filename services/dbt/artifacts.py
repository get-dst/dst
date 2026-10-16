"""Typed reader for dbt compiled artifacts.

Parses ``manifest.json`` (models + columns + physical relations) and
``semantic_manifest.json`` (the dbt semantic layer: semantic models, entities,
dimensions, measures, metrics) into typed objects. This is deliberately NOT the text
chunker the GitHub connector uses — dbt's structure is parsed, not embedded.

Stdlib JSON only. Version-detects the manifest schema. Without a semantic layer
(no ``semantic_manifest.json``, or one with no semantic models) the manifest's own
models become the semantic models: one per model, columns as fields, ``catalog.json``
types when the catalog is present, and a ``unique`` + ``not_null`` tested column as the
primary key. Metrics and dimensions are not invented; ``DbtArtifacts.manifest_only``
says which path was taken so the caller can say so too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class DbtArtifactError(Exception):
    """Artifacts are missing, malformed, or an unsupported schema version."""


@dataclass(frozen=True)
class DbtColumn:
    name: str
    data_type: str | None
    description: str | None = None


@dataclass(frozen=True)
class DbtModel:
    unique_id: str
    name: str
    alias: str
    database: str | None
    schema: str | None
    relation_name: str | None
    description: str | None
    columns: list[DbtColumn] = field(default_factory=list)


@dataclass(frozen=True)
class DbtEntity:
    name: str
    type: str  # "primary" | "foreign" | "unique" | "natural"
    expr: str | None = None  # defaults to name when absent


@dataclass(frozen=True)
class DbtDimension:
    name: str
    type: str  # "categorical" | "time"
    expr: str | None = None
    granularity: str | None = None


@dataclass(frozen=True)
class DbtMeasure:
    name: str
    agg: str  # dbt agg: sum | count | count_distinct | average | min | max | median | …
    expr: str | None = None  # defaults to name when absent
    agg_time_dimension: str | None = None


@dataclass(frozen=True)
class DbtSemanticModel:
    name: str
    description: str | None
    model_alias: str  # node_relation.alias — the physical table this grain maps to
    relation_name: str | None
    agg_time_dimension: str | None = None  # defaults.agg_time_dimension
    entities: list[DbtEntity] = field(default_factory=list)
    dimensions: list[DbtDimension] = field(default_factory=list)
    measures: list[DbtMeasure] = field(default_factory=list)


@dataclass(frozen=True)
class DbtMetric:
    name: str
    type: str  # "simple" | "ratio" | "derived" | "cumulative" | "conversion"
    description: str | None
    measure: str | None = None  # for simple metrics: the backing measure name
    numerator: str | None = None  # for ratio metrics
    denominator: str | None = None  # for ratio metrics
    expr: str | None = None  # for derived metrics: arithmetic over input metric names
    inputs: list[str] = field(default_factory=list)  # for derived metrics: input metrics
    # False when a derived input carries alias/offset/filter — not importable as-is.
    inputs_plain: bool = True
    # The metric-level where filter (jinja template SQL), AND-joined when dbt
    # declares several. Dropping this silently would change the numbers.
    filter: str | None = None


@dataclass
class DbtArtifacts:
    project: str
    manifest_version: str
    dbt_version: str | None
    models: dict[str, DbtModel]  # keyed by alias
    semantic_models: list[DbtSemanticModel]
    metrics: list[DbtMetric]
    # True when no semantic layer was found and the semantic models were
    # derived from the manifest's models alone (no measures, no dimensions).
    manifest_only: bool = False

    def model_for(self, sm: DbtSemanticModel) -> DbtModel | None:
        """The manifest model backing a semantic model — matched by alias, then name."""
        return self.models.get(sm.model_alias) or self.models.get(sm.name)


# ---------------------------------------------------------------------------- parsing


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _catalog_types(catalog: dict[str, object] | None) -> dict[str, dict[str, str]]:
    """model unique_id -> column name -> warehouse type, from ``catalog.json``."""
    nodes = catalog.get("nodes") if catalog else None
    if not isinstance(nodes, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for unique_id, node in nodes.items():
        cols = node.get("columns") if isinstance(node, dict) else None
        if not isinstance(cols, dict):
            continue
        out[str(unique_id)] = {
            str(name): str(col["type"])
            for name, col in cols.items()
            if isinstance(col, dict) and _str(col.get("type"))
        }
    return out


def _parse_models(
    manifest: dict[str, object], catalog: dict[str, object] | None = None
) -> dict[str, DbtModel]:
    nodes = manifest.get("nodes")
    if not isinstance(nodes, dict):
        return {}
    types = _catalog_types(catalog)
    models: dict[str, DbtModel] = {}
    for unique_id, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        name = _str(node.get("name")) or str(unique_id)
        alias = _str(node.get("alias")) or name
        cols_raw = node.get("columns")
        columns: list[DbtColumn] = []
        catalog_cols = types.get(str(unique_id), {})
        if isinstance(cols_raw, dict):
            for col_name, col in cols_raw.items():
                if not isinstance(col, dict):
                    continue
                cname = _str(col.get("name")) or str(col_name)
                columns.append(
                    DbtColumn(
                        name=cname,
                        data_type=_str(col.get("data_type")) or catalog_cols.get(cname),
                        description=_str(col.get("description")),
                    )
                )
        # The catalog is what the warehouse built; a schema.yml that documents
        # fewer columns than the table holds must not shrink the entity.
        documented = {c.name.lower() for c in columns}
        columns += [
            DbtColumn(name=cname, data_type=ctype)
            for cname, ctype in catalog_cols.items()
            if cname.lower() not in documented
        ]
        models[alias] = DbtModel(
            unique_id=str(unique_id),
            name=name,
            alias=alias,
            database=_str(node.get("database")),
            schema=_str(node.get("schema")),
            relation_name=_str(node.get("relation_name")),
            description=_str(node.get("description")),
            columns=columns,
        )
    return models


def _tested_columns(manifest: dict[str, object]) -> dict[str, dict[str, set[str]]]:
    """model unique_id -> column -> the generic test names dbt attaches to it
    (``unique``, ``not_null``, ...), read off the manifest's test nodes."""
    nodes = manifest.get("nodes")
    if not isinstance(nodes, dict):
        return {}
    out: dict[str, dict[str, set[str]]] = {}
    for node in nodes.values():
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        meta = node.get("test_metadata")
        test = _str(meta.get("name")) if isinstance(meta, dict) else None
        column = _str(node.get("column_name"))
        model = _str(node.get("attached_node"))
        if not model and isinstance(node.get("depends_on"), dict):
            deps = node["depends_on"].get("nodes")
            model = next((d for d in deps or [] if str(d).startswith("model.")), None)
        if test and column and model:
            out.setdefault(model, {}).setdefault(column, set()).add(test)
    return out


def _models_as_semantic(
    models: dict[str, DbtModel], manifest: dict[str, object]
) -> list[DbtSemanticModel]:
    """One semantic model per manifest model: the grain is the column dbt tests
    both ``unique`` and ``not_null`` (when exactly one qualifies); nothing else
    is inferred — a measure or a dimension is a meaning, and the manifest
    declares none."""
    tested = _tested_columns(manifest)
    out: list[DbtSemanticModel] = []
    for model in models.values():
        keys = [
            c.name
            for c in model.columns
            if {"unique", "not_null"} <= tested.get(model.unique_id, {}).get(c.name, set())
        ]
        out.append(
            DbtSemanticModel(
                name=model.name,
                description=model.description,
                model_alias=model.alias,
                relation_name=model.relation_name,
                entities=[DbtEntity(name=keys[0], type="primary")] if len(keys) == 1 else [],
            )
        )
    return out


def _parse_semantic_models(sem: dict[str, object]) -> list[DbtSemanticModel]:
    raw = sem.get("semantic_models")
    if not isinstance(raw, list):
        return []
    out: list[DbtSemanticModel] = []
    for sm in raw:
        if not isinstance(sm, dict):
            continue
        rel = sm.get("node_relation") if isinstance(sm.get("node_relation"), dict) else {}
        assert isinstance(rel, dict)
        name = _str(sm.get("name")) or ""
        if not name:
            continue
        entities = [
            DbtEntity(
                name=_str(e.get("name")) or "",
                type=_str(e.get("type")) or "foreign",
                expr=_str(e.get("expr")),
            )
            for e in sm.get("entities", [])
            if isinstance(e, dict) and _str(e.get("name"))
        ]
        dimensions = [
            DbtDimension(
                name=_str(d.get("name")) or "",
                type=_str(d.get("type")) or "categorical",
                expr=_str(d.get("expr")),
                granularity=_granularity(d),
            )
            for d in sm.get("dimensions", [])
            if isinstance(d, dict) and _str(d.get("name"))
        ]
        measures = [
            DbtMeasure(
                name=_str(m.get("name")) or "",
                agg=_str(m.get("agg")) or "",
                expr=_str(m.get("expr")),
                agg_time_dimension=_str(m.get("agg_time_dimension")),
            )
            for m in sm.get("measures", [])
            if isinstance(m, dict) and _str(m.get("name"))
        ]
        defaults = sm.get("defaults") if isinstance(sm.get("defaults"), dict) else {}
        assert isinstance(defaults, dict)
        out.append(
            DbtSemanticModel(
                name=name,
                description=_str(sm.get("description")),
                model_alias=_str(rel.get("alias")) or name,
                relation_name=_str(rel.get("relation_name")),
                agg_time_dimension=_str(defaults.get("agg_time_dimension")),
                entities=entities,
                dimensions=dimensions,
                measures=measures,
            )
        )
    return out


def _granularity(dim: dict[str, object]) -> str | None:
    tp = dim.get("type_params")
    if isinstance(tp, dict):
        return _str(tp.get("time_granularity"))
    return None


def _parse_metrics(sem: dict[str, object]) -> list[DbtMetric]:
    raw = sem.get("metrics")
    if not isinstance(raw, list):
        return []
    out: list[DbtMetric] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        name = _str(m.get("name"))
        if not name:
            continue
        tp = m.get("type_params") if isinstance(m.get("type_params"), dict) else {}
        assert isinstance(tp, dict)
        measure = tp.get("measure")
        measure_name = _str(measure.get("name")) if isinstance(measure, dict) else None
        num = tp.get("numerator")
        den = tp.get("denominator")
        inputs: list[str] = []
        inputs_plain = True
        raw_inputs = tp.get("metrics")
        if isinstance(raw_inputs, list):
            for im in raw_inputs:
                if isinstance(im, str):
                    inputs.append(im)
                elif isinstance(im, dict) and _str(im.get("name")):
                    inputs.append(str(im["name"]))
                    if any(
                        im.get(k) for k in ("alias", "offset_window", "offset_to_grain", "filter")
                    ):
                        inputs_plain = False
        out.append(
            DbtMetric(
                name=name,
                type=_str(m.get("type")) or "simple",
                description=_str(m.get("description")),
                measure=measure_name,
                numerator=_str(num.get("name")) if isinstance(num, dict) else _str(num),
                denominator=_str(den.get("name")) if isinstance(den, dict) else _str(den),
                expr=_str(tp.get("expr")),
                inputs=inputs,
                inputs_plain=inputs_plain,
                filter=_parse_filter(m.get("filter")),
            )
        )
    return out


def _parse_filter(raw: object) -> str | None:
    """dbt metric filters: a plain template string (older SL) or
    {"where_filters": [{"where_sql_template": ...}]} (newer). AND-joined."""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, dict):
        parts = [
            str(f.get("where_sql_template")).strip()
            for f in raw.get("where_filters") or []
            if isinstance(f, dict) and f.get("where_sql_template")
        ]
        if parts:
            return " AND ".join(f"({p})" for p in parts) if len(parts) > 1 else parts[0]
    return None


def parse_artifacts(
    manifest: dict[str, object],
    semantic_manifest: dict[str, object] | None = None,
    catalog: dict[str, object] | None = None,
) -> DbtArtifacts:
    """Build typed artifacts from already-loaded JSON dicts (pure; unit-testable).

    With semantic models present the semantic layer is the source; without one
    (``semantic_manifest`` absent or empty) the manifest's models stand in, so a
    dbt project with no MetricFlow still imports its tables, columns and keys."""
    meta = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    assert isinstance(meta, dict)
    manifest_version = _str(meta.get("dbt_schema_version")) or "unknown"
    project = _str(meta.get("project_name")) or "dbt_project"

    models = _parse_models(manifest, catalog)
    semantic_models = _parse_semantic_models(semantic_manifest or {})
    manifest_only = not semantic_models
    if manifest_only:
        if not models:
            raise DbtArtifactError(
                "no semantic models and no models in manifest.json — is this a built dbt "
                "project? (run `dbt parse` or `dbt build` first)"
            )
        semantic_models = _models_as_semantic(models, manifest)

    return DbtArtifacts(
        project=project,
        manifest_version=manifest_version,
        dbt_version=_str(meta.get("dbt_version")),
        models=models,
        semantic_models=semantic_models,
        metrics=_parse_metrics(semantic_manifest or {}),
        manifest_only=manifest_only,
    )


def _read_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DbtArtifactError(f"could not read dbt artifact {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise DbtArtifactError(f"dbt artifact {path} is not a JSON object")
    return data


def load_artifacts(target_dir: str | Path) -> DbtArtifacts:
    """Load and parse a dbt ``target/`` directory: ``manifest.json`` (required),
    ``semantic_manifest.json`` and ``catalog.json`` (each used when present).
    Raises DbtArtifactError when the manifest is missing or any file is invalid."""
    target = Path(target_dir)
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        raise DbtArtifactError(f"manifest.json not found in {target}")
    semantic_path = target / "semantic_manifest.json"
    catalog_path = target / "catalog.json"
    return parse_artifacts(
        _read_json(manifest_path),
        _read_json(semantic_path) if semantic_path.is_file() else None,
        _read_json(catalog_path) if catalog_path.is_file() else None,
    )
