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
import uuid
from dataclasses import replace

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field

from services.api import route_store
from services.api.query import (
    BINDINGS_HELP,
    FORMAT_HELP,
    PURPOSE_HELP,
    PURPOSE_MAX,
    UNTYPED_HELP,
    AnswerFormat,
    exposed_hint,
    exposed_tables_named,
    lens_refusal_named,
    run_lens_query,
)
from services.auth.deps import get_caller
from services.certify import store as certified_store
from services.certify.binding import render_question
from services.contracts.response import ConsumerAnswer, QueryResponse
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
from services.router.decider import LlmDecider, route_question
from services.router.profiles import coverage_profile
from services.runtime.not_for import not_for_refusal

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
    certified = _certified_questions(caller, [p.lens for p in profiles])
    return [
        replace(p, certified=list(dict.fromkeys(p.certified + certified.get(p.lens, []))))
        for p in profiles
    ]


def _certified_questions(caller: CallerIdentity, lenses: list[str]) -> dict[str, list[str]]:
    """Each lens's active certified-answer questions, a template at its first
    sample rendering (the one its match anchors on). Best-effort: without them
    routing still runs, it just cannot take the certified shortcut."""
    out: dict[str, list[str]] = {}
    try:
        with org_session(caller.org_id) as session:
            for lens in lenses:
                for a in certified_store.list_for_lens(session, lens):
                    if not certified_store.is_active(a.status):
                        continue
                    q = a.question
                    if a.slots and a.sample_bindings:
                        q = render_question(q, a.sample_bindings[0])
                    out.setdefault(lens, []).append(q)
    except Exception:
        log.exception("certified questions unavailable for routing")
    return out


def _resolve_decider() -> LlmDecider | None:
    """The LLM coverage decider on the fast tier — the stronger routing signal:
    a bare cosine score cannot tell "two lenses genuinely cover this" from
    "uncovered noise scores similar against several", and a model reading the
    shortlisted lenses' coverage can. Re-run the comparison with
    scripts/router_experiment.py; tests/test_router_gate.py pins the decider's
    accuracy floor. None falls the router back to pure cosine. The signal is
    dst's machinery to own — no customer knob."""
    decider = registry.resolve_decider()
    if decider is None:
        return None
    return LlmDecider(None, "", decider=decider)


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
    """Route the question: the deterministic sibling rules (not_for, certified question,
    distinct anchors, declared name — services/router/sibling.py), then cosine prefilter
    → LLM coverage decision (with a confident-match fast-path), or pure cosine when no
    LLM is configured. Blocking (embedder + decider over
    sync httpx) — call it off the event loop (the handler uses asyncio.to_thread)."""
    profiles = _accessible_profiles(caller)
    embedder = _resolve_embedder()
    scorer = None
    if profiles and not isinstance(embedder, _LexicalEmbedder):
        # The lexical fallback stays in-memory: its vectors are a different dim and
        # cost nothing to rebuild — only provider embeddings are worth persisting.
        scorer = _stored_scorer(caller, profiles, embedder)
    decider = _resolve_decider()
    if not profiles:
        return Router(profiles, embedder).route(question)
    try:
        return route_question(profiles, embedder, decider, question, scorer=scorer)
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


def _not_for_named(
    question: str, caller: CallerIdentity, lens: str | None
) -> tuple[str, str] | None:
    """``(lens, refusal)`` when a lens this caller may use (*lens* only, when
    given; else the first by name) declares in ``not_for`` that it does not
    answer this ask — the sentence its own door refuses with, sibling named.
    A failed lookup finds nothing."""
    try:
        bundles = list_published_for_org(caller.org_id)
    except Exception:
        log.exception("not_for lookup failed")
        return None
    for name, _display, _desc, bundle in sorted(bundles, key=lambda b: b[0]):
        if lens is not None and name != lens:
            continue
        if not authorize(caller, bundle.config)[0]:
            continue
        if (wording := not_for_refusal(question, bundle.semantic_model)) is not None:
            return name, wording
    return None


class JustAskBody(BaseModel):
    q: str = Field(description="the natural-language question — the only required field")
    format: AnswerFormat = Field(default="both", description=FORMAT_HELP)
    bindings: dict[str, str] = Field(default_factory=dict, description=BINDINGS_HELP)
    allow_untyped: bool | None = Field(default=None, description=UNTYPED_HELP)
    purpose: str = Field(default="", max_length=PURPOSE_MAX, description=PURPOSE_HELP)


class RouteProvenance(BaseModel):
    """Which governed lens answered, and what decided it.

    ``score`` is the lens's anchor-cosine SIMILARITY — a retrieval signal, not
    a decision confidence. Do not threshold on it: a wrong lens can score near
    1.0 while a correct route scores well below it; separability
    lives in the DECIDER, which is why ``decided_by`` names what made the
    call ("certified" = the question is the lens's certified question,
    "declared-name" = it names what only one of two candidates declares,
    "decider" = LLM coverage decision over the shortlist,
    "cosine-verbatim" = near-exact anchor match, "cosine" = no decider
    configured — pure similarity, the weakest basis)."""

    lens: str
    score: float
    decided_by: str = "cosine"


class RoutedAnswer(BaseModel):
    """A covered question: the governed answer plus which-lens provenance."""

    covered: bool = True
    routed_to: RouteProvenance
    # The ConsumerAnswer when the demo's audience is consumer (services/api/query.py).
    answer: QueryResponse | ConsumerAnswer


class ExposedIn(BaseModel):
    """A lens that exposes a table the question named (no definitions, never routed)."""

    lens: str
    table: str


class UncoveredEnvelope(BaseModel):
    """A declined question: the honest surface-area signal — no ungoverned answer.

    ``nearest_miss`` is the closest lens and its score (below the route threshold),
    for diagnostics and the uncovered-metric clustering — never a fallback target.
    """

    covered: bool = False
    # The routing decision's own id: the handle a caller quotes when it asks
    # about a decline, as `request_id` is for a served answer.
    request_id: str
    reason: str
    nearest_miss: RouteProvenance | None = None
    # The lenses that expose a table the question named word for word: the router
    # never routes to an exposed table (it governs nothing), so the envelope says
    # which lens to ask by name instead.
    exposed_in: list[ExposedIn] = Field(default_factory=list)
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
    decision: RouteDecision | None = None,
) -> str:
    """Record the routing decision off the deny/error paths (best-effort) and
    return its id — a generated one when the row could not be written, so the
    caller always has a handle."""
    try:
        with org_session(caller.org_id) as session:
            return route_store.record(
                session,
                question,
                lens,
                score,
                covered,
                degraded=degraded,
                decision=_decision_record(decision),
            ).id
    except Exception:
        log.exception("routing-decision persist failed")
        return "rtd_" + uuid.uuid4().hex[:16]


def _decision_record(d: RouteDecision | None) -> dict[str, object] | None:
    """The measured half of a route, as the row stores it — None when the
    decider was not consulted (cosine paths, outages)."""
    if d is None or d.decision is None:
        return None
    m = d.decision
    return {
        "provider": m.provider,
        "chosen": m.chosen,
        "p": m.p,
        "runner_up": m.runner_up,
        "margin": m.margin,
        "verdict": d.verdict,
    }


@router.post("/query", response_model=RoutedAnswer | UncoveredEnvelope)
async def just_ask(
    body: JustAskBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> RoutedAnswer | UncoveredEnvelope:
    """Ask without naming a lens. The router routes to the covering lens (second hop,
    governed, provenance-stamped) or declines with the uncovered envelope. A routed
    lens's own refusal rides the envelope as ``answer.status == "refused"`` with the
    reason in ``answer.answer`` — the direct door's ``status``/``answer``, one level
    down. A decline the router would have made where a lens the caller may use
    declares the named term not computable returns that lens's refusal the same
    way (``decided_by == "refusal"``), never the uncovered envelope."""
    # Off the event loop: routing makes blocking embedder + decider calls over sync httpx.
    decision = await asyncio.to_thread(_route, caller, body.q)

    if decision.covered and decision.lens is not None:
        # Persist BEFORE the second hop: the routing decision happened regardless of
        # whether the routed lens's query then succeeds — persisting after meant a
        # routed-then-errored request left no row and was invisible to surface area.
        decision_id = _persist(
            caller, body.q, decision.lens, decision.score, True, decision=decision
        )
        # The routed lens declares it is not for this ask: its own refusal,
        # naming the sibling, before any serve (the lens's door runs the same rail).
        declared = await asyncio.to_thread(_not_for_named, body.q, caller, decision.lens)
        if declared is not None:
            return RoutedAnswer(
                routed_to=RouteProvenance(
                    lens=decision.lens, score=decision.score, decided_by=decision.decided_by
                ),
                answer=QueryResponse(
                    lens=decision.lens,
                    status="refused",
                    answer=declared[1],
                    request_id=decision_id,
                ),
            )
        # Second hop: the routed lens's full governed pipeline (allow-list, rate
        # limit, certified definitions, certified answers, guard, trace) — like a direct call.
        # Off the event loop too: the second hop blocks exactly as long as the first
        # (generation + warehouse over sync httpx).
        answer = await asyncio.to_thread(
            run_lens_query,
            decision.lens,
            body.q,
            caller,
            background,
            body.format,
            body.bindings,
            body.allow_untyped,
            body.purpose,
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
    # A lens the caller may use would REFUSE this question by declaration — a
    # `not_computable` term it names. That refusal is the governed answer, and
    # its wording (the reason, where to ask instead) is what the caller needs;
    # "no lens covers this" would hide it. Deterministic: the same word-boundary
    # match the lens's own door runs, no model consulted.
    # So would one whose `not_for` names this ask: the router dropped it, and
    # its refusal names the sibling to ask instead.
    refusal = await asyncio.to_thread(lens_refusal_named, body.q, caller)
    if refusal is None:
        refusal = await asyncio.to_thread(_not_for_named, body.q, caller, None)
    if refusal is not None:
        lens, wording = refusal
        decision_id = _persist(caller, body.q, lens, decision.score, True, decision=decision)
        return RoutedAnswer(
            routed_to=RouteProvenance(lens=lens, score=decision.score, decided_by="refusal"),
            answer=QueryResponse(
                lens=lens, status="refused", answer=wording, request_id=decision_id
            ),
        )
    decision_id = _persist(
        caller, body.q, None, decision.score, False, degraded=decision.degraded, decision=decision
    )
    # Off the event loop: the lookup reads the org's published lenses.
    exposed = await asyncio.to_thread(exposed_tables_named, body.q, caller)
    reason = decision.reason
    if exposed:
        reason = f"{reason.rstrip('.')}; {exposed_hint(exposed)}"
    return UncoveredEnvelope(
        request_id=decision_id,
        reason=reason,
        nearest_miss=nearest,
        exposed_in=[ExposedIn(lens=lens, table=table) for lens, table in exposed],
        degraded=decision.degraded,
    )
