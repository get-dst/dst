"""Observe (read-only) endpoints: KPIs + per-lens caller report. Admin-authed."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from services.auth.deps import get_app_session
from services.observability import observe

router = APIRouter(prefix="/mgmt/observe", tags=["observe"])


@router.get("/kpis")
def get_kpis(session: Session = Depends(get_app_session)) -> dict[str, object]:
    """The Observe headline numbers. `errors` counts status='error' only —
    refusals and clarifications are governed declines with their own number
    (`outcomes` carries the full split), never faults."""
    return observe.kpis(session)


@router.get("/callers")
def get_callers(
    lens: str | None = None, session: Session = Depends(get_app_session)
) -> list[dict[str, object]]:
    """Per-caller rollup — queries, cost, errors vs governed declines —
    optionally scoped to one lens."""
    return observe.caller_report(session, lens)


@router.get("/evals")
def get_eval_trend(session: Session = Depends(get_app_session)) -> list[dict[str, object]]:
    """Per-lens accuracy trend — is each lens getting better?"""
    return observe.eval_trend(session)


@router.get("/requests")
def get_requests(
    lens: str | None = None,
    status: str | None = None,
    limit: int = 50,
    session: Session = Depends(get_app_session),
) -> list[dict[str, object]]:
    """Recent request summaries for the trace explorer, filterable by lens and
    status. `status` is the stored triage field — refused/clarification/rejected
    are governed declines, 'error' is a fault."""
    # There is no offset/cursor, so the cap IS the only page — at 200 it
    # silently hid the tail of any org past its first afternoon. Admin-authed,
    # read-only summaries; the generous ceiling just bounds a runaway query.
    return observe.recent_requests(session, min(limit, 10000), lens, status)


@router.get("/requests/{request_id}")
def get_request(request_id: str, session: Session = Depends(get_app_session)) -> dict[str, object]:
    """The full trace for one request — question, SQL, answer, citations,
    confidence, tokens, cost, outcome. 404 for an unknown id."""
    trace = observe.request_detail(session, request_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"request '{request_id}' not found")
    return trace
