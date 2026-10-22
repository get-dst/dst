"""Auto-derive a lens's coverage profile — no customer input.

The router's view of a lens is built from what the lens ALREADY declares: its
description, governed metrics (semantic-model definitions + entity metrics + bound
certified metrics/questions), and sample questions. No new config — the profile is a
projection of the published lens, refreshed when it changes.
"""

from __future__ import annotations

from pathlib import Path

from services.certdefs import CertifiedDefinition, load_certified_defs
from services.contracts.semantic_model import SemanticModel
from services.lenses.store import LensBundle
from services.router import CoverageProfile
from services.runtime.not_for import declared_asks, is_avoid_line


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in items if s and s.strip()))


def coverage_profile(
    bundle: LensBundle, *, certified: list[CertifiedDefinition] | None = None
) -> CoverageProfile:
    """Project a published lens into the router's matchable surface.

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
        sources=_dedupe(list(getattr(cfg, "sources", None) or [])),
        data_scope=str(getattr(cfg, "scope", None) or ""),
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
