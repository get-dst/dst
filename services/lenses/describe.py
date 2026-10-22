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

from pydantic import BaseModel

from services.contracts.semantic_model import (
    Entity,
    Metric,
    SemanticModel,
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


class ExposedTableInfo(BaseModel):
    """A table the lens reads WITHOUT definitions (an `expose:` grant): its
    columns as profiled, and nothing else — no metric, no meaning."""

    table: str
    columns: list[LensFieldInfo]
    description: str | None = None


EXPOSED_NOTE = (
    "exposed_tables are read without governed definitions: answers from these tables "
    "are ungoverned: they carry an EXPOSED line and never a declared or certified tag "
    "— the sql tool reads them directly"
)


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
) -> LensDescription:
    """Project a compiled semantic model into the caller-facing description.
    ``unsampled``: entity name -> why its table was never sampled
    (``value_guard.entity_unsampled``)."""
    why = unsampled or {}
    metrics, dimensions = catalog_of(model, domains=domains, certified=certified)
    return LensDescription(
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
            ExposedTableInfo(
                table=e.source.table,
                description=e.description,
                columns=[
                    LensFieldInfo(name=f.name, type=f.type, description=f.description)
                    for f in e.fields
                ],
            )
            for e in model.entities
            if e.exposed
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
