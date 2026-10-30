"""The Cube model as dst reads it: ``GET /v1/meta``, parsed.

dst never reads Cube's model files. What a lens may call, what each member
means and what the overlay declares all come from the meta document Cube
serves, so the model Cube compiles and the model dst reasons over are one
object. ``meta.dst`` on a cube, view, measure, dimension or segment is dst's
overlay, a CLOSED schema: a key the product does not read is an error, never
ignored, so nothing an author writes is silently inert.

``model_version()`` is the sha256 of the canonical meta restricted to the views
a lens uses: it changes when a member a caller can see changes, and nothing
else moves it. Certified pairs pin it.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services.contracts.cube_query import ViewMembers

Additivity = Literal["flow", "level"]


class HideRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    except_groups: list[str] = Field(default_factory=list)


class ClockMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meaning: str | None = None
    timezone: str | None = None


class MeasureDst(BaseModel):
    """``meta.dst`` on a measure."""

    model_config = ConfigDict(extra="forbid")

    additivity: Additivity | None = None
    default_segment: str | None = None
    window_reading: str | None = None
    snapshot_grain: str | None = None
    rejected_reading: str | None = None
    aliases: list[str] = Field(default_factory=list)
    not_for: list[str] = Field(default_factory=list)
    better: Literal["higher", "lower"] | None = None
    currency: str | None = None
    clock: str | None = None
    # A balance read at each feed's own data end, not today: the dimension of
    # the measure's cube that carries that as-of day per row.
    as_of: str | None = None


class DimensionDst(BaseModel):
    """``meta.dst`` on a dimension.

    ``dictionary`` is ``true`` (bind values from the probe's dictionary) or the
    list of values the author declares the column holds; the monitor reports a
    value the warehouse gains or loses against a declared list.
    ``value_aliases`` maps a value to the words people use for it; the list
    form ``[{alias, value}]`` is read into the same mapping."""

    model_config = ConfigDict(extra="forbid")

    aliases: list[str] = Field(default_factory=list)
    dictionary: bool | list[str] | None = None
    internal: bool | None = None
    hide: HideRule | None = None
    clock: ClockMeta | None = None
    value_aliases: dict[str, list[str]] = Field(default_factory=dict)
    # A name that labels a keyed row (customer_name labels customer_id): two
    # rows with one name are two things, so a ranking groups by the key too.
    label_of: str | None = None

    @field_validator("value_aliases", mode="before")
    @classmethod
    def _pairs(cls, v: object) -> object:
        if not isinstance(v, list):
            return v
        out: dict[str, list[str]] = {}
        for pair in v:
            if not isinstance(pair, dict) or set(pair) != {"alias", "value"}:
                raise ValueError("each value_aliases entry is {alias, value}")
            out.setdefault(str(pair["value"]), []).append(str(pair["alias"]))
        return out

    @property
    def declared_values(self) -> list[str] | None:
        """The values the author declares, or None when the probe owns them."""
        return list(self.dictionary) if isinstance(self.dictionary, list) else None


class CubeDst(BaseModel):
    """``meta.dst`` on a cube or a view."""

    model_config = ConfigDict(extra="forbid")

    grain: str | None = None
    key: list[str] = Field(default_factory=list)
    population: str | None = None
    population_segment: str | None = None
    system: str | None = None
    data_end: str | None = None
    sources: list[str] = Field(default_factory=list)


class SegmentDst(BaseModel):
    """``meta.dst`` on a segment."""

    model_config = ConfigDict(extra="forbid")

    meaning: str | None = None
    aliases: list[str] = Field(default_factory=list)
    term: str | None = None


class Measure(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    title: str | None = None
    shortTitle: str | None = None  # noqa: N815 — Cube's wire name
    public: bool | None = None
    description: str | None = None
    type: str | None = None
    aggType: str | None = None  # noqa: N815 — Cube's wire name
    format: str | None = None
    currency: str | None = None
    aliasMember: str | None = None  # noqa: N815
    cumulative: bool | None = None
    # ``?extended=true`` only: the member's SQL template and filters.
    sql: str | None = None
    filters: list[dict[str, Any]] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    dst: MeasureDst | None = None

    @property
    def cube(self) -> str:
        return self.name.split(".", 1)[0]


class Dimension(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    title: str | None = None
    shortTitle: str | None = None  # noqa: N815 — Cube's wire name
    public: bool | None = None
    description: str | None = None
    type: str | None = None
    primaryKey: bool | None = None  # noqa: N815
    granularities: list[dict[str, Any]] | None = None
    aliasMember: str | None = None  # noqa: N815
    sql: str | None = None  # ``?extended=true`` only
    meta: dict[str, Any] = Field(default_factory=dict)
    dst: DimensionDst | None = None

    @property
    def is_time(self) -> bool:
        return self.type == "time"


class Segment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    title: str | None = None
    shortTitle: str | None = None  # noqa: N815 — Cube's wire name
    public: bool | None = None
    description: str | None = None
    aliasMember: str | None = None  # noqa: N815
    meta: dict[str, Any] = Field(default_factory=dict)
    dst: SegmentDst | None = None


class Join(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    relationship: str | None = None


class CubeDef(BaseModel):
    """One ``cubes[]`` entry: a cube or a view."""

    model_config = ConfigDict(extra="ignore")

    name: str
    type: Literal["cube", "view"] = "cube"
    title: str | None = None
    description: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    dst: CubeDst | None = None
    measures: list[Measure] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    joins: list[Join] = Field(default_factory=list)

    @property
    def is_view(self) -> bool:
        return self.type == "view"

    def member_names(self) -> list[str]:
        return [
            *(m.name for m in self.measures),
            *(d.name for d in self.dimensions),
            *(s.name for s in self.segments),
        ]

    def time_dimensions(self) -> list[str]:
        return [d.name for d in self.dimensions if d.is_time]


class MetaDoc(BaseModel):
    """The parsed meta document plus every overlay error it carried."""

    model_config = ConfigDict(extra="ignore")

    cubes: list[CubeDef] = Field(default_factory=list)
    overlay_errors: list[str] = Field(
        default_factory=list,
        description="one line per `meta.dst` block the closed schema refused, "
        "naming the member and the key — plan prints them as errors",
    )

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> MetaDoc:
        errors: list[str] = []
        cubes: list[CubeDef] = []
        for raw in payload.get("cubes") or []:
            cube = CubeDef.model_validate(raw)
            cube.dst = _overlay(CubeDst, cube.meta, cube.name, errors)
            for m in cube.measures:
                m.dst = _overlay(MeasureDst, m.meta, m.name, errors)
            for d in cube.dimensions:
                d.dst = _overlay(DimensionDst, d.meta, d.name, errors)
            for s in cube.segments:
                s.dst = _overlay(SegmentDst, s.meta, s.name, errors)
            cubes.append(cube)
        return cls(cubes=cubes, overlay_errors=errors)

    # ── lookups ──────────────────────────────────────────────────────────

    def cube(self, name: str) -> CubeDef | None:
        return next((c for c in self.cubes if c.name == name), None)

    def views(self) -> list[CubeDef]:
        return [c for c in self.cubes if c.is_view]

    def member(self, name: str) -> Measure | Dimension | Segment | None:
        cube = self.cube(name.split(".", 1)[0]) if "." in name else None
        if cube is None:
            return None
        for group in (cube.measures, cube.dimensions, cube.segments):
            for m in group:
                if m.name == name:
                    return m
        return None

    def resolve(self, name: str) -> str:
        """A view member's base member: ``Orders_view.revenue`` →
        ``Orders.revenue`` by following ``aliasMember`` until a cube's own
        member, or the name itself when nothing aliases it."""
        seen: set[str] = set()
        current = name
        while current not in seen:
            seen.add(current)
            member = self.member(current)
            alias = getattr(member, "aliasMember", None) if member else None
            if not alias:
                return current
            current = alias
        return current

    def member_counts(self, views: Iterable[str] | None = None) -> dict[str, int]:
        chosen = self._restricted(views)
        return {
            "cubes": sum(1 for c in chosen if not c.is_view),
            "views": sum(1 for c in chosen if c.is_view),
            "measures": sum(len(c.measures) for c in chosen),
            "dimensions": sum(len(c.dimensions) for c in chosen),
            "segments": sum(len(c.segments) for c in chosen),
        }

    # ── identity ─────────────────────────────────────────────────────────

    def canonical(self, views: Iterable[str] | None = None) -> dict[str, Any]:
        """The meta restricted to ``views`` (every cube and view when None),
        titles and descriptions dropped, members sorted: what the model IS,
        not how it is labelled."""
        out = []
        for c in sorted(self._restricted(views), key=lambda c: c.name):
            out.append(
                {
                    "name": c.name,
                    "type": c.type,
                    "meta": c.meta,
                    "measures": [_member_identity(m) for m in sorted(c.measures, key=_name)],
                    "dimensions": [_member_identity(d) for d in sorted(c.dimensions, key=_name)],
                    "segments": [_member_identity(s) for s in sorted(c.segments, key=_name)],
                    "joins": [
                        {"name": j.name, "relationship": j.relationship}
                        for j in sorted(c.joins, key=lambda j: j.name)
                    ],
                }
            )
        return {"cubes": out}

    def model_version(self, views: Iterable[str] | None = None) -> str:
        return hashlib.sha256(
            json.dumps(self.canonical(views), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _restricted(self, views: Iterable[str] | None) -> list[CubeDef]:
        if views is None:
            return list(self.cubes)
        wanted = set(views)
        return [c for c in self.cubes if c.name in wanted]


def _name(member: Measure | Dimension | Segment) -> str:
    return member.name


def _member_identity(member: Measure | Dimension | Segment) -> dict[str, Any]:
    # The SQL text is not identity: it rides only on ``?extended=true``, and the
    # compiled SQL a query produces is pinned on its own (sql_sha256).
    out = member.model_dump(
        exclude={"title", "shortTitle", "description", "dst", "sql", "filters"},
        exclude_none=True,
    )
    if not out.get("meta"):
        out.pop("meta", None)
    return out


_UNION_BRANCH = re.compile(r"^(bool|str|int|float|none|list\[.*\]|dict\[.*\])$")


def _overlay[T: BaseModel](
    kind: type[T], meta: dict[str, Any], owner: str, errors: list[str]
) -> T | None:
    raw = meta.get("dst")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append(f"{owner}: meta.dst must be a mapping")
        return None
    try:
        return kind.model_validate(raw)
    except ValidationError as exc:
        seen: set[str] = set()
        for err in exc.errors():
            # A union field reports once per branch (``dictionary.bool``,
            # ``dictionary.list[str]``): the field is the error, not the branch.
            parts = [str(p) for p in err["loc"]]
            while len(parts) > 1 and _UNION_BRANCH.match(parts[-1]):
                parts.pop()
            key = ".".join(parts) or "(root)"
            if key in seen:
                continue
            seen.add(key)
            what = "unknown key" if err["type"] == "extra_forbidden" else err["msg"]
            errors.append(
                f"{owner}: meta.dst {what} '{key}'"
                if what == "unknown key"
                else f"{owner}: meta.dst.{key}: {what}"
            )
        return None


def view_members(doc: MetaDoc, view: str) -> ViewMembers:
    """What ``from_intent`` needs about one view: its callable members and its
    clock (the dimension a measure's ``meta.dst.clock`` names, else the first
    time dimension). Raises ``KeyError`` when the view is not in the meta."""
    cube = doc.cube(view)
    if cube is None:
        raise KeyError(view)
    clocks = [m.dst.clock for m in cube.measures if m.dst and m.dst.clock]
    clock = next((c if "." in c else f"{view}.{c}" for c in clocks), None)
    return ViewMembers(
        view=view,
        measures=[m.name for m in cube.measures],
        dimensions=[d.name for d in cube.dimensions],
        segments=[s.name for s in cube.segments],
        time_dimensions=cube.time_dimensions(),
        clock=clock,
    )
