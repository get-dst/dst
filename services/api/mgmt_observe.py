"""Observe (read-only) endpoints: KPIs + per-lens caller report. Admin-authed."""

from __future__ import annotations

import csv
import datetime as _dt
import io
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from services.auth.deps import get_app_session
from services.observability import observe

router = APIRouter(prefix="/mgmt/observe", tags=["observe"])
# The request ledger export lives beside the observe reads, at /mgmt/requests.
ledger_router = APIRouter(prefix="/mgmt", tags=["observe"])


@router.get("/kpis")
def get_kpis(
    include_test: bool = False, session: Session = Depends(get_app_session)
) -> dict[str, object]:
    """The Observe headline numbers. `errors` counts status='error' only —
    refusals and clarifications are governed declines with their own number
    (`outcomes` carries the full split), never faults. Test traffic (agent
    `dst-test` or `benchmark`) is left out unless `include_test`;
    `test_requests` counts what was left out."""
    return observe.kpis(session, include_test=include_test)


@router.get("/audit")
def get_audit_statement(
    days: int = 30, include_test: bool = False, session: Session = Depends(get_app_session)
) -> dict[str, object]:
    """The Statement behind the audit page: asked/answered/verified with a
    prior-window comparison, the daily series, cost, and the per-lens rollup —
    one payload, every figure traceable to request_log rows."""
    return observe.audit_statement(session, days=max(1, min(days, 365)), include_test=include_test)


@router.get("/callers")
def get_callers(
    lens: str | None = None,
    include_test: bool = False,
    session: Session = Depends(get_app_session),
) -> list[dict[str, object]]:
    """Per-caller rollup — queries, cost, errors vs governed declines —
    optionally scoped to one lens; test traffic out unless `include_test`."""
    return observe.caller_report(session, lens, include_test=include_test)


@router.get("/evals")
def get_eval_trend(session: Session = Depends(get_app_session)) -> list[dict[str, object]]:
    """Per-lens accuracy trend — is each lens getting better?"""
    return observe.eval_trend(session)


@router.get("/runs")
def get_runs(last: int = 5, session: Session = Depends(get_app_session)) -> dict[str, object]:
    """Every lens's ``last`` recorded eval runs, newest first, in one call —
    the cross-lens history `dst runs --all` prints."""
    from services.evals import store

    by_lens = store.recent_runs_by_lens(session, max(0, min(last, 1000)))
    return {
        "runs": [
            {
                "lens": lens,
                "runs": [
                    {
                        "id": r.id,
                        "mode": r.mode,
                        "score": r.score,
                        "passed": r.passed,
                        "failed": r.failed,
                        "errored": r.errored,
                        "started_at": r.started_at,
                    }
                    for r in runs
                ],
            }
            for lens, runs in by_lens.items()
        ]
    }


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


@ledger_router.get("/requests", response_model=None)
def export_requests(
    since: str | None = None,
    until: str | None = None,
    lens: str | None = None,
    caller: str | None = None,
    format: Literal["csv", "json"] = "json",
    session: Session = Depends(get_app_session),
) -> Response | list[dict[str, object]]:
    """The request ledger export: one row per request in the window (`since`
    inclusive, `until` exclusive, ISO dates or instants), optionally one lens
    or one caller — `ts, caller, agent, question, lens, covered, answer_kind
    (certified|generated|clarify|refuse|error), certification, request_id`.
    Every row, uncapped; `format=csv` for a spreadsheet."""
    for value in (since, until):
        if value:
            try:
                _dt.datetime.fromisoformat(value)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"{value!r} is not an ISO date or instant"
                ) from exc
    rows = observe.request_export(session, since=since, until=until, lens=lens, caller=caller)
    if format == "json":
        return rows
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(observe.EXPORT_COLUMNS))
    writer.writeheader()
    writer.writerows(rows)
    return Response(content=buf.getvalue(), media_type="text/csv")
