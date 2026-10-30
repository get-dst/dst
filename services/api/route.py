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
from collections.abc import Callable
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
    effective_allow_untyped,
    exposed_hint,
    exposed_tables_named,
    lens_refusal_named,
    routing_spend,
    run_lens_query,
)
from services.auth.deps import get_caller
from services.certify import store as certified_store
from services.certify.binding import render_question
from services.contracts.cube_probe import CubeProbe
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_model import LensModel
from services.contracts.profile import TableProfile
from services.contracts.response import ConsumerAnswer, QueryResponse
from services.db import embedding_meta
from services.db.session import org_session
from services.governance.credentials import CallerIdentity
from services.governance.policy import authorize
from services.lenses import profile_store
from services.lenses.store import LensBundle, list_published_for_org
from services.llm import decision_memo, registry
from services.observability import cost
from services.observability import usage as ai_usage
from services.project.cube_plan import stored_lens_model
from services.router import (
    ROUTER_DOWN,
    CoverageProfile,
    Embedder,
    RouteDecision,
    Router,
    Scorer,
    anchor_store,
)
from services.router.coverage import asked_words, held_by, unused_words, vocabulary, word_keys
from services.router.decider import COVERAGE_K, LlmDecider, route_question
from services.router.profiles import coverage_profile, cube_coverage_profile
from services.router.sibling import by_sources, certified_owner, drop_not_for
from services.runtime import value_guard
from services.runtime.not_for import not_for_hit, not_for_refusal
from services.runtime.pipeline import LENS_DATA_REFUSAL, deterministic_refusal_not_computable
from services.serve import door as cube_door
from services.serve import driver

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


def _accessible_profiles(caller: CallerIdentity) -> tuple[list[CoverageProfile], list[str]]:
    """The org's routing targets — ACCESS-AWARE: only lenses this caller may use (the
    router honours each lens's access policy; control stays at the lens level) —
    and the source systems every published lens declares, the caller's or not:
    a question naming one only a lens it may not use reads is uncovered for
    that reason, never answered from a lens that does not read it."""
    profiles = []
    known: list[str] = []
    stored: dict[str, list[TableProfile]] = {}
    probe: list[CubeProbe] = []
    for _name, _display, _desc, bundle in list_published_for_org(caller.org_id):
        known += list(getattr(bundle.config, "sources", None) or [])
        ok, _ = authorize(caller, bundle.config)
        if ok:
            cube_model = _cube_lens_model(bundle)
            if cube_model is not None:
                # A lens served through Cube is routed on what its lane reads:
                # its views' members, the overlay and the probe's dictionaries.
                if not probe:
                    probe.append(_cube_probe())
                profiles.append(cube_coverage_profile(bundle.config, cube_model, probe[0]))
                continue
            domains = value_guard.entity_domains(
                bundle.semantic_model, _stored_profiles(caller, bundle, stored)
            )
            profiles.append(coverage_profile(bundle, domains=domains))
    certified = _certified_questions(caller, [p.lens for p in profiles])
    return [
        replace(
            p,
            certified=list(dict.fromkeys(p.certified + certified.get(p.lens, []))),
            # A Cube-backed lens's certified questions are anchors too: its
            # profile is read from what it serves, and they are its own asks.
            anchors=list(dict.fromkeys(p.anchors + certified.get(p.lens, [])))
            if p.cube_lane
            else p.anchors,
        )
        for p in profiles
    ], known


def _cube_lens_model(bundle: LensBundle) -> LensModel | None:
    """The LensModel apply stored with a lens served through Cube, or None."""
    if cube_door.serving_core(bundle.config, bundle.semantic_model) != "cube":
        return None
    try:
        return stored_lens_model(bundle.semantic_model.cube)
    except Exception:
        log.exception("stored lens model unreadable for routing")
        return None


def _cube_probe() -> CubeProbe:
    """The probe's dictionaries for routing Cube-backed lenses. Best-effort:
    none read, every text dimension counts as one whose values are unknown."""
    try:
        return cube_door.load_probe(cube_door.project_root())
    except Exception:
        log.exception("cube probe unavailable for routing")
        return CubeProbe()


def _stored_profiles(
    caller: CallerIdentity, bundle: LensBundle, seen: dict[str, list[TableProfile]]
) -> list[TableProfile]:
    """The probe's stored table profiles for the lens's connection, read once
    per connection per request. Best-effort: none read, none used (every text
    column then counts as one whose values are unknown)."""
    if not bundle.config.connections:
        return []
    connection = bundle.config.connections[0]
    if connection not in seen:
        try:
            with org_session(caller.org_id) as session:
                seen[connection] = [
                    s.profile for s in profile_store.list_profiles(session, connection)
                ]
        except Exception:
            log.exception("stored profiles unavailable for routing")
            seen[connection] = []
    return seen[connection]


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
    shortlisted lenses' coverage can. tests/test_router_gate.py pins the decider's
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

    def scorer(query_vec: list[float]) -> list[tuple[str, float]]:
        with org_session(caller.org_id) as session:
            return anchor_store.scored(session, lenses, query_vec)

    return scorer


def _route(
    caller: CallerIdentity, question: str, exclude: frozenset[str] = frozenset()
) -> RouteDecision:
    """Route the question: the deterministic sibling rules (not_for, certified question,
    distinct anchors, declared name — services/router/sibling.py), then cosine prefilter
    → LLM coverage decision (with a confident-match fast-path), or pure cosine when no
    LLM is configured. Blocking (embedder + decider over
    sync httpx) — call it off the event loop (the handler uses asyncio.to_thread).
    ``exclude``: lenses left out of the routing (a driver's Cube lenses)."""
    profiles, known_sources = _accessible_profiles(caller)
    if exclude:
        profiles = [p for p in profiles if p.lens not in exclude]
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
        return route_question(
            profiles, embedder, decider, question, scorer=scorer, known_sources=known_sources
        )
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
    # Why no governed answer came back, in the failure-mode vocabulary:
    # ``unroutable`` (no lens the caller may use covers it), ``provider_down``
    # (the decider or the router failed — read ``degraded`` for which).
    mode: FailureMode | None = None
    # Set when the decline is an OUTAGE (DECIDER_DOWN / ROUTER_DOWN), not a
    # coverage signal — callers and the surface report treat the two differently.
    degraded: str | None = None


class DriverRouteAnswer(driver.DriverRoute):
    """route_query for a driver caller (``services/serve/driver.py``), and,
    in a catalogue mixing Cube-backed and typed lenses, today's answer from
    the typed lenses under ``routed`` when no Cube lens covers the question."""

    routed: RoutedAnswer | UncoveredEnvelope | None = None


def _persist(
    caller: CallerIdentity,
    question: str,
    lens: str | None,
    score: float,
    covered: bool,
    degraded: str | None = None,
    decision: RouteDecision | None = None,
    request_id: str | None = None,
) -> str:
    """Record the routing decision off the deny/error paths (best-effort) and
    return its id — a generated one when the row could not be written, so the
    caller always has a handle. ``request_id``: the served answer's id on the
    second hop, the join observe uses to count the decider's spend once."""
    try:
        with org_session(caller.org_id) as session:
            return route_store.record(
                session,
                question,
                lens,
                score,
                covered,
                degraded=degraded,
                decision=_decision_record(decision, caller, request_id),
            ).id
    except Exception:
        log.exception("routing-decision persist failed")
        return "rtd_" + uuid.uuid4().hex[:16]


def _decision_record(
    d: RouteDecision | None, caller: CallerIdentity, request_id: str | None
) -> dict[str, object] | None:
    """The measured half of a route, as the row stores it — None when the
    decider was not consulted (cosine paths, outages) and no lens was tried.
    The chain of lenses tried (``RouteDecision.chain``) rides it with what
    decided the route.

    The decider's spend rides it on every path (tokens_in, tokens_out,
    cost_usd; cost None when unpriced), with who asked (caller, agent) and,
    on the second hop, the answer's ``request_id``. A route that reaches a
    request_log row is charged there; a decline or a refusal reaches none,
    and observe sums the spend from here instead. A decider outage decides
    nothing, but what it billed before failing is stored the same way."""
    if d is None:
        return None
    m = d.decision
    spend = routing_spend([*([(m.provider, m.usage)] if m is not None else []), *d.spent])
    record: dict[str, object]
    if m is not None:
        record = {
            "provider": m.provider,
            "chosen": m.chosen,
            "p": m.p,
            "runner_up": m.runner_up,
            "margin": m.margin,
            "verdict": d.verdict,
        }
    elif spend is not None:
        record = {"provider": d.spent[0][0]}
    elif d.chain:
        record = {}
    else:
        return None
    if d.chain:
        # Which lenses were tried, in order, and why each was passed over.
        record.update(decided_by=d.decided_by, chain=d.chain)
    # The decline's failure mode, on the record: an outage is provider_down,
    # a question no lens covers is unroutable; a lens passed over for data it
    # did not hold is misrouted (the chain names it).
    if d.degraded:
        record["mode"] = str(FailureMode.provider_down)
    elif not d.covered:
        record["mode"] = str(FailureMode.unroutable)
    elif any(c.get("result") == "refused" for c in d.chain):
        record["mode"] = str(FailureMode.misrouted)
    if spend is not None:
        record.update(
            tokens_in=spend[0],
            tokens_out=spend[1],
            # The decider's prompt-cache reads and writes, from its calls'
            # usage records: persisted before any serve, so they are the
            # routing calls alone.
            cost_usd=cost.add_cost(spend[2], ai_usage.surcharge(ai_usage.current())),
            caller=caller.name,
            agent=caller.agent,
            request_id=request_id,
        )
    return record


@router.post("/query", response_model=RoutedAnswer | UncoveredEnvelope | DriverRouteAnswer)
async def just_ask(
    body: JustAskBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> RoutedAnswer | UncoveredEnvelope | DriverRouteAnswer:
    """Ask without naming a lens (see ``_just_ask``). Every provider call the
    question made, the routing decider's and the routed lens's, is written
    to ai_call under the id the caller got back: the answer's request id, or
    the routing record's when no lens served it.

    Driver mode (an agent caller) applies to the lenses served through Cube:
    those are routed on deterministic coverage with no model called, and the
    response lists them with why (``DriverRouteAnswer``). Lenses on the
    typed core keep today's path, decider included: with none served through
    Cube the response is exactly today's; in a mixed catalogue a question no
    Cube lens covers is routed among the typed lenses as today and returned
    under ``routed``. ``no_model`` states per lens whether dst calls a model."""
    exclude: frozenset[str] = frozenset()
    if caller.driver:
        with ai_usage.no_models():
            plan, exclude = await asyncio.to_thread(_driver_route, caller, body.q)
        if plan is not None:
            return plan
    ref: dict[str, str] = {}
    with ai_usage.collecting() as calls:
        try:
            out = await _just_ask(body, background, caller, ref, exclude)
            if exclude:
                # A mixed catalogue: the typed lenses' answer, the Cube lenses
                # named as the ones dst would have called no model for.
                return DriverRouteAnswer(
                    covered=out.covered,
                    request_id=out.answer.request_id
                    if isinstance(out, RoutedAnswer)
                    else out.request_id,
                    reason="no lens served through Cube covers the question; the lenses on "
                    "the typed core routed it as they do without driver mode",
                    no_model=_no_model_by_lens(caller),
                    routed=out,
                )
            return out
        finally:
            background.add_task(
                ai_usage.write,
                caller.org_id,
                ref.get("id") or "rtd_" + uuid.uuid4().hex[:16],
                list(calls),
                lens=ref.get("lens"),
                caller=caller.name,
                agent=caller.agent,
            )


async def _just_ask(
    body: JustAskBody,
    background: BackgroundTasks,
    caller: CallerIdentity,
    ref: dict[str, str],
    exclude: frozenset[str] = frozenset(),
) -> RoutedAnswer | UncoveredEnvelope:
    """Ask without naming a lens. The router routes to the covering lens (second hop,
    governed, provenance-stamped) or declines with the uncovered envelope. A routed
    lens's own refusal rides the envelope as ``answer.status == "refused"`` with the
    reason in ``answer.answer`` — the direct door's ``status``/``answer``, one level
    down. A routed lens that refuses for data it does not hold hands the question
    to the next lens that covers it (``RouteDecision.fallbacks``), its refusal
    recorded on the routing row. A decline the router would have made where a
    lens the caller may use declares the named term not computable returns that
    lens's refusal the same way (``decided_by == "refusal"``); otherwise a lens
    exposing tables, when the caller may read it untyped, answers from them
    (``decided_by == "explore"``); otherwise the uncovered envelope."""
    # Off the event loop: routing makes blocking embedder + decider calls over sync httpx.
    if exclude:
        decision = await asyncio.to_thread(_route, caller, body.q, exclude)
    else:
        decision = await asyncio.to_thread(_route, caller, body.q)

    if decision.covered and decision.lens is not None:
        # Persist BEFORE the second hop: the routing decision happened regardless of
        # whether the routed lens's query then succeeds — persisting after meant a
        # routed-then-errored request left no row and was invisible to surface area.
        # The second hop's request id, on the routing row: the join that
        # tells observe the decider's spend was charged to that answer's row.
        rid = "req_" + uuid.uuid4().hex[:16]
        ref.update(id=rid, lens=decision.lens)
        decision_id = _persist(
            caller, body.q, decision.lens, decision.score, True, decision=decision, request_id=rid
        )
        # The routed lens declares it is not for this ask: its own refusal,
        # naming the sibling, before any serve (the lens's door runs the same
        # rail). The sibling it names, then the router's fallbacks, take the
        # question; with none to take it, the refusal is the answer.
        declared = await asyncio.to_thread(_not_for_named, body.q, caller, decision.lens)
        order = None
        if declared is not None:
            order = await asyncio.to_thread(
                _next_lenses, caller, body.q, decision.lens, decision.fallbacks
            )
            if not order:
                ref["id"] = decision_id
                return RoutedAnswer(
                    routed_to=RouteProvenance(
                        lens=decision.lens, score=decision.score, decided_by=decision.decided_by
                    ),
                    answer=QueryResponse(
                        lens=decision.lens,
                        status="refused",
                        answer=declared[1],
                        mode=FailureMode.misrouted,
                        request_id=decision_id,
                    ),
                )
            decision = replace(
                decision,
                chain=[
                    *decision.chain,
                    {
                        "lens": decision.lens,
                        "result": "refused",
                        "why": declared[1],
                        "mode": str(FailureMode.misrouted),
                    },
                ],
            )
        # Second hop: the routed lens's full governed pipeline (allow-list, rate
        # limit, certified definitions, certified answers, guard, trace) — like a direct call.
        # Off the event loop too: the second hop blocks exactly as long as the first
        # (generation + warehouse over sync httpx).
        lens, answer = await _serve_in_order(
            body, background, caller, decision, rid, decision_id, order
        )
        ref.update(id=answer.request_id, lens=lens)
        return RoutedAnswer(
            routed_to=RouteProvenance(
                lens=lens,
                score=dict(decision.alternatives).get(lens, decision.score),
                decided_by=decision.decided_by if lens == decision.lens else "fallback",
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
    # A declared not-computable term is the lens saying "not here" (the
    # layer lacks it); a `not_for` ask is the lens pointing at the sibling
    # that holds it (the router chose none).
    mode = FailureMode.not_declared
    if refusal is None:
        refusal = await asyncio.to_thread(_not_for_named, body.q, caller, None)
        mode = FailureMode.misrouted
    if refusal is not None and mode == FailureMode.misrouted:
        # The lens that is not for this ask names the one that is: that
        # lens takes the question when the caller may use it.
        order = await asyncio.to_thread(_next_lenses, caller, body.q, refusal[0], [])
        if order:
            chain = [
                *decision.chain,
                {
                    "lens": refusal[0],
                    "result": "refused",
                    "why": refusal[1],
                    "mode": str(FailureMode.misrouted),
                },
            ]
            sent = replace(decision, covered=True, lens=order[0], decided_by="not_for", chain=chain)
            rid = "req_" + uuid.uuid4().hex[:16]
            ref.update(id=rid, lens=order[0])
            decision_id = _persist(
                caller, body.q, order[0], 0.0, True, decision=sent, request_id=rid
            )
            lens, answer = await _serve_in_order(
                body, background, caller, sent, rid, decision_id, order
            )
            ref.update(id=answer.request_id, lens=lens)
            return RoutedAnswer(
                routed_to=RouteProvenance(lens=lens, score=0.0, decided_by="not_for"),
                answer=answer,
            )
    if refusal is not None:
        lens, wording = refusal
        decision_id = _persist(caller, body.q, lens, decision.score, True, decision=decision)
        ref.update(id=decision_id, lens=lens)
        return RoutedAnswer(
            routed_to=RouteProvenance(lens=lens, score=decision.score, decided_by="refusal"),
            answer=QueryResponse(
                lens=lens, status="refused", answer=wording, mode=mode, request_id=decision_id
            ),
        )
    # No governed lens covers it: the explore scope, when the caller may use
    # one — a lens's exposed tables, read untyped and described by their
    # blueprints, every answer marked EXPOSED. Never on an outage, never in
    # place of a question asked back (which lens, which meaning).
    explore = None
    if decision.degraded is None and decision.verdict != "clarify":
        explore = await asyncio.to_thread(_explore_lens, body.q, caller, body.allow_untyped)
        if explore in exclude:
            explore = None
    if explore is not None:
        chain = [*decision.chain, {"lens": explore, "result": "explore", "why": decision.reason}]
        explored = replace(decision, chain=chain, decided_by="explore")
        rid = "req_" + uuid.uuid4().hex[:16]
        ref.update(id=rid, lens=explore)
        _persist(caller, body.q, explore, 0.0, False, decision=explored, request_id=rid)
        answer = await asyncio.to_thread(
            run_lens_query,
            explore,
            body.q,
            caller,
            background,
            body.format,
            body.bindings,
            body.allow_untyped,
            body.purpose,
            request_id=rid,
        )
        return RoutedAnswer(
            routed_to=RouteProvenance(lens=explore, score=0.0, decided_by="explore"),
            answer=answer,
        )
    decision_id = _persist(
        caller, body.q, None, decision.score, False, degraded=decision.degraded, decision=decision
    )
    ref["id"] = decision_id
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
        mode=FailureMode.provider_down if decision.degraded else FailureMode.unroutable,
    )


# The refusals that say "not this lens" rather than "not computable": the ask
# is another lens's (not_for, naming it), or names a member outside the lens's
# views. A question passed over for one of them goes to the next lens.
_NOT_THIS_LENS = (FailureMode.misrouted, FailureMode.scope_violation)


def _refused_for_data(answer: QueryResponse | ConsumerAnswer) -> bool:
    """The lens refused because it does not hold what was asked — the lane's
    confident decline, an ask it declares is another lens's, a member outside
    its views — never a declared not-computable term, an outage or a question
    asked back."""
    if answer.status != "refused":
        return False
    return answer.answer.startswith(LENS_DATA_REFUSAL) or (
        getattr(answer, "mode", None) in _NOT_THIS_LENS
    )


def _next_lenses(
    caller: CallerIdentity, question: str, refused: str | None, fallbacks: list[str]
) -> list[str]:
    """Where a question goes after ``refused`` declines it as another lens's:
    the sibling its ``not_for`` names, then ``fallbacks``; only published
    lenses the caller may use, ``refused`` never, at most ``COVERAGE_K``."""
    try:
        bundles = {name: b for name, _d, _x, b in list_published_for_org(caller.org_id)}
    except Exception:
        log.exception("fallback lookup failed")
        return []
    sibling = None
    if refused in bundles:
        hit = not_for_hit(question, bundles[refused].semantic_model)
        sibling = hit.sibling.strip() if hit is not None else None
    out: list[str] = []
    for name in [sibling, *fallbacks]:
        if not name or name == refused or name in out or name not in bundles:
            continue
        if caller.driver and _cube_lens_model(bundles[name]) is not None:
            continue  # a driver's Cube lenses are routed by coverage, never served a question
        if authorize(caller, bundles[name].config)[0]:
            out.append(name)
    return out[:COVERAGE_K]


async def _serve_in_order(
    body: JustAskBody,
    background: BackgroundTasks,
    caller: CallerIdentity,
    decision: RouteDecision,
    rid: str,
    decision_id: str,
    order: list[str] | None = None,
) -> tuple[str, QueryResponse | ConsumerAnswer]:
    """The routed lens's answer, or, while a lens refuses for data it does not
    hold or as another lens's ask, the next one: the sibling its ``not_for``
    names, then the covering lenses (``decision.fallbacks``), at most
    ``COVERAGE_K`` lenses in all. ``order``: the lenses to try instead of the
    routed one and its fallbacks. Decisions repeated across the lenses are
    made once (``decision_memo``). The routing row is amended with the lens
    that answered and each refusal passed over."""
    lenses = list(order) if order else [decision.lens or "", *decision.fallbacks]
    chain = list(decision.chain)
    tried: set[str] = set()
    with decision_memo.shared():
        i = 0
        while i < len(lenses):
            lens = lenses[i]
            tried.add(lens)
            if i == 0:
                answer = await asyncio.to_thread(
                    run_lens_query,
                    lens,
                    body.q,
                    caller,
                    background,
                    body.format,
                    body.bindings,
                    body.allow_untyped,
                    body.purpose,
                    routing=decision.decision,
                    request_id=rid,
                )
            else:
                # Its own collection: the row is charged only its own calls'
                # cache tokens, not the lens's before it.
                answer = await asyncio.to_thread(
                    _run_collected,
                    lens,
                    body.q,
                    caller,
                    background,
                    body.format,
                    body.bindings,
                    body.allow_untyped,
                    body.purpose,
                )
            if _refused_for_data(answer) and len(tried) < COVERAGE_K:
                if getattr(answer, "mode", None) == FailureMode.misrouted:
                    # The lens named the sibling that answers it: that one next.
                    named = await asyncio.to_thread(_next_lenses, caller, body.q, lens, [])
                    lenses[i + 1 : i + 1] = [n for n in named if n not in tried]
                    lenses = list(dict.fromkeys(lenses))
                if i + 1 < len(lenses):
                    # The routed lens did not hold it: the misroute, on the record.
                    chain.append(
                        {
                            "lens": lens,
                            "result": "refused",
                            "why": answer.answer,
                            "mode": str(FailureMode.misrouted),
                        }
                    )
                    i += 1
                    continue
            break
    if lens != decision.lens:
        chain.append({"lens": lens, "result": "answered", "why": "the next lens covering it"})
        _amend(caller, decision_id, lens, replace(decision, chain=chain), answer.request_id)
    return lens, answer


def _run_collected(
    lens: str,
    q: str,
    caller: CallerIdentity,
    background: BackgroundTasks,
    fmt: AnswerFormat,
    bindings: dict[str, str],
    allow_untyped: bool | None,
    purpose: str,
) -> QueryResponse | ConsumerAnswer:
    with ai_usage.collecting():
        return run_lens_query(lens, q, caller, background, fmt, bindings, allow_untyped, purpose)


def _amend(
    caller: CallerIdentity, decision_id: str, lens: str, decision: RouteDecision, request_id: str
) -> None:
    """Point the routing row at the lens that answered, with the chain that got there."""
    try:
        with org_session(caller.org_id) as session:
            route_store.amend(
                session, decision_id, lens, _decision_record(decision, caller, request_id)
            )
    except Exception:
        log.exception("routing-decision amend failed")


def _explore_lens(question: str, caller: CallerIdentity, allow_untyped: bool | None) -> str | None:
    """The lens whose exposed tables hold most of the question's words, among
    the lenses this caller may use that expose tables and answer untyped
    (the lens's ``untyped_fallback``, or the caller's ``allow_untyped``); the
    first by name on a tie. None when no such lens holds any word of it."""
    try:
        bundles = list_published_for_org(caller.org_id)
    except Exception:
        log.exception("explore lookup failed")
        return None
    words = asked_words(question)
    best: tuple[int, str] | None = None
    for name, _display, _desc, bundle in sorted(bundles, key=lambda b: b[0]):
        if not effective_allow_untyped(bundle.config, allow_untyped):
            continue
        exposed = [e for e in bundle.semantic_model.entities if e.exposed]
        if not exposed or not authorize(caller, bundle.config)[0]:
            continue
        held = vocabulary(
            t
            for e in exposed
            for t in (e.name, e.source.table, e.description or "", *(f.name for f in e.fields))
        )
        hits = sum(1 for w in words if word_keys(w) & held)
        if hits and (best is None or hits > best[0]):
            best = (hits, name)
    return best[1] if best else None


# ── driver mode ─────────────────────────────────────────────────────────────

# Exposed tables listed in one explore kit; the rest are counted.
EXPLORE_TABLES_MAX = 40
# Beside covering lenses: the exposed tables holding the question as well.
EXPLORE_BESIDE_MAX = 5


def _driver_route(
    caller: CallerIdentity, question: str
) -> tuple[DriverRouteAnswer | None, frozenset[str]]:
    """Route for an agent caller over the lenses served through Cube:
    deterministic coverage only, never the decider, never a model. The Cube
    lenses holding every word of the question some lens declares are the
    candidates (several: the driver chooses), each with the members and
    values that carry its words; typed lenses covering it are listed beside
    them (``no_model: false``). A Cube lens's declared refusal (a
    not-computable term, an ask it says is another's) is the answer when no
    other lens covers the question.

    ``(None, frozenset())`` when no Cube lens is in play: today's path.
    ``(None, <the Cube lenses>)`` when none covers it and typed lenses
    exist: today's path over the typed lenses. Otherwise the route, and with
    nothing covering it the nearest lenses and the explore kit."""
    profiles, known = _accessible_profiles(caller)
    bundles = {
        name: b
        for name, _d, _x, b in list_published_for_org(caller.org_id)
        if authorize(caller, b.config)[0]
    }
    cubes = frozenset(n for n, b in bundles.items() if _cube_lens_model(b) is not None)
    if not cubes:
        return None, frozenset()
    no_model = {n: n in cubes for n in sorted(bundles)}
    held = held_by(profiles)
    by_lens = {p.lens: p for p in profiles}
    chain: list[dict[str, str]] = []
    reason = ""
    kept = drop_not_for(question, profiles)
    for p in profiles:
        if p not in kept:
            chain.append(
                {"lens": p.lens, "result": "passed over", "why": "its not_for names the ask"}
            )
    lacking: dict[str, list[str]] = {}
    owner = certified_owner(question, kept)
    covering: list[str]
    if owner is not None:
        covering, why = [owner], {owner: "the question is its certified question"}
    else:
        kept, uncovered = by_sources(question, kept, known)
        if uncovered is not None:
            reason = uncovered
        lacking = {p.lens: unused_words(question, p, held) for p in kept}
        # A lens covers a question it holds some word of and lacks none of:
        # a question of words no lens declares is covered by none.
        holds = {
            p.lens
            for p in kept
            if p.vocabulary is not None
            and any(word_keys(w) & p.vocabulary for w in asked_words(question))
        }
        covering = sorted(n for n, words in lacking.items() if not words and n in holds)
        why = {n: "holds every word of the question" for n in covering}
    typed_covering = [n for n in covering if n not in cubes]
    covering = [n for n in covering if n in cubes]
    declared = _declared_refusals(question, {n: bundles[n] for n in cubes})
    refusal: tuple[str, str] | None = None
    refusal_mode = FailureMode.not_declared
    for name in list(covering):
        hit = declared.get(name)
        if hit is not None and hit[0] == FailureMode.not_declared:
            # It holds the term only to refuse it: another covering lens answers.
            covering.remove(name)
            chain.append({"lens": name, "result": "refused", "why": hit[1]})
            refusal = refusal or (name, hit[1])
    if not covering:
        # A lens saying this ask is another lens's: that lens takes it, when
        # the caller may use it; else the sentence is the answer.
        for name, (mode_, wording, sibling) in sorted(declared.items()):
            if mode_ != FailureMode.misrouted:
                continue
            chain.append({"lens": name, "result": "refused", "why": wording})
            if sibling in cubes and sibling not in covering:
                covering.append(sibling)
                why[sibling] = f"{name} says this ask is this lens's"
        if not covering and refusal is None:
            # A not-computable term first, then an ask that is another lens's.
            ranked = sorted(
                declared.items(), key=lambda x: (x[1][0] != FailureMode.not_declared, x[0])
            )
            if ranked:
                name, (refusal_mode, wording, _sibling) = ranked[0]
                refusal = (name, wording)

    # Coverage counts a word met anywhere the lens holds it, prose included,
    # so a lens whose description mentions a word covers it. Rank on the
    # words each lens NAMES (members, titles, aliases, values): a lens naming
    # as many as the best covering one is a candidate too, its missing words
    # listed; one naming none of them drops beside one that names some; the
    # top few are returned, each with its score.
    def named(n: str) -> int:
        p = by_lens.get(n)
        d = p.declared if p is not None else None
        return sum(1 for w in asked_words(question) if d is not None and word_keys(w) & d)

    if covering and owner is None:
        best = max(named(n) for n in covering)
        if best > 0:
            for n in sorted(lacking):
                hit = declared.get(n)
                if (
                    n in cubes
                    and n not in covering
                    and named(n) >= best
                    and not (hit is not None and hit[0] == FailureMode.not_declared)
                ):
                    covering.append(n)
                    why[n] = (
                        "names as many of the question's words as the lenses covering it; "
                        f"has nothing for {', '.join(repr(w) for w in lacking[n])}"
                    )
            covering = [n for n in covering if named(n) > 0]
        covering = _top(covering, named)
    typed = frozenset(bundles) - cubes
    if not covering and refusal is None and typed:
        return None, cubes
    asked = [w for w in asked_words(question) if held is None or word_keys(w) & held]
    nearest = (
        _top(
            [n for n, words in lacking.items() if n in cubes and words and len(words) < len(asked)],
            lambda n: -len(lacking[n]),
        )
        if not covering
        else []
    )

    def candidate(name: str, covers: bool, because: str) -> driver.DriverCandidate:
        bundle = bundles[name]
        model = _cube_lens_model(bundle)
        if model is not None:
            matched = driver.word_targets(
                question, model, _cube_probe().dictionaries, sorted(caller.groups)
            )
        else:
            vocab = by_lens[name].vocabulary if name in by_lens else None
            matched = [
                driver.WordMatch(word=w)
                for w in asked_words(question)
                if vocab is not None and word_keys(w) & vocab
            ]
        return driver.DriverCandidate(
            lens=name,
            covers=covers,
            no_model=name in cubes,
            serving=cube_door.serving_core(bundle.config, bundle.semantic_model),
            why=because,
            matched=matched,
            uncovered=lacking.get(name, []),
            score=named(name),
        )

    candidates = sorted(
        (candidate(n, not lacking.get(n), why[n]) for n in covering if n in bundles),
        key=lambda c: (-c.score, -len(c.matched), c.lens),
    )
    beside = [
        candidate(
            n,
            True,
            "holds every word of the question; it serves on the typed core, where dst "
            "reads the question with a model: ask it with query",
        )
        for n in typed_covering
    ]
    near = [
        candidate(n, False, f"has nothing for {', '.join(repr(w) for w in lacking[n])}")
        for n in nearest
        if n in bundles
    ]
    refused = None
    mode: FailureMode | None = None
    if candidates:
        reason = (
            f"{len(candidates)} lenses cover the question: choose one (ranked by score, "
            "the question's words each names)"
            if len(candidates) > 1
            else f"{candidates[0].lens} covers the question"
        )
        for c in candidates:
            chain.append({"lens": c.lens, "result": "candidate", "why": c.why})
    elif refusal is not None:
        mode = refusal_mode
        reason = refusal[1]
    else:
        mode = FailureMode.unroutable
        reason = reason or (
            f"no lens covers every word of this question: the closest, {near[0].lens}, "
            f"{near[0].why}"
            if near
            else "no lens you may use holds the words of this question"
        )
    decision = RouteDecision(
        bool(candidates),
        candidates[0].lens if len(candidates) == 1 else None,
        0.0,
        [],
        reason,
        decided_by="driver",
        chain=chain,
        fallbacks=[c.lens for c in candidates[1:]],
    )
    request_id = _persist(
        caller,
        question,
        decision.lens or (refusal[0] if refusal else None),
        0.0,
        bool(candidates),
        decision=decision,
    )
    if refusal is not None and not candidates:
        refused = QueryResponse(
            lens=refusal[0],
            status="refused",
            answer=refusal[1],
            mode=mode,
            request_id=request_id,
        )
    kit: driver.ExploreKit | None = None
    if not candidates and refused is None:
        kit = _explore_kit(caller, list(bundles.values()), question)
    elif candidates:
        # A covered ask still sees an exposed table holding its words at least
        # as well as the best lens names them: the lens may cover the words
        # and not hold the thing asked (a quota table beside a pipeline lens).
        beside_kit = _explore_kit(
            caller,
            list(bundles.values()),
            question,
            at_least=max(1, max(c.score for c in candidates)),
            limit=EXPLORE_BESIDE_MAX,
        )
        kit = beside_kit if beside_kit.tables else None
    return (
        DriverRouteAnswer(
            covered=bool(candidates),
            request_id=request_id,
            reason=reason,
            choose=len(candidates) + len(beside) > 1,
            candidates=candidates + beside,
            nearest=near,
            refusal=refused,
            explore_kit=kit,
            mode=mode,
            no_model=no_model,
        ),
        cubes,
    )


def _no_model_by_lens(caller: CallerIdentity) -> dict[str, bool]:
    """Per lens the caller may use: does dst call no model for a driver
    there (served through Cube) or read questions with one (typed core)."""
    try:
        bundles = list_published_for_org(caller.org_id)
    except Exception:
        log.exception("lens lookup failed")
        return {}
    return {
        n: _cube_lens_model(b) is not None
        for n, _d, _x, b in sorted(bundles, key=lambda x: x[0])
        if authorize(caller, b.config)[0]
    }


def _declared_refusals(
    question: str, bundles: dict[str, LensBundle]
) -> dict[str, tuple[FailureMode, str, str]]:
    """``lens → (mode, sentence, sibling)`` for each lens that refuses this
    question by declaration, with the rule its own door refuses with: a term
    it declares not computable (``not_declared``), or an ask it says is
    another lens's (``misrouted``, the sibling named). A lens served through
    Cube reads its overlay's entries too, as its lane does."""
    from services.contracts.cube_meta import MetaDoc
    from services.contracts.lens_model import Overlay
    from services.cube.lens_view import CubeLensView
    from services.runtime.not_for import names_ask
    from services.verify.base import Ask
    from services.verify.pre import not_computable, not_for

    out: dict[str, tuple[FailureMode, str, str]] = {}
    for name, bundle in bundles.items():
        sm = bundle.semantic_model
        model = _cube_lens_model(bundle)
        if model is not None:
            view = CubeLensView(doc=MetaDoc(), overlay=Overlay(lenses={name: model.policy}))
            ask = Ask(question=question, lens=view, config=bundle.config)
            nc, nf = not_computable(ask, []), not_for(ask, [])
            if nc.stops:
                out[name] = (FailureMode.not_declared, nc.detail, "")
            elif nf.stops:
                entries = [*bundle.config.not_for, *model.policy.not_for]
                sibling = next((e.sibling for e in entries if names_ask(question, e.ask)), "")
                out[name] = (FailureMode.misrouted, nf.detail, sibling.strip())
            continue
        if wording := deterministic_refusal_not_computable(question, sm):
            out[name] = (FailureMode.not_declared, wording, "")
        elif (hit := not_for_hit(question, sm)) is not None:
            out[name] = (
                FailureMode.misrouted,
                not_for_refusal(question, sm) or "",
                hit.sibling.strip(),
            )
    return out


def _top(names: list[str], score: Callable[[str], int]) -> list[str]:
    """The ``COVERAGE_K`` best by ``score``, highest first, then by name — and
    every name tied with the last one kept: a tie is not a ranking, and a cut
    by name inside one dropped a lens scoring as well as those offered."""
    ranked = sorted(names, key=lambda n: (-score(n), n))
    if len(ranked) <= COVERAGE_K:
        return ranked
    floor = score(ranked[COVERAGE_K - 1])
    return [n for n in ranked if score(n) >= floor]


def _explore_kit(
    caller: CallerIdentity,
    bundles: list[LensBundle],
    question: str = "",
    *,
    at_least: int = 0,
    limit: int = EXPLORE_TABLES_MAX,
) -> driver.ExploreKit:
    """The exposed tables of the lenses this caller may use, each with its
    blueprint's grain, key and caveats, and the tools that read them:
    ranked by how many of the question's words each carries, the first
    ``limit`` listed and the rest counted. ``at_least``: beside covering
    lenses, only tables carrying that many of the question's words."""
    from services.lenses.describe import _exposed_info
    from services.runtime import assembly

    ranked: list[tuple[int, str, str, driver.ExploreTable]] = []
    for bundle in sorted(bundles, key=lambda b: b.config.name):
        sm = bundle.semantic_model
        exposed = [e for e in sm.entities if e.exposed]
        if not exposed:
            continue
        try:
            profiles, _as_of, age, _stamps = assembly.profile_facts(bundle, caller.org_id)
        except Exception:  # noqa: BLE001 — the kit lists the tables without facts
            log.exception("profile facts unavailable for the explore kit")
            profiles, age = [], None
        by_table = {p.table: p for p in profiles}
        for e in exposed:
            info = _exposed_info(e, sm, by_table, age is None or not age.stale)
            caveats = (info.blueprint or {}).get("caveats")
            matched = driver.table_words(
                question,
                [
                    info.table,
                    e.description or "",
                    info.grain or "",
                    *(f.name for f in e.fields),
                    *(f.description or "" for f in e.fields),
                ],
            )
            table = driver.ExploreTable(
                lens=bundle.config.name,
                table=info.table,
                grain=info.grain,
                key=info.key,
                versions=info.versions,
                rows=info.rows,
                caveats=[str(c) for c in caveats] if isinstance(caveats, list) else [],
                flags=info.flags,
                matched=matched,
            )
            if len(matched) < at_least:
                continue
            ranked.append((-len(matched), bundle.config.name, info.table, table))
    ranked.sort(key=lambda r: r[:3])
    tables = [r[3] for r in ranked[:limit]]
    note = driver.EXPLORE_BESIDE_NOTE if at_least else driver.EXPLORE_NOTE
    return driver.ExploreKit(
        tables=tables,
        tools=list(driver.EXPLORE_TOOLS) if tables else [],
        note=note if tables else "no lens you may use exposes a table",
        elided=len(ranked) - len(tables),
    )
