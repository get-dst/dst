"""Auto-derive a lens's coverage profile — no customer input.

The router's view of a lens is built from what the lens ALREADY declares: its
description, governed metrics (semantic-model definitions + entity metrics + bound
certified metrics/questions), and sample questions. No new config — the profile is a
projection of the published lens, refreshed when it changes.
"""

from __future__ import annotations

from pathlib import Path

from services.certdefs import CertifiedDefinition, load_certified_defs
from services.contracts.cube_probe import CubeProbe
from services.contracts.lens_config import LensConfig
from services.contracts.lens_model import LensCube, LensMember, LensModel, LensView
from services.contracts.semantic_model import (
    Entity,
    SemanticModel,
    column_hidden,
    column_internal,
)
from services.lenses.store import LensBundle
from services.router import CoverageProfile
from services.router.coverage import vocabulary
from services.runtime.not_for import declared_asks, is_avoid_line
from services.runtime.typed_resolver import _grain_nouns, _keyish

# What the decider is shown of a lens's stored values: a few per dimension,
# a few dimensions. Enough to say which lens holds a value, bounded so the
# option text stays inside the fast tier's prompt budget at any catalogue size.
_VALUES_PER_DIMENSION = 8
_DIMENSIONS_SHOWN = 8


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in items if s and s.strip()))


def coverage_profile(
    bundle: LensBundle,
    *,
    certified: list[CertifiedDefinition] | None = None,
    domains: dict[str, dict[str, list[str]]] | None = None,
) -> CoverageProfile:
    """Project a published lens into the router's matchable surface.

    ``domains``: each entity's complete stored value dictionaries
    (``value_guard.entity_domains`` over the probe's profiles). They feed the
    coverage vocabulary and the key values the decider is shown; without
    them every text column counts as one whose values are unknown.

    The broad ``"<display_name>: <description>"`` blurb goes on ``description``
    (display only — it matches almost any question and over-routes). Only
    the SPECIFIC governed-metric, definition, and sample-question anchors go into
    ``anchors``, the set the router actually scores.
    """
    cfg = bundle.config
    sm = bundle.semantic_model
    description = f"{cfg.display_name}: {cfg.description}".strip(": ")

    # Specific anchors only — the broad description is recorded separately, not here.
    anchors: list[str] = [d.term.replace("_", " ") for d in sm.definitions]
    anchors += [s.question for s in sm.sample_queries]
    # "Use this when…" asks are curated, in-the-user's-voice routing anchors:
    # the phrasings people actually use, which the specific metric/definition anchors miss.
    anchors += list(sm.use_when)
    # Exposed tables contribute no anchors: the router routes to what a lens
    # GOVERNS, and an exposed table governs nothing. Its table stays in
    # ``scope`` — the lens does read it.
    governed = [e for e in sm.entities if not e.exposed]
    anchors += [e.name.replace("_", " ") for e in governed]
    anchors += [m.name.replace("_", " ") for e in governed for m in e.metrics]
    # Entity-level meta: canonical questions and use-cases are question-shaped —
    # exactly the phrasings the router should hit near-exactly.
    anchors += [q for e in governed for q in e.common_questions]
    # An "Avoid for:" line names what the entity is NOT for: as an anchor it
    # pulls exactly those asks toward the lens. It rides ``not_for`` instead.
    anchors += [u for e in governed for u in e.use_cases if not is_avoid_line(u)]

    pages = certified
    if pages is None and cfg.model.certified_dir:
        try:
            pages = load_certified_defs(Path(cfg.model.certified_dir))
        except Exception:
            pages = None
    if pages:
        anchors += [m.metric.replace("_", " ") for m in pages]
        anchors += [m.question for m in pages if m.question]
        anchors += [m.summary for m in pages if m.summary]

    names = [e.name for e in governed] + [e.source.table for e in governed]
    names += [m.name for e in governed for m in e.metrics] + [d.term for d in sm.definitions]
    metric_names = [
        phrase
        for e in governed
        for m in e.metrics
        for phrase in (m.name, m.name.replace("_", " "), m.label or "")
    ]
    time_fields = [e.default_time_field or "" for e in governed]
    time_fields += [m.agg_time_field or "" for e in governed for m in e.metrics]
    sources = _dedupe(list(getattr(cfg, "sources", None) or []))
    return CoverageProfile(
        lens=cfg.name,
        anchors=_dedupe(anchors),
        scope=frozenset(sm.allowed_tables()),
        description=description,
        certified=_dedupe([m.question for m in pages or [] if m.question]),
        names=_dedupe(names),
        not_for=_not_for(cfg, sm),
        metric_names=_dedupe(metric_names),
        time_fields=_dedupe(time_fields),
        sources=sources,
        data_scope=str(getattr(cfg, "scope", None) or ""),
        dimensions=_dedupe([n for e in governed for n in _dimension_names(e)]),
        values=_key_values(governed, domains or {}),
        vocabulary=vocabulary(
            _held_texts(cfg.name, sm, governed, domains or {}, prose=True) + sources
        ),
        declared=vocabulary(_held_texts(cfg.name, sm, governed, domains or {}) + sources),
        open_values=any(_open_text(e, (domains or {}).get(e.name, {})) for e in governed),
    )


def _readable(e: Entity, column: str) -> bool:
    return not column_hidden(column, e) and not column_internal(column, e)


def _dimension_names(e: Entity) -> list[str]:
    return [
        f"{d.name} ({', '.join(d.aliases)})" if d.aliases else d.name
        for d in e.dimensions
        if _readable(e, d.name)
    ]


def _key_values(
    governed: list[Entity], domains: dict[str, dict[str, list[str]]]
) -> dict[str, list[str]]:
    """A few stored values of each readable dimension (else each readable,
    non-key text field) with a complete dictionary: what the decider reads to
    tell which lens holds a value the question names."""
    out: dict[str, list[str]] = {}
    for e in governed:
        own = domains.get(e.name, {})
        columns = [d.name for d in e.dimensions] or [
            f.name for f in e.fields if f.type == "string" and not _keyish(e, f.name)
        ]
        for column in columns:
            values = own.get(column.lower())
            if values and _readable(e, column) and column not in out:
                out[column] = [str(v) for v in values[:_VALUES_PER_DIMENSION]]
            if len(out) >= _DIMENSIONS_SHOWN:
                return out
    return out


def _held_texts(
    lens: str,
    sm: SemanticModel,
    governed: list[Entity],
    domains: dict[str, dict[str, list[str]]],
    *,
    prose: bool = False,
) -> list[str]:
    """Everything a question's word may land on in this lens: the typing-time
    qualifier check's list (typed_resolver.unmapped_qualifier) over every
    governed entity. ``prose``: descriptions and grain too, else the declared
    names alone. Hidden and internal columns hold nothing."""
    texts: list[str] = [lens]
    for e in governed:
        texts += [e.name, *_grain_nouns(e), e.default_time_field or ""]
        texts += [f.name for f in e.fields if _readable(e, f.name)]
        if prose:
            texts += [e.description or "", e.grain or "", e.population or ""]
            texts += [f.description or "" for f in e.fields if _readable(e, f.name)]
        for d in e.dimensions:
            if _readable(e, d.name):
                texts += [d.name, *d.aliases, *([d.description or ""] if prose else [])]
        for m in e.metrics:
            texts += [m.name, m.label or "", *m.aliases, m.agg_time_field or ""]
            texts += [m.description or "", *m.filters] if prose else []
        for column, values in domains.get(e.name, {}).items():
            if _readable(e, column):
                texts += [str(v) for v in values]
    for definition in sm.definitions:
        texts += [definition.term, *definition.aliases, *definition.value_aliases]
    return texts


def _open_text(e: Entity, own: dict[str, list[str]]) -> bool:
    """A readable, non-key text column with no complete stored dictionary."""
    return any(
        f.type == "string"
        and not _keyish(e, f.name)
        and _readable(e, f.name)
        and f.name.lower() not in own
        for f in e.fields
    )


def _not_for(cfg: object, sm: SemanticModel) -> list[str]:
    """The asks the lens declares it does not answer — the same list its own
    door refuses on (``not_for.declared_asks``: ``not_for`` and a single
    governed entity's ``Avoid for:`` use cases), plus the config's entries
    for a model compiled before it carried them. Read defensively: each entry
    is an ``{ask, sibling}`` mapping or model."""
    out = [a.ask for a in declared_asks(sm)]
    for item in getattr(cfg, "not_for", None) or []:
        ask = item.get("ask") if isinstance(item, dict) else getattr(item, "ask", None)
        if isinstance(ask, str):
            out.append(ask)
    return _dedupe(out)


def cube_coverage_profile(
    config: LensConfig,
    model: LensModel,
    probe: CubeProbe | None = None,
    *,
    certified: list[str] | None = None,
) -> CoverageProfile:
    """The router's view of a Cube-backed lens, from its ``LensModel``
    (``/v1/meta`` of its views plus the overlay) and the probe's dictionaries:
    what the lens's own lane reads, not the entity YAML it may have been
    migrated from.

    Anchors are its measures (title, else name), its definitions, its cubes'
    questions and use cases, and its certified questions; names its cubes,
    views, measures and definitions; the vocabulary every member's name,
    title, description and ``meta.dst`` aliases, the observed values of its
    dimensions, its definitions' terms and aliases, and the words that name a
    company or region (``scope.names``). Members hidden from anyone are left
    out, as hidden columns are on an entity lens."""
    dictionaries = (probe.dictionaries if probe is not None else {}) or {}
    shown = [m for m in model.members if not model.hidden(m)]
    once = list({m.base: m for m in shown}.values())
    measures = [m for m in once if m.kind == "measure"]
    dims = [m for m in once if m.kind == "dimension" and not (m.dimension and m.dimension.internal)]
    segments = [m for m in once if m.kind == "segment"]
    definitions = [*model.definitions, *model.ambiguous]

    def phrase(text: str | None) -> str:
        return (text or "").replace("_", " ").strip()

    anchors = [phrase(m.title) or phrase(m.member) for m in measures]
    anchors += [phrase(d.metric) for d in definitions]
    anchors += [
        phrase(s.segment.term if s.segment and s.segment.term else s.member) for s in segments
    ]
    for c in model.cubes:
        anchors += list(c.overlay.common_questions)
        anchors += [u for u in c.overlay.use_cases if not is_avoid_line(u)]
    anchors += list(certified or [])

    values: dict[str, list[str]] = {}
    for d in dims:
        observed = dictionaries.get(d.base)
        if observed and d.member not in values and len(values) < _DIMENSIONS_SHOWN:
            values[d.member] = [str(v) for v in observed[:_VALUES_PER_DIMENSION]]

    scope_words: list[str] = []
    for company, words in model.scope.names.items():
        scope_words.append(company)
        for w in words:
            w = str(w)
            if w.startswith("dictionary:"):
                scope_words += [str(v) for v in dictionaries.get(w[len("dictionary:") :], [])]
            else:
                scope_words.append(w)

    def aliases(m: LensMember) -> list[str]:
        dst = m.dst
        return list(getattr(dst, "aliases", None) or [])

    declared: list[str] = [
        config.name,
        *(c.name for c in model.cubes),
        *(v.name for v in model.views),
    ]
    for m in once:
        if m.kind == "dimension" and m not in dims:
            continue
        declared += [m.member, m.title or "", m.short_title or "", *aliases(m)]
        if m.dimension is not None:
            for value, words in m.dimension.value_aliases.items():
                declared += [value, *words]
        if m.segment is not None:
            declared += [m.segment.term or "", *m.segment.aliases]
        observed = dictionaries.get(m.base) if m.kind == "dimension" else None
        declared += [str(v) for v in observed or []]
    for term in definitions:
        declared += [term.metric, *term.aliases]
    # A term the lens declares it cannot compute is still its own: a question
    # naming it belongs at this lens's door, which refuses it with the reason.
    for nc in [*config.not_computable, *model.policy.not_computable]:
        declared += [nc.measure, *nc.aliases]
    sources = _dedupe([*(config.sources or []), *model.policy.sources])
    declared += sources + scope_words

    prose = list(declared)
    prose += [config.description or ""]
    prose += [m.description or "" for m in once]
    parts: list[LensCube | LensView] = [*model.cubes, *model.views]
    for part in parts:
        prose += [part.title or "", part.description or ""]
        if part.dst is not None:
            prose += [part.dst.grain or "", part.dst.population or "", part.dst.system or ""]
    prose += [d.summary or "" for d in definitions]

    not_for = [a.ask for a in [*config.not_for, *model.policy.not_for] if a.ask.strip()]
    clocks = [model.clock_of(m) for m in measures]
    description = f"{config.display_name}: {config.description}".strip(": ")
    return CoverageProfile(
        lens=config.name,
        anchors=_dedupe(anchors),
        scope=frozenset(c.name for c in model.cubes),
        description=description,
        certified=_dedupe(list(certified or [])),
        names=_dedupe(
            [c.name for c in model.cubes]
            + [v.name for v in model.views]
            + [m.member for m in measures]
            + [d.metric for d in definitions]
        ),
        not_for=_dedupe(not_for),
        metric_names=_dedupe(
            [p for m in measures for p in (m.member, phrase(m.member), m.short_title or "")]
        ),
        time_fields=_dedupe([c.split(".", 1)[-1] for c in clocks if c]),
        sources=sources,
        data_scope=str(config.scope or model.policy.scope or ""),
        dimensions=_dedupe(
            [f"{d.member} ({', '.join(aliases(d))})" if aliases(d) else d.member for d in dims]
        ),
        values=values,
        vocabulary=vocabulary(prose),
        declared=vocabulary(declared),
        open_values=any(
            (d.type or "string") == "string" and not d.primary_key and d.base not in dictionaries
            for d in dims
        ),
        cube_lane=True,
    )
