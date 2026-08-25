"""Deterministic dbt → shared-semantic-layer import compiler.

No LLM, one-shot: `dst import dbt` turns a dbt project's compiled artifacts
into dst-owned `semantic/` files — SharedEntities (table, grain, primary
key, fields, dimensions, queryable metrics incl. ratio/derived compounds whose
inputs all resolve on one entity, FK-side many_to_one joins) and Definitions
(source="authored": from the moment they land, dst's own drift audit and
review cycle maintain them; dbt is never re-synced). Constructs we can't
faithfully compile (unsupported aggregations, cross-entity or unresolvable
compound metrics, cumulative/conversion) are reported in
``ImportResult.skipped`` — never silently dropped.

The Entity name aliases the table in generated SQL (``FROM table AS entity``), so metric
expressions are qualified ``entity.column`` to match the runtime compiler's resolution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import cast

from services.contracts.semantic_model import (
    Definition,
    Dimension,
    Entity,
    EntitySource,
    Field,
    FieldType,
    Metric,
    warehouse_field_type,
)
from services.contracts.shared_semantic import SharedEntity, SharedJoin
from services.dbt.artifacts import (
    DbtArtifacts,
    DbtMeasure,
    DbtMetric,
    DbtModel,
    DbtSemanticModel,
)
from services.runtime.compiler import metric_sql

# dbt aggregation → our AggType. Anything not here is reported as skipped (not computable
# deterministically by the runtime metric compiler).
_AGG_MAP: dict[str, str] = {
    "sum": "sum",
    "count": "count",
    "count_distinct": "count_distinct",
    "average": "avg",
    "avg": "avg",
    "min": "min",
    "max": "max",
}

_TIME_GRAINS = {"day", "week", "month", "quarter", "year", "date"}


@dataclass(frozen=True)
class SkippedConstruct:
    kind: str  # "measure" | "metric" | "dimension" | "semantic_model"
    name: str
    reason: str


@dataclass
class ImportResult:
    entities: list[SharedEntity] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)
    skipped: list[SkippedConstruct] = field(default_factory=list)


def _field_type(data_type: str | None) -> FieldType:
    """dbt's `data_type` is the warehouse's own spelling — one mapping serves
    every source of physical types (introspect, dbt, the type-enum error)."""
    if not data_type:
        return "string"
    return cast(FieldType, warehouse_field_type(data_type) or "string")


def _dimension_type(dim_type: str, granularity: str | None) -> FieldType:
    if dim_type == "time":
        return "date" if (granularity or "").lower() in _TIME_GRAINS else "timestamp"
    return "string"


def _is_identifier(expr: str) -> bool:
    """A bare column reference we can safely qualify with the entity alias."""
    return expr.replace("_", "").isalnum() and not expr[0].isdigit()


def _qualify(entity_name: str, expr: str) -> str:
    """Qualify a bare column with the entity alias; leave literals/expressions as-is
    (e.g. a ``count`` measure's ``expr`` of ``1`` must stay ``1``)."""
    if "." in expr or not _is_identifier(expr):
        return expr
    return f"{entity_name}.{expr}"


def _table_name(sm: DbtSemanticModel, strategy: str) -> str:
    if strategy == "relation_name" and sm.relation_name:
        return sm.relation_name.replace('"', "")
    return sm.model_alias


def _fields(model: DbtModel | None) -> list[Field]:
    if model is None:
        return []
    return [
        Field(name=c.name, type=_field_type(c.data_type), description=c.description)
        for c in model.columns
    ]


def _measure_metric(
    entity_name: str, m: DbtMeasure, default_time: str | None = None
) -> Metric | None:
    agg = _AGG_MAP.get(m.agg.strip().lower())
    if agg is None:
        return None
    expr = _qualify(entity_name, m.expr or m.name)
    # agg_time_dimension only survives as a metric-level override; the entity's
    # default_time_field covers the common case.
    time_field = m.agg_time_dimension if m.agg_time_dimension != default_time else None
    return Metric(name=m.name, agg=agg, expr=expr, agg_time_field=time_field)  # type: ignore[arg-type]


def _compile_entity(
    sm: DbtSemanticModel,
    artifacts: DbtArtifacts,
    connection: str,
    strategy: str,
    skipped: list[SkippedConstruct],
) -> tuple[Entity, dict[str, DbtMeasure]]:
    model = artifacts.model_for(sm)
    if model is None:
        skipped.append(
            SkippedConstruct("semantic_model", sm.name, "no backing manifest model found")
        )

    primary_key = [e.expr or e.name for e in sm.entities if e.type == "primary"]

    dimensions = [
        Dimension(
            name=d.name,
            expr=_qualify(sm.name, d.expr) if d.expr else None,
            type=_dimension_type(d.type, d.granularity),
        )
        for d in sm.dimensions
    ]

    # The entity's canonical event date: the model-level default when dbt declares
    # one, else the single agg_time_dimension its measures agree on.
    times = {m.agg_time_dimension for m in sm.measures if m.agg_time_dimension}
    default_time = sm.agg_time_dimension or (next(iter(times)) if len(times) == 1 else None)

    metrics: list[Metric] = []
    supported_measures: dict[str, DbtMeasure] = {}
    for m in sm.measures:
        metric = _measure_metric(sm.name, m, default_time)
        if metric is None:
            skipped.append(
                SkippedConstruct("measure", m.name, f"unsupported aggregation '{m.agg}'")
            )
            continue
        supported_measures[m.name] = m
        metrics.append(metric)

    entity = Entity(
        name=sm.name,
        description=sm.description,
        source=EntitySource(connection=connection, table=_table_name(sm, strategy)),
        default_time_field=default_time,
        primary_key=primary_key,
        fields=_fields(model),
        dimensions=dimensions,
        metrics=metrics,
    )
    return entity, supported_measures


def _compile_definitions(
    artifacts: DbtArtifacts,
    measures_by_entity: dict[str, dict[str, DbtMeasure]],
    owner_of: dict[str, str],
    skipped: list[SkippedConstruct],
    entity_map: dict[str, str],
    entities: list[Entity],
    compound_owner: dict[str, str],
) -> tuple[list[Definition], list[str]]:
    """dbt metrics → governed Definitions, dst-owned on arrival. Simple metrics get an
    enforceable ``sql_expr``; ratio/derived get their EXPANDED aggregate when they
    imported as queryable compound metrics (``compound_owner``), prose-only otherwise.
    Metric filters are part of the number: a translated filter lands inside the
    sql_expr (CASE form); an untranslatable one strips the sql_expr and is reported
    — an unfiltered aggregate would be the wrong number, silently."""
    by_name = {e.name: e for e in entities}
    definitions: list[Definition] = []
    used_metric_names: list[str] = []
    for metric in artifacts.metrics:
        used_metric_names.append(metric.name)
        body = metric.description or f"dbt {metric.type} metric '{metric.name}'."
        filter_sql: str | None = None
        if metric.filter:
            filter_sql = _translate_filter(metric.filter, entity_map)
            if filter_sql is None:
                body += f" Filter (translate by hand): {metric.filter}"
                skipped.append(
                    SkippedConstruct(
                        "metric",
                        metric.name,
                        f"filter not auto-translatable ({metric.filter!r}) — "
                        "definition kept prose-only",
                    )
                )
        # Bind the definition to the entity metric that computes it (meaning
        # attaches to structure — the metric is the enforceable thing). No
        # binding when no queryable metric lands (an unimportable compound, or a
        # filter we couldn't translate) — a dangling ref would just warn forever.
        queryable = not (metric.filter and filter_sql is None)
        owner: str | None = None
        sql_expr: str | None = None
        if queryable and metric.name in compound_owner:
            owner = compound_owner[metric.name]
            ent = by_name[owner]
            sql_expr = metric_sql(next(m for m in ent.metrics if m.name == metric.name), ent)
        elif queryable:
            sql_expr = _definition_sql_expr(metric, measures_by_entity, owner_of, filter_sql)
            owner = owner_of.get(metric.measure) if metric.measure else None
        definitions.append(
            Definition(
                term=metric.name,
                about=f"{owner}.{metric.name}" if owner and queryable else None,
                body=body,
                sql_expr=sql_expr,
            )
        )
        if metric.type in ("cumulative", "conversion"):
            # Documented, but not deterministically queryable via the runtime
            # compiler — keep as a definition only. (Unimportable ratio/derived
            # are reported by _apply_compound_metrics.)
            skipped.append(
                SkippedConstruct(
                    "metric", metric.name, f"'{metric.type}' metric kept as definition only"
                )
            )
        elif metric.type == "simple" and metric.measure and metric.measure not in owner_of:
            skipped.append(
                SkippedConstruct(
                    "metric", metric.name, f"backing measure '{metric.measure}' is unavailable"
                )
            )
    return definitions, used_metric_names


def _definition_sql_expr(
    metric: DbtMetric,
    measures_by_entity: dict[str, dict[str, DbtMeasure]],
    owner_of: dict[str, str],
    filter_sql: str | None = None,
) -> str | None:
    """Enforceable SQL for a simple metric (``AGG(entity.col)``); None for ratio/derived.
    A translated metric filter lands INSIDE the expression (CASE form) so the
    embedded SQL carries the metric's full meaning."""
    if metric.type != "simple" or metric.measure is None:
        return None
    ent_name = owner_of.get(metric.measure)
    if ent_name is None:
        return None
    m = measures_by_entity[ent_name][metric.measure]
    agg = _AGG_MAP.get(m.agg.strip().lower())
    if agg is None:
        return None
    expr = _qualify(ent_name, m.expr or m.name)
    if filter_sql:
        expr = f"CASE WHEN {filter_sql} THEN {expr} END"
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {expr})"
    return f"{agg.upper()}({expr})"


_DIMENSION_REF = re.compile(r"\{\{\s*Dimension\(\s*'(\w+)__(\w+)'\s*\)\s*\}\}")


def _entity_to_model(semantic_models: list[DbtSemanticModel]) -> dict[str, str]:
    """dbt primary-entity name -> semantic-model (= our entity) name."""
    return {e.name: sm.name for sm in semantic_models for e in sm.entities if e.type == "primary"}


def _translate_filter(template: str, entity_map: dict[str, str]) -> str | None:
    """{{ Dimension('order__status') }} = 'completed' -> orders.status = 'completed'.
    None when jinja remains after substitution (TimeDimension, macros, unknown
    entities) — the caller must skip honestly, not guess."""

    def _sub(m: re.Match[str]) -> str:
        model = entity_map.get(m.group(1))
        return f"{model}.{m.group(2)}" if model else m.group(0)

    out = _DIMENSION_REF.sub(_sub, template)
    if "{{" in out or "}}" in out:
        return None
    return out.strip()


def _fk_side_joins(semantic_models: list[DbtSemanticModel]) -> dict[str, list[SharedJoin]]:
    """A foreign entity in model A that is a primary entity in model B → a join OWNED
    by A (the FK side), many_to_one by construction."""
    primary_by_entity: dict[str, tuple[str, str]] = {}  # entity name -> (model, key col)
    for sm in semantic_models:
        for e in sm.entities:
            if e.type == "primary":
                primary_by_entity[e.name] = (sm.name, e.expr or e.name)
    joins: dict[str, list[SharedJoin]] = {}
    seen: set[tuple[str, str]] = set()
    for sm in semantic_models:
        for e in sm.entities:
            if e.type != "foreign" or e.name not in primary_by_entity:
                continue
            right_model, right_col = primary_by_entity[e.name]
            if right_model == sm.name or (sm.name, right_model) in seen:
                continue
            left_col = e.expr or e.name
            joins.setdefault(sm.name, []).append(
                SharedJoin(
                    right=right_model,
                    on=f"{sm.name}.{left_col} = {right_model}.{right_col}",
                    type="left",
                    relationship="many_to_one",
                )
            )
            seen.add((sm.name, right_model))
    return joins


def import_shared_assets(
    artifacts: DbtArtifacts,
    *,
    connection: str,
    relation_strategy: str = "alias",
) -> ImportResult:
    """Compile dbt artifacts into shared-layer assets (one-shot import).

    ``relation_strategy`` picks the physical table name: ``"alias"`` (the dbt model alias,
    matching most introspected warehouses) or ``"relation_name"`` (the fully-qualified
    relation, quotes stripped). The result includes a list of any constructs that
    could not be compiled.
    """
    skipped: list[SkippedConstruct] = []
    entities: list[Entity] = []
    measures_by_entity: dict[str, dict[str, DbtMeasure]] = {}

    for sm in artifacts.semantic_models:
        entity, measures = _compile_entity(sm, artifacts, connection, relation_strategy, skipped)
        entities.append(entity)
        measures_by_entity[entity.name] = measures

    owner_of = {
        measure_name: ent
        for ent, measures in measures_by_entity.items()
        for measure_name in measures
    }
    entity_map = _entity_to_model(artifacts.semantic_models)
    _apply_extra_metrics(entities, artifacts, measures_by_entity, owner_of, entity_map)
    compound_owner = _apply_compound_metrics(entities, artifacts, entity_map, skipped)
    definitions, _used_metric_names = _compile_definitions(
        artifacts, measures_by_entity, owner_of, skipped, entity_map, entities, compound_owner
    )

    joins_by_owner = _fk_side_joins(artifacts.semantic_models)
    grain_by_model = {
        sm.name: next((e.name for e in sm.entities if e.type == "primary"), None)
        for sm in artifacts.semantic_models
    }
    shared = [
        SharedEntity(
            **entity.model_dump(exclude={"grain"}),
            grain=(
                f"one row per {grain_by_model.get(entity.name)}"
                if grain_by_model.get(entity.name)
                else None
            ),
            joins=joins_by_owner.get(entity.name, []),
        )
        for entity in entities
    ]
    return ImportResult(entities=shared, definitions=definitions, skipped=skipped)


def _apply_extra_metrics(
    entities: list[Entity],
    artifacts: DbtArtifacts,
    measures_by_entity: dict[str, dict[str, DbtMeasure]],
    owner_of: dict[str, str],
    entity_map: dict[str, str],
) -> None:
    """Append business-named simple metrics (e.g. 'revenue') alongside the raw measures,
    so callers can select either name. A metric filter becomes Metric.filters;
    untranslatable filters mean NO queryable metric (already reported by
    _compile_definitions) — an unfiltered stand-in would be the wrong number."""
    by_name = {e.name: e for e in entities}
    existing: dict[str, set[str]] = {e.name: {m.name for m in e.metrics} for e in entities}
    for metric in artifacts.metrics:
        if metric.type != "simple" or metric.measure is None:
            continue
        ent_name = owner_of.get(metric.measure)
        if ent_name is None or metric.name == metric.measure:
            continue
        if metric.name in existing[ent_name]:
            continue
        filters: list[str] = []
        if metric.filter:
            translated = _translate_filter(metric.filter, entity_map)
            if translated is None:
                continue
            filters = [translated]
        src = measures_by_entity[ent_name][metric.measure]
        biz = _measure_metric(ent_name, src, by_name[ent_name].default_time_field)
        if biz is not None:
            by_name[ent_name].metrics.append(
                Metric(
                    name=metric.name,
                    agg=biz.agg,
                    expr=biz.expr,
                    filters=filters,
                    agg_time_field=biz.agg_time_field,
                )
            )
            existing[ent_name].add(metric.name)


def _apply_compound_metrics(
    entities: list[Entity],
    artifacts: DbtArtifacts,
    entity_map: dict[str, str],
    skipped: list[SkippedConstruct],
) -> dict[str, str]:
    """Ratio/derived dbt metrics whose inputs ALL resolve to imported metrics on ONE
    entity become compound Metric entries there — queryable via compile-time
    expansion. Cross-entity or unresolvable ones stay prose-only definitions,
    reported here (never a silently-wrong stand-in). Returns queryable compound
    metric name -> owning entity."""
    by_name = {e.name: e for e in entities}
    owner: dict[str, str] = {m.name: e.name for e in entities for m in e.metrics}
    placed: dict[str, str] = {}
    for metric in artifacts.metrics:
        if metric.type not in ("ratio", "derived"):
            continue
        inputs = (
            [n for n in (metric.numerator, metric.denominator) if n]
            if metric.type == "ratio"
            else list(dict.fromkeys(metric.inputs))
        )
        reason: str | None = None
        if metric.type == "ratio" and not (metric.numerator and metric.denominator):
            reason = "missing numerator/denominator"
        elif metric.type == "derived" and not (metric.expr and inputs):
            reason = "missing expr/input metrics"
        elif metric.type == "derived" and not metric.inputs_plain:
            reason = "inputs use alias/offset/filter"
        else:
            unknown = sorted(n for n in inputs if n not in owner)
            owners = {owner[n] for n in inputs if n in owner}
            if unknown:
                reason = f"input metric(s) {unknown} did not import as queryable"
            elif len(owners) > 1:
                reason = f"inputs span entities {sorted(owners)}"
            elif metric.name in owner:
                reason = f"name collides with an existing metric on '{owner[metric.name]}'"
        if reason is not None:
            skipped.append(
                SkippedConstruct(
                    "metric",
                    metric.name,
                    f"'{metric.type}' metric kept as definition only ({reason})",
                )
            )
            continue
        filters: list[str] = []
        if metric.filter:
            translated = _translate_filter(metric.filter, entity_map)
            if translated is None:
                continue  # _compile_definitions reports the untranslatable filter
            filters = [translated]
        ent_name = owner[inputs[0]]
        if metric.type == "ratio":
            compound = Metric(
                name=metric.name,
                type="ratio",
                numerator=metric.numerator,
                denominator=metric.denominator,
                filters=filters,
            )
        else:
            expr = metric.expr or ""
            for n in sorted(inputs, key=len, reverse=True):
                expr = re.sub(rf"\b{re.escape(n)}\b", "{" + n + "}", expr)
            compound = Metric(name=metric.name, type="derived", expr=expr, filters=filters)
        by_name[ent_name].metrics.append(compound)
        owner[metric.name] = ent_name
        placed[metric.name] = ent_name
    return placed
