"""Review endpoints.

Data plane (caller-authed): send a traced request for review, read status back.
Control plane (admin-authed): the data-team review queue + human ruling.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.auth import scopes
from services.auth.deps import AdminIdentity, get_admin_identity, get_app_session, get_caller
from services.config import settings
from services.contracts.correction import CorrectionDelta
from services.db.session import org_session
from services.governance.credentials import CallerIdentity
from services.lenses import store as lens_store
from services.reviews import service, store
from services.runtime import sql_guard

data_router = APIRouter(prefix="/v1/reviews", tags=["reviews"])
mgmt_router = APIRouter(prefix="/mgmt/reviews", tags=["reviews"])


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


def _ticket_payload(
    ticket: store.Ticket, explore: dict[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {**ticket.model_dump(), "url": _tracking_url(ticket.ticket_id)}
    if explore is not None:
        payload["explore"] = explore
    return payload


def _lens_model(session: Session, lens: str) -> lens_store.LensBundle | None:
    row = lens_store.get_lens(session, lens)
    raw = (row or {}).get("published") or (row or {}).get("draft")
    return lens_store.LensBundle.model_validate(raw) if raw is not None else None


def _explore_detail(session: Session, trace: store.Trace) -> dict[str, object] | None:
    """What a ticket on an outside-lens answer carries: the question asked,
    the SQL that ran, the exposed tables it read, and every check with what
    it decided. None for a governed answer."""
    if trace.lane != "explore":
        return None
    bundle = _lens_model(session, trace.lens)
    tables = sql_guard.exposed_tables(trace.sql, bundle.semantic_model) if bundle else []
    raw = (trace.verification or {}).get("checks")
    checks = [
        str(c.get("reason") or f"{c.get('name')}:{c.get('status')}")
        for c in (raw if isinstance(raw, list) else [])
        if isinstance(c, dict)
    ]
    return {
        "lane": "explore",
        "question": trace.question,
        "sql": trace.sql,
        "tables": tables,
        "checks": checks,
        "lines": list(trace.degraded or []),
    }


def _ticket_with_detail(session: Session, ticket: store.Ticket) -> dict[str, object]:
    trace = store.get_trace(session, ticket.request_id)
    return _ticket_payload(ticket, _explore_detail(session, trace) if trace else None)


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
        # A ticket on an outside-lens answer carries what the reviewer rules
        # on: the question, the SQL, the tables read and every check.
        explore = _explore_detail(session, trace)
    return _ticket_payload(ticket, explore)


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
        return _ticket_with_detail(session, ticket)


@mgmt_router.get("")
def review_queue(
    state: str | None = None, session: Session = Depends(get_app_session)
) -> list[store.Ticket]:
    """The whole org's review queue — every ticket, every origin, optionally
    filtered by state. Admin-only; callers read their own via `GET /v1/reviews`."""
    return store.list_queue(session, state)


@mgmt_router.post("/{ticket_id}/rule")
def rule_review(
    ticket_id: str,
    body: RuleBody,
    session: Session = Depends(get_app_session),
    identity: AdminIdentity = Depends(get_admin_identity),
) -> dict[str, object]:
    """Rule on a ticket — verdict `approve` | `changes` | `reject`, with reasoning
    and an optional correction. The ruling lands on the ticket; the fix itself is
    authored in the project's files. The ruling records WHO ruled (the resolved
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
    return _ticket_with_detail(session, ticket)


@mgmt_router.get("/{ticket_id}/seed")
def seed_for_review(
    ticket_id: str, session: Session = Depends(get_app_session)
) -> dict[str, object]:
    """The slot map `dst reviews seed-lens` scaffolds from: for an outside-lens
    answer, the exposed tables its SQL read with their fields and blueprint
    facts, beside the question and the SQL. The entity and lens files the CLI
    writes from this say WHERE the definitions go; the author's agent fills
    them. 409 on a ticket whose answer was governed: it has a lens already."""
    ticket = store.get_ticket(session, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"review '{ticket_id}' not found")
    trace = store.get_trace(session, ticket.request_id)
    if trace is None or trace.lane != "explore":
        raise HTTPException(
            status_code=409,
            detail=f"review '{ticket_id}' is not on an outside-lens answer; seed-lens "
            "scaffolds a lens over the exposed tables such an answer read",
        )
    bundle = _lens_model(session, trace.lens)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"lens '{trace.lens}' not found")
    model = bundle.semantic_model
    tables: list[dict[str, object]] = []
    for table in sql_guard.exposed_tables(trace.sql, model):
        entity = next((e for e in model.entities if e.source.table == table), None)
        if entity is None:
            continue
        blueprint = model.blueprints.get(table)
        tables.append(
            {
                "table": table,
                "connection": entity.source.connection,
                "entity": entity.name,
                "description": entity.description,
                "grain": blueprint.grain if blueprint else entity.grain,
                "key": list(blueprint.key) if blueprint and blueprint.key else [],
                "fields": [
                    {"name": f.name, "type": str(f.type), "description": f.description}
                    for f in entity.fields
                ],
                "clocks": {
                    c: {"meaning": clock.meaning, "timezone": clock.timezone}
                    for c, clock in (blueprint.clocks if blueprint else {}).items()
                },
                "caveats": list(blueprint.caveats) if blueprint else [],
            }
        )
    return {
        "ticket_id": ticket_id,
        "lens": trace.lens,
        "question": trace.question,
        "sql": trace.sql,
        "tables": tables,
    }
