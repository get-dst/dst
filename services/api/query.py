"""Data-plane query endpoint: POST /v1/lenses/{name}/query.

Caller-authenticated (API key, or admin token as superuser). Enforces the lens's
allow-list (deny-by-default), audits the decision, runs the governed pipeline, and
logs the full trace off the response path.
"""

from __future__ import annotations

import contextlib
import logging
import re
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from services.api.llm import require_llm
from services.auth.deps import get_caller
from services.certify import binding as certify_binding
from services.certify import store as certify_store
from services.certify.store import CertifiedAnswer
from services.config import settings
from services.contracts.correction import CorrectionDelta
from services.contracts.lens_config import LensConfig
from services.contracts.protocols import Connector, Decision, DecisionUsage, SharedSnapshot
from services.contracts.query_intent import QueryIntent
from services.contracts.resolution import DecisionRecord
from services.contracts.response import (
    CertifiedProvenance,
    ClarificationRequest,
    ConsumerAnswer,
    QueryResponse,
)
from services.contracts.trace import TraceLog
from services.db.session import org_session
from services.governance import audit, drift_watch, quota, ratelimit
from services.governance.credentials import CallerIdentity
from services.governance.policy import authorize
from services.lenses import profile_enrich, profile_store
from services.lenses import store as lens_store
from services.lenses.connections import resolve_connector
from services.lenses.describe import (
    LensDescription,
    MetricCatalog,
    catalog_of,
    describe_model,
    metric_tree,
)
from services.lenses.store import LensBundle, list_published_for_org, load_published_lens
from services.llm.registry import ResolvedModel
from services.observability import cost
from services.observability import usage as ai_usage
from services.observability.logger import log_trace
from services.project import demo_page
from services.reviews import service as reviews_service
from services.reviews import store as reviews_store
from services.runtime import assembly, value_guard
from services.runtime.answer import AnswerComposer, Audience
from services.runtime.attribution import Attribution, attributed
from services.runtime.compiler import CompileError, UnpinnedDimension, compile_intent
from services.runtime.generator import FixedSQLGenerator
from services.runtime.intent_generator import intent_term
from services.runtime.pipeline import (
    PipelineResult,
    deterministic_refusal_not_computable,
    run_query,
)
from services.runtime.typed_resolver import (
    TypedIntentGenerator,
    pin_intent,
    sole_metric_intent,
)

log = logging.getLogger("dst")

# Question-dependent input assembly — retrieval, certified page, profile
# enrichment, exemplar folding, generator tiering — lives in services/runtime/assembly.py.
# The eval suites call the SAME seam, so evals grade the serving pipeline.
# Thin aliases keep the moved pure helpers importable from here (tests use them).
_certified_context = assembly.certified_context
_certified_sql = assembly.certified_sql
_certified_equivalent = assembly.certified_equivalent


def _auto_review_flags(mode: str, response: QueryResponse) -> bool:
    """Does the lens's auto_review policy flag this response into the queue?

    Only served data answers are eligible: refusals/clarifications/errors carry
    no confidence grade, and certified answers are the trust ceiling — a human
    already approved that SQL.
    """
    if mode == "off" or response.clarification is not None:
        return False
    if response.certification == "certified":
        return False
    if response.confidence == "unverified":
        return True
    return mode == "partial" and response.confidence == "partial"


def _auto_review(org_id: uuid.UUID, request_id: str) -> None:
    """Open an origin='ai' review over a just-served low-confidence answer.

    Queued as a background task AFTER log_trace (they run in order), so the
    request_log row exists when the trace is looked up — from there it is the
    caller path's exact flow (same trace, same judge policy via judge_llm, same
    open_review), just stamped origin='ai'. Check-before-insert: a request that
    already has a ticket — this task re-run, or a caller who beat us to it —
    is never double-ticketed.
    """
    try:
        with org_session(org_id) as session:
            already = session.execute(
                text("SELECT 1 FROM review WHERE request_id = :r LIMIT 1"), {"r": request_id}
            ).first()
            if already is not None:
                return
            trace = reviews_store.get_trace(session, request_id)
            if trace is None:
                return
            llm, judge_model = reviews_service.judge_llm()
            reviews_service.open_review(session, trace, llm, model=judge_model, origin="ai")
    except Exception:
        log.exception("auto-review failed for request %s", request_id)


# The answer SHAPE — and the data plane's single biggest latency knob. Composing the
# English prose dominates a certified request's wall clock (it is nearly all of it),
# and no programmatic consumer needs it: agents grade `data.rows` and touch `answer`
# only as an error string. Asking for `structured` skips the compose call outright,
# which on the certified door is an order-of-magnitude latency drop; on generated
# answers it removes a compose stage measured in seconds.
#   both       prose + data payload (the default — no existing caller changes behaviour)
#   structured rows only, zero LLM calls; `data`, `sql`, `citations` and the
#              deterministic `verification` checks all survive, `answer` becomes a
#              one-line marker naming the result shape
#   data       an alias for `structured` (the spelling an agent guesses first)
#   prose      the written answer without the data payload
AnswerFormat = Literal["both", "structured", "data", "prose"]
_ROWS_ONLY = ("structured", "data")
BINDINGS_HELP = (
    "typed values for open slots the question cannot type on its own — "
    "column -> value (a customer name, a number, a YYYY-MM-DD date, a "
    "YYYY-Qn period); the answer's `clarification` of kind `unresolved_slot` "
    "names the slot to bind and its grammar"
)
PURPOSE_MAX = 300
PURPOSE_HELP = (
    "one sentence on what the asker is trying to accomplish — 'building a forecasting "
    "app', 'preparing the board deck'. Optional. Stored on the request's log row only "
    "when the lens sets logging.log_purpose; never changes what a term means"
)
UNTYPED_HELP = (
    "when the question does not type (a slot clarifies), fall to raw-SQL "
    "generation instead — always disclosed with an UNTYPED line and `typed: false` "
    "on its ledger; its tag is what its SQL reads back as. Unset: the lens's "
    "`untyped_fallback` "
    "(default false: ask, don't guess); false demands typed-only"
)
FORMAT_HELP = (
    "answer shape: `both` (default), `structured` (rows only — skips the prose LLM "
    "call entirely, far faster), `data` (alias for `structured`), or `prose`"
)


class QueryBody(BaseModel):
    # A stranger's first call guesses the body shape — and every probe guesses
    # {"question": ...} first, so accept it as an alias instead of teaching
    # via 422 (the 422 still names `q` for anything else).
    q: str = Field(
        validation_alias=AliasChoices("q", "question"),
        description="the natural-language question — the only required field "
        "(accepted under `q` or `question`)",
    )
    format: AnswerFormat = Field(default="both", description=FORMAT_HELP)
    bindings: dict[str, str] = Field(default_factory=dict, description=BINDINGS_HELP)
    allow_untyped: bool | None = Field(default=None, description=UNTYPED_HELP)
    purpose: str = Field(default="", max_length=PURPOSE_MAX, description=PURPOSE_HELP)


router = APIRouter(prefix="/v1", tags=["query"])


def effective_allow_untyped(config: LensConfig, requested: bool | None) -> bool:
    """The caller's choice when it made one; otherwise the lens owner's default.
    An explicit false always demands typed-only, whatever the lens allows."""
    return config.untyped_fallback if requested is None else requested


def _lens_unavailable(name: str) -> HTTPException:
    """ONE response for 'does not exist' and 'exists but not granted to you'.
    A 404-vs-403 split is a reliable existence oracle — any caller key can
    enumerate the org's lens inventory by probing names,
    and lens names describe the business (split_checkout, revrec_oss,
    ae_quota each disclose a program or a GTM motion). The managed router
    already upholds non-disclosure (candidates are scoped BEFORE routing);
    the direct routes now match it. The audit trail keeps the true reason —
    observability keeps the distinction, the wire does not leak it — and the
    detail names every possibility so a legitimately-denied caller still
    knows their next move without a probe learning anything. The body is
    byte-identical for every probed name — not even an echo of the name —
    so two responses can never be diffed against each other."""
    del name
    return HTTPException(
        status_code=404,
        detail=(
            "this lens is not available to this caller — it may not exist, may "
            "not be published, or may not be granted to your key; if you expect "
            "access, ask this org's dst admin to check the lens's allow-list"
        ),
    )


# `[[term]]` cross-references authored into definition bodies.
WIKI_LINK = re.compile(r"\[\[([^\]]+)\]\]")


def _normal(text: str) -> str:
    """Fold a term to its lookup form: authored terms are `net transaction amount`
    in one project and `repeat_customer` in the next, and a caller says either."""
    return re.sub(r"[\s_-]+", " ", text).strip().lower()


class LensListItem(BaseModel):
    name: str
    display_name: str
    description: str


class DefinitionHit(BaseModel):
    """One governed term, and the lens that governs it."""

    lens: str
    term: str
    body: str
    status: str = "active"
    possible_mappings: list[str] = []
    cites: list[str] = []  # [[links]] this body depends on
    matched: Literal["term", "body"] = "term"


class DefinitionLookup(BaseModel):
    q: str
    definitions: list[DefinitionHit]


class CertifiedEntry(BaseModel):
    cert_id: str
    question: str
    sql: str
    certified_by: str
    certified_at: str
    score: float | None = None  # cosine similarity; present only on a `q` search
    # Set ⇒ this entry is a TEMPLATE — running it needs `bindings`
    # for these slots (name -> {type, values?}); sample_bindings show the shape.
    slots: dict[str, Any] | None = None
    sample_bindings: list[dict[str, Any]] | None = None


class CertifiedRunBody(BaseModel):
    """The run-door body — slot values for a template answer."""

    bindings: dict[str, str] = {}
    format: AnswerFormat = Field(default="both", description=FORMAT_HELP)
    purpose: str = Field(default="", max_length=PURPOSE_MAX, description=PURPOSE_HELP)


class MetricsBody(QueryIntent):
    """The structured door's body: a QueryIntent, plus the answer shape.

    Inherits the intent contract verbatim so the wire shape and the compiler's
    input cannot drift apart.
    """

    # Rows by default: a caller reaching for this door wants a number, not prose,
    # and composing costs an LLM round-trip that is most of the latency — on the
    # certified door it is nearly all of it, since nothing else calls a model.
    format: AnswerFormat = Field(default="structured", description=FORMAT_HELP)


class CertifiedLibrary(BaseModel):
    lens: str
    certified: list[CertifiedEntry]
    # The configured embedder's EXACT serve band — bands are
    # embedder-relative, so score-threshold decisions (e.g. MCP's
    # run-by-question resolve) must use this, never a hardcoded 0.95.
    exact_band: float = 0.95


@router.get("/lenses", response_model=list[LensListItem])
def list_lenses_for_caller(
    caller: CallerIdentity = Depends(get_caller),
) -> list[LensListItem]:
    """The published lenses this caller is permitted to use (scoped discovery)."""
    out: list[LensListItem] = []
    for name, display_name, description, bundle in list_published_for_org(caller.org_id):
        ok, _ = authorize(caller, bundle.config)
        if ok:
            out.append(LensListItem(name=name, display_name=display_name, description=description))
    return out


@router.get("/definitions", response_model=DefinitionLookup)
def lookup_definitions(
    q: str,
    caller: CallerIdentity = Depends(get_caller),
) -> DefinitionLookup:
    """Look a governed term up across every lens this caller may use — the READ door.

    Deterministic on purpose: exact term, then term substring, then body substring.
    No embedding, no generation, no warehouse. Top-k retrieval over the same
    definitions does not recall all of them — vector, BM25 and hybrid all miss
    terms a question depends on but never names, which is exactly the governed
    meaning an agent must not silently lose. An index lookup cannot miss, so
    this is one.
    """
    needle = _normal(q)
    if not needle:
        # An empty needle is `in` every term — that would dump the org's whole
        # vocabulary through the read door. Ask for a term.
        raise HTTPException(status_code=422, detail="q must not be empty")
    hits: list[DefinitionHit] = []
    for name, _display, _desc, bundle in list_published_for_org(caller.org_id):
        ok, _ = authorize(caller, bundle.config)
        if not ok:
            continue
        for d in bundle.semantic_model.definitions:
            term = _normal(d.term)
            matched: Literal["term", "body"] | None = None
            if needle == term or needle in term or term in needle:
                matched = "term"
            elif needle in _normal(d.body):
                matched = "body"
            if matched is None:
                continue
            hits.append(
                DefinitionHit(
                    lens=name,
                    term=d.term,
                    body=d.body,
                    status=d.status,
                    possible_mappings=d.possible_mappings,
                    cites=WIKI_LINK.findall(d.body),
                    matched=matched,
                )
            )
    # exact term first, then term substrings, then body mentions — stable within a band
    hits.sort(key=lambda h: (h.matched != "term", _normal(h.term) != needle, h.lens, h.term))
    return DefinitionLookup(q=q, definitions=hits)


@router.get("/lenses/{name}", response_model=LensDescription)
def describe_lens_for_caller(
    name: str,
    caller: CallerIdentity = Depends(get_caller),
) -> LensDescription:
    """A lens's entities, fields, and definitions — so an agent knows what it can ask.

    Goes through `_authorized_bundle` like every other caller door. A hand-rolled
    prologue here would check existence BEFORE authorization (a 404/403 oracle)
    and never reach the rate limiter — leaving the metadata surface both more
    revealing and less throttled than the query surface, and duplicated logic is
    how two paths drift apart."""
    bundle = _authorized_bundle(name, caller)
    sm = bundle.semantic_model
    # The count is search's own predicate counted in SQL, so it can never say
    # 0 while search_certified finds answers; the examples are decoration and
    # may fail on their own without zeroing the count.
    certified_examples: list[str] = []
    with org_session(caller.org_id) as session:
        certified_count = certify_store.count_active(session, name)
        try:
            certified_examples = [e.question for e in active_certified(session, name)[:3]]
        except Exception:
            log.exception("certified example lookup failed for lens %s", name)
    domains, certified, unsampled = _catalog_facts(bundle, caller, name)
    return describe_model(
        name,
        display_name=bundle.config.display_name,
        description=bundle.config.description,
        model=sm,
        certified_count=certified_count,
        certified_examples=certified_examples,
        domains=domains,
        certified=certified,
        unsampled=unsampled,
    )


def _catalog_facts(
    bundle: LensBundle, caller: CallerIdentity, name: str
) -> tuple[dict[str, dict[str, list[str]]], list[tuple[str, list[str]]], dict[str, str]]:
    """What the metric catalog reads besides the model: each entity's complete
    value dictionaries from the stored profile (none when the profile is past
    probe.max_age: a stale dictionary proves nothing about today's values),
    and each active certified answer's id with its stored terms. Best-effort,
    like the certified examples: a failed lookup leaves the catalog without
    values or certified links, never without metrics. Third: each entity
    whose table was never sampled, and why (its values cannot bind)."""
    domains: dict[str, dict[str, list[str]]] = {}
    profiles, _as_of, age, _stamps = assembly.profile_facts(bundle, caller.org_id)
    if profiles and (age is None or not age.stale):
        domains = value_guard.entity_domains(bundle.semantic_model, profiles)
    unsampled = value_guard.entity_unsampled(bundle.semantic_model, profiles)
    certified: list[tuple[str, list[str]]] = []
    try:
        with org_session(caller.org_id) as session:
            certified = [(a.id, list(a.terms or [])) for a in active_certified(session, name)]
    except Exception:
        log.exception("certified terms lookup failed for lens %s", name)
    return domains, certified, unsampled


@router.get("/lenses/{name}/catalog", response_model=MetricCatalog)
def metric_catalog_for_caller(
    name: str,
    caller: CallerIdentity = Depends(get_caller),
) -> MetricCatalog:
    """The lens's metric catalog alone: metrics, dimensions, and the tree the
    metrics' dependencies form — what an app needs to render a metric tree in
    one call. Same authorization as describe; no model call, no warehouse."""
    bundle = _authorized_bundle(name, caller)
    domains, certified, _unsampled = _catalog_facts(bundle, caller, name)
    metrics, dimensions = catalog_of(bundle.semantic_model, domains=domains, certified=certified)
    return MetricCatalog(
        lens=name, metrics=metrics, dimensions=dimensions, tree=metric_tree(metrics)
    )


def _authorized_bundle(
    name: str, caller: CallerIdentity, request_id: str | None = None
) -> LensBundle:
    """The governed prologue every data-plane call shares: resolve the published lens,
    enforce the deny-by-default allow-list (audited), and apply the per-caller rate
    limit. Raises HTTPException (404/403/429) on the deny paths.

    `request_id`, when the caller generated one up front, joins each audit row to the
    `request_log` row of the same query — including the deny rows, which have no
    request_log row but stay null-joinable and honest."""

    def _audit(decision: str, reason: str | None) -> None:
        audit.record(
            caller.org_id,
            caller.name,
            name,
            decision,
            reason,
            request_id=request_id,
            agent=caller.agent,
        )

    bundle = load_published_lens(caller.org_id, name)
    if bundle is None:
        # Audit synchronously — the deny/error path raises, so a background task wouldn't run.
        _audit("deny", "lens not found")
        raise _lens_unavailable(name)

    ok, reason = authorize(caller, bundle.config)
    _audit("allow" if ok else "deny", reason)
    if not ok:
        # Same body and status as absent — see _lens_unavailable; the audit row
        # above keeps the true reason.
        raise _lens_unavailable(name)

    # Per-caller rate limit (admins exempt). Deny-by-429 before any expensive work.
    if not caller.is_admin:
        rl_key = f"{caller.org_id}:{name}:{caller.name}"
        if not ratelimit.check(rl_key, bundle.config.rate_limit.per_caller_rpm):
            _audit("deny", "rate limit exceeded")
            raise HTTPException(
                status_code=429,
                detail=f"rate limit exceeded for caller '{caller.name}' on lens '{name}'",
                headers={"Retry-After": str(ratelimit.retry_after(rl_key))},
            )
        # Daily quotas, same shape: the caller's own on this lens, then the org's
        # kill switch. Both count served answers, so this deny costs no budget.
        rpd = bundle.config.rate_limit.per_caller_rpd
        if rpd > 0:
            use = quota.usage(caller.org_id, caller=caller.name, lens=name)
            if quota.exceeded(rpd, use):
                _audit("deny", "daily quota exceeded")
                raise HTTPException(
                    status_code=429,
                    detail=f"daily quota of {rpd} answers reached for caller "
                    f"'{caller.name}' on lens '{name}'",
                    headers={"Retry-After": str(use.retry_after())},
                )
        cap = settings.daily_request_cap
        if cap > 0:
            use = quota.usage(caller.org_id)
            if quota.exceeded(cap, use):
                _audit("deny", "daily request cap exceeded")
                raise HTTPException(
                    status_code=429,
                    detail=f"this deployment's daily cap of {cap} answers is reached",
                    headers={"Retry-After": str(use.retry_after())},
                )
    return bundle


def _authorized_connector(bundle: LensBundle, caller: CallerIdentity) -> Connector:
    """The lens's connector for THIS caller — the other half of
    `_authorized_bundle`, and the only sanctioned way a caller-scoped door gets one.

    Authorization is where the caller is known, so a door that resolves its own
    connector is a door deciding for itself what the caller may reach: that is how
    `POST /v1/sql` once shipped answering past its lens's grant. One seam means a
    door written next month inherits whatever the gate does, having written none
    of it.

    Raises whatever `resolve_connector` raises; each door maps that to its own
    status code (`/query` 400 "lens connection error", the probe 404 naming the
    fix), which is why the mapping is not done here.
    """
    return resolve_connector(bundle.config.connections[0], caller.org_id, caller=caller)


def _verbatim_question(answer: CertifiedAnswer, question: str) -> bool:
    """Is the question the stored one, word for word — whitespace collapsed,
    case ignored — or a template's own sample-bound rendering of it?"""

    def norm(text: str) -> str:
        return " ".join(text.split()).casefold()

    asked = norm(question)
    if not asked:
        return False
    if asked == norm(answer.question):
        return True
    return bool(answer.slots) and any(
        asked == norm(certify_binding.render_question(answer.question, b))
        for b in answer.sample_bindings or []
    )


def active_certified(session: Session, lens: str) -> list[CertifiedAnswer]:
    """The lens's certified library as callers see it: every answer that is
    not retired. Retired answers are history (mgmt/export only), never served;
    an unembedded one still belongs here — the door by ID works, and matching
    (the thing it cannot do) is a different door. `describe_lens` counts this
    list and `search_certified` lists it, so the two agree by construction."""
    return [
        a for a in certify_store.list_for_lens(session, lens) if certify_store.is_active(a.status)
    ]


def _lenses_carrying(org_id: uuid.UUID | str, asking_lens: str, metric: str) -> list[str]:
    """OTHER published lenses whose compiled model carries *metric* — the
    shape refusal's sibling hint ("lens 'growth' carries conversion_rate").
    Looked up lazily, refusals only; a failed lookup just drops the hint."""
    try:
        return sorted(
            lens_name
            for lens_name, _display, _desc, bundle in list_published_for_org(org_id)
            if lens_name != asking_lens
            and any(m.name == metric for e in bundle.semantic_model.entities for m in e.metrics)
        )
    except Exception:
        log.exception("metric-carrier lookup failed for metric %s", metric)
        return []


def exposed_tables_named(
    question: str, caller: CallerIdentity, *, exclude: str | None = None
) -> list[tuple[str, str]]:
    """``(lens, table)`` pairs for every exposed table the question names word for
    word — the bare name or ``schema.table`` — across the lenses this caller may use,
    *exclude* left out. Deterministic and model-free: an exposed table has no
    definitions, so the router never routes to it and a lens never generates for
    it; naming the lens that holds the grant is the only way a miss can say where
    the data lives. A failed lookup just drops the hint."""
    try:
        bundles = list_published_for_org(caller.org_id)
    except Exception:
        log.exception("exposed-table lookup failed")
        return []
    hits: list[tuple[str, str]] = []
    for lens_name, _display, _desc, bundle in bundles:
        if lens_name == exclude or not authorize(caller, bundle.config)[0]:
            continue
        for entity in bundle.semantic_model.entities:
            if not entity.exposed:
                continue
            table = entity.source.table
            names = {entity.name, table, table.rsplit(".", 1)[-1]}
            if any(
                re.search(rf"(?<![\w.]){re.escape(n)}(?![\w.])", question, re.IGNORECASE)
                for n in names
            ):
                hits.append((lens_name, table))
    return sorted(set(hits))


def lens_refusal_named(question: str, caller: CallerIdentity) -> tuple[str, str] | None:
    """``(lens, refusal)`` when a lens this caller may use declares a term the
    question names `not_computable` — the first such lens by name, and the
    sentence its own door would refuse with. Deterministic (the lens's own
    word-boundary rail); a failed lookup just finds nothing."""
    try:
        bundles = list_published_for_org(caller.org_id)
    except Exception:
        log.exception("refusal lookup failed")
        return None
    for lens_name, _display, _desc, bundle in sorted(bundles, key=lambda b: b[0]):
        if not authorize(caller, bundle.config)[0]:
            continue
        wording = deterministic_refusal_not_computable(question, bundle.semantic_model)
        if wording:
            return lens_name, wording
    return None


def tables_held_elsewhere(
    caller: CallerIdentity | None, *, exclude: str, org_id: uuid.UUID | str | None = None
) -> dict[str, str]:
    """name -> lens, for every entity name and source table the OTHER lenses this
    caller may use declare or expose (*exclude* left out). What a generated
    answer is checked against when the question names a table this lens does
    not read: the clarification can then say which lens holds it. With no
    caller (the publish gate and `dst test`, which ask for nobody in
    particular) every lens of *org_id* counts. A failed lookup just drops the
    hint."""
    org = caller.org_id if caller is not None else org_id
    if org is None:
        return {}
    try:
        bundles = list_published_for_org(org)
    except Exception:
        log.exception("held-table lookup failed")
        return {}
    out: dict[str, str] = {}
    for lens_name, _display, _desc, bundle in bundles:
        if lens_name == exclude or (caller is not None and not authorize(caller, bundle.config)[0]):
            continue
        for entity in bundle.semantic_model.entities:
            for name in (entity.name, entity.source.table):
                out.setdefault(name, lens_name)
    return out


def exposed_hint(hits: list[tuple[str, str]]) -> str:
    """The sentence a miss ends with when an exposed table was named."""
    return "; ".join(
        f"lens '{lens}' exposes {table} (no governed definitions): ask it by name"
        for lens, table in hits
    )


# Causes that are the warehouse's whatever SQL it was sent (a cost cap, a
# refused grant, a timeout, no route): compiled SQL meeting one of these is
# the warehouse's incident like any other.
_WAREHOUSE_SIDE = frozenset(
    drift_watch.cause_class(sample)
    for sample in ("bytes billed", "access denied", "timed out", "connection")
)
_COMPILED_SQL_FAULT = (
    "This question hit a dst defect: the warehouse rejected the SQL dst compiled for "
    "it from the declared layer{matches}. The SQL is dst's own and no data-team ticket "
    "was opened. Asking again compiles the same SQL; report it to whoever runs dst "
    "with request id {request_id}."
)
_SCHEMA_CHANGED = "the warehouse schema changed since the layer was applied"


def _serve_error_backstop(
    result: PipelineResult, *, org_id: uuid.UUID, lens: str, connection: str
) -> None:
    """The first warehouse error becomes the incident, not the eighty-first.

    On a warehouse execution error for a governed query: run the schema diff for
    the lens's connection immediately (against the applied baseline);
    drift found ⇒ mark the lens degraded and file ONE deduplicated ticket
    (origin `serve_error`) per (connection, drift fingerprint); no drift ⇒ the
    ticket dedups on (connection, cause class) instead — an error the reviewer
    never sees is a silent failure whatever its cause turns out to be.

    Either way the caller-facing answer becomes a deterministic template naming
    the cause CLASS and the ticket: the raw engine text stays in the trace,
    never the response (engine shrapnel is not an answer for a business user),
    and no LLM runs on the error path.

    SQL dst compiled from an intent (``result.compiled``) that the applied
    baseline shows no drift under is dst's own fault, whatever the engine
    called it: no ticket, and the answer says the SQL is dst's. With no
    baseline only an error about the SQL's own vocabulary is. Drift counts
    only where it can break the statement (``DriftReport.breaks_sql``)."""
    if result.error_kind != "warehouse":
        return
    cause = drift_watch.cause_class(result.trace.error or "")
    # SQL dst compiled from the declared layer: only the schema diff can say
    # whether the warehouse moved under it (the data team's) or the SQL itself
    # is wrong (dst's), so the diff runs even for a dialect-shaped error.
    compiled = result.compiled and cause not in _WAREHOUSE_SIDE
    if cause == drift_watch.GENERATION_FAULT_CAUSE and not compiled:
        # A dialect-invalid function / bad cast is dst's own defect:
        # no schema diff, no data-team ticket, no degraded mark — the
        # data team cannot action "the mart is fine, the query is wrong". The
        # raw engine text stays in the trace as the generation-quality signal.
        result.response.answer = (
            f"This question hit a dst defect: {cause}. The schema and the data are "
            "fine and no data-team ticket was opened. Re-asking may succeed; if it "
            "repeats, report it to whoever runs dst."
        )
        return
    if cause == drift_watch.CONFIG_LIMIT_CAUSE:
        # A dst-side cost cap is not the data team's incident:
        # no schema diff, no ticket, no degraded mark — the deterministic
        # answer names the cap and the fix, addressed to whoever runs dst.
        result.response.answer = (
            f"This question could not be answered: {cause}. The schema is fine "
            "and no data-team ticket was opened — this is a dst configuration "
            "limit (BigQuery maximum_bytes_billed)."
        )
        return
    ticket_id: str | None = None
    try:
        with org_session(org_id) as session:
            report = (
                drift_watch.check_connection(session, connection, org_id) if connection else None
            )
            if report is not None and not report.breaks_sql(
                result.response.sql or result.trace.sql or ""
            ):
                # Drift that cannot have made the warehouse reject this
                # statement (a rewritten comment, a gained column, a change on
                # a table it does not read) is not its cause: the schema the
                # SQL reads is unchanged, and the lanes below attribute it.
                report = None
            if report is not None:
                ticket, _created = drift_watch.file_ticket(
                    session,
                    report,
                    origin="serve_error",
                    lens=lens,
                    caller=result.trace.caller,
                    request_id=result.response.request_id,
                )
                note = (
                    f"DEGRADED: schema drift on connection '{connection}' — "
                    f"ticket {ticket.ticket_id}; repair the layer and `dst apply` to clear"
                )
                lens_store.set_degraded(session, lens, note)
                for sink in (result.response.degraded, result.trace.degraded):
                    sink.append(note)
                if compiled:
                    cause = _SCHEMA_CHANGED
            elif compiled and (
                (baseline := bool(connection and profile_store.list_profiles(session, connection)))
                or cause == drift_watch.GENERATION_FAULT_CAUSE
            ):
                # With no applied baseline nothing says the schema held, and
                # only an error about the SQL's own vocabulary is still dst's.
                result.response.answer = _COMPILED_SQL_FAULT.format(
                    matches=", and the schema still matches what was applied" if baseline else "",
                    request_id=result.response.request_id,
                )
                return
            else:
                ticket, _created = reviews_store.create_incident_ticket(
                    session,
                    fingerprint=drift_watch.fingerprint(connection or lens, ["serve_error", cause]),
                    request_id=result.response.request_id,
                    lens=lens,
                    caller=result.trace.caller,
                    origin="serve_error",
                    correction=CorrectionDelta(
                        kind="other",
                        note=f"warehouse execution error on lens '{lens}' ({cause}) — "
                        f"raw error preserved in trace {result.response.request_id}",
                    ),
                )
            ticket_id = ticket.ticket_id
    except Exception:
        log.exception("serve-error backstop failed for %s", result.response.request_id)
    tail = (
        f" — ticket {ticket_id} opened for the data team"
        if ticket_id
        else " — a ticket could not be opened; the trace holds the details"
    )
    # Deterministic template, always: even when ticketing itself failed, the raw
    # engine error must not fall through to the caller.
    result.response.answer = f"This question hit a data problem ({cause}){tail}."


def _standing_degraded(result: PipelineResult, *, org_id: uuid.UUID, lens: str) -> None:
    """The persisted degraded mark rides EVERY answer until an apply clears it —
    a standing fault must not depend on the next request also hitting it."""
    try:
        with org_session(org_id) as session:
            note = lens_store.get_degraded(session, lens)
    except Exception:
        log.exception("degraded lookup failed for lens %s", lens)
        return
    if note and note not in result.response.degraded:
        result.response.degraded.append(note)
        result.trace.degraded.append(note)


def _trust_backstops(
    result: PipelineResult, *, org_id: uuid.UUID, lens: str, connection: str
) -> None:
    """The serve-time drift pair, in order: the error backstop may set the mark the
    standing check then reads. Shared by every serving door."""
    _serve_error_backstop(result, org_id=org_id, lens=lens, connection=connection)
    _standing_degraded(result, org_id=org_id, lens=lens)


def _fail_on_warehouse_timeout(result: PipelineResult, caller: CallerIdentity) -> None:
    """A warehouse step that never answered fails the request loud: a 504 naming
    the step, never an `ok` and never a templated data problem (the drift check
    would only ask the same silent warehouse). Logged here, synchronously: FastAPI
    drops a request's BackgroundTasks when an exception replaces the response, and
    the request log must still hold the error."""
    if result.error_kind != "timeout":
        return
    result.trace.agent = caller.agent
    log_trace(result.trace)
    raise HTTPException(
        status_code=504, detail=result.trace.error or "the warehouse did not answer"
    )


def _audience(caller: CallerIdentity) -> Audience:
    """Who this caller's answer is written for — decided once per request, before
    the pipeline runs, and read twice: the composer writes in that register and
    `_shaped` cuts the envelope to it. The same predicate for both, so the
    consumer never gets an engineer's sentence in a consumer's envelope."""
    return "consumer" if demo_page.consumer_view(caller) else "engineer"


def _shaped(
    response: QueryResponse, fmt: AnswerFormat, audience: Audience
) -> QueryResponse | ConsumerAnswer:
    """Apply the response-side half of `format`, and the demo's audience.

    Rows-only is handled upstream (the composer is simply never built, which is where
    the latency goes); `prose` only has to drop the payload the caller declined. A
    public demo serving consumers (`demo.audience: consumer` in its dst.yaml) hands a
    demo caller the ConsumerAnswer: the sentence, its status, a clarification, the
    freshness date — none of the working and none of the trust apparatus (receipt,
    grade, certification, degraded lines, ledger), which belong to the operator and
    only read as noise, or as a warning, to the person the demo serves. The TRACE
    keeps the full result either way — observability is neither the caller's nor
    the page's choice.
    """
    if fmt == "prose":
        response = response.model_copy(update={"data": None})
    if audience == "consumer":
        return ConsumerAnswer.of(response)
    return response


@contextlib.contextmanager
def calls_logged(
    background: BackgroundTasks, caller: CallerIdentity, rid: str, lens: str
) -> Iterator[None]:
    """Every provider call made inside the block is written to ai_call under
    *rid*, off the response path, whatever the block's outcome (a call billed
    before a 504 was still billed). A door that already opened a collection
    (the routed one) writes it under the same id, with its own calls."""
    if ai_usage.active():
        yield
        return
    with ai_usage.collecting() as calls:
        try:
            yield
        finally:
            background.add_task(
                ai_usage.write,
                caller.org_id,
                rid,
                list(calls),
                lens=lens,
                caller=caller.name,
                agent=caller.agent,
            )


def charge_cache(trace: TraceLog) -> None:
    """Add what the request's calls so far billed for prompt-cache tokens to
    its row. Every step charges the row its calls' uncached input and output
    tokens; the cache reads and writes are known only to the provider's
    usage record, so they are charged here, once, from the request's open
    collection (the routing decider's calls included on a routed answer).
    A cache token with no price leaves the row unpriced."""
    trace.ai_cost_usd = cost.add_cost(trace.ai_cost_usd, ai_usage.surcharge(ai_usage.current()))


def run_lens_query(
    name: str,
    q: str,
    caller: CallerIdentity,
    background: BackgroundTasks,
    fmt: AnswerFormat = "both",
    bindings: dict[str, str] | None = None,
    allow_untyped: bool | None = None,
    purpose: str = "",
    routing: Decision | None = None,
    request_id: str | None = None,
) -> QueryResponse | ConsumerAnswer:
    """Authorize + run the governed query pipeline for one lens question.

    ``routing``: the measured decision that routed the question here (the
    routed door's), charged to this answer's AI spend. ``request_id``: the id
    the routed door already wrote on its routing_decision row, so observe can
    tell a routing spend charged here from one that reached no request_log row.

    request_id_generated_here — the join key. Generated up front so the audit rows
    written inside `_authorized_bundle` (allow AND the deny paths) carry the same id
    the served answer's `request_log` row will, and the two are one join.

    The shared core behind every caller surface (REST, MCP, OpenAI-compatible): it
    resolves the lens, enforces the deny-by-default allow-list and per-caller rate
    limit, runs ground → guard → execute → compose, and logs the trace off the response
    path. Raises HTTPException (404/403/429/400/503) on the deny/error paths so each
    surface can map it to its own wire format.

    ``fmt`` shapes the answer, and owning it HERE is why every surface gets the same
    behaviour: rows-only skips the composer (no LLM prose call), prose drops the data
    payload the caller said it did not want.

    FULLY BLOCKING (generation over sync httpx + the warehouse round-trip, seconds to
    minutes) — an async caller MUST dispatch it off the event loop (run_in_threadpool /
    asyncio.to_thread); a sync FastAPI handler already gets that for free.
    """
    rid = request_id or "req_" + uuid.uuid4().hex[:16]
    with calls_logged(background, caller, rid, name):
        return _run_lens_query(
            name, q, caller, background, fmt, bindings, allow_untyped, purpose, routing, rid
        )


def _run_lens_query(
    name: str,
    q: str,
    caller: CallerIdentity,
    background: BackgroundTasks,
    fmt: AnswerFormat,
    bindings: dict[str, str] | None,
    allow_untyped: bool | None,
    purpose: str,
    routing: Decision | None,
    rid: str,
) -> QueryResponse | ConsumerAnswer:
    bundle = _authorized_bundle(name, caller, rid)

    resolved = require_llm(bundle.config.model.model_ref())
    try:
        connector = _authorized_connector(bundle, caller)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"lens connection error: {exc}") from exc

    audience = _audience(caller)
    with attributed(
        Attribution(
            principal=caller.name, agent=caller.agent, request_id=rid, org_id=str(caller.org_id)
        )
    ):
        result, assembled = serve_lens_question(
            name,
            q,
            bundle,
            org_id=caller.org_id,
            caller=caller.name,
            connector=connector,
            resolved=resolved,
            request_id=rid,
            fmt=fmt,
            audience=audience,
            bindings=bindings,
            allow_untyped=allow_untyped,
            tables_elsewhere=lambda: tables_held_elsewhere(caller, exclude=name),
        )
    # Charged before any request_log write (the timeout path logs below): a
    # row with this id always carries the routing spend, so observe counts the
    # routing row's own copy only when no such row exists.
    if routing is not None:
        _charge_routing(result.trace, routing)
    charge_cache(result.trace)
    _fail_on_warehouse_timeout(result, caller)
    if result.response.status == "refused":
        # A refusal that names a table another lens exposes says so: the caller
        # asked the wrong door, not an unanswerable question.
        hint = exposed_hint(exposed_tables_named(q, caller, exclude=name))
        if hint:
            result.response.answer = f"{result.response.answer.rstrip('.')}; {hint}"
    # Degradation rides on EVERY outcome, not just the ok ones — stamped here,
    # once, because run_query builds a response and a trace on each of its five
    # exits. A serve whose certified matching could not run says so on the wire
    # and in the log; silence there is what let a dead embedder pass for a
    # corpus that simply had no answer. The door's lines lead and the pipeline's
    # own follow (an UNTYPED escalation, a grade on partial evidence): replacing
    # them erased the line a raw-SQL answer owes on its face. The trace carries
    # what the response carried.
    if assembled.degraded:
        result.response.degraded = [*assembled.degraded, *result.response.degraded]
    result.trace.degraded = list(result.response.degraded)
    # A warehouse execution error runs the drift check NOW and becomes a
    # templated incident; a standing degraded mark rides this answer either way.
    _trust_backstops(
        result,
        org_id=caller.org_id,
        lens=name,
        connection=bundle.config.connections[0] if bundle.config.connections else "",
    )
    # The acting client, stamped once here rather than threaded through five pipeline
    # exits — the person is already `trace.caller`, this is who drove them.
    result.trace.agent = caller.agent
    result.trace.purpose = _logged_purpose(purpose, bundle.config)
    background.add_task(log_trace, result.trace)
    # The lens can auto-flag its own low-confidence answers for review (origin=ai).
    if _auto_review_flags(bundle.config.auto_review, result.response):
        background.add_task(_auto_review, caller.org_id, result.response.request_id)
    return _shaped(result.response, fmt, audience)


def routing_spend(
    spent: Iterable[tuple[str, DecisionUsage | None]],
) -> tuple[int, int, float | None] | None:
    """``(tokens_in, tokens_out, cost_usd)`` of the routing decider's calls,
    given as ``(provider, usage)`` pairs (a decision's, or what an outage
    billed before it failed), each priced by its decider's model (None cost
    when any is unpriced); None when no pair carries usage."""
    tokens_in = tokens_out = 0
    cost_usd: float | None = 0.0
    charged = False
    for provider, usage in spent:
        if usage is None:
            continue
        i, o = round(usage.input_tokens), round(usage.output_tokens)
        tokens_in += i
        tokens_out += o
        cost_usd = cost.add_cost(cost_usd, cost.decisions_cost_usd({provider or "": (i, o)}))
        charged = True
    return (tokens_in, tokens_out, cost_usd) if charged else None


def _charge_routing(trace: TraceLog, routing: Decision) -> None:
    """The routing decider's calls are this answer's AI spend, like the
    generator's and the typed decider's: the votes that chose the lens are
    added to the row's tokens and priced by the decider's model. An unpriced
    model leaves the row's cost unpriced, never a partial sum."""
    spend = routing_spend([(routing.provider, routing.usage)])
    if spend is None:
        return
    tokens_in, tokens_out, cost_usd = spend
    trace.ai_input_tokens = (trace.ai_input_tokens or 0) + tokens_in
    trace.ai_output_tokens = (trace.ai_output_tokens or 0) + tokens_out
    trace.ai_cost_usd = cost.add_cost(trace.ai_cost_usd, cost_usd)


def serve_lens_question(
    name: str,
    q: str,
    bundle: LensBundle,
    *,
    org_id: uuid.UUID | str,
    caller: str,
    connector: Connector,
    resolved: ResolvedModel,
    request_id: str | None = None,
    fmt: AnswerFormat = "both",
    audience: Audience = "engineer",
    bindings: dict[str, str] | None = None,
    allow_untyped: bool | None = None,
    serve_certified: bool = True,
    tables_elsewhere: Callable[[], Mapping[str, str]] | None = None,
) -> tuple[PipelineResult, assembly.AssembledInputs]:
    """One lens question through the serving configuration: assembly, the
    generator the install and the lens choose (typed serving and its decider,
    the lens's ``untyped_fallback`` unless the caller overrides it, the
    sole-metric compile), and the pipeline with the lens's own caps, answer mode
    and rails. No authorization, logging or incident handling: the door wraps
    those around it, and the publish gate and `dst test` call it as they are,
    so a behavioural case is graded on the path the door serves."""
    llm, model_name = resolved.llm, resolved.name
    assembled = assembly.assemble(
        bundle,
        q,
        org_id,
        bindings=bindings or {},
        allow_untyped=effective_allow_untyped(bundle.config, allow_untyped),
        serve_certified=serve_certified,
    )
    generator, escalate, certification, certified_match = assembly.select_generators(
        assembled, resolved, config=bundle.config
    )
    generator_tier: str = assembly.generator_tier(assembled)
    if generator_tier == "intent" and not isinstance(generator, TypedIntentGenerator):
        # A question that names one governed metric and nothing else is the
        # structured door's case: the compiler computes it, deterministically,
        # before any model writes a reading of it. Typed serving already does
        # this through its decisions; without a decider the text decides, and
        # only when the text leaves nothing open.
        intent = sole_metric_intent(
            q,
            assembled.model,
            period_fields=assembled.period_fields,
            domains=assembled.value_domains,
            today=assembly.business_today_date(assembled.model.timezone),
        )
        pinned: list[DecisionRecord] = []
        if intent is not None:
            # A ratio asked over no period reads the last complete month and
            # says so (typed_resolver.pin_period), as on every door.
            pin = pin_intent(
                q,
                intent,
                assembled.model,
                assembly.business_today_date(assembled.model.timezone),
                period_field=assembled.period_fields.get(intent.entity or ""),
            )
            if isinstance(pin, ClarificationRequest):
                generator = FixedSQLGenerator("", clarification=pin)
                intent = None
            elif pin is not None:
                intent, record = pin
                pinned.append(record)
        if intent is not None:
            try:
                sql = compile_intent(
                    intent,
                    assembled.model,
                    # The entity's own dictionaries, as the typed path reads
                    # them: the model-wide ones drop a column two tables share.
                    domains={
                        **assembled.value_domains,
                        **assembled.entity_domains.get(intent.entity or "", {}),
                    },
                    column_types=assembled.column_types,
                )
            except UnpinnedDimension as exc:
                # The metric sums kinds of record that do not add: ask which.
                generator = FixedSQLGenerator("", clarification=exc.clarification())
            except CompileError:
                log.exception("sole-metric intent did not compile; generating instead")
            else:
                generator = FixedSQLGenerator(
                    sql, definition_used=intent_term(intent), intent=intent, decisions=pinned
                )
                generator_tier = "metrics"

    provenance = (
        CertifiedProvenance(
            cert_id=assembled.certified.id,
            certified_by=assembled.certified.created_by,
            certified_at=assembled.certified.created_at,
            bound_values=assembled.certified_bound,
        )
        if assembled.certified is not None
        else None
    )
    result = run_query(
        question=q,
        lens_name=name,
        org_id=org_id,
        caller=caller,
        request_id=request_id,
        audience=audience,
        semantic_model=assembled.model,
        value_domains=assembled.value_domains,
        table_domains=assembled.table_domains,
        entity_coverage=assembled.entity_coverage,
        profile_age=assembled.profile_age,
        decisions=assembled.decisions,
        spent=assembled.spent,
        connector=connector,
        generator=generator,
        escalate_generator=escalate,
        composer=None if fmt in _ROWS_ONLY else AnswerComposer(llm, model=model_name),
        prose_context=assembled.prose,
        max_rows=bundle.config.model.max_rows_to_return,
        compose_rows=bundle.config.model.max_rows_to_compose,
        max_repairs=bundle.config.model.max_repairs,
        model_name=model_name,
        certification=certification,
        certified_match=certified_match,
        certified_provenance=provenance,
        # The judge/adversary carve-out is verbatim-scoped: the pipeline
        # compares this against the asked question.
        certified_question=(
            assembled.certified.question if assembled.certified is not None else None
        ),
        # A matched fixed answer's certify-time prose serves
        # verbatim — the pipeline skips the composer and numeric_grounding
        # on it. None (legacy answers, templates) composes as before.
        verified_prose=(
            assembled.certified.verified_prose if assembled.certified is not None else None
        ),
        # The value a human verified, and for a template the sample binding
        # it was recorded under: the `restated` check compares against it.
        verified_value=(
            assembled.certified.verified_value if assembled.certified is not None else None
        ),
        verified_bindings=(
            next(iter(assembled.certified.sample_bindings or []), None)
            if assembled.certified is not None
            else None
        ),
        # Strict mode tightens scrutiny — judge + adversary on, so off-scope or
        # low-confidence answers get downgraded/declined more readily — regardless of
        # the explicit per-lens flags.
        inline_judge_llm=llm if bundle.config.model.answer_mode == "strict" else None,
        judge_model=model_name,
        adversary_llm=llm if bundle.config.model.answer_mode == "strict" else None,
        adversary_model=model_name,
        data_as_of=assembled.data_as_of,
        table_as_of=assembled.table_as_of,
        serve_ungoverned_shapes=bundle.config.serve_ungoverned_shapes,
        metric_carriers=lambda metric: _lenses_carrying(org_id, name, metric),
        tables_elsewhere=tables_elsewhere,
        # Which prompt this answer was written against (`dst lens prompt`
        # prints the same tier's prompt for a question).
        generator_tier=generator_tier,
        # The per-lens switch the pipeline cannot see from the semantic model.
        log_samples=bundle.config.logging.log_samples,
    )
    return result, assembled


@router.post("/lenses/{name}/query", response_model=QueryResponse | ConsumerAnswer)
async def query_lens(
    name: str,
    body: QueryBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> QueryResponse | ConsumerAnswer:
    """Ask this lens a question — the governed door: certified match, else generated
    SQL, guarded execution, and receipts on every answer (`sql`, `citations`, graded
    `confidence`, `data_as_of`, `request_id`). `format` is `both` (default) |
    `structured` (rows only — skips the prose LLM call) | `prose`. A `clarification`
    in the response is a governed non-answer, never a failure."""
    # Off the event loop: run_lens_query blocks for the whole generation + warehouse
    # round-trip over sync httpx. Awaiting nothing in an `async def` handler pinned the
    # loop for the duration — measured: /health went 0.3ms idle -> 2.3s (then timing out
    # on real, minutes-long queries) while ONE query was in flight, and the MCP tools,
    # which proxy back into this same loop, could not make progress. Same pattern as
    # /v1/query in route.py; the sibling handlers here are sync `def`, which FastAPI
    # already dispatches to this same threadpool.
    return await run_in_threadpool(
        run_lens_query,
        name,
        body.q,
        caller,
        background,
        body.format,
        body.bindings,
        body.allow_untyped,
        body.purpose,
    )


def _logged_purpose(purpose: str, config: LensConfig) -> str | None:
    """The purpose sentence as the log row keeps it: None unless the lens opted in
    (`logging.log_purpose`) and the caller sent one."""
    purpose = purpose.strip()
    return purpose if purpose and config.logging.log_purpose else None


def describe_intent(intent: QueryIntent) -> str:
    """A one-line rendering of an intent, for the trace and the review queue.

    Every other door logs the caller's question; this one has no question, and
    logging "(structured intent)" would make the request log useless exactly where
    it is most auditable. So spell the intent back in words."""
    parts = [", ".join(intent.metrics) or "rows"]
    if intent.dimensions:
        parts.append("by " + ", ".join(intent.dimensions))
    conds = [f"{f.field} {f.op} {f.value}" for f in intent.filters]
    conds += [f"[{term}]" for term in intent.definitions]
    if conds:
        parts.append("where " + " and ".join(conds))
    if intent.order_by:
        parts.append("ordered by " + ", ".join(f"{o.field} {o.dir}" for o in intent.order_by))
    if intent.limit:
        parts.append(f"limit {intent.limit}")
    return " ".join(parts)


def intent_scan_text(intent: QueryIntent) -> str:
    """What the deterministic rails may read from a structured intent: NAMES ONLY.

    ``describe_intent`` renders filter VALUES so the request log stays readable —
    and that rendering used to be handed to the pipeline as the caller's
    question, which meant the ambiguity/exclusion/not-computable pre-checks read
    the customer's own row labels as if a human had typed them. A filter value of
    'Revenue (SE)' then forced a clarify on the governed term "revenue"; the same
    hole could force a reject or a refusal.

    A caller who NAMES a metric, dimension or definition is asking about it, so
    those still scan. Data values are not the question.
    """
    parts = [", ".join(intent.metrics) or "rows"]
    if intent.dimensions:
        parts.append("by " + ", ".join(intent.dimensions))
    names = [f.field for f in intent.filters] + [f"[{term}]" for term in intent.definitions]
    if names:
        parts.append("where " + " and ".join(names))
    if intent.order_by:
        parts.append("ordered by " + ", ".join(o.field for o in intent.order_by))
    return " ".join(parts)


def run_lens_metrics(
    name: str,
    intent: QueryIntent,
    caller: CallerIdentity,
    background: BackgroundTasks,
    fmt: AnswerFormat = "structured",
) -> QueryResponse | ConsumerAnswer:
    """Authorize + compile + run a STRUCTURED intent — the door with no LLM in it.

    The metric layer already resolves names, joins, aggregation and dialect
    deterministically; until now the only way to reach it was to have a model write
    an intent from a question. A caller that already knows which metric it wants —
    a BI tool, a notebook, or an agent drilling into an answer it just received —
    should not pay for a generation to ask a question it can already spell.

    Everything after compilation is the shared governed path: same sql_guard, same
    read-only execution, same row caps, same trace. A compile error
    is a 400 carrying the compiler's own sentence, which already names the fix
    ("no declared join path from 'orders' to 'regions'").
    """
    rid = "req_" + uuid.uuid4().hex[:16]
    bundle = _authorized_bundle(name, caller, rid)
    serve = _MetricsServe.prepare(name, bundle, caller, fmt)
    intent, pinned = serve.pinned(intent)
    sql = serve.compiled(intent)
    with calls_logged(background, caller, rid, name):
        result = serve.run(intent, sql, rid, serve.connector, pinned)
        charge_cache(result.trace)
    background.add_task(log_trace, result.trace)
    return _shaped(result.response, fmt, serve.audience)


class _MetricsServe:
    """Everything the metrics door resolves once per request — connector,
    composer, profile facts, audience — so one intent and a batch of fifty
    serve from the same facts. ``run`` is one intent through the governed
    pipeline; it raises HTTPException on the paths the single door maps to a
    status (a warehouse timeout is a 504)."""

    def __init__(
        self,
        name: str,
        bundle: LensBundle,
        caller: CallerIdentity,
        connector: Connector,
        composer: AnswerComposer | None,
        model_name: str,
    ) -> None:
        self.name, self.bundle, self.caller = name, bundle, caller
        self.connector, self.composer, self.model_name = connector, composer, model_name
        profiles, self.as_of, self.age, self.stamps = assembly.profile_facts(bundle, caller.org_id)
        self.coverage = profile_enrich.entity_coverage(bundle.semantic_model, profiles)
        # The warehouse's own column types, as the prose door compiles with:
        # a bound or a bucket shift on a column declared `date` that the
        # warehouse stores as a TIMESTAMP must be written for the TIMESTAMP.
        self.column_types = value_guard.entity_column_types(bundle.semantic_model, profiles)
        self.audience = _audience(caller)

    @classmethod
    def prepare(
        cls, name: str, bundle: LensBundle, caller: CallerIdentity, fmt: AnswerFormat
    ) -> _MetricsServe:
        try:
            connector = _authorized_connector(bundle, caller)
        except (IndexError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"lens connection error: {exc}") from exc
        # An LLM is required only to WRITE the prose. Demanding one for the rows-only
        # case would make the door that exists to avoid model calls 503 on an install
        # with no provider configured — which is exactly what it should serve happily.
        composer = None
        model_name = "none"
        if fmt not in _ROWS_ONLY:
            resolved = require_llm(bundle.config.model.model_ref())
            model_name = resolved.name
            composer = AnswerComposer(resolved.llm, model=model_name)
        return cls(name, bundle, caller, connector, composer, model_name)

    def pinned(self, intent: QueryIntent) -> tuple[QueryIntent, list[DecisionRecord]]:
        """The intent with the last complete month pinned when it asks a
        ratio over no period (typed_resolver.pin_period), and the record the
        answer discloses it by. A month the entity's clock cannot hold is a
        400 naming the period to pass."""
        model = self.bundle.semantic_model
        # No question text: a structured caller states a window as a filter.
        pin = pin_intent("", intent, model, assembly.business_today_date(model.timezone))
        if isinstance(pin, ClarificationRequest):
            raise HTTPException(status_code=400, detail=pin.question)
        if pin is None:
            return intent, []
        return pin[0], [pin[1]]

    def compiled(self, intent: QueryIntent) -> str:
        """The intent's SQL, or a 400 carrying the compiler's own sentence."""
        try:
            return compile_intent(
                intent, self.bundle.semantic_model, column_types=self.column_types
            )
        except CompileError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def run(
        self,
        intent: QueryIntent,
        sql: str,
        rid: str,
        connector: Connector,
        pinned: list[DecisionRecord] | None = None,
    ) -> PipelineResult:
        caller, bundle = self.caller, self.bundle
        with attributed(
            Attribution(
                principal=caller.name,
                agent=caller.agent,
                request_id=rid,
                org_id=str(caller.org_id),
            )
        ):
            result = run_query(
                question=describe_intent(intent),
                # The rails read names, never values — the log keeps the values.
                scan_text=intent_scan_text(intent),
                lens_name=self.name,
                org_id=caller.org_id,
                caller=caller.name,
                request_id=rid,
                audience=self.audience,
                semantic_model=bundle.semantic_model,
                connector=connector,
                # The caller's own intent names the metric — basis provenance is a
                # fact here, not an attribution guess.
                generator=FixedSQLGenerator(
                    sql, definition_used=intent_term(intent), intent=intent, decisions=pinned
                ),
                composer=self.composer,
                max_rows=bundle.config.model.max_rows_to_return,
                compose_rows=bundle.config.model.max_rows_to_compose,
                model_name=self.model_name,
                # Not `certified`: no human approved THIS query. But nothing was guessed
                # either — every name came from the model and the SQL was built by code.
                certification="none",
                data_as_of=self.as_of,
                table_as_of=self.stamps,
                entity_coverage=self.coverage,
                profile_age=self.age,
                generator_tier="metrics",
                log_samples=bundle.config.logging.log_samples,
            )
        _fail_on_warehouse_timeout(result, caller)
        _trust_backstops(
            result,
            org_id=caller.org_id,
            lens=self.name,
            connection=bundle.config.connections[0] if bundle.config.connections else "",
        )
        result.trace.agent = caller.agent
        return result


@router.post("/lenses/{name}/metrics", response_model=QueryResponse | ConsumerAnswer)
def query_lens_metrics(
    name: str,
    body: MetricsBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> QueryResponse | ConsumerAnswer:
    """Ask with a structured intent — metrics/dimensions/filters instead of prose.
    Same governance, receipts, and trace as the prose door; the intent is spelled
    back into words for the request log."""
    intent = QueryIntent(**body.model_dump(exclude={"format"}))
    return run_lens_metrics(name, intent, caller, background, body.format)


# A batch renders one screen — a metric tree, a dashboard — at one moment.
# The cap keeps one request's warehouse load bounded; the pool bounds how many
# of its queries hold a warehouse connection at once.
BATCH_MAX = 50
BATCH_WORKERS = 4


class BatchIntent(QueryIntent):
    id: str = Field(
        min_length=1,
        max_length=200,
        description="the caller's key for this intent; its result comes back under it",
    )


class MetricsBatchBody(BaseModel):
    intents: list[BatchIntent] = Field(
        min_length=1,
        max_length=BATCH_MAX,
        description=f"up to {BATCH_MAX} structured intents, each with a unique `id`",
    )
    format: AnswerFormat = Field(default="structured", description=FORMAT_HELP)

    @model_validator(mode="after")
    def _unique_ids(self) -> MetricsBatchBody:
        ids = [i.id for i in self.intents]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"intent ids must be unique; repeated: {', '.join(dupes)}")
        return self


# The answer statuses that serve nothing: the SQL failed or was rejected.
# A clarification or a refusal is an outcome, not a failure, and stays `ok`.
_FAILED = frozenset({"error", "rejected"})


class BatchItem(BaseModel):
    """One intent's outcome. A failure is this intent's alone: `status` is the
    HTTP status the single door would have answered it with, `error` its detail.
    An intent the door answered without serving (its answer's status is
    ``error`` or ``rejected``) is a failure too: `status` 200, as the single
    door answered it, `error` the answer's sentence, and the answer itself."""

    ok: bool
    request_id: str | None = None
    status: int | None = None
    error: str | None = None
    answer: QueryResponse | ConsumerAnswer | None = None


SnapshotKind = Literal["shared", "per_query"]
SNAPSHOT_NOTES: dict[str, str] = {
    "shared": "every query read one warehouse snapshot",
    "per_query": "each query read the warehouse at its own moment; this connector "
    "cannot hold one snapshot across queries",
}


class MetricsBatchResponse(BaseModel):
    lens: str
    # One profile read for the whole batch, so every answer carries this date.
    data_as_of: str | None = None
    snapshot: SnapshotKind
    snapshot_note: str
    results: dict[str, BatchItem]


def _batch_allowance(bundle: LensBundle, caller: CallerIdentity, name: str, n: int) -> int:
    """How many of a batch's *n* intents this caller may run now. The door's
    own authorization already charged the first; each further intent takes one
    more hit of the per-minute limit and one more answer of the daily quotas,
    exactly as if it had come alone. The rest are refused one by one."""
    if caller.is_admin:
        return n
    allowed = n
    rpd = bundle.config.rate_limit.per_caller_rpd
    if rpd > 0:
        used = quota.usage(caller.org_id, caller=caller.name, lens=name).served
        allowed = min(allowed, max(1, rpd - used))
    cap = settings.daily_request_cap
    if cap > 0:
        allowed = min(allowed, max(1, cap - quota.usage(caller.org_id).served))
    rl_key = f"{caller.org_id}:{name}:{caller.name}"
    for i in range(1, allowed):
        if not ratelimit.check(rl_key, bundle.config.rate_limit.per_caller_rpm):
            return i
    return allowed


@contextmanager
def _batch_reader(connector: Connector) -> Iterator[tuple[Connector, SnapshotKind]]:
    """The connector a batch runs on: one held snapshot where the connector
    can hold one, else the connector as it is — and which of the two, so the
    response states the consistency it actually got."""
    stack = ExitStack()
    pinned: Connector | None = None
    if isinstance(connector, SharedSnapshot):
        try:
            pinned = stack.enter_context(connector.shared_snapshot())
        except Exception:
            log.exception("could not hold a shared snapshot; reading per query")
    with stack:
        if pinned is None:
            yield connector, "per_query"
        else:
            yield pinned, "shared"


def run_lens_metrics_batch(
    name: str,
    body: MetricsBatchBody,
    caller: CallerIdentity,
    background: BackgroundTasks,
) -> MetricsBatchResponse:
    """Run up to BATCH_MAX intents in one request, at one data_as_of and — where
    the connector can hold one — one warehouse snapshot. Each intent compiles,
    runs and is logged on its own (its own request_id, receipt and trace), and
    fails on its own: a bad metric name in one costs that one, not the batch."""
    rids = ["req_" + uuid.uuid4().hex[:16] for _ in body.intents]
    bundle = _authorized_bundle(name, caller, rids[0])
    allowed = _batch_allowance(bundle, caller, name, len(body.intents))
    _ok, reason = authorize(caller, bundle.config)
    for i, rid in enumerate(rids[1:], start=1):
        decision = "allow" if i < allowed else "deny"
        why = reason if i < allowed else "batch: rate limit or daily quota exceeded"
        audit.record(
            caller.org_id, caller.name, name, decision, why, request_id=rid, agent=caller.agent
        )
    serve = _MetricsServe.prepare(name, bundle, caller, body.format)
    results: dict[str, BatchItem] = {}

    def one(index: int, connector: Connector) -> BatchItem:
        item = body.intents[index]
        intent = QueryIntent(**item.model_dump(exclude={"id"}))
        try:
            intent, pinned = serve.pinned(intent)
            sql = serve.compiled(intent)
            with calls_logged(background, caller, rids[index], name):
                result = serve.run(intent, sql, rids[index], connector, pinned)
                charge_cache(result.trace)
        except HTTPException as exc:
            # A timed-out query was traced under its id; a refused compile never ran.
            rid = rids[index] if exc.status_code == 504 else None
            return BatchItem(
                ok=False, request_id=rid, status=exc.status_code, error=str(exc.detail)
            )
        except Exception:
            log.exception("batch intent %s failed on lens %s", item.id, name)
            return BatchItem(ok=False, status=500, error="internal error serving this intent")
        background.add_task(log_trace, result.trace)
        answer = _shaped(result.response, body.format, serve.audience)
        if result.response.status in _FAILED:
            # Answered, but nothing was served: the single door says so with
            # HTTP 200 and the answer's status, the batch with `ok: false`.
            return BatchItem(
                ok=False,
                request_id=result.response.request_id,
                status=200,
                error=result.response.answer,
                answer=answer,
            )
        return BatchItem(ok=True, request_id=result.response.request_id, answer=answer)

    with _batch_reader(serve.connector) as (connector, snapshot):
        with ThreadPoolExecutor(max_workers=min(BATCH_WORKERS, allowed)) as pool:
            done = list(pool.map(lambda i: one(i, connector), range(allowed)))
    for i, item in enumerate(body.intents):
        results[item.id] = (
            done[i]
            if i < allowed
            else BatchItem(
                ok=False,
                status=429,
                error=f"rate limit or daily quota reached for caller '{caller.name}' on "
                f"lens '{name}' before this intent ran",
            )
        )
    return MetricsBatchResponse(
        lens=name,
        data_as_of=serve.as_of,
        snapshot=snapshot,
        snapshot_note=SNAPSHOT_NOTES[snapshot],
        results=results,
    )


@router.post("/lenses/{name}/metrics/batch", response_model=MetricsBatchResponse)
def query_lens_metrics_batch(
    name: str,
    body: MetricsBatchBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> MetricsBatchResponse:
    """Several structured intents in one request, read at one moment — a metric
    tree or a dashboard rendered from one snapshot. Results come back keyed by
    each intent's `id`, every one with its own request_id and receipt; a failing
    intent fails alone. At most 50 intents; more is a 422."""
    return run_lens_metrics_batch(name, body, caller, background)


@router.get("/lenses/{name}/certified", response_model=CertifiedLibrary)
def list_certified_for_caller(
    name: str,
    q: str | None = None,
    caller: CallerIdentity = Depends(get_caller),
) -> CertifiedLibrary:
    """The lens's certified library: human-approved question→SQL pairs.

    With `q`, results are similarity-ranked against the question (top 5, scored);
    without it (or when embeddings are unconfigured), the full library, newest first.
    """
    _authorized_bundle(name, caller)
    qvec = assembly.embed_question(q) if q else None
    with org_session(caller.org_id) as session:
        library = active_certified(session, name)
        if qvec is not None:
            hits = certify_store.search(session, name, qvec, k=5)
            answers = [h.answer for h in hits]
            scores = {h.answer.id: h.score for h in hits}
        else:
            answers, scores = list(library), {}
        if q:
            # The stored question itself, asked verbatim, is the exact match
            # whatever its cosine (a template's vector is its sample-bound
            # anchor, so its own `{slot}` text can score under the band) —
            # and it is exact on a keyless install too.
            listed = {a.id for a in answers}
            for a in library:
                if _verbatim_question(a, q):
                    if a.id not in listed:
                        answers.append(a)
                    scores[a.id] = 1.0
            answers.sort(key=lambda a: -scores.get(a.id, 0.0))
        entries = [
            CertifiedEntry(
                cert_id=a.id,
                question=a.question,
                sql=a.sql,
                certified_by=a.created_by,
                certified_at=a.created_at,
                score=round(scores[a.id], 4) if a.id in scores else None,
                slots=a.slots,
                sample_bindings=a.sample_bindings,
            )
            for a in (answers[:5] if qvec is not None else answers)
        ]
    return CertifiedLibrary(
        lens=name, certified=entries, exact_band=assembly.certified_bands().exact
    )


@router.post(
    "/lenses/{name}/certified/{cert_id}/run", response_model=QueryResponse | ConsumerAnswer
)
def run_certified_for_caller(
    name: str,
    cert_id: str,
    background: BackgroundTasks,
    body: CertifiedRunBody | None = None,
    caller: CallerIdentity = Depends(get_caller),
) -> QueryResponse | ConsumerAnswer:
    """Run a certified answer exactly as approved — guard + execute, zero SQL generation.

    The deterministic door: the approved SQL goes through the same sql_guard and
    read-only execution as any query; only answer composition touches an LLM — and
    ``format: "structured"`` drops even that, which is where nearly all of this door's
    latency lives, since guard and execute are the only other work. The
    response carries certification='certified', provenance, and a trust_summary.
    A TEMPLATE answer requires ``bindings`` in the body — the 400
    names the slots so a caller can re-invoke with values; the rendered SQL is
    deterministic (validated typed literals, never spliced text).
    """
    rid = "req_" + uuid.uuid4().hex[:16]
    bundle = _authorized_bundle(name, caller, rid)
    try:
        with org_session(caller.org_id) as session:
            cert = certify_store.get(session, cert_id)
    except Exception:  # malformed id (not a uuid) — treat as not found
        cert = None
    if cert is None or cert.lens != name or not certify_store.is_active(cert.status):
        # A retired answer keeps its history but never serves — same 404 as absent.
        raise HTTPException(
            status_code=404, detail=f"certified answer '{cert_id}' not found for lens '{name}'"
        )
    question = cert.question
    served_sql = cert.sql
    match: Literal["exact", "parameterized"] = "exact"
    bound: dict[str, str] | None = None
    if cert.slots:
        specs, spec_errors = certify_binding.parse_slots(cert.slots)
        if spec_errors:
            raise HTTPException(
                status_code=400,
                detail=f"certified template '{cert_id}' has invalid slots: "
                + "; ".join(spec_errors),
            )
        supplied = (body.bindings if body else {}) or {}
        if not supplied:
            slot_desc = ", ".join(
                f"{n} ({s.type}" + (f": {'|'.join(s.values)}" if s.values else "") + ")"
                for n, s in specs.items()
            )
            sample = (cert.sample_bindings or [{}])[0]
            raise HTTPException(
                status_code=400,
                detail=(
                    f"certified answer '{cert_id}' is a template — pass bindings for: "
                    f"{slot_desc}. Example: {sample}"
                ),
            )
        bound, errors = certify_binding.validate_binding(specs, dict(supplied))
        if errors:
            raise HTTPException(status_code=400, detail="invalid bindings: " + "; ".join(errors))
        served_sql = certify_binding.render_sql(
            cert.sql, specs, bound, bundle.semantic_model.dialect
        )
        question = certify_binding.render_question(cert.question, dict(bound))
        match = "parameterized"
    try:
        connector = _authorized_connector(bundle, caller)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"lens connection error: {exc}") from exc

    resolved = require_llm(bundle.config.model.model_ref())
    llm, model_name = resolved.llm, resolved.name
    profiles, as_of, age, stamps = assembly.profile_facts(bundle, caller.org_id)
    coverage = profile_enrich.entity_coverage(bundle.semantic_model, profiles)
    fmt: AnswerFormat = body.format if body else "both"
    audience = _audience(caller)
    with (
        calls_logged(background, caller, rid, name),
        attributed(
            Attribution(
                principal=caller.name,
                agent=caller.agent,
                request_id=rid,
                org_id=str(caller.org_id),
            )
        ),
    ):
        result = run_query(
            question=question,
            lens_name=name,
            org_id=caller.org_id,
            caller=caller.name,
            request_id=rid,
            audience=audience,
            semantic_model=bundle.semantic_model,
            connector=connector,
            generator=FixedSQLGenerator(served_sql),
            composer=None if fmt in _ROWS_ONLY else AnswerComposer(llm, model=model_name),
            max_rows=bundle.config.model.max_rows_to_return,
            compose_rows=bundle.config.model.max_rows_to_compose,
            model_name=model_name,
            certification="certified",
            certified_match=match,  # the door pins a specific approved answer
            # The door serves the same certify-time prose the matcher would —
            # verbatim, no composer call (templates render deterministically).
            verified_prose=cert.verified_prose,
            verified_value=cert.verified_value,
            verified_bindings=next(iter(cert.sample_bindings or []), None),
            certified_provenance=CertifiedProvenance(
                cert_id=cert.id,
                certified_by=cert.created_by,
                certified_at=cert.created_at,
                bound_values=bound,
            ),
            data_as_of=as_of,
            table_as_of=stamps,
            entity_coverage=coverage,
            profile_age=age,
            generator_tier="certified",
            log_samples=bundle.config.logging.log_samples,
        )
        charge_cache(result.trace)
    _fail_on_warehouse_timeout(result, caller)
    _trust_backstops(
        result,
        org_id=caller.org_id,
        lens=name,
        connection=bundle.config.connections[0] if bundle.config.connections else "",
    )
    result.trace.agent = caller.agent
    result.trace.purpose = _logged_purpose(body.purpose if body else "", bundle.config)
    background.add_task(log_trace, result.trace)
    return _shaped(result.response, fmt, audience)
