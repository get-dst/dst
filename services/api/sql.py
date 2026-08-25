"""Governed read-only SQL probe: POST /v1/sql.

`introspect` says what the columns ARE; it cannot say what they CONTAIN together.
Deciding "is a refund a negative amount or a row with status='refunded'?" needs
five actual rows. Without a verb for that, authors open a warehouse client
directly: not confused — `dst introspect` worked fine — just missing
`SELECT * LIMIT 5`. Every such read is hand-written SQL run outside the guard,
outside the row caps and outside the audit log, against a warehouse whose
credential dst holds precisely so that would not happen.

Deciding a business rule from data is the author's job. dst's job is to make
that governed rather than ungoverned, so the same read runs here:

  * SELECT-only, single statement, no DML/DDL anywhere — `runtime.sql_guard`, the
    same guard the serving path runs generated SQL through;
  * row-capped, and the cap is honest about being hit (`truncated`);
  * logged to `request_log`, so the probe behind an authoring decision sits in
    the audit trail next to the answers that decision produced.

Two scopes, one door:

  * ``connection`` — the AUTHORING probe, admin-only (a connection is an org-level
    credential, not a caller-scoped grant). Guarded with `trust_tables` and
    `allow_star`: the admin already holds this credential and can profile every
    column, so the allow-list has nothing to protect here — SELECT-only and the
    row cap are the guarantees.
  * ``lens`` — the SERVING probe, any authorized caller, and what the MCP tool
    uses. Guarded against the lens's compiled model exactly like generated SQL:
    the allow-list holds, and `SELECT *` stays refused because a star is how a
    column the lens does not expose would come back.

The lens scope takes its connector from `_authorized_connector` rather than
resolving one itself: this door once shipped reaching past the authorization gate
for its own, and — because this is what the MCP tool calls — an agent was the one
holding the wider reach. Doors ask the gate; the gate decides.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from services.api.query import _authorized_bundle, _authorized_connector
from services.auth.deps import get_caller
from services.contracts.protocols import Connector
from services.contracts.semantic_model import SemanticModel
from services.contracts.trace import TraceLog
from services.contracts.warehouse import QueryResult
from services.governance.credentials import CallerIdentity
from services.lenses.connections import ConnectionUnavailable, resolve_connector
from services.observability import cost
from services.observability.logger import log_trace
from services.runtime import sql_guard

log = logging.getLogger("dst")

router = APIRouter(prefix="/v1", tags=["query"])

# A probe answers "what do these rows look like", not "serve me a dataset". Serving's
# FETCH_CAP (5000) is the wrong size for a terminal: nobody reads 5000 rows to decide
# what a column means. The default fits a screen; the max is the guarantee.
PROBE_DEFAULT_ROWS = 20
PROBE_MAX_ROWS = 500


class SqlBody(BaseModel):
    sql: str
    connection: str | None = None
    lens: str | None = None
    limit: int = Field(default=PROBE_DEFAULT_ROWS, ge=1, le=PROBE_MAX_ROWS)


class SqlResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    # The cap was reached, so these are not all the rows the query matched — said
    # rather than implied, because a silently truncated probe is a wrong conclusion
    # about the data, drawn confidently.
    truncated: bool
    scope: str
    request_id: str


def _probe_model(dialect: str) -> SemanticModel:
    """A model carrying only the dialect: the connection probe has no lens, and with
    `trust_tables` the allow-list is not consulted. Validated rather than constructed
    so an unmodelled connector kind fails here, not inside sqlglot."""
    return SemanticModel.model_validate({"lens": "sql", "dialect": dialect})


def _connection_connector(connection: str, caller: CallerIdentity) -> Connector:
    """The connector for the ADMIN door — connection-scoped, not lens-scoped, and
    a function of its own so that is a decision instead of a branch.

    A connection is an org-level credential the admin already holds, so there is
    no lens grant to resolve it through; the caller check above is what stands
    between this and a caller key.
    """
    try:
        return resolve_connector(connection, caller.org_id, caller=caller)
    except ConnectionUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _run(body: SqlBody, background: BackgroundTasks, caller: CallerIdentity) -> SqlResponse:
    if bool(body.connection) == bool(body.lens):
        raise HTTPException(
            status_code=400, detail="pass exactly one of `connection` (authoring) or `lens`"
        )
    request_id = "req_" + uuid.uuid4().hex[:16]

    if body.connection:
        if not caller.is_admin:
            raise HTTPException(
                status_code=403,
                detail="probing a whole connection needs an admin token; a caller key can "
                f"probe a lens it is granted — pass `lens` instead of "
                f"`connection: {body.connection}`",
            )
        connector = _connection_connector(body.connection, caller)
        scope = f"sql:{body.connection}"
        guard = sql_guard.check(
            body.sql, _probe_model(connector.kind), trust_tables=True, allow_star=True
        )
    else:
        assert body.lens is not None
        bundle = _authorized_bundle(body.lens, caller)
        try:
            connector = _authorized_connector(bundle, caller)
        except ConnectionUnavailable as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        scope = body.lens
        guard = sql_guard.check(body.sql, bundle.semantic_model)

    trace = TraceLog(
        request_id=request_id,
        org_id=str(caller.org_id),
        lens=scope,
        caller=caller.name,
        question=body.sql,
        sql=body.sql,
        # Which path produced the SQL. Not a generation tier — a human wrote it, which
        # is exactly what makes the row worth keeping.
        generator_tier="probe",
    )
    if not guard.ok:
        # Logged synchronously, not as a background task: the failure paths raise, and
        # FastAPI drops a request's BackgroundTasks when an exception replaces the
        # response — so the refusals, which are the rows an audit most wants, would be
        # the only ones missing. (Same reason `_authorized_bundle` audits inline.)
        trace.status, trace.valid, trace.error = "rejected", False, guard.reason
        log_trace(trace)
        raise HTTPException(status_code=400, detail=f"SQL refused: {guard.reason}")

    assert guard.sql is not None
    try:
        # +1 row is the "did we hit the cap" probe bit, the same idiom the serving
        # pipeline uses; it is trimmed before the rows leave here.
        result: QueryResult = connector.execute(guard.sql, read_only=True, row_limit=body.limit + 1)
    except Exception as exc:  # noqa: BLE001 — a warehouse error is the answer, not a 500
        trace.status, trace.valid, trace.error = "error", True, str(exc)[:500]
        log_trace(trace)
        raise HTTPException(status_code=400, detail=f"warehouse error: {str(exc)[:500]}") from exc

    truncated = len(result.rows) > body.limit
    rows = [list(r) for r in result.rows[: body.limit]]
    trace.valid, trace.row_count, trace.wh_bytes = True, len(rows), result.bytes_scanned
    # Priced exactly like the lens-query trace (pipeline.py): bytes recorded
    # without a cost read as $0.00 in observe, so a heavy authoring phase can
    # understate org warehouse spend by an order of magnitude.
    trace.wh_cost_usd = cost.warehouse_cost_usd(result.bytes_scanned, connector.kind)
    background.add_task(log_trace, trace)
    return SqlResponse(
        columns=list(result.columns),
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        scope=scope,
        request_id=request_id,
    )


@router.post("/sql", response_model=SqlResponse)
async def run_sql(
    body: SqlBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> SqlResponse:
    """Run read-only SQL against a connection (admin) or a lens's allow-list (caller)."""
    return await run_in_threadpool(_run, body, background, caller)
