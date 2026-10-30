"""What a lens knows, served with zero LLM calls and zero warehouse hits.

``GET /v1/lenses/{name}`` and the MCP ``describe_lens`` tool are thin wrappers
over ``describe_model``: entities, typed fields, business definitions, the lens
author's standing orders, the size of the certified library, and the metric
catalog (``catalog_of``; ``GET /v1/lenses/{name}/catalog`` serves it alone with
the ``metric_tree`` view). Pure — a
``SemanticModel`` in, a ``LensDescription`` out, no I/O — so any caller can
build one, including harnesses that hold a model but no HTTP stack.

``instructions`` is the field that used to be missing: the lens author's
standing orders already reach the *generator* on every ask, but never reached
the caller. An agent that can read the rules it will be judged by can phrase
its ask to match; keeping them secret bought nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel

from services.contracts.profile import TableProfile, prompt_values
from services.contracts.semantic_model import (
    WITHHELD_COLUMN,
    Entity,
    Metric,
    SemanticModel,
    TableBlueprint,
    column_hidden,
    column_internal,
)
from services.runtime.compiler import PLACEHOLDER, unsafe_join_reason


class LensFieldInfo(BaseModel):
    name: str
    type: str
    description: str | None = None


class LensEntityInfo(BaseModel):
    name: str
    description: str | None = None
    fields: list[LensFieldInfo]
    # Set when no value of this entity's columns can be bound from a
    # dictionary, and why (its table was never sampled: over the budget).
    values_note: str | None = None


class BlueprintCheckInfo(BaseModel):
    """One check on an exposed table's blueprint: pass, fail, or unverified
    with the reason. Read from the stored probe only — describe never hits the
    warehouse, so a key the probe cannot count says so."""

    check: str
    verdict: str
    detail: str


class ColumnFactsInfo(BaseModel):
    """What the probe measured on one column. ``distinct_exact`` False makes
    ``distinct`` a count seen in a sample (a lower bound)."""

    name: str
    distinct: int | None = None
    distinct_exact: bool = False
    null_rate: float | None = None
    values: list[str] | None = None
    min: str | None = None
    max: str | None = None


class TableFactsInfo(BaseModel):
    """The probe's cheap facts on one table. ``sampled_rows`` set means the
    column facts were read from that many rows, not all of them."""

    rows: int | None = None
    sampled_rows: int | None = None
    profiled_at: str | None = None
    stale: bool = False
    columns: list[ColumnFactsInfo] = []


class ExposedTableInfo(BaseModel):
    """A table the lens reads WITHOUT definitions (an `expose:` grant) — no
    metric, no meaning. A hidden column appears nowhere, not even by name.

    ``detail`` "summary" (describe's default) carries the line a caller needs
    to pick a table: grain, key, the sync column when rows repeat per sync,
    row and column counts, and ``flags``. "full" (one table asked by name)
    adds its columns as profiled, its blueprint (semantic/tables/) when one
    is declared, each blueprint check's verdict, and the probe's facts."""

    table: str
    detail: Literal["summary", "full"] = "full"
    grain: str | None = None
    key: list[str] | str | None = None  # "withheld" when it names a hidden column
    versions: dict[str, object] | None = None
    rows: int | None = None
    column_count: int = 0
    # no blueprint | key fails | key unverified | N caveat(s) | stale probe
    flags: list[str] = []
    columns: list[LensFieldInfo] = []
    description: str | None = None
    blueprint: dict[str, object] | None = None
    checks: list[BlueprintCheckInfo] = []
    facts: TableFactsInfo | None = None


EXPOSED_NOTE = (
    "exposed_tables are read without governed definitions: answers from these tables "
    "are ungoverned: they carry an EXPOSED line and never a declared or certified tag "
    "— the sql tool reads them directly. Each answer also carries a GRAIN line per "
    "table: rows against distinct key when the table's blueprint declares a key, else "
    "'could not verify' — check `blueprint` and `facts` before counting rows. Each "
    "table is a summary here; describe_lens with `table` returns one in full: its "
    "columns, blueprint, caveats, check verdicts and probe facts"
)

# The most a description may weigh, as JSON characters: under an MCP client's
# tool-result limit (Claude Code's is 25,000 tokens by default), so the
# caller reads the whole of it rather than a preview.
DESCRIBE_MAX_CHARS = 60_000


class LensDefinitionInfo(BaseModel):
    term: str
    body: str
    status: str = "active"  # "ambiguous" ⇒ asking about it triggers a clarification
    possible_mappings: list[str] = []


# The grains a metric can be bucketed to, by its time field's declared type.
_GRAINS = {
    "date": ["day", "week", "month", "quarter", "year"],
    "timestamp": ["hour", "day", "week", "month", "quarter", "year"],
}
# Field types that name a group, not a quantity: a number or a json column is
# something to aggregate or unpack, never a dimension an app offers.
_DIMENSION_TYPES = frozenset({"string", "integer", "boolean", "date", "timestamp"})


class CatalogMetric(BaseModel):
    """One metric as an app renders it: what it is called, how it aggregates,
    what it can be cut by, and what it is computed from."""

    name: str
    label: str | None = None
    aliases: list[str] = []
    entity: str
    description: str | None = None
    type: str
    agg: str | None = None
    # flow (rows are events, a window sums them) or level (rows are snapshots,
    # a window reads its latest one) — the compiler's reading, so an
    # undeclared metric says flow.
    additivity: str = "flow"
    # A level's declared reading of a stated period: snapshot or any_in_window.
    window_reading: str | None = None
    # The declared unit: an ISO currency code, or `percent`; None when the
    # author declared neither, and then no unit is implied.
    unit: str | None = None
    currency: str | None = None
    format: str | None = None
    time_field: str | None = None
    grains: list[str] = []
    # Every member the metrics door accepts as a dimension with this metric:
    # its own entity's by bare name, a joined entity's as `entity.member`
    # (reachable over declared relationships without fanning the rows out).
    dimensions: list[str] = []
    # The sibling metrics a ratio or derived metric is computed from.
    depends_on: list[str] = []
    filters_summary: str | None = None
    # Active certified answers whose stored terms name this metric.
    certified_answer_ids: list[str] = []


class CatalogDimension(BaseModel):
    name: str
    entity: str
    label: str | None = None
    # Further words that name the dimension (`aliases:`).
    aliases: list[str] = []
    type: str
    description: str | None = None
    # The complete value dictionary from the stored profile when the column
    # is enum-sized and the profile is fresh; None otherwise (never a sample).
    values: list[str] | None = None


class CatalogNode(BaseModel):
    """A metric in the tree view: its children are the metrics it depends on."""

    metric: str
    entity: str
    label: str | None = None
    children: list[CatalogNode] = []


class MetricCatalog(BaseModel):
    lens: str
    metrics: list[CatalogMetric]
    dimensions: list[CatalogDimension]
    # Roots are the metrics nothing else depends on; a metric two parents
    # share appears under both.
    tree: list[CatalogNode]


class LensDescription(BaseModel):
    name: str
    display_name: str
    description: str
    dialect: str
    entities: list[LensEntityInfo]
    definitions: list[LensDefinitionInfo]
    sample_questions: list[str]
    # The lens author's standing orders (SemanticModel.ai_instructions) — the
    # answering practice this lens enforces. Advertised so a caller can see the
    # rules before it pays for an ask.
    instructions: str | None = None
    # The certified library, advertised so agents know a deterministic path exists
    # before paying for generation.
    certified_count: int = 0
    certified_examples: list[str] = []
    # Tables the lens exposes without definitions, listed APART from entities:
    # an agent must never read one as a governed entity. ``note`` says what an
    # answer from them is, so the caller learns it before it pays for an ask.
    exposed_tables: list[ExposedTableInfo] = []
    note: str | None = None
    # Set when the size bound cut the description: what was left out, and how
    # to read it. ``elided_tables`` names exposed tables whose summary did not
    # fit (each can be described alone with `table`).
    elided: str | None = None
    elided_tables: list[str] = []
    # The metric catalog: every governed metric with its label, unit, time
    # grains, callable dimensions and dependencies, and every dimension with
    # its value dictionary — what an app needs to render a metric tree.
    metrics: list[CatalogMetric] = []
    dimensions: list[CatalogDimension] = []


def depends_on(metric: Metric) -> list[str]:
    """The sibling metrics a ratio or derived metric references, in order."""
    if metric.type == "ratio":
        refs = [metric.numerator or "", metric.denominator or ""]
    elif metric.type == "derived":
        refs = PLACEHOLDER.findall(metric.expr or "")
    else:
        refs = []
    return list(dict.fromkeys(r for r in refs if r))


def _unit(metric: Metric) -> str | None:
    if metric.currency:
        return metric.currency.strip().upper()
    return "percent" if metric.format == "percent" else None


def _members(entity: Entity) -> list[tuple[str, str, str | None, list[str]]]:
    """``(name, type, description, aliases)`` of every member groupable on
    *entity*: its declared dimensions, then its fields of a groupable type.
    An `internal` column is never offered."""
    out: dict[str, tuple[str, str, str | None, list[str]]] = {}
    for d in entity.dimensions:
        if not column_internal(d.name, entity):
            out[d.name] = (d.name, d.type, d.description, list(d.aliases))
    for f in entity.fields:
        if (
            f.name not in out
            and f.type in _DIMENSION_TYPES
            and not column_hidden(f.name, entity)
            and not column_internal(f.name, entity)
        ):
            out[f.name] = (f.name, f.type, f.description, [])
    return list(out.values())


def catalog_of(
    model: SemanticModel,
    *,
    domains: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
    certified: Sequence[tuple[str, Sequence[str]]] = (),
) -> tuple[list[CatalogMetric], list[CatalogDimension]]:
    """The metric and dimension catalog of a model. ``domains`` is entity ->
    column (lowercased) -> complete value dictionary; ``certified`` is
    ``(cert_id, stored terms)`` per active certified answer. Exposed tables
    carry no governed meaning and stay out of both lists."""
    governed = [e for e in model.entities if not e.exposed]
    dimensions = [
        CatalogDimension(
            name=name,
            entity=e.name,
            aliases=aliases,
            type=type_,
            description=desc,
            values=(
                list(vals)
                if (vals := ((domains or {}).get(e.name) or {}).get(name.lower()))
                else None
            ),
        )
        for e in governed
        for name, type_, desc, aliases in _members(e)
    ]
    metrics: list[CatalogMetric] = []
    for e in governed:
        reachable = [
            other
            for other in governed
            if other.name != e.name and unsafe_join_reason(model, e.name, other.name) is None
        ]
        callable_dims = [n for n, _t, _d, _a in _members(e)] + [
            f"{other.name}.{n}" for other in reachable for n, _t, _d, _a in _members(other)
        ]
        types = {f.name: f.type for f in e.fields}
        for m in e.metrics:
            time_field = m.agg_time_field or e.default_time_field
            metrics.append(
                CatalogMetric(
                    name=m.name,
                    label=m.label,
                    aliases=list(m.aliases),
                    entity=e.name,
                    description=m.description,
                    type=m.type,
                    agg=m.agg,
                    additivity=m.additivity or "flow",
                    window_reading=m.window_reading,
                    unit=_unit(m),
                    currency=m.currency,
                    format=m.format,
                    time_field=time_field,
                    grains=list(_GRAINS.get(types.get(time_field or "", ""), [])),
                    dimensions=callable_dims,
                    depends_on=depends_on(m),
                    filters_summary=" AND ".join(m.filters) or None,
                    certified_answer_ids=[
                        cid for cid, terms in certified if m.name.lower() in terms
                    ],
                )
            )
    return metrics, dimensions


def metric_tree(metrics: Sequence[CatalogMetric]) -> list[CatalogNode]:
    """The catalog as a forest: roots are the metrics no other metric depends
    on, and each node's children are what it depends on. A dependency cycle
    (which apply refuses) is cut where it closes rather than recursed."""
    by_key = {(m.entity, m.name): m for m in metrics}
    used = {(m.entity, d) for m in metrics for d in m.depends_on}

    def node(m: CatalogMetric, path: frozenset[tuple[str, str]]) -> CatalogNode:
        path = path | {(m.entity, m.name)}
        children = [
            node(by_key[(m.entity, d)], path)
            for d in m.depends_on
            if (m.entity, d) in by_key and (m.entity, d) not in path
        ]
        return CatalogNode(metric=m.name, entity=m.entity, label=m.label, children=children)

    return [node(m, frozenset()) for m in metrics if (m.entity, m.name) not in used]


def _exposed_info(
    entity: Entity,
    model: SemanticModel,
    profiles: Mapping[str, TableProfile],
    fresh: bool,
    full: bool = True,
) -> ExposedTableInfo:
    from services.project.blueprints import blueprint_verdicts
    from services.runtime.grain import profile_for

    visible = {f.name.lower() for f in entity.fields}
    profile = profile_for(profiles, entity.source.table)
    blueprint = model.blueprints.get(entity.source.table)
    shown: dict[str, object] | None = None
    checks: list[BlueprintCheckInfo] = []
    if blueprint is not None:
        # Withheld: every profiled column the lens does not read, and any
        # declared one it does not read either.
        hidden = (
            frozenset(
                {c.name.lower() for c in (profile.columns if profile else [])}
                | {c.lower() for _where, c in blueprint.own_columns()}
            )
            - visible
        )
        shown = _visible_blueprint(blueprint, hidden)
        if profile is not None:
            verdicts, _errors, _unverified = blueprint_verdicts(
                blueprint,
                list(profiles.values()),
                known_connection=True,
                fresh=lambda _p: fresh,
                live=None,
                hidden=hidden,
            )
            checks = [
                BlueprintCheckInfo(check=v.check, verdict=v.status, detail=v.detail)
                for v in verdicts
            ]
        else:
            checks = [
                BlueprintCheckInfo(
                    check="table",
                    verdict="unverified",
                    detail=f"could not verify {entity.source.table}: no stored profile",
                )
            ]
    facts = None
    if profile is not None:
        facts = TableFactsInfo(
            rows=profile.row_count,
            sampled_rows=profile.sampled_rows,
            profiled_at=profile.profiled_at.isoformat(),
            stale=not fresh,
            columns=[
                ColumnFactsInfo(
                    name=c.name,
                    distinct=c.distinct_count,
                    distinct_exact=c.distinct_is_exact,
                    null_rate=c.null_rate,
                    values=prompt_values(c),
                    min=c.min,
                    max=c.max,
                )
                for c in profile.columns
                if c.name.lower() in visible
            ],
        )
    flags: list[str] = []
    if blueprint is None:
        flags.append("no blueprint")
    else:
        flags += [f"key {c.verdict}s" for c in checks if c.check == "key" and c.verdict == "fail"]
        flags += [
            "key unverified" for c in checks if c.check == "key" and c.verdict == "unverified"
        ]
        if blueprint.caveats:
            flags.append(f"{len(blueprint.caveats)} caveat(s)")
    if profile is not None and not fresh:
        flags.append("stale probe")
    view = shown or {}
    key = view.get("key")
    versions = view.get("versions")
    info = ExposedTableInfo(
        table=entity.source.table,
        grain=entity.grain,
        key=key if isinstance(key, (list, str)) else None,
        versions=versions if isinstance(versions, dict) else None,
        rows=profile.row_count if profile is not None else None,
        column_count=len(entity.fields),
        flags=flags,
    )
    if not full:
        return info.model_copy(update={"detail": "summary"})
    return info.model_copy(
        update={
            "description": entity.description,
            "columns": [
                LensFieldInfo(name=f.name, type=f.type, description=f.description)
                for f in entity.fields
            ],
            "blueprint": shown,
            "checks": checks,
            "facts": facts,
        }
    )


def _visible_blueprint(blueprint: TableBlueprint, hidden: frozenset[str]) -> dict[str, object]:
    """The blueprint as a caller may read it: every mention of a hidden column
    dropped — a key on one reads as `withheld`, never by name."""
    data: dict[str, object] = blueprint.model_dump(mode="json", exclude_defaults=True)
    data = {"connection": blueprint.connection, "table": blueprint.table, **data}
    prose = [blueprint.grain or "", *blueprint.caveats]
    prose += [c.meaning or "" for c in blueprint.clocks.values()]
    if any(WITHHELD_COLUMN in text for text in prose):
        data["withheld"] = (
            f"this blueprint's text names a column the lens hides; it reads {WITHHELD_COLUMN} "
            "in its place"
        )
    if not hidden:
        return data
    if any(c.lower() in hidden for c in blueprint.key):
        data["key"] = "withheld"
    if blueprint.versions is not None:
        versions = blueprint.versions
        if versions.sync_column.lower() in hidden:
            data.pop("versions", None)
        else:
            data["versions"] = {
                "sync_column": versions.sync_column,
                "latest_only": [c for c in versions.latest_only if c.lower() not in hidden],
            }
    if blueprint.clocks:
        data["clocks"] = {
            name: clock.model_dump(mode="json", exclude_none=True)
            for name, clock in blueprint.clocks.items()
            if name.lower() not in hidden
        }
    if blueprint.joins:
        from services.project.blueprints import on_columns

        data["joins"] = [
            j.model_dump(mode="json")
            for j in blueprint.joins
            if not any(column.lower() in hidden for _q, column in on_columns(j.on))
        ]
    return data


def describe_model(
    name: str,
    *,
    display_name: str,
    description: str,
    model: SemanticModel,
    certified_count: int = 0,
    certified_examples: list[str] | None = None,
    domains: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
    certified: Sequence[tuple[str, Sequence[str]]] = (),
    unsampled: Mapping[str, str] | None = None,
    profiles: Mapping[str, TableProfile] | None = None,
    profiles_fresh: bool = True,
    exposed: Literal["summary", "full"] = "full",
    table: str | None = None,
    max_chars: int | None = None,
) -> LensDescription:
    """Project a compiled semantic model into the caller-facing description.
    ``unsampled``: entity name -> why its table was never sampled
    (``value_guard.entity_unsampled``). ``profiles``: table -> its stored
    profile, for an exposed table's facts and blueprint verdicts;
    ``profiles_fresh`` False past probe.max_age (counts then decide nothing).

    ``exposed`` picks each exposed table's detail; ``table`` narrows the
    exposed tables to that one, in full (KeyError when the lens exposes no
    such table). ``max_chars`` bounds the JSON: exposed summaries that do not
    fit are named in ``elided_tables`` instead, and ``elided`` says so."""
    from services.project.lineage import same_table

    why = unsampled or {}
    metrics, dimensions = catalog_of(model, domains=domains, certified=certified)
    shown = [e for e in model.entities if e.exposed]
    if table is not None:
        shown = [e for e in shown if same_table(e.source.table, table)]
        if not shown:
            raise KeyError(table)
    full = exposed == "full" or table is not None
    described = LensDescription(
        name=name,
        display_name=display_name,
        description=description,
        dialect=model.dialect,
        entities=[
            LensEntityInfo(
                name=e.name,
                description=e.description,
                fields=[
                    LensFieldInfo(name=f.name, type=f.type, description=f.description)
                    for f in e.fields
                ],
                values_note=(
                    f"values cannot bind: {why[e.name]}, so dst holds no values of its "
                    "columns to check or bind a named value against; supply one as a binding"
                    if e.name in why
                    else None
                ),
            )
            for e in model.entities
            if not e.exposed
        ],
        exposed_tables=[
            _exposed_info(e, model, profiles or {}, profiles_fresh, full) for e in shown
        ],
        note=EXPOSED_NOTE if any(e.exposed for e in model.entities) else None,
        definitions=[
            LensDefinitionInfo(
                term=d.term,
                body=d.body,
                status=d.status,
                possible_mappings=d.possible_mappings,
            )
            for d in model.definitions
        ],
        sample_questions=[s.question for s in model.sample_queries],
        instructions=model.ai_instructions,
        certified_count=certified_count,
        certified_examples=list(certified_examples or []),
        metrics=metrics,
        dimensions=dimensions,
    )
    return described if max_chars is None else bounded(described, max_chars)


def bounded(description: LensDescription, max_chars: int) -> LensDescription:
    """The description cut to *max_chars* of JSON, from the end of its exposed
    tables: each one cut is named in ``elided_tables``, and past what even the
    names fit, counted. ``elided`` says what was cut and how to read it. A
    description that fits comes back unchanged."""
    if len(description.model_dump_json()) <= max_chars:
        return description
    if any(t.detail == "full" for t in description.exposed_tables):
        return _bounded_full(description, max_chars)
    tables = list(description.exposed_tables)
    names: list[str] = []
    unnamed = 0
    out = description

    def said() -> str:
        cut = len(names) + unnamed
        return (
            f"bounded at {max_chars:,} characters: {cut} of "
            f"{len(tables) + cut} exposed table(s) are not summarised here"
            + (f", {unnamed} of them not even by name" if unnamed else "")
            + " — call describe_lens with `table` set to one of elided_tables for it in full"
        )

    while len(out.model_dump_json()) > max_chars:
        if tables:
            names.insert(0, tables.pop().table)
        elif names:
            names.pop()
            unnamed += 1
        else:
            break
        out = description.model_copy(
            update={"exposed_tables": tables, "elided_tables": names, "elided": said()}
        )
    return out


def _bounded_full(description: LensDescription, max_chars: int) -> LensDescription:
    """A table described in full that is over the bound: its probe facts lose
    their value lists first, then go whole; ``elided`` says which."""
    lean = [
        t.model_copy(
            update={
                "facts": t.facts.model_copy(
                    update={
                        "columns": [c.model_copy(update={"values": None}) for c in t.facts.columns]
                    }
                )
            }
        )
        if t.facts is not None
        else t
        for t in description.exposed_tables
    ]
    out = description.model_copy(
        update={
            "exposed_tables": lean,
            "elided": f"bounded at {max_chars:,} characters: the value lists in `facts` are "
            "left out — the sql tool reads a column's values",
        }
    )
    if len(out.model_dump_json()) <= max_chars:
        return out
    return description.model_copy(
        update={
            "exposed_tables": [t.model_copy(update={"facts": None}) for t in lean],
            "elided": f"bounded at {max_chars:,} characters: the probe's `facts` are left "
            "out — the sql tool reads the table itself",
        }
    )
