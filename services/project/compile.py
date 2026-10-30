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
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from services.contracts.lens_config import LensConfig
from services.contracts.profile import TableProfile
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    GroupColumn,
    HideRule,
    Join,
    Metric,
    SampleQuery,
    SemanticModel,
    SharedProvenance,
    TableBlueprint,
    column_hidden,
    hidden_names,
    passthrough_dimension,
    withheld_columns,
    withhold_hidden,
)
from services.contracts.shared_semantic import (
    SharedEntity,
    SharedRelationship,
    asset_content_hash,
)
from services.project.expose import resolve_expose
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
    shared_relationships: dict[str, SharedRelationship],
    local_definitions: list[Definition],
    use_when: list[str],
    sample_queries: list[SampleQuery],
    dialect: str,
    asset_hashes: dict[str, str] | None = None,
    default_qualifiers: Mapping[str, str] | None = None,
    profiles: Mapping[str, Sequence[TableProfile]] | None = None,
    dropped_tables: Mapping[str, Sequence[str]] | None = None,
    blueprints: Mapping[str, TableBlueprint] | None = None,
    groups: Collection[str] = (),
) -> tuple[SemanticModel, list[str]]:
    """``groups`` are the caller groups dst.yaml declares: a group `hide`
    entry naming any other group fails the compile.
    ``profiles`` (connection -> stored table profiles) is what the lens's
    `expose:` grants resolve against; a lens that exposes nothing needs none.
    ``dropped_tables`` names the profiled tables the push's own probe no longer
    lists, so a grant that matches only one of them fails by name.
    ``blueprints`` (semantic/tables/, by table name) are the table facts every
    entity on a blueprinted table inherits (``inherit_blueprints``)."""
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
    pre_hide: dict[str, set[str]] = {}  # table -> the columns it read before `hide`
    group_columns: list[GroupColumn] = []
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
        entity_dict = shared.model_dump()
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
        entity = Entity.model_validate(entity_dict)
        pre_hide[entity.source.table] = {f.name for f in entity.fields} | {
            d.name for d in entity.dimensions if passthrough_dimension(d)
        }
        hide_errors, withheld = _apply_hide(entity, config.name, groups)
        if hide_errors:
            raise CompileError("\n".join(hide_errors))
        group_columns += withheld
        entities.append(entity)
        selected_names.add(pick.name)
        consumed[f"entity/{pick.name}"] = hashes.get(f"entity/{pick.name}", "")
    # Exposed tables ride as entities of the same model — same allow-list, same
    # drift check — flagged so every runtime rail reads them as ungoverned.
    # Resolved from the profiles, never from a file, so the columns are the
    # warehouse's as of the last probe; they qualify like a declared source.
    if config.expose:
        exposed, expose_errors = resolve_expose(
            config, profiles or {}, declared=sorted(selected_names), dropped=dropped_tables
        )
        if expose_errors:
            raise CompileError("\n".join(expose_errors))
        for entity in exposed:
            table = qualify_table(
                entity.source.table, (default_qualifiers or {}).get(entity.source.connection)
            )
            entity.source = EntitySource(connection=entity.source.connection, table=table)
            entities.append(entity)
    attached = inherit_blueprints(
        entities,
        blueprints or {},
        consumed,
        hashes,
        hidden=_withheld_by_table(entities, profiles or {}, pre_hide),
    )
    # A relationship rides iff the lens selects BOTH endpoints; with exactly one
    # selected it is dropped with a warning (the author meant to join something
    # this lens cannot see). Neither endpoint selected = not this lens's fact.
    for rel_name in sorted(shared_relationships):
        rel = shared_relationships[rel_name]
        picked = [e for e in (rel.left, rel.right) if e in selected_names]
        if len(picked) == 1:
            absent = rel.right if picked[0] == rel.left else rel.left
            warnings.append(
                f"relationship {rel.left} <-> {rel.right} dropped: '{absent}' is not selected"
            )
            continue
        if not picked:
            continue
        joins.append(
            Join(
                left=rel.left,
                right=rel.right,
                on=rel.on,
                type=rel.type,
                relationship=rel.relationship,
            )
        )
        consumed[f"relationship/{rel_name}"] = hashes.get(f"relationship/{rel_name}", "")

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
    # Only governed entities can resolve an ambiguous term's meaning: a
    # mapping onto an exposed table's column would be a "declared" reading of
    # a table that declares nothing.
    definitions = _tailor_ambiguous(definitions, [e for e in entities if not e.exposed], warnings)
    warnings.extend(sensitive_column_warnings(entities, definitions))

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
        # The not-computable declarations ride the compiled model (a fact that
        # reaches one tier doesn't exist) — the runtime's refusal rail and
        # result-column backstop both read them from here.
        not_computable=[nc.model_dump(mode="json") for nc in config.not_computable],
        not_for=[nf.model_dump(mode="json") for nf in config.not_for],
        excluded_metric_shapes=excluded_shapes,
        shared_provenance=SharedProvenance(
            compiled_at=datetime.now(UTC).isoformat(), assets=consumed
        ),
        blueprints=attached,
        group_columns=group_columns,
        cube_views=list(config.views),
    )
    # References resolve or the compile fails. Table-qualified refs are
    # rewritten to entity form on the compiled model only (authored files stay).
    model, resolve_errors, resolve_warnings = resolve_model(model)
    if resolve_errors:
        raise CompileError("\n".join(resolve_errors))
    warnings.extend(resolve_warnings)
    return model, warnings


def _withheld_by_table(
    entities: Sequence[Entity],
    profiles: Mapping[str, Sequence[TableProfile]],
    pre_hide: Mapping[str, set[str]],
) -> dict[str, set[str]]:
    """Table -> the columns this lens withholds on it (``withheld_columns``),
    read from the table's profile and its fields before `hide`."""
    from services.project.lineage import same_table

    out: dict[str, set[str]] = {}
    for entity in entities:
        table = entity.source.table
        profiled = next(
            (p for p in profiles.get(entity.source.connection, ()) if same_table(p.table, table)),
            None,
        )
        columns = {c.name for c in profiled.columns} if profiled else set()
        if withheld := withheld_columns(entity, columns | pre_hide.get(table, set())):
            out[table] = withheld
    return out


def inherit_blueprints(
    entities: Sequence[Entity],
    blueprints: Mapping[str, TableBlueprint],
    consumed: dict[str, str],
    hashes: Mapping[str, str],
    hidden: Mapping[str, set[str]] | None = None,
) -> dict[str, TableBlueprint]:
    """Attach each entity's table blueprint, in place, and return them keyed by
    the entity's compiled ``source.table``.

    An entity declares only what its file says; the table's own facts come from
    the blueprint, once, for every entity and exposed table on it. ``grain`` is
    inherited when the entity states none, and ``key`` becomes the entity's
    ``primary_key`` when it names none and every key column is one the entity
    reads (a hidden column never comes back as a key). Every blueprint whose
    table name matches is recorded as consumed, the one on the entity's own
    connection is attached, so a blueprint edit marks the lens stale.

    ``hidden`` maps a table to the columns the lens withholds on it: the
    attached copy names none of them in its prose (``withhold_hidden``), so no
    door that reads the compiled model can serve the name."""
    from services.project.lineage import same_table

    attached: dict[str, TableBlueprint] = {}
    for entity in entities:
        table = entity.source.table
        for blueprint in blueprints.values():
            if not same_table(blueprint.table, table):
                continue
            key = f"blueprint/{blueprint.name}"
            consumed[key] = hashes.get(key, "") or shared_blueprint_hash(blueprint)
            if blueprint.connection != entity.source.connection:
                continue
            readable = {f.name.lower() for f in entity.fields}
            blueprint = withhold_hidden(blueprint, hidden_names(table, hidden or {}, readable))
            attached[table] = blueprint
            if not entity.grain and blueprint.grain:
                entity.grain = blueprint.grain
            if (
                not entity.primary_key
                and blueprint.key
                and all(c.lower() in readable for c in blueprint.key)
            ):
                entity.primary_key = list(blueprint.key)
    return attached


def _apply_hide(
    entity: Entity, lens: str, groups: Collection[str] = ()
) -> tuple[list[str], list[GroupColumn]]:
    """Withhold the entity's `hide` columns from its fields and passthrough
    dimensions, in place, and return ``(errors, group_columns)``.

    A column the lens reads is a field or a bare dimension (``allowed_columns``
    treats both as in scope), so a pattern matches either; a bare dimension of
    a hidden column is the column, and goes with it. Every error names the
    pattern and the fix: a pattern that withholds nothing is the same silent
    miss as an `expose:` hide that matches no column, a hidden column an
    expression, key or filter still reads would come back through it, and a
    group entry naming a group dst.yaml does not declare is a typo that would
    lock its own group out — all three fail the compile rather than hide other
    than the author believes. ``group_columns`` are the columns hidden only by
    group entries, kept for ``SemanticModel.for_groups``."""
    if not entity.hide:
        return [], []
    errors: list[str] = []
    scope = [f.name for f in entity.fields] + [
        d.name for d in entity.dimensions if passthrough_dimension(d)
    ]
    declared = set(groups)
    for entry in entity.hide:
        pattern = entry if isinstance(entry, str) else entry.column
        if not any(column_hidden(c, _one_pattern(entity, entry)) for c in scope):
            errors.append(
                f"lens '{lens}': entity '{entity.name}' hides '{pattern}' but no field "
                f"or dimension matches it — fix the pattern (the column name, bare or "
                f"as {entity.name}.column) or drop it"
            )
        if isinstance(entry, HideRule) and (
            unknown := [g for g in entry.except_groups if g not in declared]
        ):
            known = f"it declares {sorted(declared)}" if declared else "it declares none"
            errors.append(
                f"lens '{lens}': entity '{entity.name}' hides '{pattern}' except for "
                f"group(s) {unknown}, which dst.yaml does not declare ({known}) — add "
                "them under `groups:` in dst.yaml or fix the name"
            )
    hidden = entity.hidden_columns()
    readers: list[tuple[str, str | None]] = [
        *(
            (f"dimension {d.name}", d.expr)
            for d in entity.dimensions
            if not passthrough_dimension(d)
        ),
        *((f"metric {m.name}", m.expr) for m in entity.metrics),
        *((f"metric {m.name} filter", f) for m in entity.metrics for f in m.filters or []),
        ("primary_key", " ".join(entity.primary_key)),
        ("default_time_field", entity.default_time_field),
        ("population_filter", entity.population_filter),
        ("pinned_dimensions", " ".join(entity.pinned_dimensions)),
    ]
    for label, text in readers:
        if not text:
            continue
        names = set(re.findall(r"[A-Za-z_]\w*", text))
        for column in sorted(hidden & names):
            errors.append(
                f"lens '{lens}': entity '{entity.name}' hides '{column}' but its {label} "
                f"still reads it — a hidden column cannot ride an expression; drop the "
                f"{label.split(' ')[0]} or the hide"
            )
    plain = entity.model_copy(update={"hide": [h for h in entity.hide if isinstance(h, str)]})
    rules = [h for h in entity.hide if isinstance(h, HideRule)]
    withheld = [
        GroupColumn(
            entity=entity.name,
            column=column,
            groups=sorted(
                {
                    g
                    for r in rules
                    if column_hidden(column, _one_pattern(entity, r))
                    for g in r.except_groups
                }
            ),
            field=next((f for f in entity.fields if f.name == column), None),
            dimensions=[
                d for d in entity.dimensions if passthrough_dimension(d) and d.name == column
            ],
        )
        for column in sorted(hidden)
        if not column_hidden(column, plain)
    ]
    entity.withhold_hidden()
    return errors, withheld


def _one_pattern(entity: Entity, pattern: str | HideRule) -> Entity:
    """The entity as if `hide` held only this pattern — for naming the one
    that withholds nothing."""
    return entity.model_copy(update={"hide": [pattern]})


# Column names that are personal data on their face. A match is a declared
# schema fact (the column exists and the lens reads it), never a reading of
# prose: the warning asks for the hide or the definition that says what the
# column is for, and goes quiet once either exists.
_SENSITIVE = re.compile(
    r"(?<![a-z])(personal_id|ssn|social_security(?:_number)?|email|e_mail|phone|"
    r"mobile|birth_?date|date_of_birth|dob|address|street|postcode|zip_?code)(?![a-z])"
)
SENSITIVE_COLUMNS = "sensitive_columns"


def sensitive_column_warnings(
    entities: Sequence[Entity], definitions: Sequence[Definition]
) -> list[str]:
    """One `sensitive_columns` warning per entity whose in-scope columns (fields
    and passthrough dimensions) are named like personal data — personal_id,
    ssn, email, phone, birth_date, date_of_birth, address — with no `hide` on
    them and no definition about them (`about: entity.column`). The column
    reaching a door is the fact; the author decides whether a hide or a
    definition is the fix."""
    described = {d.about for d in definitions if d.about}
    out: list[str] = []
    for e in entities:
        served = {f.name for f in e.fields} | {
            d.name for d in e.dimensions if passthrough_dimension(d)
        }
        flagged = sorted(
            name
            for name in served
            if _SENSITIVE.search(name.lower())
            and not column_hidden(name, e)
            and f"{e.name}.{name}" not in described
        )
        if flagged:
            out.append(
                f"{SENSITIVE_COLUMNS}: entity '{e.name}' serves {', '.join(flagged)} on every "
                "door with no `hide:` and no definition about them — hide the column(s) "
                f"(`hide: [{flagged[0]}]` in the entity file) or declare a definition with "
                f"`about: {e.name}.{flagged[0]}` saying what they are for"
            )
    return out


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


def shared_relationship_hash(relationship: SharedRelationship) -> str:
    return asset_content_hash("relationship", relationship.model_dump(mode="json"))


def shared_blueprint_hash(blueprint: TableBlueprint) -> str:
    return asset_content_hash("blueprint", blueprint.model_dump(mode="json"))


def relationship_pairs(
    relationships: dict[str, SharedRelationship],
) -> dict[str, tuple[str, str]]:
    """Asset key → endpoints, the shape ``stale_asset_keys`` reads: the name
    alone cannot be split back into a pair (entity names may contain '__')."""
    return {f"relationship/{name}": (r.left, r.right) for name, r in relationships.items()}
