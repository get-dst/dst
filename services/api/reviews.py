"""Review endpoints.

Data plane (caller-authed): send a traced request for review, read status back.
Control plane (admin-authed): the data-team review queue + human ruling, plus the
self-healing loop's patch surface: draft a fix from a correction,
list a lens's drafted candidates, and rule on one — approving PROPOSES the file
change that lands it (files author, the UI governs).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.api.llm import require_embedder
from services.auth import scopes
from services.auth.deps import AdminIdentity, get_admin_identity, get_app_session, get_caller
from services.certify import store as certify_store
from services.config import settings
from services.contracts.correction import CorrectionDelta
from services.db import embedding_meta
from services.db.session import org_session
from services.evals import store as eval_store
from services.governance.credentials import CallerIdentity
from services.lenses import profile_store
from services.lenses import store as lens_store
from services.llm import registry
from services.reviews import anchor, patch, patch_store, proposal, service, store

data_router = APIRouter(prefix="/v1/reviews", tags=["reviews"])
mgmt_router = APIRouter(prefix="/mgmt/reviews", tags=["reviews"])
patches_router = APIRouter(prefix="/mgmt/lenses/{lens}/patches", tags=["reviews"])


class ReviewRequest(BaseModel):
    request_id: str
    # A caller can report the delta (what's wrong / the right SQL) up front.
    correction: CorrectionDelta | None = None


class RuleBody(BaseModel):
    verdict: str  # approve | changes | reject
    reasoning: str = ""
    # The reviewer can attach the wrong-vs-right delta while ruling.
    correction: CorrectionDelta | None = None


def _tracking_url(ticket_id: str) -> str:
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/reviews/{ticket_id}"


def _untraced_detail(request_id: str) -> str:
    """Why a request_id the caller was just handed has no trace behind it.

    "no traced request 'req_…' to review" named the wrong cause on the one condition
    that actually produces it in bulk: a server whose schema is behind this build
    answers normally and hands out request_ids while every request_log write fails in
    the background. A stranger reads this message and concludes request_ids are broken.
    Ask the schema before blaming the id.
    """
    from services.db.schema_state import REMEDY, schema_state

    base = f"no traced request '{request_id}' to review"
    state = schema_state()
    if state.status != "behind":
        return base
    return (
        f"{base} — this server's schema is behind its code ({state.summary()}), so NO "
        f"trace is being recorded for any answer it serves. The request_id is fine; the "
        f"audit trail is not: {REMEDY}, and note that traces already served in this "
        "state were never written and cannot be recovered."
    )


def _ticket_payload(ticket: store.Ticket) -> dict[str, object]:
    return {**ticket.model_dump(), "url": _tracking_url(ticket.ticket_id)}


def _own_or_403(caller: CallerIdentity, owner: str, detail: str) -> None:
    """The data plane's scoping rule: a governed caller acts only on what was
    served to THEM; an admin token acts org-wide.

    Both halves matter. The org check alone (``org_session``) let any caller key
    file a correction against — and read the verdict on — any other caller's
    request, which is a different person's question and answer. And the rule has
    to live here rather than in the CLI: a client-side check is not a boundary,
    since the same key reaches the same endpoint with curl.

    Admin stays org-wide deliberately. The data team's triage (`dst rule`,
    the dashboard queue, `patches draft`) rules on tickets raised by other
    people, and the auto_review path opens tickets for callers it is not."""
    if caller.is_admin or owner == caller.name:
        return
    raise HTTPException(status_code=403, detail=detail)


@data_router.post("", status_code=201)
def send_for_review(
    body: ReviewRequest, caller: CallerIdentity = Depends(get_caller)
) -> dict[str, object]:
    """Open a review ticket on a served answer, optionally carrying a correction.
    Needs the `write` scope, and only the caller the answer was served to may
    file (admin excepted); the judge pre-screens the ticket into the queue."""
    # The only mutating route on the data plane, so it is the only place `read`
    # differs from `write`. Enforced here rather than by HTTP method: POST
    # /v1/lenses/{n}/query runs a SELECT and is a read, so the verb is not the
    # discriminator — what the call DOES is.
    scopes.require(caller.scopes, scopes.WRITE)
    with org_session(caller.org_id) as session:
        trace = store.get_trace(session, body.request_id)
        if trace is None:
            raise HTTPException(status_code=404, detail=_untraced_detail(body.request_id))
        _own_or_403(
            caller,
            trace.caller,
            f"caller '{caller.name}' may only file corrections against their own "
            f"requests, and '{body.request_id}' was served to a different caller",
        )
        llm, judge_model = service.judge_llm()
        # Caller-initiated (REST / MCP send_for_review) — a human-raised ticket.
        ticket = service.open_review(
            session, trace, llm, model=judge_model, correction=body.correction, origin="human"
        )
    return _ticket_payload(ticket)


@data_router.get("")
def my_reviews(
    state: str | None = None, caller: CallerIdentity = Depends(get_caller)
) -> list[store.Ticket]:
    """The tickets raised on THIS caller's own requests — the reporter's half of
    the review queue, and the only one a caller key can read.

    The flywheel starts with a business user reporting a wrong answer, and it
    only feels closed when that person learns the outcome. Filing was reachable
    with a caller key; the verdict was not, unless the reporter had kept the
    ticket id — and the whole queue (`/mgmt/reviews`) is admin-only, correctly.
    So the data plane gets the scoped list: my requests, my tickets, their state.
    """
    with org_session(caller.org_id) as session:
        return store.list_for_caller(session, caller.name, state)


@data_router.get("/{ticket_id}")
def get_review(ticket_id: str, caller: CallerIdentity = Depends(get_caller)) -> dict[str, object]:
    """One ticket's state + verdict — readable only by the caller whose request
    it was raised on (or an admin)."""
    with org_session(caller.org_id) as session:
        ticket = store.get_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"review '{ticket_id}' not found")
    _own_or_403(
        caller,
        ticket.caller,
        f"review '{ticket_id}' belongs to a different caller than '{caller.name}'",
    )
    return _ticket_payload(ticket)


@mgmt_router.get("")
def review_queue(
    state: str | None = None, session: Session = Depends(get_app_session)
) -> list[store.Ticket]:
    """The whole org's review queue — every ticket, every origin, optionally
    filtered by state. Admin-only; callers read their own via `GET /v1/reviews`."""
    return store.list_queue(session, state)


@mgmt_router.get("/judge-agreement")
def judge_agreement(
    rejudge: bool = False,
    limit: int = 50,
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """Judge calibration over decided tickets.

    ``stored`` compares the judge's recorded verdicts against human rulings —
    free, no LLM. ``?rejudge=true`` re-scores the same traces with today's
    judge (one call per row, capped by ``limit``): ``vs_human`` is current
    calibration, ``vs_stored`` isolates judge drift.
    """
    from dataclasses import asdict

    from services.api.llm import require_llm

    out: dict[str, object] = {"stored": asdict(anchor.agreement(session))}
    if rejudge:
        judge = require_llm(registry.tier("smart"))
        out["rejudge"] = asdict(anchor.rejudge(session, judge.llm, judge.name, limit=limit))
    return out


@mgmt_router.post("/{ticket_id}/rule")
def rule_review(
    ticket_id: str,
    body: RuleBody,
    session: Session = Depends(get_app_session),
    identity: AdminIdentity = Depends(get_admin_identity),
) -> dict[str, object]:
    """Rule on a ticket — verdict `approve` | `changes` | `reject`, with reasoning
    and an optional correction. The ruling lands on the ticket; drafting the fix
    is the separate `draft-patch` step. The ruling records WHO ruled (the resolved
    admin identity), so a verdict is always evidence someone stands behind."""
    if body.verdict not in {"approve", "changes", "reject"}:
        raise HTTPException(status_code=400, detail="verdict must be approve|changes|reject")
    updated = store.set_human_ruling(
        session, ticket_id, body.verdict, body.reasoning, body.correction, ruled_by=identity.actor
    )
    if updated == 0:
        raise HTTPException(status_code=404, detail=f"review '{ticket_id}' not found")
    ticket = store.get_ticket(session, ticket_id)
    assert ticket is not None
    return _ticket_payload(ticket)


# ─── correction → drafted patch ──────────────────────────────────────────────


def _load_bundle(session: Session, lens: str) -> lens_store.LensBundle:
    """The lens's draft bundle (what a patch is drafted and proposed against), else
    its published one. Read-only here — a patch never writes it back."""
    row = lens_store.get_lens(session, lens)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{lens}' not found")
    raw = row.get("draft") or row.get("published")
    if raw is None:
        raise HTTPException(status_code=400, detail=f"lens '{lens}' has no bundle")
    return lens_store.LensBundle.model_validate(raw)


@mgmt_router.post("/{ticket_id}/draft-patch", status_code=201)
def draft_patch_for_ticket(
    ticket_id: str, session: Session = Depends(get_app_session)
) -> dict[str, object]:
    """Draft the smallest concrete fix for a correction-carrying ticket.

    Pure drafting happens in ``services/reviews/patch.py``; this endpoint only loads
    the inputs (ticket, trace, bundle, profiles, this ticket's REJECTED drafts),
    persists the candidate, and returns it as an approvable diff routed to its owner.

    The rejected drafts are the redraft's feedback: their ``rejection_note`` says
    what the reviewer wanted preserved or changed, and if nothing reads it a
    rejected draft is redrafted into the same mistake.
    """
    ticket = store.get_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"review '{ticket_id}' not found")
    if ticket.correction is None:
        raise HTTPException(
            status_code=400, detail="ticket has no correction to draft a patch from"
        )
    trace = store.get_trace(session, ticket.request_id)
    if trace is None:
        raise HTTPException(
            status_code=404, detail=f"no traced request '{ticket.request_id}' behind this ticket"
        )
    bundle = _load_bundle(session, ticket.lens)
    profiles = (
        profile_store.list_profiles(session, bundle.config.connections[0])
        if bundle.config.connections
        else []
    )
    model_ref = bundle.config.model.model_ref()
    resolved = registry.resolve(model_ref)
    # Keyless: no client, but the candidate still records the model it WOULD
    # have used — read from the registry, since a lens with no `model:` block
    # carries no name of its own.
    llm, patch_model = (
        (resolved.llm, resolved.name) if resolved else (None, registry.wire_name(model_ref))
    )
    prior = patch_store.list_for_ticket(session, ticket_id)
    rejected = [c for c in prior if c.status == "rejected"]
    candidate = patch.draft_patch(
        llm, ticket, trace, bundle, profiles, model=patch_model, rejections=rejected
    )
    if candidate is None:
        raise HTTPException(status_code=422, detail="no patch route applies to this correction")
    candidate.id = patch_store.record(session, candidate)
    return candidate.model_dump()


@patches_router.get("")
def list_patches(
    lens: str, status: str | None = None, session: Session = Depends(get_app_session)
) -> list[dict[str, object]]:
    """A lens's drafted patch candidates (all statuses unless filtered)."""
    return [c.model_dump() for c in patch_store.list_for_lens(session, lens, status)]


# ─── approve → govern what's server-side, propose what's authored ────────────


def _embed_question(session: Session, question: str) -> list[float]:
    embedder = require_embedder()
    embedding_meta.guard_write(session, embedder)
    return embedder.embed([question])[0]


def _apply_certified(
    session: Session, candidate: patch.PatchCandidate, question: str, approved_by: str
) -> dict[str, object]:
    """Certify question → corrected SQL; an existing pair for the exact question is
    replaced (the certified-tier fix: the stored pair itself was wrong). The pair is
    stamped ``source: review:*`` — server-origin provenance is what lets it survive
    applies whose certified_answers.yaml doesn't carry it (files own only
    file-originated rows), the same traceability rule as certify-from-request."""
    replaced = 0
    for existing in certify_store.list_for_lens(session, candidate.lens):
        if existing.question == question:
            replaced += certify_store.delete(session, existing.id)
    emb = _embed_question(session, question)
    origin = candidate.ticket_id or f"patch:{candidate.id}"
    cert_id = certify_store.create(
        session,
        candidate.lens,
        question,
        candidate.diff_after,
        emb,
        # The approver, not "patch": certified_by reaches trust_summary, and the
        # mechanism is already recorded by source=review:*.
        created_by=approved_by,
        source=f"review:{origin}",
    )
    return {"certified_id": cert_id, "replaced": replaced}


def _eval_case_from_patch(
    session: Session,
    candidate: patch.PatchCandidate,
    trace: store.Trace | None,
) -> str | None:
    """The correction becomes a new eval case — always ``candidate``,
    never auto-approved: a human promotes it into the baseline.

    NEVER from the SQL that prompted the correction. A definition/instruction patch is
    prose: the only SQL in hand is ``trace.sql``, the wrong answer the curator is
    correcting, and harvesting it would pin that wrong answer as the case's ORACLE —
    after which the lens can only pass by staying broken and ``dst plan``
    can never go clean. Those file a BEHAVIORAL case
    instead (``expect: answer``): the question must keep being answered with data,
    which is true regardless of which SQL is right, and it round-trips through
    export/apply so the case stops showing as a pending diff.

    A certified patch is the one shape that carries a verified oracle: a human just
    ruled ``diff_after`` correct and it was certified moments ago.
    """
    if candidate.kind not in {"definition", "instruction", "certified"}:
        return None  # reprofile/reindex carry no expected result to regress against
    if trace is None and candidate.kind != "certified":
        return None  # ticket-less non-certified candidate: no question to test
    question = trace.question if trace is not None else candidate.target
    if not question:
        return None
    verified_sql = candidate.diff_after if candidate.kind == "certified" else None
    if candidate.kind == "certified" and not verified_sql:
        return None
    return eval_store.create_case(
        session,
        candidate.lens,
        question,
        "harvested",
        expected_sql=verified_sql,
        expect=None if verified_sql else "answer",
        status="candidate",
        created_by=f"patch:{candidate.id}",
    )


@mgmt_router.post("/patches/{patch_id}/approve")
def approve_patch(
    patch_id: str,
    shared: bool = False,
    session: Session = Depends(get_app_session),
    identity: AdminIdentity = Depends(get_admin_identity),
) -> dict[str, object]:
    """Approve a drafted patch — a RULING, which PROPOSES a file change.

    ``?shared=true`` lands a definition patch in the org's SHARED layer
    (``semantic/definitions/``) instead of the lens's own tree. Without it, a term
    the lens didn't already compile from shared could only ever be proposed
    lens-locally, so a ruling on a cross-cutting term was silently confined to one
    lens. Promotion stays explicit — a shared page is every selecting lens's
    business, which is not a call the drafter gets to make.

    Governing writes land here (a certified pair): they are DB-first and survive
    apply. Authored truth does NOT: a definition and a lens's ``instructions`` are
    compiled from files on every ``dst apply``, so writing them into the bundle only
    looked applied — the next apply of the SAME unchanged files silently reverted
    the human's ruling. Those come back as ``proposed_file`` (path + whole new
    content + diff) and ``live: false``: not in effect until the file is committed
    and applied. The measurement of the fix is the apply's eval gate, which scores
    the lens that actually landed; re-running the regression set here scored the
    OLD lens and reported it as proof the fix worked.
    """
    candidate = patch_store.get(session, patch_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="patch candidate not found")
    if candidate.status != "candidate":
        raise HTTPException(status_code=409, detail=f"patch already {candidate.status}")

    trace: store.Trace | None = None
    if candidate.ticket_id is not None:
        ticket = store.get_ticket(session, candidate.ticket_id)
        if ticket is not None:
            trace = store.get_trace(session, ticket.request_id)

    bundle = _load_bundle(session, candidate.lens)
    applied: dict[str, object] = {}
    proposed: proposal.ProposedFile | None = None
    live = False
    if candidate.kind == "definition":
        proposed = proposal.propose_definition(candidate, bundle, shared=shared or None)
    elif candidate.kind == "instruction":
        proposed = proposal.propose_instruction(candidate, bundle)
        live = proposed is None  # the lens already carries this exact instruction
    elif candidate.kind == "certified":
        question = trace.question if trace is not None else candidate.target
        applied = _apply_certified(session, candidate, question, identity.actor)
        live = True
    else:  # reprofile / reindex: the approve names the follow-up, it doesn't run it
        applied = {"action": candidate.kind, "target": candidate.target}

    patch_store.approve(session, patch_id)
    case_id = _eval_case_from_patch(session, candidate, trace)
    return {
        "id": patch_id,
        "status": "approved",
        "kind": candidate.kind,
        "target": candidate.target,
        "live": live,
        "applied": applied,
        "proposed_file": proposed.model_dump() if proposed is not None else None,
        "next_step": _next_step(candidate, proposed, live),
        "eval_case_id": case_id,
    }


def _next_step(
    candidate: patch.PatchCandidate, proposed: proposal.ProposedFile | None, live: bool
) -> str:
    """The one act that makes the ruling real — named, never implied."""
    if proposed is not None:
        return (
            f"write {proposed.path}, commit it, then run `dst apply` — "
            "this ruling is not live until then"
        )
    if candidate.kind == "certified":
        return (
            f"live now; `dst export --lens {candidate.lens}` round-trips it into "
            f"lenses/{candidate.lens}/certified_answers.yaml"
        )
    if live:  # instruction: the lens's instructions already carry this sentence
        return "live now — the lens already carries this instruction"
    return f"follow-up, not yet run: {candidate.diff_after}"


class RejectBody(BaseModel):
    note: str = ""  # why the draft was declined — stored as drafter feedback


@mgmt_router.post("/patches/{patch_id}/reject")
def reject_patch(
    patch_id: str, body: RejectBody | None = None, session: Session = Depends(get_app_session)
) -> dict[str, str]:
    """Reject a drafted patch, recording why. The body is optional so
    note-less callers (the dashboard's plain reject) keep working."""
    note = (body.note if body is not None else "").strip() or None
    if patch_store.reject(session, patch_id, note) == 0:
        raise HTTPException(status_code=404, detail="patch candidate not found or already ruled")
    return {"id": patch_id, "status": "rejected"}
