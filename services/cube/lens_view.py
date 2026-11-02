"""One lens's view of the Cube model: what the cube lane's checks read.

The meta document (``MetaDoc``), the overlay and the probe file, joined into
the lookups every check asks: a member's base member, its clock, whether it is
a level, which dimension labels which key, which values a dimension holds,
which words a member or a cube carries, which members the lens may call.

This is the thin adapter until the lens model (``LensModel``) is the one
object downstream reads; the checks depend on this class's methods only, so
swapping its source is one constructor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from services.contracts.cube_meta import CubeDef, Dimension, Measure, MetaDoc, Segment
from services.contracts.cube_probe import CubeProbe, MeasureFacts, TimeDimensionFacts
from services.contracts.lens_model import LensModel, Overlay, OverlayDefinition

GRAINS = ("day", "week", "month", "quarter", "year")
_ALL_GRAINS = {"second", "minute", "hour", *GRAINS}


def norm(text: object) -> str:
    """Lowercase, Nordic letters folded, underscores and hyphens as spaces:
    the one spelling every word comparison reads."""
    return (
        str(text)
        .lower()
        .replace("ö", "o")
        .replace("ä", "a")
        .replace("å", "a")
        .replace("_", " ")
        .replace("-", " ")
    )


def toks(text: object) -> list[str]:
    return re.findall(r"[a-z0-9%][a-z0-9%']*", norm(text))


@dataclass
class CubeLensView:
    doc: MetaDoc
    overlay: Overlay = field(default_factory=Overlay)
    probe: CubeProbe = field(default_factory=CubeProbe)
    # What the lens may call (its views' members, resolved to the cube members
    # behind them); None = no lens scope declared, every member Cube serves.
    model: LensModel | None = None
    # The lens model apply stored, built from the meta as a caller in every
    # exception group reads it: the one record of which view members exist
    # when Cube's access policy has dropped some from this caller's meta.
    full: LensModel | None = None

    def __post_init__(self) -> None:
        self._members: dict[str, Measure | Dimension | Segment] = {}
        self._cube_of: dict[str, CubeDef] = {}
        for c in self.doc.cubes:
            group: list[Measure | Dimension | Segment] = [*c.measures, *c.dimensions, *c.segments]
            for m in group:
                self._members[m.name] = m
                self._cube_of[m.name] = c
        self._allowed: set[str] | None = None
        if self.model is not None and self.model.views:
            self._allowed = {m.name for m in self.model.members} | {
                m.base for m in self.model.members
            }

    @property
    def views(self) -> list[str]:
        return [v.name for v in self.model.views] if self.model else []

    @property
    def model_version(self) -> str:
        return self.model.model_version if self.model else self.doc.model_version()

    def ambiguous_terms(self) -> list[OverlayDefinition]:
        """The ambiguous terms with readings the lens reads."""
        terms = self.model.ambiguous if self.model else self.overlay.ambiguous
        return [t for t in terms if t.readings]

    def hidden_rules(self) -> dict[str, list[str]]:
        """Cube member → the groups it is shown to: the overlay's hide entries
        and every dimension's own ``meta.dst.hide``. Read on dst's side,
        whatever Cube itself exposes."""
        rules = {f"{h.cube}.{h.dimension}": list(h.except_groups) for h in self.overlay.hide}
        for c in self.doc.cubes:
            for d in c.dimensions:
                if d.dst and d.dst.hide is not None:
                    rules.setdefault(self.doc.resolve(d.name), list(d.dst.hide.except_groups))
        return rules

    def withheld_rule(self, member: str) -> list[str] | None:
        """The hide rule on a view member Cube withheld from this caller's
        ``/v1/meta``. A view's access policy drops the member from the
        caller's model altogether, so it cannot be resolved to its cube; the
        model apply stored (``full``, read as a caller in every exception
        group) says whether the view holds a member of that exact name and
        which cube member it aliases. A name the stored model does not hold
        either is a miss, never a withheld member: a joined cube's member
        named without its prefix (``view.region`` for ``view.stores_region``)
        is reported as undeclared, so the caller can spell it right."""
        if self.declared(member) or self.full is None:
            return None
        parts = member.split(".")
        if len(parts) > 2 and parts[-1] in _ALL_GRAINS:
            member = ".".join(parts[:2])
        held = next((m for m in self.full.members if m.name == member), None)
        if held is None:
            return None
        return self.hidden_rules().get(held.base)

    # ── members ───────────────────────────────────────────────────────────

    def base(self, member: str) -> str:
        """The cube member behind a view member, a granularity suffix dropped
        (``orders_view.created_at.month`` → ``orders.created_at``)."""
        parts = member.split(".")
        if len(parts) > 2 and parts[-1] in _ALL_GRAINS:
            member = ".".join(parts[:2])
        return self.doc.resolve(member)

    def cube_name(self, member: str) -> str:
        return self.base(member).split(".", 1)[0]

    def member(self, name: str) -> Measure | Dimension | Segment | None:
        return self._members.get(self.base(name)) or self._members.get(name)

    def declared(self, name: str) -> bool:
        parts = name.split(".")
        if len(parts) > 2 and parts[-1] in _ALL_GRAINS:
            name = ".".join(parts[:2])
        return name in self._members

    def in_scope(self, name: str) -> bool | None:
        """Whether the lens may call the member: None when the lens declares
        no views (nothing to check against)."""
        if self._allowed is None:
            return None
        parts = name.split(".")
        if len(parts) > 2 and parts[-1] in _ALL_GRAINS:
            name = ".".join(parts[:2])
        return name in self._allowed or self.base(name) in self._allowed

    def measure(self, name: str) -> Measure | None:
        m = self.member(name)
        return m if isinstance(m, Measure) else None

    def dimension(self, name: str) -> Dimension | None:
        m = self.member(name)
        return m if isinstance(m, Dimension) else None

    def is_time(self, name: str) -> bool:
        d = self.dimension(name)
        return bool(d and d.is_time)

    def cube_members(self, cube: str) -> set[str]:
        c = self.doc.cube(cube)
        return set(c.member_names()) if c else set()

    def time_dimensions(self, cube: str) -> list[str]:
        c = self.doc.cube(cube)
        return c.time_dimensions() if c else []

    def level_grain(self, measure: str) -> str | None:
        """The snapshot grain of a level measure (a stock), else None."""
        m = self.measure(measure)
        if m and m.dst and m.dst.additivity == "level":
            g = m.dst.snapshot_grain or "month"
            return g if g in GRAINS else "month"
        return None

    def creation_dated(self, measure: str) -> bool:
        """Is this level measure on a table of one row per record (no
        snapshot to pin when no period is asked)?"""
        m = self.measure(measure)
        return bool(m and m.dst and m.dst.creation_dated)

    def label_key(self, dimension: str) -> str | None:
        d = self.dimension(dimension)
        if d and d.dst and d.dst.label_of:
            return f"{self.cube_name(dimension)}.{d.dst.label_of}"
        return None

    def as_of(self, measure: str) -> str | None:
        m = self.measure(measure)
        if m and m.dst and m.dst.as_of:
            return f"{self.cube_name(measure)}.{m.dst.as_of}"
        return None

    def clock(self, measure: str) -> str | None:
        """The time dimension a measure is windowed on: the probe's record,
        else the measure's own ``meta.dst.clock``."""
        facts = self.measure_facts(measure)
        if facts and facts.clock:
            return facts.clock
        m = self.measure(measure)
        if m and m.dst and m.dst.clock:
            c = m.dst.clock
            return c if "." in c else f"{self.cube_name(measure)}.{c}"
        return None

    def not_for(self, measure: str) -> list[str]:
        m = self.measure(measure)
        return list(m.dst.not_for) if m and m.dst else []

    def measure_phrases(self) -> list[str]:
        """What the lens's measures are called: each one's name, title and
        ``meta.dst`` aliases (the measures of its views when it has a lens
        scope, else every measure the model serves)."""
        names = (
            [m.base for m in self.model.members if m.kind == "measure"]
            if self.model is not None and self.model.views
            else [n for n, m in self._members.items() if isinstance(m, Measure)]
        )
        out: list[str] = []
        for name in names:
            m = self.measure(name)
            if m is None:
                continue
            aliases = list(m.dst.aliases) if m.dst and hasattr(m.dst, "aliases") else []
            out += [name.rsplit(".", 1)[-1], m.title or "", *aliases]
        return [p for p in out if p.strip()]

    # ── words ─────────────────────────────────────────────────────────────

    def member_text(self, name: str) -> str:
        m = self.member(name)
        if m is None:
            return norm(name)
        aliases = list(m.dst.aliases) if m.dst and hasattr(m.dst, "aliases") else []
        return norm(" ".join([m.name, m.title or "", m.description or "", *aliases]))

    def cube_text(self, cube: str) -> str:
        c = self.doc.cube(cube)
        if c is None:
            return norm(cube)
        d = c.dst
        return norm(
            " ".join(
                [
                    c.name,
                    c.title or "",
                    c.description or "",
                    (d.population or "") if d else "",
                    (d.system or "") if d else "",
                ]
            )
        )

    def caveats(self, members: list[str]) -> list[str]:
        """The overlay's caveats on what ``members`` read: each cube's own
        (``<cube>: …``), then each member's (``<cube>.<member>: …``), in the
        order read, once each."""
        bases = list(dict.fromkeys(self.base(m) for m in members if m))
        out: list[str] = []
        for cube in dict.fromkeys(b.split(".", 1)[0] for b in bases):
            ov = self.overlay.cubes.get(cube)
            out += [f"{cube}: {c}" for c in (ov.caveats if ov else [])]
        for b in bases:
            cube, _, member = b.partition(".")
            ov = self.overlay.cubes.get(cube)
            out += [f"{b}: {c}" for c in (ov.member_caveats.get(member, []) if ov else [])]
        return list(dict.fromkeys(out))

    # ── values ────────────────────────────────────────────────────────────

    def dictionary(self, dimension: str) -> list[str] | None:
        """The observed values of a dimension (the probe's), or None."""
        values = self.probe.dictionaries.get(self.base(dimension))
        return list(values) if values is not None else None

    def value_alias(self, dimension: str, word: str) -> str | None:
        d = self.dimension(dimension)
        if not d or not d.dst:
            return None
        for value, aliases in d.dst.value_aliases.items():
            if any(a.lower() == word.lower() for a in aliases):
                return value
        return None

    # ── data facts ────────────────────────────────────────────────────────

    def measure_facts(self, measure: str) -> MeasureFacts | None:
        return self.probe.measures.get(self.base(measure))

    def time_facts(self, dimension: str) -> TimeDimensionFacts | None:
        cube, _, dim = self.base(dimension).partition(".")
        facts = self.probe.cubes.get(cube)
        return facts.time_dimensions.get(dim) if facts else None

    # ── scope ─────────────────────────────────────────────────────────────

    def scope_axes(self) -> set[str]:
        """Bare dimension names whose values are companies or countries."""
        if self.overlay.scope.axes:
            return set(self.overlay.scope.axes)
        named = set(self.overlay.scope.names)
        return {
            m.split(".")[-1] for m, values in self.probe.dictionaries.items() if named & set(values)
        }

    def scope_word_axes(self) -> set[str]:
        return set(self.overlay.scope.word_axes) or self.scope_axes()

    def entity_dimension(self, cube: str) -> str | None:
        """The dimension of a cube (not a view) that says which company a row
        belongs to: a cube with one can hold several companies."""
        c = self.doc.cube(cube)
        if c is None or c.is_view:
            return None
        axes = self.scope_axes()
        return next((d.name for d in c.dimensions if d.name.split(".")[-1] in axes), None)

    def scope_names(self) -> dict[str, list[str]]:
        """Company / country value → the words (normalised) that name it: the
        overlay's list, each ``dictionary:`` entry expanded to the observed
        values of that dimension, plus the value itself."""
        out: dict[str, list[str]] = {}
        for value, words in self.overlay.scope.names.items():
            expanded: list[str] = []
            for w in words:
                w = str(w)
                if w.startswith("dictionary:"):
                    expanded += self.probe.dictionaries.get(w[len("dictionary:") :], [])
                else:
                    expanded.append(w)
            out[value] = [norm(w) for w in expanded if str(w).strip()] + [norm(value)]
        return out

    @classmethod
    def build(
        cls,
        doc: MetaDoc,
        *,
        overlay: Overlay | None = None,
        probe: CubeProbe | None = None,
        lens: str | None = None,
        views: list[str] | None = None,
        full: LensModel | None = None,
    ) -> CubeLensView:
        """The view for one lens: its ``LensModel`` over the meta and the
        overlay (``views`` from the lens config win over the overlay's), and
        ``full`` the model apply stored (every view member, whoever asks)."""
        overlay = overlay or Overlay()
        model = LensModel.build(doc, overlay, lens, views=views or None) if lens else None
        if model is not None and not model.views and not model.unknown_views:
            model = None  # the lens declares no views: no lens scope to hold
        return cls(doc=doc, overlay=overlay, probe=probe or CubeProbe(), model=model, full=full)
