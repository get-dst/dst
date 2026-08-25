"""Auto-derive a lens's coverage profile — no customer input.

The router's view of a lens is built from what the lens ALREADY declares: its
description, governed metrics (semantic-model definitions + entity metrics + bound
certified metrics/questions), and sample questions. No new config — the profile is a
projection of the published lens, refreshed when it changes.
"""

from __future__ import annotations

from pathlib import Path

from services.certdefs import CertifiedDefinition, load_certified_defs
from services.lenses.store import LensBundle
from services.router import CoverageProfile


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
    anchors += [e.name.replace("_", " ") for e in sm.entities]
    anchors += [m.name.replace("_", " ") for e in sm.entities for m in e.metrics]
    # Entity-level meta: canonical questions and use-cases are question-shaped —
    # exactly the phrasings the router should hit near-exactly.
    anchors += [q for e in sm.entities for q in e.common_questions]
    anchors += [u for e in sm.entities for u in e.use_cases]

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

    return CoverageProfile(
        lens=cfg.name,
        anchors=_dedupe(anchors),
        scope=frozenset(sm.allowed_tables()),
        description=description,
    )
