"""The lens as a Cube-backed project holds it: ``/v1/meta`` plus the overlay.

A Cube-backed project keeps its model in Cube (``cube/model/**``, read only
through ``GET /v1/meta``) and everything dst needs that a member's
``meta.dst`` does not carry in one file, ``cube/overlay.yaml``: per lens the
views it serves and its scope and policy, per cube routing prose and the
default clock, prose definitions, ambiguous terms and the hide exceptions.

``LensModel`` is one lens's view of both: the members of the views the lens
serves, each resolved to the cube member behind it with its parsed
``meta.dst``, the overlay slice the lens reads, and two versions —
``model_version`` (the sha256 of the canonical meta of the lens's views, the
one ``MetaDoc.model_version`` computes) and ``overlay_version`` (the sha256 of
the overlay slice). Everything downstream of a Cube-backed lens reads this
object; nothing reads Cube's model files.

``LensModel.build`` never raises on a model that does not hold together: a view
the overlay names that the meta lacks lands in ``unknown_views``, and the plan
(``services.project.cube_plan``) turns every such fact into an error, warning
or hint. The object is plain data and JSON round-trips, so it can be stored
with the lens at apply and rebuilt by a test from a recorded meta.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.contracts.authoring import Authored, parse_authored
from services.contracts.cube_meta import (
    CubeDef,
    CubeDst,
    Dimension,
    DimensionDst,
    Measure,
    MeasureDst,
    MetaDoc,
    Segment,
    SegmentDst,
)
from services.contracts.lens_config import AccessConfig, ExposeGrant, NotComputable, NotFor

OVERLAY_PATH = "cube/overlay.yaml"

MemberKind = Literal["measure", "dimension", "segment"]


# ── the overlay file ──────────────────────────────────────────────────────


class OverlayCube(Authored):
    """What dst knows about one cube that no member carries."""

    model_config = ConfigDict(extra="forbid")

    default_time_dimension: str | None = Field(
        default=None,
        description="the cube's clock: the time dimension a window binds to when a "
        "measure names none (`meta.dst.clock`)",
    )
    use_cases: list[str] = Field(
        default_factory=list, description="when a question belongs on this cube (routing prose)"
    )
    common_questions: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(
        default_factory=list,
        description="what a reader of any figure from this cube must know: every answer "
        "that reads the cube carries them (`caveats`, a CAVEAT line, the receipt)",
    )
    member_caveats: dict[str, list[str]] = Field(
        default_factory=dict,
        description="member of this cube → what a reader of a figure using it must know "
        "(an allocated measure: `allocated: refunds spread over lines by share`); every "
        "answer that reads the member carries them like the cube's caveats",
    )
    pinned_dimensions: list[str] = Field(
        default_factory=list,
        description="dimensions every answer from this cube is grouped by (a currency column)",
    )
    clock_kind: dict[str, Literal["event", "schedule"]] = Field(
        default_factory=dict,
        description="time dimension → event (the day something happened: windows are clipped "
        "to the lens clock, its data end is freshness) or schedule (the day something is due "
        "or ends, a renewal date: read as asked, never clipped). Undeclared, a date is what its "
        "name or description says (decided, paid = event; due, planned = schedule), else a "
        "schedule when it has rows after the clock. Read by the cube lane's window and "
        "freshness checks and by the monitor",
    )
    feed_axes: list[str] = Field(
        default_factory=list,
        description="dimensions that say which system or book a row came from; the monitor "
        "reads each combination's data end on the cube clock as its own feed",
    )
    feed_measures: list[str] = Field(
        default_factory=list,
        description="the measures that say whether a period of this cube's feeds is fully "
        "loaded (a billed amount that lags the rows); empty = its row counts. A period they "
        "find partial is partial for every measure of the cube on the same clock",
    )
    loaded_at: str | None = Field(
        default=None,
        description="the time dimension stamping when each row was loaded (a sync or export "
        "time): a period the monitor finds short is a partial load only when its rows were "
        "last loaded before the period ended",
    )
    entity_scope: OverlayEntityScope | None = Field(
        default=None,
        description="this cube holds one company's figures: which, and per measure the "
        "group-level member that holds every company's (an unscoped ask is asked which)",
    )


class OverlayEntityScope(Authored):
    """A single-company cube and, per measure, its group-level counterpart."""

    model_config = ConfigDict(extra="forbid")

    entity: str
    group_level: dict[str, str] = Field(default_factory=dict)


class OverlayScope(Authored):
    """The words that scope a figure to one company, and the dimensions that
    carry companies: data, so a new company, region or city is a file edit."""

    model_config = ConfigDict(extra="forbid")

    names: dict[str, list[str]] = Field(
        default_factory=dict,
        description="company (or country) value → the words that name it in a question; "
        "`dictionary:<cube.dimension>` stands for every observed value of that dimension",
    )
    group_names: list[str] = Field(
        default_factory=lambda: ["group"],
        description="words that name the whole group: the answer must read every company",
    )
    axes: list[str] = Field(
        default_factory=list,
        description="dimension names whose values are a company or a country; empty = every "
        "dimension whose observed values include a `names` key",
    )
    word_axes: list[str] = Field(
        default_factory=list,
        description="dimension names whose values a question scopes by (companies, revenue "
        "streams): such a word must be bound by a filter, breakdown or member; empty = `axes`",
    )


class OverlayLens(Authored):
    """One lens: the views it serves, and its scope and policy over them."""

    model_config = ConfigDict(extra="forbid")

    views: list[str] = Field(default_factory=list, description="the Cube views this lens serves")
    owner: str = ""
    timezone: str = ""
    stale_after_days: int | None = None
    scope: Literal["own", "market"] | None = None
    sources: list[str] = Field(default_factory=list)
    not_for: list[NotFor] = Field(default_factory=list)
    not_computable: list[NotComputable] = Field(default_factory=list)
    access: AccessConfig = Field(default_factory=AccessConfig)
    expose: list[ExposeGrant] = Field(default_factory=list)
    eval_gate: Literal["off", "warn", "block", "block_generation"] | None = None
    auto_review: Literal["off", "unverified", "partial"] | None = None
    definitions: list[str] = Field(
        default_factory=list, description="the overlay definitions (by term) this lens reads"
    )


class OverlayReading(Authored):
    """One reading of an ambiguous term: the members that compute it, in the
    same order in every reading (a member's counterpart in another reading is
    the one at its position), and the words that select it."""

    model_config = ConfigDict(extra="forbid")

    says: str
    members: list[str] = Field(default_factory=list)
    select_words: list[str] = Field(default_factory=list)
    default: bool = Field(
        default=False, description="served (with the others disclosed) when they are within 1 %"
    )


class OverlayDefinition(Authored):
    """A definition the model cannot carry as a segment (prose), or an
    ambiguous term with its readings."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    about: str | None = Field(
        default=None, description="the cube (`orders`) or member (`orders.status`) it is about"
    )
    summary: str | None = None
    aliases: list[str] = Field(default_factory=list)
    status: str | None = None
    possible_mappings: list[str] = Field(default_factory=list)
    audiences: dict[str, str] = Field(default_factory=dict)
    file: str | None = Field(default=None, description="the page it was migrated from")
    readings: list[OverlayReading] = Field(
        default_factory=list,
        description="an ambiguous term's readings, each computed when the question does not "
        "name one",
    )
    default: str | None = Field(
        default=None,
        description="the reading (its `says`) served when the question names none, however far "
        "the others are; they are disclosed beside it, with their values when they compute. "
        "Without one the caller is asked when the readings differ by more than 1 %",
    )

    @model_validator(mode="after")
    def _default_names_a_reading(self) -> OverlayDefinition:
        if self.default is None:
            return self
        says = [r.says for r in self.readings]
        if self.default not in says:
            raise ValueError(
                f"ambiguous term '{self.metric}': default '{self.default}' names none of its "
                f"readings ({'; '.join(says) or 'none declared'})"
            )
        other = [r.says for r in self.readings if r.default and r.says != self.default]
        if other:
            raise ValueError(
                f"ambiguous term '{self.metric}': default '{self.default}' and the reading "
                f"'{other[0]}' marked `default: true` disagree — keep one"
            )
        return self


class OverlayHide(Authored):
    """A dimension withheld from every caller outside ``except_groups``."""

    model_config = ConfigDict(extra="forbid")

    cube: str
    dimension: str
    except_groups: list[str] = Field(default_factory=list)


class Overlay(Authored):
    """``cube/overlay.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    cubes: dict[str, OverlayCube] = Field(default_factory=dict)
    lenses: dict[str, OverlayLens] = Field(default_factory=dict)
    definitions: list[OverlayDefinition] = Field(default_factory=list)
    ambiguous: list[OverlayDefinition] = Field(default_factory=list)
    hide: list[OverlayHide] = Field(default_factory=list)
    scope: OverlayScope = Field(default_factory=OverlayScope)


def parse_overlay(text: str, path: str = OVERLAY_PATH) -> Overlay:
    """The overlay file, strictly: an unknown key is a ValueError naming the
    file, the key path and the nearest valid key."""
    from services.project.loader import parse_yaml

    data = parse_yaml(text, path) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: must be a mapping")
    return parse_authored(Overlay, data, path)


# ── the lens model ────────────────────────────────────────────────────────


class LensMember(BaseModel):
    """A member a lens may call: the view member, and the cube member behind it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="the view member, `<view>.<member>`: what a query names")
    view: str
    base: str = Field(description="the cube member it aliases, `<cube>.<member>`")
    kind: MemberKind
    type: str | None = Field(
        default=None,
        description="Cube's type: number/string/time/boolean for a dimension; for a "
        "measure its aggregation (sum, count, count_distinct, avg, min, max, number)",
    )
    title: str | None = None
    short_title: str | None = None
    description: str | None = None
    format: str | None = None
    primary_key: bool = False
    public: bool = True
    measure: MeasureDst | None = None
    dimension: DimensionDst | None = None
    segment: SegmentDst | None = None

    @property
    def cube(self) -> str:
        return self.base.split(".", 1)[0]

    @property
    def member(self) -> str:
        """The member's own name inside its cube (`revenue` of `orders.revenue`)."""
        return self.base.split(".", 1)[1]

    @property
    def dst(self) -> MeasureDst | DimensionDst | SegmentDst | None:
        return self.measure or self.dimension or self.segment


class LensView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str | None = None
    description: str | None = None
    dst: CubeDst | None = None
    members: list[str] = Field(default_factory=list)


class LensCube(BaseModel):
    """A cube behind a lens's views, with what the overlay says about it."""

    model_config = ConfigDict(extra="forbid")

    name: str
    title: str | None = None
    description: str | None = None
    dst: CubeDst | None = None
    overlay: OverlayCube = Field(default_factory=OverlayCube)
    joins: list[str] = Field(default_factory=list)


class LensModel(BaseModel):
    """One lens over a Cube model: members, the overlay slice, the versions."""

    model_config = ConfigDict(extra="forbid")

    lens: str
    model_version: str
    overlay_version: str
    views: list[LensView] = Field(default_factory=list)
    cubes: list[LensCube] = Field(default_factory=list)
    members: list[LensMember] = Field(default_factory=list)
    policy: OverlayLens = Field(default_factory=OverlayLens)
    definitions: list[OverlayDefinition] = Field(default_factory=list)
    ambiguous: list[OverlayDefinition] = Field(default_factory=list)
    hide: list[OverlayHide] = Field(default_factory=list)
    scope: OverlayScope = Field(default_factory=OverlayScope)
    unknown_views: list[str] = Field(
        default_factory=list, description="views the lens names that the meta does not hold"
    )

    # ── construction ─────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        meta: MetaDoc,
        overlay: Overlay,
        lens: str,
        *,
        views: list[str] | None = None,
    ) -> LensModel:
        """The lens's model from a parsed meta and the overlay. ``views`` (a
        lens.yaml's own list) wins over the overlay's ``lenses.<lens>.views``."""
        policy = overlay.lenses.get(lens) or OverlayLens()
        wanted = list(dict.fromkeys(views if views is not None else policy.views))
        found: list[LensView] = []
        members: list[LensMember] = []
        unknown: list[str] = []
        for name in wanted:
            view = meta.cube(name)
            if view is None:
                unknown.append(name)
                continue
            found.append(
                LensView(
                    name=view.name,
                    title=view.title,
                    description=view.description,
                    dst=view.dst,
                    members=view.member_names(),
                )
            )
            members += _members(meta, view)
        cube_names = list(dict.fromkeys(m.cube for m in members))
        cubes = [_cube(meta, overlay, c) for c in cube_names]
        known = set(cube_names)
        terms = set(policy.definitions)
        definitions = [
            d
            for d in overlay.definitions
            if d.metric in terms or (d.about and d.about.split(".", 1)[0] in known)
        ]
        ambiguous = [
            d
            for d in overlay.ambiguous
            if d.metric in terms or not d.about or d.about.split(".", 1)[0] in known
        ]
        hide = [h for h in overlay.hide if h.cube in known]
        slice_ = {
            "policy": policy.model_dump(mode="json"),
            "cubes": {c.name: c.overlay.model_dump(mode="json") for c in cubes},
            "definitions": [d.model_dump(mode="json") for d in definitions],
            "ambiguous": [d.model_dump(mode="json") for d in ambiguous],
            "hide": [h.model_dump(mode="json") for h in hide],
            "scope": overlay.scope.model_dump(mode="json"),
        }
        return cls(
            lens=lens,
            model_version=meta.model_version([v.name for v in found]),
            overlay_version=_sha256(slice_),
            views=found,
            cubes=cubes,
            members=members,
            policy=policy,
            definitions=definitions,
            ambiguous=ambiguous,
            hide=hide,
            scope=overlay.scope,
            unknown_views=unknown,
        )

    # ── lookups ──────────────────────────────────────────────────────────

    def member(self, name: str) -> LensMember | None:
        """A member by its view name, or by its cube name when exactly one
        view member aliases it."""
        for m in self.members:
            if m.name == name:
                return m
        by_base = [m for m in self.members if m.base == name]
        return by_base[0] if len(by_base) == 1 else None

    def measures(self) -> list[LensMember]:
        return [m for m in self.members if m.kind == "measure"]

    def dimensions(self) -> list[LensMember]:
        return [m for m in self.members if m.kind == "dimension"]

    def segments(self) -> list[LensMember]:
        return [m for m in self.members if m.kind == "segment"]

    def cube(self, name: str) -> LensCube | None:
        return next((c for c in self.cubes if c.name == name), None)

    def view(self, name: str) -> LensView | None:
        return next((v for v in self.views if v.name == name), None)

    def clock_of(self, measure: LensMember) -> str | None:
        """The cube time dimension (``<cube>.<dim>``) a window on this measure
        binds to: its own ``meta.dst.clock``, else the overlay's
        ``default_time_dimension`` of its cube, else None (never guessed)."""
        declared = measure.measure.clock if measure.measure else None
        cube = self.cube(measure.cube)
        name = declared or (cube.overlay.default_time_dimension if cube else None)
        if not name:
            return None
        return name if "." in name else f"{measure.cube}.{name}"

    def view_member_for(self, base: str, view: str) -> str | None:
        """The member of ``view`` that aliases cube member ``base``."""
        return next((m.name for m in self.members if m.view == view and m.base == base), None)

    def hidden(self, member: LensMember, groups: list[str] | tuple[str, ...] = ()) -> bool:
        """Withheld from a caller in ``groups``: ``meta.dst.hide`` on the
        member or an overlay hide entry on its cube dimension, unless the caller
        is in one of the exception groups."""
        rules: list[list[str]] = []
        if member.dimension and member.dimension.hide is not None:
            rules.append(member.dimension.hide.except_groups)
        rules += [
            h.except_groups
            for h in self.hide
            if h.cube == member.cube and h.dimension == member.member
        ]
        return any(not set(groups) & set(rule) for rule in rules)


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _members(meta: MetaDoc, view: CubeDef) -> list[LensMember]:
    out: list[LensMember] = []
    groups: list[tuple[MemberKind, list[Measure] | list[Dimension] | list[Segment]]] = [
        ("measure", view.measures),
        ("dimension", view.dimensions),
        ("segment", view.segments),
    ]
    for kind, items in groups:
        for item in items:
            base_name = meta.resolve(item.name)
            if base_name == item.name and kind == "segment":
                base_name = _segment_base(meta, view.name, item.name)
            base = meta.member(base_name) if base_name != item.name else item
            source = item if item.meta.get("dst") is not None else (base or item)
            out.append(
                LensMember(
                    name=item.name,
                    view=view.name,
                    base=base_name,
                    kind=kind,
                    type=_member_type(item),
                    title=item.title,
                    short_title=item.shortTitle,
                    description=item.description or (base.description if base else None),
                    format=getattr(item, "format", None),
                    primary_key=bool(getattr(base or item, "primaryKey", False)),
                    public=item.public is not False,
                    measure=getattr(source, "dst", None) if kind == "measure" else None,
                    dimension=getattr(source, "dst", None) if kind == "dimension" else None,
                    segment=getattr(source, "dst", None) if kind == "segment" else None,
                )
            )
    return out


def _segment_base(meta: MetaDoc, view: str, name: str) -> str:
    """A view's segment carries no ``aliasMember`` in Cube's meta: its cube
    segment is the one named ``<cube>_<segment>`` (a prefixed include) or
    ``<segment>``, when exactly one cube holds such a segment."""
    local = name.split(".", 1)[1]
    found = [
        s.name
        for c in meta.cubes
        if not c.is_view and c.name != view
        for s in c.segments
        if local in (f"{c.name}_{s.name.split('.', 1)[1]}", s.name.split(".", 1)[1])
    ]
    return found[0] if len(found) == 1 else name


def _member_type(item: Measure | Dimension | Segment) -> str | None:
    if isinstance(item, Measure):
        # A view's measure reports ``type: number`` for every aggregation; the
        # aggregation itself is ``aggType``.
        return item.aggType or item.type
    if isinstance(item, Dimension):
        return item.type
    return None


def _cube(meta: MetaDoc, overlay: Overlay, name: str) -> LensCube:
    cube = meta.cube(name)
    return LensCube(
        name=name,
        title=cube.title if cube else None,
        description=cube.description if cube else None,
        dst=cube.dst if cube else None,
        overlay=overlay.cubes.get(name) or OverlayCube(),
        joins=[j.name for j in cube.joins] if cube else [],
    )


# ── meta.dst keys and who reads them ──────────────────────────────────────

# Every key of the closed ``meta.dst`` schema and the part of dst that reads
# it today. A key with no reader yet says which step reads it, so the plan's
# "keys read" table never claims a guarantee nothing enforces.
KEY_READERS: dict[str, dict[str, str]] = {
    "measure": {
        "additivity": "plan (level needs a clock), monitor (levels are not judged for "
        "partial periods), catalog",
        "default_segment": "plan (the segment must exist)",
        "window_reading": "catalog",
        "snapshot_grain": "catalog",
        "rejected_reading": "not read yet (the Cube lane)",
        "aliases": "catalog",
        "not_for": "not read yet (the Cube lane)",
        "better": "not read yet (the Cube lane)",
        "currency": "catalog (unit)",
        "clock": "plan (must name a time dimension), monitor, catalog",
        "as_of": "verify (post: the answer says the balance is as of each feed's data end)",
    },
    "dimension": {
        "aliases": "catalog",
        "dictionary": "probe --cube (values), monitor (declared vs observed)",
        "internal": "catalog (never offered)",
        "hide": "plan (pairs, hide_unenforced_in_cube), catalog (withheld)",
        "clock": "monitor (the clock's meaning and zone are reported)",
        "value_aliases": "not read yet (the Cube lane)",
        "label_of": "verify (pre label_key: a ranking by a label groups by its key too)",
    },
    "cube": {
        "grain": "describe",
        "key": "monitor (rows vs distinct key)",
        "population": "describe",
        "population_segment": "plan (the segment must exist)",
        "system": "describe",
        "data_end": "not read yet (the monitor measures data ends itself)",
        "sources": "describe",
    },
    "segment": {
        "meaning": "describe",
        "aliases": "catalog",
        "term": "catalog",
    },
}
