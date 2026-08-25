"""Typed reader for dbt compiled artifacts.

Parses ``manifest.json`` (models + columns + physical relations) and
``semantic_manifest.json`` (the dbt semantic layer: semantic models, entities,
dimensions, measures, metrics) into typed objects. This is deliberately NOT the text
chunker the GitHub connector uses — dbt's structure is parsed, not embedded.

Stdlib JSON only. Version-detects the manifest schema and fails loudly on a missing or
empty semantic manifest, so the caller never silently compiles an empty model.
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

    def model_for(self, sm: DbtSemanticModel) -> DbtModel | None:
        """The manifest model backing a semantic model — matched by alias, then name."""
        return self.models.get(sm.model_alias) or self.models.get(sm.name)


# ---------------------------------------------------------------------------- parsing


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_models(manifest: dict[str, object]) -> dict[str, DbtModel]:
    nodes = manifest.get("nodes")
    if not isinstance(nodes, dict):
        return {}
    models: dict[str, DbtModel] = {}
    for unique_id, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        name = _str(node.get("name")) or str(unique_id)
        alias = _str(node.get("alias")) or name
        cols_raw = node.get("columns")
        columns: list[DbtColumn] = []
        if isinstance(cols_raw, dict):
            for col_name, col in cols_raw.items():
                if not isinstance(col, dict):
                    continue
                columns.append(
                    DbtColumn(
                        name=_str(col.get("name")) or str(col_name),
                        data_type=_str(col.get("data_type")),
                        description=_str(col.get("description")),
                    )
                )
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
    manifest: dict[str, object], semantic_manifest: dict[str, object]
) -> DbtArtifacts:
    """Build typed artifacts from already-loaded JSON dicts (pure; unit-testable)."""
    meta = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    assert isinstance(meta, dict)
    manifest_version = _str(meta.get("dbt_schema_version")) or "unknown"
    project = _str(meta.get("project_name")) or "dbt_project"

    semantic_models = _parse_semantic_models(semantic_manifest)
    if not semantic_models:
        raise DbtArtifactError(
            "semantic_manifest.json has no semantic_models — is the dbt semantic layer "
            "configured and built? (run `dbt parse` with semantic models defined)"
        )

    return DbtArtifacts(
        project=project,
        manifest_version=manifest_version,
        dbt_version=_str(meta.get("dbt_version")),
        models=_parse_models(manifest),
        semantic_models=semantic_models,
        metrics=_parse_metrics(semantic_manifest),
    )


def load_artifacts(target_dir: str | Path) -> DbtArtifacts:
    """Load and parse ``manifest.json`` + ``semantic_manifest.json`` from a dbt
    ``target/`` directory. Raises DbtArtifactError if either file is missing or invalid."""
    target = Path(target_dir)
    manifest_path = target / "manifest.json"
    semantic_path = target / "semantic_manifest.json"
    if not manifest_path.is_file():
        raise DbtArtifactError(f"manifest.json not found in {target}")
    if not semantic_path.is_file():
        raise DbtArtifactError(
            f"semantic_manifest.json not found in {target} — the dbt semantic layer "
            "produces it on `dbt parse`/`dbt build`."
        )
    try:
        manifest = json.loads(manifest_path.read_text())
        semantic = json.loads(semantic_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DbtArtifactError(f"could not read dbt artifacts in {target}: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(semantic, dict):
        raise DbtArtifactError(f"dbt artifacts in {target} are not JSON objects")
    return parse_artifacts(manifest, semantic)
