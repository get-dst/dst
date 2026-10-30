"""Plan and apply for a Cube-backed project: the overlay checked against Cube.

A project is Cube-backed when its push carries ``cube/overlay.yaml``. Plan
reads ``/v1/meta`` (extended, as the ``cube.plan_context`` groups), builds a
``LensModel`` per lens and reports, per lens, everything that does not hold
together, each with one failure mode:

errors (apply refuses)
  ``unknown_view``          a view the lens names is not in the meta
  ``unknown_member``        the overlay names a cube, dimension or member the
                            meta does not hold (a clock, pinned dimension, feed
                            axis, hide entry or a definition's ``about``)
  ``meta_dst_unknown_key``  a ``meta.dst`` key outside the closed schema
  ``level_without_clock``   an ``additivity: level`` measure with no clock to
                            pick the latest snapshot by
  ``default_segment_missing`` a ``default_segment`` / ``population_segment``
                            naming no segment of its cube
  ``certified_member``      a certified pair's ``query`` names a member the
                            lens does not hold
  ``certified_hidden``      a certified pair's ``query`` names a hidden member
  ``provider_down``         Cube itself did not answer (the files still check)
warnings
  ``hide_unenforced_in_cube`` a hidden member Cube still serves to a caller in
                            no group (measured: the meta read as nobody)
  ``text_month_dimension``  a string dimension whose declared values are all
                            ``YYYY-MM`` (a period cannot bind to it)
hints
  ``definition_about_hidden`` a definition about a member Cube lists to no
                            caller that the overlay hides: kept, served to
                            the callers who may see the member
  ``hide_unlisted``         a hide with no exception groups whose member Cube
                            lists to no one (withheld, or misspelt)
  ``dictionary_unprobed``   ``dictionary: true`` with no values in
                            ``profiles/cube.probe.json`` (run ``dst probe --cube``)

and the "keys read" table: every ``meta.dst`` key the lens's members carry,
how many carry it, and what in dst reads it, so a scaffolded key nothing
reads is visible. Apply runs the same checks, refuses on an error, and stamps
each lens's ``model_version`` (``CubeStamp``) onto the compiled model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import yaml

from services.contracts.cube_meta import MetaDoc
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_model import (
    KEY_READERS,
    OVERLAY_PATH,
    LensModel,
    Overlay,
    parse_overlay,
)
from services.contracts.semantic_model import CubeStamp

CUBE_PROBE_PATH = "profiles/cube.probe.json"
_MONTH = re.compile(r"^\d{4}-\d{2}$")
# A default segment Cube does not hold but dst reads: the latest snapshot of a
# level measure, applied only to asks with no window.
BUILTIN_SEGMENTS = frozenset({"latest_snapshot"})

Severity = Literal["error", "warning", "hint"]


@dataclass(frozen=True)
class CubeIssue:
    severity: Severity
    code: str
    mode: FailureMode
    message: str
    lens: str | None = None

    def row(self) -> dict[str, object]:
        status = {"error": "invalid", "warning": "warning", "hint": "hint"}[self.severity]
        out: dict[str, object] = {
            "scope": "cube",
            "status": status,
            "code": self.code,
            "mode": str(self.mode),
            "lens": self.lens,
        }
        out["error" if self.severity == "error" else self.severity] = self.message
        return out


# ── reading the push ──────────────────────────────────────────────────────


def is_cube_backed(files: Mapping[str, str]) -> bool:
    return OVERLAY_PATH in files


def lens_views(files: Mapping[str, str]) -> dict[str, list[str] | None]:
    """lens → its lens.yaml ``views:`` (None when it names none)."""
    out: dict[str, list[str] | None] = {}
    for path, text in files.items():
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "lenses" and parts[2] == "lens.yaml":
            try:
                data = yaml.safe_load(text) or {}
            except yaml.YAMLError:
                continue  # the lens row reports it
            views = data.get("views") if isinstance(data, dict) else None
            out[parts[1]] = [str(v) for v in views] if isinstance(views, list) and views else None
    return out


def cube_lenses(overlay: Overlay, files: Mapping[str, str]) -> dict[str, list[str] | None]:
    """The Cube-backed lenses: every overlay lens, and every lens.yaml naming
    ``views:``; the value is the lens.yaml override (None = the overlay's)."""
    named = lens_views(files)
    out: dict[str, list[str] | None] = {lens: named.get(lens) for lens in overlay.lenses}
    out.update({lens: views for lens, views in named.items() if views})
    return dict(sorted(out.items()))


def backed_lenses(files: Mapping[str, str]) -> set[str]:
    """The lenses apply stamps against Cube, and so serves through it by
    default: with ``cube/overlay.yaml`` in the push, every overlay lens and
    every lens.yaml naming ``views:``. Without an overlay, or with one that
    does not parse (apply refuses it), none: a lens naming ``views:`` alone
    is not stamped and serves typed unless it says ``serving: cube``."""
    if not is_cube_backed(files):
        return set()
    try:
        return set(cube_lenses(parse_overlay(files[OVERLAY_PATH]), files))
    except ValueError:
        return set()


def certified_pairs(files: Mapping[str, str], lens: str) -> list[dict[str, Any]]:
    """The lens's certified entries that carry a Cube ``query``."""
    text = files.get(f"lenses/{lens}/certified_answers.yaml")
    if not text:
        return []
    try:
        entries = yaml.safe_load(text) or []
    except yaml.YAMLError:
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("query"), dict)]


def probed_dictionaries(files: Mapping[str, str]) -> dict[str, list[str]]:
    text = files.get(CUBE_PROBE_PATH)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return {
        str(k): [str(x) for x in v]
        for k, v in (data.get("dictionaries") or {}).items()
        if isinstance(v, list)
    }


def query_members(query: Mapping[str, Any]) -> list[str]:
    """Every member a Cube query JSON names."""
    out: list[str] = []
    for key in ("measures", "dimensions", "segments"):
        out += [str(m) for m in query.get(key) or []]
    out += [
        str(t.get("dimension")) for t in query.get("timeDimensions") or [] if t.get("dimension")
    ]

    def walk(filters: Iterable[Any]) -> None:
        for f in filters:
            if not isinstance(f, dict):
                continue
            if f.get("member") or f.get("dimension"):
                out.append(str(f.get("member") or f.get("dimension")))
            walk(f.get("and") or [])
            walk(f.get("or") or [])

    walk(query.get("filters") or [])
    order = query.get("order")
    if isinstance(order, dict):
        out += [str(k) for k in order]
    elif isinstance(order, list):
        out += [str(o[0]) for o in order if isinstance(o, list) and o]
    return list(dict.fromkeys(out))


# ── the checks ────────────────────────────────────────────────────────────


def validate(
    model: LensModel,
    meta: MetaDoc,
    *,
    public_meta: MetaDoc | None = None,
    pairs: Iterable[Mapping[str, Any]] = (),
    probed: Mapping[str, list[str]] | None = None,
) -> list[CubeIssue]:
    """Every check on one lens. ``meta`` is the meta read as the plan
    context; ``public_meta`` the meta read as a caller in no group (what Cube
    enforces); ``probed`` the dictionaries ``profiles/cube.probe.json`` holds,
    by cube member."""
    lens = model.lens
    issues: list[CubeIssue] = []

    def add(sev: Severity, code: str, mode: FailureMode, message: str) -> None:
        issues.append(CubeIssue(sev, code, mode, message, lens))

    for view in model.unknown_views:
        add(
            "error",
            "unknown_view",
            FailureMode.not_declared,
            f"lens '{lens}' names view '{view}', which Cube's /v1/meta does not hold",
        )
    by_base = {m.base: m for m in model.members}

    def known(member: str) -> bool:
        return member in by_base or meta.member(member) is not None

    def cube_dims(cube: str) -> set[str]:
        held = {m.member for m in model.members if m.cube == cube and m.kind == "dimension"}
        c = meta.cube(cube)
        return held | ({d.name.split(".", 1)[1] for d in c.dimensions} if c else set())

    for cube in model.cubes:
        ov = cube.overlay
        if meta.cube(cube.name) is None and cube.name not in {m.cube for m in model.members}:
            continue
        dims = cube_dims(cube.name)
        if ov.default_time_dimension and ov.default_time_dimension not in dims:
            add(
                "error",
                "unknown_member",
                FailureMode.window_misbound,
                f"overlay cubes.{cube.name}.default_time_dimension names "
                f"'{ov.default_time_dimension}', not a dimension of {cube.name}",
            )
        for td in ov.clock_kind:
            if td not in dims:
                add(
                    "error",
                    "unknown_member",
                    FailureMode.window_misbound,
                    f"overlay cubes.{cube.name}.clock_kind names '{td}', not a dimension "
                    f"of {cube.name}",
                )
        measures = {m.member for m in model.members if m.cube == cube.name and m.kind == "measure"}
        c_meta = meta.cube(cube.name)
        measures |= {x.name.split(".", 1)[1] for x in (c_meta.measures if c_meta else [])}
        for name in ov.feed_measures:
            if name not in measures:
                add(
                    "error",
                    "unknown_member",
                    FailureMode.data_gap,
                    f"overlay cubes.{cube.name}.feed_measures names '{name}', not a measure "
                    f"of {cube.name}",
                )
        for name in ov.member_caveats:
            if not known(f"{cube.name}.{name}"):
                add(
                    "error",
                    "unknown_member",
                    FailureMode.not_declared,
                    f"overlay cubes.{cube.name}.member_caveats names '{name}', not a member "
                    f"of {cube.name}",
                )
        for key in ("pinned_dimensions", "feed_axes"):
            for dim in getattr(ov, key):
                if dim not in dims:
                    add(
                        "error",
                        "unknown_member",
                        FailureMode.not_declared,
                        f"overlay cubes.{cube.name}.{key} names '{dim}', not a dimension "
                        f"of {cube.name}",
                    )
        if cube.dst and cube.dst.population_segment:
            seg = cube.dst.population_segment
            if not known(seg if "." in seg else f"{cube.name}.{seg}"):
                add(
                    "error",
                    "default_segment_missing",
                    FailureMode.unasked_filter,
                    f"{cube.name}: meta.dst.population_segment '{seg}' names no segment",
                )
    for h in model.hide:
        if not known(f"{h.cube}.{h.dimension}"):
            if not h.except_groups:
                # Hidden from everyone: Cube lists no public: false member, so
                # the meta cannot tell a withheld name from a misspelt one.
                add(
                    "hint",
                    "hide_unlisted",
                    FailureMode.hidden_column,
                    f"overlay hide names {h.cube}.{h.dimension}, which Cube lists to no "
                    "caller (withheld from everyone, as declared): check the name",
                )
                continue
            add(
                "warning",
                "unknown_member",
                FailureMode.hidden_column,
                f"overlay hide names {h.cube}.{h.dimension}, which no view of the lens "
                "serves and the meta does not list: nothing to enforce — check the name",
            )
    withheld = {f"{h.cube}.{h.dimension}": h.except_groups for h in model.hide}
    for d in [*model.definitions, *model.ambiguous]:
        if d.about and "." in d.about and not known(d.about) and d.about in withheld:
            shown = ", ".join(withheld[d.about]) or "no caller"
            add(
                "hint",
                "definition_about_hidden",
                FailureMode.hidden_column,
                f"definition '{d.metric}' is about {d.about}, which Cube lists to no caller "
                f"(hidden, shown to {shown}): the definition is kept and served to the "
                "callers who may see the member",
            )
        elif d.about and "." in d.about and not known(d.about):
            add(
                "error",
                "unknown_member",
                FailureMode.not_declared,
                f"definition '{d.metric}' is about {d.about}, which the meta does not hold",
            )
        elif d.about and "." not in d.about and meta.cube(d.about) is None:
            add(
                "error",
                "unknown_member",
                FailureMode.not_declared,
                f"definition '{d.metric}' is about cube '{d.about}', which the meta does not hold",
            )
    names = {m.name for m in model.members} | set(by_base)
    # A cube member several views alias is checked once, under its cube name.
    once = list({m.base: m for m in reversed(model.members)}.values())[::-1]
    for member in (m for m in once if m.kind == "measure"):
        dst = member.measure
        if dst is None:
            continue
        if dst.clock:
            clock = dst.clock if "." in dst.clock else f"{member.cube}.{dst.clock}"
            target = by_base.get(clock) or meta.member(clock)
            if target is None or getattr(target, "type", None) not in ("time", None):
                add(
                    "error",
                    "unknown_member",
                    FailureMode.window_misbound,
                    f"{member.base}: meta.dst.clock '{dst.clock}' names no time dimension",
                )
        if dst.additivity == "level" and model.clock_of(member) is None:
            add(
                "error",
                "level_without_clock",
                FailureMode.wrong_grain,
                f"{member.base} is a level (additivity: level) with no clock: declare "
                f"meta.dst.clock or overlay cubes.{member.cube}.default_time_dimension, "
                "or no window can pick its latest snapshot",
            )
        if dst.default_segment and dst.default_segment not in BUILTIN_SEGMENTS:
            seg = dst.default_segment
            if not known(seg if "." in seg else f"{member.cube}.{seg}"):
                add(
                    "error",
                    "default_segment_missing",
                    FailureMode.not_declared,
                    f"{member.base}: meta.dst.default_segment '{seg}' names no segment",
                )
    for member in (m for m in once if m.kind == "dimension"):
        dim = member.dimension
        if dim is None:
            continue
        values = dim.declared_values
        if member.type == "string" and values and all(_MONTH.match(v) for v in values):
            add(
                "warning",
                "text_month_dimension",
                FailureMode.period_unreadable,
                f"{member.base} is a string whose values are all YYYY-MM: declare it "
                "`type: time` (cast to the month's first day) so a period binds to it",
            )
        if dim.dictionary is True and not (probed or {}).get(member.base):
            add(
                "hint",
                "dictionary_unprobed",
                FailureMode.unbound_value,
                f"{member.base} declares dictionary: true and {CUBE_PROBE_PATH} holds no "
                "values for it — run `dst probe --cube`",
            )
    if public_meta is not None:
        public = {m.name for c in public_meta.cubes for m in c.dimensions if m.public is not False}
        for member in model.dimensions():
            if model.hidden(member) and member.name in public:
                add(
                    "warning",
                    "hide_unenforced_in_cube",
                    FailureMode.hidden_column,
                    f"{member.name} is hidden ({member.base}) but Cube serves it to a caller "
                    "in no group: dst withholds it, Cube does not — add an access_policy on "
                    "security_context.dst.groups",
                )
    for pair in pairs:
        question = str(pair.get("question") or "")[:80]
        for name in query_members(pair.get("query") or {}):
            called = model.member(name)
            if name not in names or called is None:
                add(
                    "error",
                    "certified_member",
                    FailureMode.not_declared,
                    f"certified '{question}': query names {name}, not a member of the lens",
                )
            elif model.hidden(called, _every_group(model)):
                add(
                    "error",
                    "certified_hidden",
                    FailureMode.hidden_column,
                    f"certified '{question}': query names {name}, hidden from every caller",
                )
            elif model.hidden(called):
                add(
                    "error",
                    "certified_hidden",
                    FailureMode.hidden_column,
                    f"certified '{question}': query names {name}, hidden outside "
                    "its exception groups — a certified answer serves every caller",
                )
    return issues


def _every_group(model: LensModel) -> list[str]:
    groups = {g for h in model.hide for g in h.except_groups}
    groups |= {
        g
        for m in model.dimensions()
        if m.dimension and m.dimension.hide
        for g in m.dimension.hide.except_groups
    }
    return sorted(groups)


def meta_errors(meta: MetaDoc) -> list[CubeIssue]:
    """The closed schema's refusals, once per project."""
    return [
        CubeIssue("error", "meta_dst_unknown_key", FailureMode.not_declared, line)
        for line in meta.overlay_errors
    ]


def keys_read(model: LensModel) -> list[dict[str, object]]:
    """Per ``meta.dst`` key the lens's members carry: on what, how many, and
    what in dst reads it."""
    counts: dict[tuple[str, str], int] = {}

    def tally(on: str, dst: Any) -> None:
        if dst is None:
            return
        for key, value in dst.model_dump(exclude_defaults=True).items():
            if value not in (None, [], {}):
                counts[(on, key)] = counts.get((on, key), 0) + 1

    for m in model.members:
        tally(m.kind, m.dst)
    for c in model.cubes:
        tally("cube", c.dst)
    for v in model.views:
        tally("cube", v.dst)
    return [
        {"on": on, "key": key, "members": n, "reader": KEY_READERS[on].get(key, "not read")}
        for (on, key), n in sorted(counts.items())
    ]


# ── plan rows, apply stamps ───────────────────────────────────────────────


@dataclass
class CubeCheck:
    """One pass over a Cube-backed push: models per lens, every issue."""

    models: dict[str, LensModel] = field(default_factory=dict)
    issues: list[CubeIssue] = field(default_factory=list)
    keys: dict[str, list[dict[str, object]]] = field(default_factory=dict)

    @property
    def errors(self) -> list[CubeIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def rows(self) -> list[dict[str, object]]:
        out = [i.row() for i in self.issues]
        for lens, keys in sorted(self.keys.items()):
            model = self.models[lens]
            out.append(
                {
                    "scope": "cube",
                    "status": "keys",
                    "lens": lens,
                    "model_version": model.model_version,
                    "overlay_version": model.overlay_version,
                    "views": [v.name for v in model.views],
                    "keys": keys,
                }
            )
        return out

    def stamp(self, lens: str, *, now: datetime | None = None) -> CubeStamp | None:
        model = self.models.get(lens)
        if model is None:
            return None
        return CubeStamp(
            model_version=model.model_version,
            overlay_version=model.overlay_version,
            stamped_at=(now or datetime.now(UTC)).isoformat(),
            views=[v.name for v in model.views],
            lens_model=model.model_dump(mode="json"),
        )


def check(files: Mapping[str, str], *, fetch: Any = None) -> CubeCheck:
    """The whole Cube-side check of a push. ``fetch`` returns ``(meta,
    public_meta)`` for the push's Cube (``fetch_meta`` by default); a failure
    to reach Cube is one ``provider_down`` error, and the overlay file is
    still checked."""
    result = CubeCheck()
    try:
        overlay = parse_overlay(files[OVERLAY_PATH])
    except ValueError as exc:
        result.issues.append(
            CubeIssue("error", "overlay_invalid", FailureMode.not_declared, str(exc))
        )
        return result
    try:
        meta, public_meta = (fetch or fetch_meta)(files)
    except Exception as exc:  # noqa: BLE001 — any Cube failure is the one row
        from services.cube.client import CubeError

        detail = exc.detail if isinstance(exc, CubeError) else f"{type(exc).__name__}: {exc}"
        result.issues.append(
            CubeIssue(
                "error",
                "provider_down",
                FailureMode.provider_down,
                f"provider_down: cube — {detail}; the overlay parsed, the model was not "
                "checked and apply cannot stamp a model_version",
            )
        )
        return result
    result.issues += meta_errors(meta)
    probed = probed_dictionaries(files)
    for lens, views in cube_lenses(overlay, files).items():
        model = LensModel.build(meta, overlay, lens, views=views)
        result.models[lens] = model
        result.issues += validate(
            model,
            meta,
            public_meta=public_meta,
            pairs=certified_pairs(files, lens),
            probed=probed,
        )
        result.keys[lens] = keys_read(model)
    return result


def _exception_groups(files: Mapping[str, str]) -> list[str]:
    """Every group the overlay's hide entries except: a plan context with no
    groups of its own reads the model as a caller in all of them, or Cube
    withholds the excepted members from plan too and plan cannot check them."""
    try:
        overlay = parse_overlay(files[OVERLAY_PATH])
    except (KeyError, ValueError):
        return []
    return sorted({g for h in overlay.hide for g in h.except_groups})


def fetch_meta(files: Mapping[str, str], *, transport: Any = None) -> tuple[MetaDoc, MetaDoc]:
    """``(meta as the plan context, meta as a caller in no group)``, extended,
    from the Cube the push's dst.yaml (or this server's env) names. The plan
    context is ``cube.plan_context.groups``, or else every exception group
    the overlay's hide entries name."""
    from services.cube.client import CubeClient, CubeError, configured_from
    from services.cube.context import CubeContext
    from services.project.schema import parse_project_yaml

    decl = parse_project_yaml(files["dst.yaml"]).cube if "dst.yaml" in files else None
    cfg = configured_from(decl)
    if cfg is None:
        raise CubeError(
            "no Cube configured — set `cube: {url: …}` in dst.yaml or DST_CUBE_URL",
            mode=FailureMode.provider_down,
            provider="cube",
        )
    client = CubeClient(cfg.url, cfg.api_secret, meta_ttl=0, timeout=30.0, transport=transport)
    try:
        groups = list(decl.plan_context.groups) if decl else []
        if not groups:
            groups = _exception_groups(files)
        plan_ctx = CubeContext(org="", caller="dst-plan", groups=sorted(groups))
        meta = client.meta(plan_ctx, extended=True)
        public = meta if not groups else client.meta(CubeContext.operator(), extended=True)
        return meta, public
    finally:
        client.close()


def stored_lens_model(stamp: CubeStamp | None) -> LensModel | None:
    """The LensModel apply stored with a lens, or None for a lens without one."""
    if stamp is None or not stamp.lens_model:
        return None
    return LensModel.model_validate(stamp.lens_model)
