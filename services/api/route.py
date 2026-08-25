"""The "just ask" surface: POST /v1/query {q} — a lens-LESS data-plane entry.

The router is a *managed system lens*: auto-derived from the org's published lenses'
coverage profiles, owned by dst, never configured by the customer. A caller asks
without naming a lens; the router matches the question against every *accessible*
lens's coverage profile and either ROUTES (covered) or DECLINES (uncovered).

- On a ROUTE: a SECOND HOP through the routed lens's existing governed pipeline
  (``run_lens_query``) — the same allow-list, rate limit, certified definitions,
  certified-answer library, guard, and trace as a direct ``/v1/lenses/{name}/query``
  call — and the answer comes
  back WITH which-lens provenance (the routed lens + the routing score).
- On a DECLINE: a structured *uncovered* envelope (``covered=false`` + reason +
  nearest-miss lens/score). NEVER an ungoverned answer from the whole warehouse — the
  honesty is in the decline.

Every decision (route or decline) is persisted (``routing_decision``, migration 0019)
so surface area = routed / asked and the uncovered-question clusters compute.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field

from services.api import route_store
from services.api.query import FORMAT_HELP, AnswerFormat, run_lens_query
from services.auth.deps import get_caller
from services.contracts.response import QueryResponse
from services.db import embedding_meta
from services.db.session import org_session
from services.governance.credentials import CallerIdentity
from services.governance.policy import authorize
from services.lenses.store import list_published_for_org
from services.llm import registry
from services.router import (
    ROUTER_DOWN,
    CoverageProfile,
    Embedder,
    RouteDecision,
    Router,
    Scorer,
    anchor_store,
)
from services.router.decider import LlmDecider, route_with_llm
from services.router.profiles import coverage_profile

log = logging.getLogger("dst")

router = APIRouter(prefix="/v1", tags=["query"])

# Embedder dimension for the lexical fallback — a fixed bag-of-words basis so cosine
# is genuinely token-OVERLAP (identical text → 1.0, disjoint → 0.0). Sized well above
# realistic vocab to keep hash collisions negligible.
_FALLBACK_DIM = 4096
_TOKEN = re.compile(r"[a-z0-9]+")


class _LexicalEmbedder:
    """A deterministic, offline ``Embedder`` for when no embedder is configured.

    Each normalized token is hashed to one basis dimension; the vector is the
    L2-normalized bag of words. Two texts' cosine is then their token Jaccard-ish
    overlap — so the router routes only on real lexical coverage and **declines**
    on a question whose words no lens governs (the safe shape: an honest decline,
    never an ungoverned answer). This is the cold-start matcher; the live semantic
    signal is the configured embedder (the routing eval tunes against it)."""

    dim = _FALLBACK_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * _FALLBACK_DIM
            for tok in set(_TOKEN.findall(t.lower())):
                h = int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], "big")
                vec[h % _FALLBACK_DIM] = 1.0
            norm = math.sqrt(sum(v * v for v in vec))
            out.append([v / norm for v in vec] if norm else vec)
        return out


def _resolve_embedder() -> Embedder:
    """The matcher's embedder: the configured embedding provider (the live routing
    signal), else a deterministic lexical fallback. The fallback keeps the surface
    *available* — it routes on token overlap and otherwise declines, which is the
    safe shape (an honest decline, never an ungoverned answer)."""
    try:
        embedder = registry.resolve_embedder()
        if embedder is not None:
            return embedder
    except Exception:
        log.exception("embedder init failed; falling back to offline matcher")
    return _LexicalEmbedder()


def _accessible_profiles(caller: CallerIdentity) -> list[CoverageProfile]:
    """The org's routing targets — ACCESS-AWARE: only lenses this caller may use (the
    router honours each lens's access policy; control stays at the lens level)."""
    profiles = []
    for _name, _display, _desc, bundle in list_published_for_org(caller.org_id):
        ok, _ = authorize(caller, bundle.config)
        if ok:
            profiles.append(coverage_profile(bundle))
    return profiles


def _resolve_decider() -> LlmDecider | None:
    """The LLM coverage decider on the fast tier — the stronger routing signal:
    a bare cosine score cannot tell "two lenses genuinely cover this" from
    "uncovered noise scores similar against several", and a model reading the
    shortlisted lenses' coverage can. Re-run the comparison with
    scripts/router_experiment.py; tests/test_router_gate.py pins the decider's
    accuracy floor. None falls the router back to pure cosine. The signal is
    dst's machinery to own — no customer knob."""
    resolved = registry.resolve(registry.tier("fast"))
    if resolved is None:
        return None
    return LlmDecider(resolved.llm, resolved.name)


def _stored_scorer(
    caller: CallerIdentity, profiles: list[CoverageProfile], embedder: Embedder
) -> Scorer | None:
    """Score against the publish-time anchor embeddings: the request path
    embeds ONLY the question; pgvector computes the per-lens max cosine where the
    vectors live. Publish pre-warms the rows; the sync here is the drift repair
    (certified-def files can change a profile without a publish) and is a text-only
    SELECT per lens in steady state. Returns None when the store can't serve —
    mismatched embedder pending `dst reindex`, DB hiccup — and the caller embeds
    in-memory for this request, loudly."""
    try:
        with org_session(caller.org_id) as session:
            for p in profiles:
                anchor_store.sync(session, p, embedder)
    except embedding_meta.EmbeddingMismatchError as e:
        log.warning("stored router anchors unusable (%s); embedding in-memory this request", e)
        return None
    except Exception:
        log.exception("router anchor store unavailable; embedding in-memory this request")
        return None
    lenses = [p.lens for p in profiles]

    def scorer(question: str) -> list[tuple[str, float]]:
        query_vec = embedder.embed([question])[0]
        with org_session(caller.org_id) as session:
            return anchor_store.scored(session, lenses, query_vec)

    return scorer


def _route(caller: CallerIdentity, question: str) -> RouteDecision:
    """Route the question: cosine prefilter → LLM coverage decision (with a confident-match
    fast-path), or pure cosine when no LLM is configured. Blocking (embedder + decider over
    sync httpx) — call it off the event loop (the handler uses asyncio.to_thread)."""
    profiles = _accessible_profiles(caller)
    embedder = _resolve_embedder()
    scorer = None
    if profiles and not isinstance(embedder, _LexicalEmbedder):
        # The lexical fallback stays in-memory: its vectors are a different dim and
        # cost nothing to rebuild — only provider embeddings are worth persisting.
        scorer = _stored_scorer(caller, profiles, embedder)
    router = Router(profiles, embedder, scorer=scorer)
    decider = _resolve_decider()
    if decider is None or not profiles:
        return router.route(question)
    try:
        return route_with_llm(router, {p.lens: p for p in profiles}, decider, question)
    except Exception:
        # Never 500 the "just ask" path — but never silently route from the
        # mid-band cosine arm either (the deleted mid-confidence path must not
        # resurrect as a fallback). Decider failures already decline inside
        # route_with_llm with DECIDER_DOWN; anything that still lands here is
        # the routing machinery itself → an honest degraded decline.
        log.exception("routing machinery failed; declining (degraded)")
        return RouteDecision(
            False,
            None,
            0.0,
            [],
            "routing degraded: the router could not score this question — declining",
            degraded=ROUTER_DOWN,
        )


class JustAskBody(BaseModel):
    q: str = Field(description="the natural-language question — the only required field")
    format: AnswerFormat = Field(default="both", description=FORMAT_HELP)


class RouteProvenance(BaseModel):
    """Which governed lens answered, and what decided it.

    ``score`` is the lens's anchor-cosine SIMILARITY — a retrieval signal, not
    a decision confidence. Do not threshold on it: a wrong lens can score near
    1.0 while a correct route scores well below it; separability
    lives in the DECIDER, which is why ``decided_by`` names what made the
    call ("decider" = LLM coverage decision over the shortlist,
    "cosine-verbatim" = near-exact anchor match, "cosine" = no decider
    configured — pure similarity, the weakest basis)."""

    lens: str
    score: float
    decided_by: str = "cosine"


class RoutedAnswer(BaseModel):
    """A covered question: the governed answer plus which-lens provenance."""

    covered: bool = True
    routed_to: RouteProvenance
    answer: QueryResponse


class UncoveredEnvelope(BaseModel):
    """A declined question: the honest surface-area signal — no ungoverned answer.

    ``nearest_miss`` is the closest lens and its score (below the route threshold),
    for diagnostics and the uncovered-metric clustering — never a fallback target.
    """

    covered: bool = False
    reason: str
    nearest_miss: RouteProvenance | None = None
    # Set when the decline is an OUTAGE (DECIDER_DOWN / ROUTER_DOWN), not a
    # coverage signal — callers and the surface report treat the two differently.
    degraded: str | None = None


def _persist(
    caller: CallerIdentity,
    question: str,
    lens: str | None,
    score: float,
    covered: bool,
    degraded: str | None = None,
) -> None:
    """Record the routing decision off the deny/error paths (best-effort)."""
    try:
        with org_session(caller.org_id) as session:
            route_store.record(session, question, lens, score, covered, degraded=degraded)
    except Exception:
        log.exception("routing-decision persist failed")


@router.post("/query", response_model=RoutedAnswer | UncoveredEnvelope)
async def just_ask(
    body: JustAskBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> RoutedAnswer | UncoveredEnvelope:
    """Ask without naming a lens. The router routes to the covering lens (second hop,
    governed, provenance-stamped) or declines with the uncovered envelope."""
    # Off the event loop: routing makes blocking embedder + decider calls over sync httpx.
    decision = await asyncio.to_thread(_route, caller, body.q)

    if decision.covered and decision.lens is not None:
        # Persist BEFORE the second hop: the routing decision happened regardless of
        # whether the routed lens's query then succeeds — persisting after meant a
        # routed-then-errored request left no row and was invisible to surface area.
        _persist(caller, body.q, decision.lens, decision.score, True)
        # Second hop: the routed lens's full governed pipeline (allow-list, rate
        # limit, certified definitions, certified answers, guard, trace) — like a direct call.
        # Off the event loop too: the second hop blocks exactly as long as the first
        # (generation + warehouse over sync httpx).
        answer = await asyncio.to_thread(
            run_lens_query, decision.lens, body.q, caller, background, body.format
        )
        return RoutedAnswer(
            routed_to=RouteProvenance(
                lens=decision.lens, score=decision.score, decided_by=decision.decided_by
            ),
            answer=answer,
        )

    nearest = (
        RouteProvenance(lens=decision.alternatives[0][0], score=decision.alternatives[0][1])
        if decision.alternatives
        else None
    )
    _persist(caller, body.q, None, decision.score, False, degraded=decision.degraded)
    return UncoveredEnvelope(
        reason=decision.reason, nearest_miss=nearest, degraded=decision.degraded
    )
