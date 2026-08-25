"""Data-plane query endpoint: POST /v1/lenses/{name}/query.

Caller-authenticated (API key, or admin token as superuser). Enforces the lens's
allow-list (deny-by-default), audits the decision, runs the governed pipeline, and
logs the full trace off the response path.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from services.api.llm import require_llm
from services.auth.deps import get_caller
from services.certify import binding as certify_binding
from services.certify import store as certify_store
from services.contracts.correction import CorrectionDelta
from services.contracts.protocols import Connector
from services.contracts.query_intent import QueryIntent
from services.contracts.response import CertifiedProvenance, QueryResponse
from services.db.session import org_session
from services.governance import audit, drift_watch, ratelimit
from services.governance.credentials import CallerIdentity
from services.governance.policy import authorize
from services.lenses import store as lens_store
from services.lenses.connections import resolve_connector
from services.lenses.describe import LensDescription, describe_model
from services.lenses.store import LensBundle, list_published_for_org, load_published_lens
from services.observability.logger import log_trace
from services.reviews import service as reviews_service
from services.reviews import store as reviews_store
from services.runtime import assembly
from services.runtime.answer import AnswerComposer
from services.runtime.attribution import Attribution, attributed
from services.runtime.compiler import CompileError, compile_intent
from services.runtime.generator import FixedSQLGenerator
from services.runtime.intent_generator import intent_term
from services.runtime.pipeline import PipelineResult, run_query

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


router = APIRouter(prefix="/v1", tags=["query"])


def _lens_unavailable(name: str) -> HTTPException:
    """ONE response for 'does not exist' and 'exists but not granted to you'.
    A 404-vs-403 split is a reliable existence oracle — any caller key can
    enumerate the org's lens inventory by probing names,
    and lens names describe the business (two_step_checkout, revrec_oss,
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
    certified_count, certified_examples = 0, []
    try:
        with org_session(caller.org_id) as session:
            entries = [
                a
                for a in certify_store.list_for_lens(session, name)
                if certify_store.is_active(a.status)
            ]
        certified_count = len(entries)
        certified_examples = [e.question for e in entries[:3]]
    except Exception:
        log.exception("certified library lookup failed for lens %s", name)
    return describe_model(
        name,
        display_name=bundle.config.display_name,
        description=bundle.config.description,
        model=sm,
        certified_count=certified_count,
        certified_examples=certified_examples,
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


def _lenses_carrying(org_id: uuid.UUID, asking_lens: str, metric: str) -> list[str]:
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
    and no LLM runs on the error path."""
    if result.error_kind != "warehouse":
        return
    cause = drift_watch.cause_class(result.trace.error or "")
    if cause == drift_watch.GENERATION_FAULT_CAUSE:
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


def _shaped(response: QueryResponse, fmt: AnswerFormat) -> QueryResponse:
    """Apply the response-side half of `format`.

    Rows-only is handled upstream (the composer is simply never built, which is where
    the latency goes); `prose` only has to drop the payload the caller declined. The
    TRACE keeps the full result either way — observability is not the caller's choice.
    """
    return response.model_copy(update={"data": None}) if fmt == "prose" else response


def run_lens_query(
    name: str,
    q: str,
    caller: CallerIdentity,
    background: BackgroundTasks,
    fmt: AnswerFormat = "both",
) -> QueryResponse:
    """Authorize + run the governed query pipeline for one lens question.

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
    rid = "req_" + uuid.uuid4().hex[:16]
    bundle = _authorized_bundle(name, caller, rid)

    model_ref = bundle.config.model.model_ref()
    resolved = require_llm(model_ref)
    llm, model_name = resolved.llm, resolved.name
    try:
        connector = _authorized_connector(bundle, caller)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"lens connection error: {exc}") from exc

    assembled = assembly.assemble(bundle, q, caller.org_id)
    generator, escalate, certification, certified_match = assembly.select_generators(
        assembled, resolved, config=bundle.config
    )

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
    with attributed(
        Attribution(
            principal=caller.name, agent=caller.agent, request_id=rid, org_id=str(caller.org_id)
        )
    ):
        result = run_query(
            question=q,
            lens_name=name,
            org_id=caller.org_id,
            caller=caller.name,
            request_id=rid,
            semantic_model=assembled.model,
            value_domains=assembled.value_domains,
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
            # Strict mode tightens scrutiny — judge + adversary on, so off-scope or
            # low-confidence answers get downgraded/declined more readily — regardless of
            # the explicit per-lens flags.
            inline_judge_llm=llm
            if (bundle.config.model.inline_judge or bundle.config.model.answer_mode == "strict")
            else None,
            judge_model=model_name,
            adversary_llm=llm
            if (
                bundle.config.model.adversarial_review
                or bundle.config.model.answer_mode == "strict"
            )
            else None,
            adversary_model=model_name,
            data_as_of=assembled.data_as_of,
            serve_ungoverned_shapes=bundle.config.serve_ungoverned_shapes,
            metric_carriers=lambda metric: _lenses_carrying(caller.org_id, name, metric),
            # Which prompt this answer was written against (`dst lens prompt`
            # prints the same tier's prompt for a question).
            generator_tier=assembly.generator_tier(assembled),
            # The per-lens switch the pipeline cannot see from the semantic model.
            log_samples=bundle.config.logging.log_samples,
        )
    # Degradation rides on EVERY outcome, not just the ok ones — stamped here,
    # once, because run_query builds a response and a trace on each of its five
    # exits. A serve whose certified matching could not run says so on the wire
    # and in the log; silence there is what let a dead embedder pass for a
    # corpus that simply had no answer.
    if assembled.degraded:
        result.response.degraded = list(assembled.degraded)
        result.trace.degraded = list(assembled.degraded)
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
    background.add_task(log_trace, result.trace)
    # The lens can auto-flag its own low-confidence answers for review (origin=ai).
    if _auto_review_flags(bundle.config.auto_review, result.response):
        background.add_task(_auto_review, caller.org_id, result.response.request_id)
    return _shaped(result.response, fmt)


@router.post("/lenses/{name}/query", response_model=QueryResponse)
async def query_lens(
    name: str,
    body: QueryBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> QueryResponse:
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
    return await run_in_threadpool(run_lens_query, name, body.q, caller, background, body.format)


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


def run_lens_metrics(
    name: str,
    intent: QueryIntent,
    caller: CallerIdentity,
    background: BackgroundTasks,
    fmt: AnswerFormat = "structured",
) -> QueryResponse:
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
    try:
        sql = compile_intent(intent, bundle.semantic_model)
    except CompileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    _, as_of = assembly.profile_facts(bundle, caller.org_id)
    with attributed(
        Attribution(
            principal=caller.name, agent=caller.agent, request_id=rid, org_id=str(caller.org_id)
        )
    ):
        result = run_query(
            question=describe_intent(intent),
            lens_name=name,
            org_id=caller.org_id,
            caller=caller.name,
            request_id=rid,
            semantic_model=bundle.semantic_model,
            connector=connector,
            # The caller's own intent names the metric — basis provenance is a
            # fact here, not an attribution guess.
            generator=FixedSQLGenerator(sql, definition_used=intent_term(intent)),
            composer=composer,
            max_rows=bundle.config.model.max_rows_to_return,
            compose_rows=bundle.config.model.max_rows_to_compose,
            model_name=model_name,
            # Not `certified`: no human approved THIS query. But nothing was guessed
            # either — every name came from the model and the SQL was built by code.
            certification="none",
            data_as_of=as_of,
            generator_tier="metrics",
            log_samples=bundle.config.logging.log_samples,
        )
    _trust_backstops(
        result,
        org_id=caller.org_id,
        lens=name,
        connection=bundle.config.connections[0] if bundle.config.connections else "",
    )
    result.trace.agent = caller.agent
    background.add_task(log_trace, result.trace)
    return _shaped(result.response, fmt)


@router.post("/lenses/{name}/metrics", response_model=QueryResponse)
def query_lens_metrics(
    name: str,
    body: MetricsBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> QueryResponse:
    """Ask with a structured intent — metrics/dimensions/filters instead of prose.
    Same governance, receipts, and trace as the prose door; the intent is spelled
    back into words for the request log."""
    intent = QueryIntent(**body.model_dump(exclude={"format"}))
    return run_lens_metrics(name, intent, caller, background, body.format)


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
        if qvec is not None:
            hits = certify_store.search(session, name, qvec, k=5)
            entries = [
                CertifiedEntry(
                    cert_id=h.answer.id,
                    question=h.answer.question,
                    sql=h.answer.sql,
                    certified_by=h.answer.created_by,
                    certified_at=h.answer.created_at,
                    score=round(h.score, 4),
                    slots=h.answer.slots,
                    sample_bindings=h.answer.sample_bindings,
                )
                for h in hits
            ]
        else:
            entries = [
                CertifiedEntry(
                    cert_id=a.id,
                    question=a.question,
                    sql=a.sql,
                    certified_by=a.created_by,
                    certified_at=a.created_at,
                    slots=a.slots,
                    sample_bindings=a.sample_bindings,
                )
                for a in certify_store.list_for_lens(session, name)
                # The caller-facing library advertises the deterministic door —
                # retired answers are history (mgmt/export only), never served.
                # An unembedded one still belongs here: the door is by ID, and
                # matching (the thing it can't do) is a different door.
                if certify_store.is_active(a.status)
            ]
    return CertifiedLibrary(
        lens=name, certified=entries, exact_band=assembly.certified_bands().exact
    )


@router.post("/lenses/{name}/certified/{cert_id}/run", response_model=QueryResponse)
def run_certified_for_caller(
    name: str,
    cert_id: str,
    background: BackgroundTasks,
    body: CertifiedRunBody | None = None,
    caller: CallerIdentity = Depends(get_caller),
) -> QueryResponse:
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
    _, as_of = assembly.profile_facts(bundle, caller.org_id)
    fmt: AnswerFormat = body.format if body else "both"
    with attributed(
        Attribution(
            principal=caller.name, agent=caller.agent, request_id=rid, org_id=str(caller.org_id)
        )
    ):
        result = run_query(
            question=question,
            lens_name=name,
            org_id=caller.org_id,
            caller=caller.name,
            request_id=rid,
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
            certified_provenance=CertifiedProvenance(
                cert_id=cert.id,
                certified_by=cert.created_by,
                certified_at=cert.created_at,
                bound_values=bound,
            ),
            data_as_of=as_of,
            generator_tier="certified",
            log_samples=bundle.config.logging.log_samples,
        )
    _trust_backstops(
        result,
        org_id=caller.org_id,
        lens=name,
        connection=bundle.config.connections[0] if bundle.config.connections else "",
    )
    result.trace.agent = caller.agent
    background.add_task(log_trace, result.trace)
    return _shaped(result.response, fmt)
