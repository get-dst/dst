"""Compile a lens: shared-layer selection + lens-local extras → SemanticModel.

The runtime never references shared assets — it consumes the same embedded
SemanticModel as always; this function is the only place selection becomes a
model. Pure: inputs in, (model, warnings) out, CompileError on an invalid
selection. Conflict rule: a term defined both shared-and-selected AND
lens-locally is an ERROR, not a silent winner — the shared layer is the org's
governed truth, and silent overrides reinstate exactly the drift the shared
layer exists to kill.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import (
    Definition,
    Entity,
    Join,
    Metric,
    SampleQuery,
    SemanticModel,
    SharedProvenance,
)
from services.contracts.shared_semantic import SharedEntity, asset_content_hash
from services.semantic.resolve import resolve_model

# Connection type → SQL dialect. 1:1 today; the seam for gateway types later.
_DIALECTS = {"bigquery", "duckdb", "postgres", "mysql", "snowflake"}

# Connection type → the config key naming the catalog every table on that
# connection lives under. Only the two warehouses whose physical names carry a
# catalog ABOVE the schema: BigQuery's project, Snowflake's database. Postgres,
# MySQL and DuckDB address `schema.table`, which an author already writes in full.
_QUALIFIER_KEYS = {"bigquery": "project", "snowflake": "database"}

# The structural tail of a possible_mappings entry: "entity" or "entity.member",
# the same shape Definition.about uses. Anything else after " - " is prose.
_STRUCT_REF = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?$")


class CompileError(ValueError):
    """The selection can't compile — names the file/field to fix."""


def dialect_for(connection_type: str) -> str:
    if connection_type not in _DIALECTS:
        raise CompileError(
            f"connection type '{connection_type}' has no SQL dialect — a lens needs a "
            "warehouse connection first in its connections list"
        )
    return connection_type


def default_qualifier(connection_type: str, config: Mapping[str, Any]) -> str | None:
    """The catalog this connection's tables live under, from its own declaration.

    The BigQuery project (and the Snowflake database) is a property of the
    CONNECTION, not of the model: dst.yaml already pins it, the client is built
    with it, and repeating it in every entity's `source.table` is the one thing
    that stops the same `semantic/` tree from serving two environments. So the
    connection supplies it and `qualify_table` stamps it at compile time.

    None when the key is absent — a connection that leaves the project to the
    service-account JSON qualifies nothing, because dst does not know the value
    the connector will pick and a guessed catalog is worse than a short name."""
    key = _QUALIFIER_KEYS.get(connection_type)
    value = config.get(key) if key else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def qualify_table(table: str, qualifier: str | None) -> str:
    """`marts.orders` + `acme-prod` → `acme-prod.marts.orders`.

    Three guards, and each one is a case where filling the catalog in would be
    wrong rather than merely unnecessary: no qualifier to apply; a table already
    carrying its own catalog (explicit always wins — that is how one entity
    reaches ACROSS to another project); and a BARE table name, where the schema
    is what is missing, so prefixing a catalog would build `acme-prod.orders`,
    a name that resolves nowhere."""
    if not qualifier or table.count(".") != 1:
        return table
    return f"{qualifier}.{table}"


def compile_lens_model(
    *,
    config: LensConfig,
    shared_entities: dict[str, SharedEntity],
    shared_definitions: dict[str, Definition],
    local_definitions: list[Definition],
    use_when: list[str],
    sample_queries: list[SampleQuery],
    dialect: str,
    asset_hashes: dict[str, str] | None = None,
    default_qualifiers: Mapping[str, str] | None = None,
) -> tuple[SemanticModel, list[str]]:
    warnings: list[str] = []
    consumed: dict[str, str] = {}
    hashes = asset_hashes or {}

    # ── entities (+ metric subsets) ──────────────────────────────────────────
    picks = config.select.entities
    if any(p.name == "*" for p in picks):
        picks = [p for p in picks if p.name != "*"] + [
            type(p)(name=n)
            for p in picks
            if p.name == "*"
            for n in shared_entities
            if n not in {q.name for q in picks}
        ]
    entities: list[Entity] = []
    joins: list[Join] = []
    selected_names: set[str] = set()
    dropped_metrics: set[str] = set()
    dropped_shapes: dict[str, list[Metric]] = {}
    for pick in picks:
        shared = shared_entities.get(pick.name)
        if shared is None:
            raise CompileError(
                f"lens '{config.name}' selects unknown entity '{pick.name}' — "
                f"no semantic/entities/{pick.name}.yaml"
            )
        entity_dict = shared.model_dump(exclude={"joins"})
        if pick.metrics is not None:
            known = {m.name for m in shared.metrics}
            missing = set(pick.metrics) - known
            if missing:
                raise CompileError(
                    f"lens '{config.name}' selects unknown metric(s) "
                    f"{sorted(missing)} on entity '{pick.name}'"
                )
            entity_dict["metrics"] = [
                m.model_dump() for m in shared.metrics if m.name in pick.metrics
            ]
            dropped_metrics.update(m.name for m in shared.metrics if m.name not in pick.metrics)
            dropped_shapes[pick.name] = [
                m.model_copy(deep=True) for m in shared.metrics if m.name not in pick.metrics
            ]
        # The physical address is resolved HERE, once, so everything downstream
        # (generated SQL, the read allow-list, profile binding, drift) sees one
        # canonical fully-qualified name while the authored file stays portable.
        source = dict(entity_dict.get("source") or {})
        if source.get("table"):
            source["table"] = qualify_table(
                str(source["table"]), (default_qualifiers or {}).get(str(source.get("connection")))
            )
            entity_dict["source"] = source
        entities.append(Entity.model_validate(entity_dict))
        selected_names.add(pick.name)
        consumed[f"entity/{pick.name}"] = hashes.get(f"entity/{pick.name}", "")
    for pick_name in sorted(selected_names):
        for sj in shared_entities[pick_name].joins:
            if sj.right not in selected_names:
                warnings.append(
                    f"join {pick_name} -> {sj.right} dropped: '{sj.right}' is not selected"
                )
                continue
            joins.append(
                Join(
                    left=pick_name,
                    right=sj.right,
                    on=sj.on,
                    type=sj.type,
                    relationship=sj.relationship,
                )
            )

    # ── definitions: shared selection + local extras ─────────────────────────
    def_picks = list(config.select.definitions)
    if "*" in def_picks:
        def_picks = sorted(shared_definitions)
    definitions: list[Definition] = []
    for term in def_picks:
        shared_def = shared_definitions.get(term)
        if shared_def is None:
            raise CompileError(
                f"lens '{config.name}' selects unknown definition '{term}' — "
                f"no semantic/definitions page defines it"
            )
        definitions.append(shared_def.model_copy(update={"source": "shared"}))
        consumed[f"definition/{term}"] = hashes.get(f"definition/{term}", "")
    shared_terms = {d.term for d in definitions}
    for local in local_definitions:
        if local.term in shared_terms:
            raise CompileError(
                f"'{local.term}' is defined both in the shared layer (selected by lens "
                f"'{config.name}') and locally in lenses/{config.name}/definitions/ — "
                "rename the local term or edit the shared page instead"
            )
        definitions.append(local)
    definitions = _tailor_ambiguous(definitions, entities, warnings)

    # Selection is a visible boundary: record the metric names the subsets
    # DROPPED (minus any a selected entity still defines) so the runtime can
    # refuse them deterministically instead of watching the model reconstruct
    # an excluded computation from raw columns. The dropped
    # metrics' full SHAPES ride along (same kept-name rule) so the shape guard
    # can refuse the reconstruction itself, not just the name.
    kept_metrics = {m.name for e in entities for m in e.metrics}
    excluded_names = sorted(dropped_metrics - kept_metrics)
    excluded_shapes = {
        entity_name: kept
        for entity_name, ms in sorted(dropped_shapes.items())
        if (kept := [m for m in ms if m.name in set(excluded_names)])
    }
    model = SemanticModel(
        lens=config.name,
        dialect=dialect,  # type: ignore[arg-type]
        timezone=config.timezone,
        stale_after_days=config.stale_after_days,
        entities=entities,
        joins=joins,
        definitions=definitions,
        sample_queries=sample_queries,
        use_when=use_when,
        ai_instructions=config.instructions,
        excluded_metrics=excluded_names,
        excluded_metric_shapes=excluded_shapes,
        shared_provenance=SharedProvenance(
            compiled_at=datetime.now(UTC).isoformat(), assets=consumed
        ),
    )
    # References resolve or the compile fails. Table-qualified refs are
    # rewritten to entity form on the compiled model only (authored files stay).
    model, resolve_errors, resolve_warnings = resolve_model(model)
    if resolve_errors:
        raise CompileError("\n".join(resolve_errors))
    warnings.extend(resolve_warnings)
    return model, warnings


def _mapping_ref(mapping: str) -> str | None:
    """The structural reference of a possible_mappings entry ("meaning - entity.member"):
    the "entity" or "entity.member" after the final " - ". None when the tail is prose
    ("gut feel - ask the CFO") or there is no separator — prose-only meanings stay legal."""
    _, sep, ref = mapping.rpartition(" - ")
    ref = ref.strip()
    return ref if sep and _STRUCT_REF.match(ref) else None


def _tailor_ambiguous(
    definitions: list[Definition], entities: list[Entity], warnings: list[str]
) -> list[Definition]:
    """Per-lens ambiguity: a lens only offers the meanings it can actually serve.

    Mappings whose structural ref isn't in this lens's compiled model are dropped from
    the clarification options (warning per drop). Exactly one structural survivor and no
    prose meanings → the term isn't ambiguous in this lens: compiled active, about set to
    the survivor. Nothing survives → keep the original options — better to ask with stale
    options than to guess. The shared page is never touched; this is compilation only."""
    members = {
        e.name: {f.name for f in e.fields}
        | {dim.name for dim in e.dimensions}
        | {m.name for m in e.metrics}
        for e in entities
    }

    def in_model(ref: str) -> bool:
        entity, _, member = ref.partition(".")
        return entity in members and (not member or member in members[entity])

    out: list[Definition] = []
    for d in definitions:
        if d.status != "ambiguous" or not d.possible_mappings:
            out.append(d)
            continue
        kept: list[str] = []
        survivors: list[str] = []  # structural refs that resolved in this lens's model
        prose = False
        drops: list[str] = []
        for mapping in d.possible_mappings:
            ref = _mapping_ref(mapping)
            if ref is None:
                prose = True
                kept.append(mapping)
            elif in_model(ref):
                survivors.append(ref)
                kept.append(mapping)
            else:
                drops.append(
                    f"'{d.term}': mapping '{mapping}' not in this lens's model — "
                    "dropped from clarification options"
                )
        if not kept:
            warnings.append(
                f"'{d.term}': no possible_mappings fit this lens's model — "
                "keeping the original clarification options"
            )
            out.append(d)
            continue
        warnings.extend(drops)
        if len(survivors) == 1 and not prose:
            warnings.append(
                f"'{d.term}': only mapping '{kept[0]}' fits this lens's model — "
                f"auto-resolved to active (about {survivors[0]})"
            )
            out.append(
                d.model_copy(
                    update={"status": "active", "about": survivors[0], "possible_mappings": []}
                )
            )
            continue
        out.append(
            d if kept == d.possible_mappings else d.model_copy(update={"possible_mappings": kept})
        )
    return out


def shared_entity_hash(entity: SharedEntity) -> str:
    return asset_content_hash("entity", entity.model_dump(mode="json"))


def shared_definition_hash(definition: Definition) -> str:
    return asset_content_hash("definition", definition.model_dump(mode="json"))
