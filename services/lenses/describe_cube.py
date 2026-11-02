"""What a Cube-backed lens knows, in the shape ``describe_model`` serves.

The same ``LensDescription`` and ``MetricCatalog`` an entity-YAML lens
describes itself with, built from a ``LensModel`` (``/v1/meta`` plus the
overlay) instead of a ``SemanticModel``: one entity per cube behind the
lens's views (a companion cube a ``sum_distinct_on`` metric migrated to folds
back into the cube it came from), its dimensions as fields, its measures as
metrics, a metric's callable dimensions the ones a view serves beside it.
Pure, like ``describe_model``: no Cube call, no warehouse.

What the Cube model does not carry reads as absent, never guessed: Cube has
one ``time`` type (no date vs timestamp, so a metric's grains are Cube's
own), one ``number`` type, and the overlay holds a definition's summary, not
its page's prose.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, Field

from services.contracts.cube_meta import CubeDef, Dimension, Measure, Segment
from services.contracts.cube_query import CubeQuery
from services.contracts.lens_config import NotComputable
from services.contracts.lens_model import LensCube, LensMember, LensModel
from services.cube.lens_view import CubeLensView
from services.lenses.describe import (
    CatalogDimension,
    CatalogMetric,
    LensDefinitionInfo,
    LensDescription,
    LensEntityInfo,
    LensFieldInfo,
)

# The granularities Cube buckets a time dimension by.
CUBE_GRAINS = ["day", "week", "month", "quarter", "year"]
_RATIO = re.compile(r"^\{(\w+)\} / NULLIF\(\{(\w+)\}, 0\)$")
_REF = re.compile(r"\{(\w+)\}")


def _template(sql: str | None) -> str:
    """Cube's extended-meta SQL (`` `${CUBE}.x` ``) as the model wrote it."""
    text = (sql or "").strip()
    if text.startswith("`") and text.endswith("`"):
        text = text[1:-1]
    return text.replace("${", "{")


def entity_of(model: LensModel, cube: str) -> str:
    """The entity a cube describes: a companion cube (``meta.dst.sources``
    naming one other cube of the lens) folds into that cube."""
    c = model.cube(cube)
    sources = list(c.dst.sources) if c is not None and c.dst is not None else []
    if len(sources) == 1 and model.cube(sources[0]) is not None and sources[0] != cube:
        return sources[0]
    return cube


def _once(model: LensModel) -> list[LensMember]:
    """Each cube member once, in the order the views serve them."""
    seen: dict[str, LensMember] = {}
    for m in model.members:
        seen.setdefault(m.base, m)
    return list(seen.values())


def _label(member: LensMember) -> str | None:
    label = member.short_title
    return label if label and label != member.member else None


def _groupable(model: LensModel, member: LensMember) -> bool:
    dim = member.dimension
    if dim is not None and dim.internal:
        return False
    return not model.hidden(member)


def _metric(
    model: LensModel,
    member: LensMember,
    sql: Mapping[str, tuple[str | None, list[str]]],
    callable_dims: list[str],
    certified: Sequence[tuple[str, Sequence[str]]],
    companion: bool,
) -> CatalogMetric:
    dst = member.measure
    template, filters = sql.get(member.base, (None, []))
    kind, agg, depends = "simple", member.type, []
    if member.type == "number":
        text = _template(template)
        ratio = _RATIO.match(text)
        kind = "ratio" if ratio else "derived"
        agg = None
        depends = list(ratio.groups()) if ratio else list(dict.fromkeys(_REF.findall(text)))
    elif companion:
        agg = "sum_distinct_on"
    clock = model.clock_of(member)
    unit = (dst.currency.upper() if dst and dst.currency else None) or (
        "percent" if member.format == "percent" else None
    )
    entity = entity_of(model, member.cube)
    return CatalogMetric(
        name=member.member,
        label=_label(member),
        aliases=list(dst.aliases) if dst else [],
        entity=entity,
        description=member.description,
        type=kind,
        agg=agg,
        additivity=(dst.additivity if dst and dst.additivity else "flow"),
        window_reading=dst.window_reading if dst else None,
        unit=unit,
        currency=dst.currency if dst else None,
        format=member.format,
        time_field=clock.split(".", 1)[1] if clock else None,
        grains=list(CUBE_GRAINS) if clock else [],
        dimensions=callable_dims,
        depends_on=depends,
        filters_summary=" AND ".join(_template(f).replace("{CUBE}.", f"{entity}.") for f in filters)
        or None,
        certified_answer_ids=[cid for cid, terms in certified if member.member.lower() in terms],
    )


def catalog_of_lens_model(
    model: LensModel,
    *,
    sql: Mapping[str, tuple[str | None, list[str]]] | None = None,
    domains: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
    certified: Sequence[tuple[str, Sequence[str]]] = (),
) -> tuple[list[CatalogMetric], list[CatalogDimension]]:
    """The metric and dimension catalog of a Cube-backed lens. ``sql`` is the
    extended meta's ``(sql, filters)`` per cube measure (``measure_sql``);
    ``domains`` entity → column → complete values, as ``catalog_of`` takes."""
    sql = sql or {}
    members = _once(model)
    dims = [m for m in members if m.kind == "dimension" and _groupable(model, m)]
    dimensions: list[CatalogDimension] = []
    seen: set[tuple[str, str]] = set()
    for d in dims:
        entity = entity_of(model, d.cube)
        if (entity, d.member) in seen:
            continue  # a companion cube repeats its source's dimensions
        seen.add((entity, d.member))
        dimensions.append(
            CatalogDimension(
                name=d.member,
                entity=entity,
                aliases=list(d.dimension.aliases) if d.dimension else [],
                type=d.type or "string",
                description=d.description,
                values=(
                    list(vals)
                    if (vals := ((domains or {}).get(entity) or {}).get(d.member.lower()))
                    else None
                ),
            )
        )
    metrics: list[CatalogMetric] = []
    for m in (x for x in members if x.kind == "measure"):
        entity = entity_of(model, m.cube)
        callable_dims: list[str] = []
        for view in model.views:
            if m.name.split(".", 1)[0] != view.name and not model.view_member_for(
                m.base, view.name
            ):
                continue
            for other in model.members:
                if other.view != view.name or other.kind != "dimension":
                    continue
                if not _groupable(model, other):
                    continue
                owner = entity_of(model, other.cube)
                name = other.member if owner == entity else f"{owner}.{other.member}"
                if _reachable(model, m.cube, other.cube) and name not in callable_dims:
                    callable_dims.append(name)
        metrics.append(_metric(model, m, sql, callable_dims, certified, companion=entity != m.cube))
    return metrics, dimensions


def _reachable(model: LensModel, cube: str, other: str) -> bool:
    """A dimension of ``other`` is callable with a measure of ``cube`` in a
    view only along declared joins, one hop or several (Cube answers no other
    pairing)."""
    seen, todo = {cube}, [cube]
    while todo:
        c: LensCube | None = model.cube(todo.pop())
        for nxt in c.joins if c is not None else []:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return other in seen


def measure_sql(meta_payload: Mapping[str, object]) -> dict[str, tuple[str | None, list[str]]]:
    """``{cube member: (sql, filter sqls)}`` from an extended meta payload."""
    out: dict[str, tuple[str | None, list[str]]] = {}
    for cube in meta_payload.get("cubes") or []:  # type: ignore[attr-defined]
        if cube.get("type") == "view":
            continue
        for m in cube.get("measures") or []:
            out[m["name"]] = (
                m.get("sql"),
                [str(f.get("sql")) for f in m.get("filters") or [] if f.get("sql")],
            )
    return out


def describe_lens_model(
    model: LensModel,
    *,
    name: str,
    display_name: str,
    description: str,
    dialect: str,
    sql: Mapping[str, tuple[str | None, list[str]]] | None = None,
    sample_questions: Sequence[str] = (),
    instructions: str | None = None,
    certified_count: int = 0,
    certified_examples: Sequence[str] = (),
    domains: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
    certified: Sequence[tuple[str, Sequence[str]]] = (),
) -> LensDescription:
    metrics, dimensions = catalog_of_lens_model(
        model, sql=sql, domains=domains, certified=certified
    )
    fields: dict[str, list[LensFieldInfo]] = {}
    for m in _once(model):
        if m.kind != "dimension" or model.hidden(m):
            continue
        entity = entity_of(model, m.cube)
        if entity != m.cube:
            continue  # the companion's dimensions are its source's
        fields.setdefault(entity, []).append(
            LensFieldInfo(name=m.member, type=m.type or "string", description=m.description)
        )
    entities = [
        LensEntityInfo(
            name=c.name,
            description=c.description or None,
            fields=fields.get(c.name, []),
        )
        for c in model.cubes
        if entity_of(model, c.name) == c.name
    ]
    segments = [s for s in _once(model) if s.kind == "segment"]
    definitions = [
        LensDefinitionInfo(term=s.segment.term or s.member, body=s.segment.meaning or "")
        for s in segments
        if s.segment is not None
    ]
    definitions += [
        LensDefinitionInfo(
            term=d.metric,
            body=d.summary or "",
            status=d.status or "active",
            possible_mappings=list(d.possible_mappings),
        )
        for d in [*model.definitions, *model.ambiguous]
    ]
    return LensDescription(
        name=name,
        display_name=display_name,
        description=description,
        dialect=dialect,
        entities=entities,
        definitions=definitions,
        sample_questions=list(sample_questions),
        instructions=instructions,
        certified_count=certified_count,
        certified_examples=list(certified_examples),
        metrics=metrics,
        dimensions=dimensions,
    )


# ── the driver's description: what an agent writing Cube queries reads ──────

# Observed values listed per dimension; a longer dictionary says how many.
DRIVER_VALUES_SHOWN = 40
# Certified question → Cube query pairs listed.
DRIVER_CERTIFIED_SHOWN = 20
# The bound on one description, in JSON characters: under what an agent's
# client shows inline (larger results are saved to a file and previewed).
DRIVER_DESCRIBE_MAX_CHARS = 40_000


class CubeMemberInfo(BaseModel):
    """One member a query may name, with what dst knows about it."""

    name: str = Field(description="what a Cube query names: `<view>.<member>`")
    kind: Literal["measure", "dimension", "segment"]
    type: str | None = None
    title: str | None = None
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    additivity: str | None = Field(
        default=None, description="flow (sums over time) or level (a stock, read at one moment)"
    )
    snapshot_grain: str | None = None
    clock: str | None = Field(default=None, description="the time dimension a window binds to")
    clock_kind: Literal["event", "schedule"] | None = None
    label_of: str | None = Field(default=None, description="the key this label belongs to")
    not_for: list[str] = Field(default_factory=list)
    values: list[str] | None = Field(
        default=None, description="the observed values (the probe's dictionary), bounded"
    )
    values_total: int | None = None
    value_aliases: dict[str, list[str]] = Field(default_factory=dict)
    data_end: str | None = None


class CubeViewInfo(BaseModel):
    name: str
    title: str | None = None
    description: str | None = None
    detail: Literal["summary", "full"] = "summary"
    measures: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)
    members: list[CubeMemberInfo] = Field(default_factory=list)


class CubeReadingInfo(BaseModel):
    says: str
    members: list[str] = Field(default_factory=list)
    select_words: list[str] = Field(default_factory=list)
    default: bool = False


class CubeAmbiguousInfo(BaseModel):
    term: str
    summary: str | None = None
    readings: list[CubeReadingInfo] = Field(default_factory=list)


class CubeFreshnessInfo(BaseModel):
    clock_end: str | None = Field(default=None, description="the last loaded day: the lens clock")
    stale_after_days: int = 3
    time_dimensions: dict[str, dict[str, object]] = Field(default_factory=dict)
    partial_periods: dict[str, dict[str, object]] = Field(
        default_factory=dict, description="measure → the period loaded well below its history"
    )


class CubeCertifiedInfo(BaseModel):
    question: str
    query: dict[str, object]


class CubeLensDescription(BaseModel):
    """A Cube-backed lens as an agent writing its own Cube queries reads it."""

    name: str
    display_name: str = ""
    description: str = ""
    serving: Literal["cube"] = "cube"
    model_version: str = ""
    views: list[CubeViewInfo] = Field(default_factory=list)
    not_computable: list[dict[str, object]] = Field(default_factory=list)
    ambiguous: list[CubeAmbiguousInfo] = Field(default_factory=list)
    definitions: list[LensDefinitionInfo] = Field(default_factory=list)
    scope: dict[str, object] = Field(default_factory=dict)
    freshness: CubeFreshnessInfo = Field(default_factory=CubeFreshnessInfo)
    certified: list[CubeCertifiedInfo] = Field(default_factory=list)
    certified_count: int = 0
    elided: list[str] = Field(default_factory=list)
    note: str = ""


DRIVER_NOTE = (
    "Write a Cube query over these members and a word ledger, and send them with "
    "query_cube(lens, query, ledger, question): dst checks scope, hidden and undeclared "
    "members, not-computable and ambiguous terms, the ledger against the question's "
    "words, filters the question did not ask for, dictionary values, windows against "
    "the lens clock, then compiles through Cube, runs the SQL and checks freshness, "
    "partial periods, empty results and ties. Name every member exactly as listed: a "
    "joined cube's members carry the cube name as a prefix (`view.stores_region`), and "
    "`view.region` is a member the view does not have. Views are summarised (member "
    "names); describe again with `view` to list one in full: each member's clock, values, "
    "data end and aliases."
)


def _member_info(
    view: CubeLensView, name: str, item: Measure | Dimension | Segment
) -> CubeMemberInfo:
    base = view.member(name) or item
    kind: Literal["measure", "dimension", "segment"] = (
        "measure"
        if isinstance(base, Measure)
        else "segment"
        if isinstance(base, Segment)
        else "dimension"
    )
    dst = base.dst
    info = CubeMemberInfo(
        name=name,
        kind=kind,
        type=base.type if not isinstance(base, Segment) else None,
        title=item.title or base.title,
        description=item.description or base.description,
        aliases=list(getattr(dst, "aliases", None) or []),
    )
    if isinstance(base, Measure):
        info.additivity = (base.dst.additivity if base.dst else None) or "flow"
        info.snapshot_grain = view.level_grain(name)
        info.clock = view.clock(name)
        info.not_for = view.not_for(name)
        if info.clock:
            facts = view.time_facts(info.clock)
            info.data_end = facts.data_end if facts else None
    elif isinstance(base, Dimension):
        values = view.dictionary(name)
        if values is not None:
            info.values = [str(v) for v in values[:DRIVER_VALUES_SHOWN]]
            info.values_total = len(values)
        if base.dst is not None:
            info.value_aliases = dict(base.dst.value_aliases)
            info.label_of = view.label_key(name)
        if base.is_time:
            facts = view.time_facts(name)
            info.clock_kind = facts.kind if facts else None
            info.data_end = facts.data_end if facts else None
    return info


def _shown_views(view: CubeLensView) -> list[CubeDef]:
    names = view.views or [c.name for c in view.doc.views()] or [c.name for c in view.doc.cubes]
    return [c for n in names if (c := view.doc.cube(n)) is not None]


def describe_view(
    view: CubeLensView,
    *,
    name: str,
    display_name: str = "",
    description: str = "",
    groups: Sequence[str] = (),
    full_view: str | None = None,
    not_computable: Sequence[NotComputable] = (),
    certified: Sequence[tuple[str, CubeQuery]] = (),
    certified_count: int = 0,
    max_chars: int = DRIVER_DESCRIBE_MAX_CHARS,
) -> CubeLensDescription:
    """The lens as Cube shows it to this caller (``view``), with the overlay
    and the monitor's facts on the lens's own cubes: every view summarised,
    and in full the one named (else the only view, when it fits), members
    hidden from the caller left out entirely. Bounded to ``max_chars`` of
    JSON: what did not fit is named in ``elided``. KeyError when
    ``full_view`` names no view of the lens."""
    cubes = _shown_views(view)
    if full_view is not None and full_view not in {c.name for c in cubes}:
        raise KeyError(full_view)
    args = (view, name, display_name, description, groups, not_computable, certified)
    if full_view is None and len(cubes) == 1:
        out = _describe(*args, cubes, cubes[0].name, certified_count)
        if len(out.model_dump_json()) <= max_chars:
            return out
    out = _describe(*args, cubes, full_view, certified_count)
    return _bounded(out, max_chars)


def _describe(
    view: CubeLensView,
    name: str,
    display_name: str,
    description: str,
    groups: Sequence[str],
    not_computable: Sequence[NotComputable],
    certified: Sequence[tuple[str, CubeQuery]],
    cubes: list[CubeDef],
    full: str | None,
    certified_count: int,
) -> CubeLensDescription:
    rules = view.hidden_rules()
    allowed = set(groups)

    def shown(member: str) -> bool:
        keep = rules.get(view.base(member))
        return keep is None or bool(allowed & set(keep))

    views: list[CubeViewInfo] = []
    for c in cubes:
        info = CubeViewInfo(
            name=c.name,
            title=c.title,
            description=c.description,
            measures=[m.name for m in c.measures if shown(m.name)],
            dimensions=[d.name for d in c.dimensions if shown(d.name)],
            segments=[s.name for s in c.segments if shown(s.name)],
        )
        if c.name == full:
            items: list[Measure | Dimension | Segment] = [*c.measures, *c.dimensions, *c.segments]
            info.detail = "full"
            info.members = [_member_info(view, i.name, i) for i in items if shown(i.name)]
        views.append(info)
    lens_policy = view.overlay.lenses.get(name)
    nc = [e.model_dump(exclude_defaults=True) for e in not_computable]
    if lens_policy is not None:
        nc += [e.model_dump(exclude_defaults=True) for e in lens_policy.not_computable]
    probe = view.probe
    model = view.model
    # The probe covers the whole project: only the lens's own cubes are its.
    own = {c.name for c in model.cubes} if model is not None else None
    times: dict[str, dict[str, object]] = {}
    for cube, facts in probe.cubes.items():
        if own is not None and cube not in own:
            continue
        for dim, t in facts.time_dimensions.items():
            times[f"{cube}.{dim}"] = t.model_dump(exclude_defaults=True)
    partial = {
        m: f.partial.model_dump(exclude_defaults=True)
        for m, f in probe.measures.items()
        if f.partial is not None and (own is None or f.cube in own)
    }
    defs = [*(model.definitions if model else view.overlay.definitions)]
    out = CubeLensDescription(
        name=name,
        display_name=display_name,
        description=description,
        model_version=view.model_version,
        views=views,
        not_computable=nc,
        ambiguous=[
            CubeAmbiguousInfo(
                term=t.metric,
                summary=t.summary,
                readings=[
                    CubeReadingInfo(
                        says=r.says,
                        members=list(r.members),
                        select_words=list(r.select_words),
                        default=r.default,
                    )
                    for r in t.readings
                ],
            )
            for t in view.ambiguous_terms()
        ],
        definitions=[
            LensDefinitionInfo(term=d.metric, body=d.summary or "", status=d.status or "active")
            for d in defs
        ],
        scope={"axes": sorted(view.scope_axes()), "names": sorted(view.scope_names())},
        freshness=CubeFreshnessInfo(
            clock_end=probe.clock_end,
            stale_after_days=probe.stale_after_days,
            time_dimensions=times,
            partial_periods=partial,
        ),
        certified=[
            CubeCertifiedInfo(question=q, query=cq.wire())
            for q, cq in certified[:DRIVER_CERTIFIED_SHOWN]
        ],
        certified_count=certified_count,
        note=DRIVER_NOTE,
    )
    if len(certified) > DRIVER_CERTIFIED_SHOWN:
        out.elided.append(f"{len(certified) - DRIVER_CERTIFIED_SHOWN} certified pairs")
    return out


def _bounded(out: CubeLensDescription, max_chars: int) -> CubeLensDescription:
    """Over ``max_chars``: certified pairs go first, then the full view's
    value lists, then the summarised views' dimensions, then the full view's
    descriptions, then the longest member lists, each named in ``elided``."""
    while len(out.model_dump_json()) > max_chars and out.certified:
        out.elided.append(f"{len(out.certified)} certified pairs (over the size bound)")
        out.certified = []
    if len(out.model_dump_json()) > max_chars:
        for v in out.views:
            for m in v.members:
                if m.values:
                    m.values = None
        out.elided.append("value lists of the full view: read them with values(lens, member)")
    if len(out.model_dump_json()) > max_chars:
        for v in out.views:
            if v.detail == "summary":
                v.dimensions, v.segments = [], []
        out.elided.append("the summarised views' dimensions: describe with `view` to read them")
    if len(out.model_dump_json()) > max_chars:
        for v in out.views:
            for m in v.members:
                m.description = None
        out.elided.append("member descriptions of the full view")
    while len(out.model_dump_json()) > max_chars:
        v = max(out.views, key=lambda x: len(x.measures) + len(x.dimensions) + len(x.members))
        if not (v.measures or v.dimensions or v.members):
            break
        cut = max(len(v.members), len(v.measures), len(v.dimensions)) // 2
        if len(v.members) > cut:
            v.members = v.members[:cut]
        v.measures, v.dimensions = v.measures[:cut], v.dimensions[:cut]
        out.elided.append(f"members of {v.name} past the first {cut}: describe with `view`")
    return out
