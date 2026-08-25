"""Publish-time anchor embeddings — the router's stored recall surface.

Coverage-profile anchors were re-embedded on EVERY /v1/query request (paying the
embedding provider for the same strings again, a real chunk of the endpoint's
latency). The ``router_anchor`` rows are a write-through cache keyed
(org, lens, anchor): publish pre-warms it (:func:`warm`), :func:`sync` repairs
drift — certified-def files can change a profile without a publish — and the
request path embeds ONLY the question. Scoring stays in pgvector
(:func:`scored`, max anchor cosine per lens) so vectors never cross the wire.

Embedder identity rides the global ``embedding_meta`` guard, same as every other
vector table: a swapped embedder raises :class:`EmbeddingMismatchError` here (the
router falls back to in-memory embedding for the request, loudly) and
``dst reindex`` empties the table — cache rows rebuild lazily, they are never
re-embedded in place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.contracts.protocols import Embedder
from services.db import embedding_meta
from services.router import CoverageProfile

if TYPE_CHECKING:
    from services.lenses.store import LensBundle

log = logging.getLogger("dst")


def _vec(values: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


def sync(session: Session, profile: CoverageProfile, embedder: Embedder) -> None:
    """Make the stored rows match the profile: embed only NEW anchors, delete ones
    the profile no longer declares. Steady state is one text-only SELECT and zero
    embedding calls. Raises ``EmbeddingMismatchError`` when the stored corpus was
    written by a different embedder — the fix is ``dst reindex``, and the caller
    serves this request by embedding in-memory."""
    stored_meta = embedding_meta.read_meta(session)
    model, dim = embedding_meta.identity(embedder)
    if stored_meta is not None and stored_meta != (model, dim):
        raise embedding_meta.EmbeddingMismatchError(
            f"stored vectors were written with {stored_meta[0]} (dim {stored_meta[1]}) "
            f"but the configured embedder is {model} (dim {dim}) — run `dst reindex`"
        )
    stored = {
        r[0]
        for r in session.execute(
            text("SELECT anchor FROM router_anchor WHERE lens = :l"), {"l": profile.lens}
        )
    }
    want = set(profile.anchors)
    stale = sorted(stored - want)
    if stale:
        session.execute(
            text("DELETE FROM router_anchor WHERE lens = :l AND anchor = ANY(CAST(:a AS text[]))"),
            {"l": profile.lens, "a": stale},
        )
    missing = [a for a in profile.anchors if a not in stored]
    if missing:
        embedding_meta.guard_write(session, embedder)
        for anchor, vec in zip(missing, embedder.embed(missing), strict=True):
            session.execute(
                text(
                    "INSERT INTO router_anchor (org_id, lens, anchor, embedding) VALUES ("
                    "NULLIF(current_setting('app.current_org', true), '')::uuid, "
                    ":l, :a, CAST(:e AS vector)) "
                    "ON CONFLICT (org_id, lens, anchor) DO NOTHING"
                ),
                {"l": profile.lens, "a": anchor, "e": _vec(vec)},
            )


def scored(session: Session, lenses: list[str], query_vec: list[float]) -> list[tuple[str, float]]:
    """Every lens ranked by max anchor cosine to the pre-embedded question (best
    first) — the same recall signal ``Router.scored`` computes in memory, done
    where the vectors live. A lens with no stored anchors gets no entry, matching
    the in-memory behaviour for an anchor-less profile."""
    rows = session.execute(
        text(
            "SELECT lens, MAX(1 - (embedding <=> CAST(:q AS vector))) AS score "
            "FROM router_anchor WHERE lens = ANY(CAST(:ls AS text[])) "
            "GROUP BY lens ORDER BY score DESC"
        ),
        {"q": _vec(query_vec), "ls": lenses},
    ).all()
    return [(r[0], float(r[1])) for r in rows]


def warm(session: Session, bundle: LensBundle) -> None:
    """Pre-warm at publish — best-effort, never blocks the publish (the read path
    repairs on the next request if this fails or no embedder is configured)."""
    from services.llm import registry
    from services.router.profiles import coverage_profile

    try:
        embedder = registry.resolve_embedder()
        if embedder is None:
            return
        sync(session, coverage_profile(bundle), embedder)
    except Exception:
        log.exception("router-anchor warm failed for lens %r", bundle.config.name)


def cross_claims(session: Session, threshold: float = 0.95) -> list[tuple[str, str, float]]:
    """Lens pairs whose anchor sets overlap at near-verbatim similarity —
    the offline half of the cosine-is-not-a-decision problem.

    A symmetric swap (lens A takes lens B's question at 1.000 and B takes A's
    at 0.958) means the two coverage profiles are not separable by embedding at
    all; adding anchors to one side just moves the error. That is a MODELLING
    problem to report at apply time, never a runtime coin flip — the decider
    absorbs most of it at serve time, but the apply warning is where the
    author learns to split the vocabulary or merge the lenses. One pgvector
    pass over the stored anchors; empty when no embedder has warmed them."""
    rows = session.execute(
        text(
            "SELECT a.lens, b.lens, MAX(1 - (a.embedding <=> b.embedding)) AS sim "
            "FROM router_anchor a JOIN router_anchor b ON a.lens < b.lens "
            "GROUP BY a.lens, b.lens "
            "HAVING MAX(1 - (a.embedding <=> b.embedding)) >= :t "
            "ORDER BY sim DESC"
        ),
        {"t": threshold},
    ).all()
    return [(r[0], r[1], float(r[2])) for r in rows]
