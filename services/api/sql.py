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
    the allow-list holds (an entity's `hide` is absent from it), `SELECT *` stays
    refused because a star is how a column the lens does not expose would come
    back, and the lens's `not_computable` refusals apply to the statement the
    way they apply to a question.

The lens scope takes its connector from `_authorized_connector` rather than
resolving one itself: this door once shipped reaching past the authorization gate
for its own, and — because this is what the MCP tool calls — an agent was the one
holding the wider reach. Doors ask the gate; the gate decides.
"""

from __future__ import annotations

import logging
import uuid
from functools import partial
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from services.api.query import (
    PURPOSE_HELP,
    PURPOSE_MAX,
    _authorized_bundle,
    _authorized_connector,
    _logged_purpose,
)
from services.auth.deps import get_caller
from services.contracts.failure_modes import FailureMode
from services.contracts.profile import TableProfile
from services.contracts.protocols import Connector
from services.contracts.response import Receipt
from services.contracts.semantic_model import SemanticModel
from services.contracts.trace import TraceLog
from services.contracts.verification import VerificationCheck, VerificationReport
from services.contracts.warehouse import QueryResult
from services.governance.credentials import CallerIdentity
from services.lenses.connections import ConnectionUnavailable, resolve_connector
from services.observability import cost
from services.observability.logger import log_trace
from services.runtime import explore_checks, grain, scope_rails, sql_guard
from services.runtime import receipt as receipts
from services.runtime.bounded import WarehouseTimeout, warehouse_bounded
from services.runtime.explore_checks import clock_columns
from services.runtime.pipeline import not_computable_refusal

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
    purpose: str = Field(default="", max_length=PURPOSE_MAX, description=PURPOSE_HELP)
    # The question this read answers, when it answers one: what the request
    # log keeps as the question (the SQL otherwise), what a review ticket on
    # the answer carries, and the question side of the pair an approval
    # certifies. A look at five rows has none.
    question: str = Field(default="", max_length=2000)


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
    # For each exposed table the statement read: rows against distinct key, or
    # "could not verify" and why (services/runtime/grain). Lens scope only.
    grain: list[str] = Field(default_factory=list)
    # For each exposed table read: where its dated columns end, from the
    # stored profile, or "could not verify" and why. Lens scope only.
    data_end: list[str] = Field(default_factory=list)
    # For each time column the statement reads from an exposed table: what
    # it records per the table's blueprint, or that no clock is declared.
    clock: list[str] = Field(default_factory=list)
    # Every post-check, as the receipt lists it: ``name:outcome``, or
    # ``name:exposed, could not verify …`` where a check could not run, or
    # ``name:skip (unverifiable): could not close: …`` (services/runtime/
    # explore_checks: join conservation, clocks, reconciliation).
    checks: list[str] = Field(default_factory=list)
    # The SQL that ran, as the guard rewrote it: shown with every outside-lens
    # answer, so the reader can see what the number is a count of.
    sql: str | None = None
    # The mode the first disclosing check carries (a join that multiplies
    # rows: wrong_grain; a delta against a certified number: unverifiable).
    mode: FailureMode | None = None
    receipt: Receipt | None = None


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
    # Set on the lens door only: a warehouse error's suggestions are then kept
    # to that lens's scope. The admin already sees the whole connection.
    scope_model: SemanticModel | None = None

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
        wh = body.connection
        guard = sql_guard.check(
            body.sql, _probe_model(connector.kind), trust_tables=True, allow_star=True
        )
        # No lens here, so nothing has opted in to keeping the sentence: the admin
        # scope never stores a purpose, whatever the body carried.
        purpose: str | None = None
    else:
        assert body.lens is not None
        bundle = _authorized_bundle(body.lens, caller)
        try:
            connector = _authorized_connector(bundle, caller)
        except ConnectionUnavailable as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        scope = body.lens
        wh = ", ".join(sorted({e.source.connection for e in bundle.semantic_model.entities}))
        scope_model = bundle.semantic_model
        guard = sql_guard.check(body.sql, bundle.semantic_model)
        # The lens's declared refusals hold on this door too: a statement that
        # names a column a `not_computable` entry names, or lists rows per
        # entity key where the entry is row-level, is refused with the same
        # sentence `query` would answer with. One rule, every door — the
        # governed path refusing a birth date while this one served it was
        # the same lens saying two things.
        if guard.ok and (hit := sql_guard.not_computable_hit(body.sql, bundle.semantic_model)):
            guard = sql_guard.GuardResult(False, reason=not_computable_refusal(hit))
        # Kept only when the lens sets `logging.log_purpose`, exactly as on /query:
        # the probe behind an authoring decision is the read whose "why" explains it.
        purpose = _logged_purpose(body.purpose, bundle.config)

    trace = TraceLog(
        request_id=request_id,
        org_id=str(caller.org_id),
        lens=scope,
        caller=caller.name,
        question=body.question.strip() or body.sql,
        sql=body.sql,
        # Which path produced the SQL. Not a generation tier — a human wrote it, which
        # is exactly what makes the row worth keeping.
        generator_tier="probe",
        # Set once, before any log_trace: the refusal and error paths log too.
        purpose=purpose,
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
        result: QueryResult = warehouse_bounded(
            "the query",
            partial(connector.execute, guard.sql, read_only=True, row_limit=body.limit + 1),
            connection=wh or "?",
        )
    except WarehouseTimeout as exc:
        trace.status, trace.valid, trace.error = "error", True, str(exc)
        log_trace(trace)
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — a warehouse error is the answer, not a 500
        trace.status, trace.valid, trace.error = "error", True, str(exc)[:500]
        log_trace(trace)
        shown = str(exc)[:500]
        if scope_model is not None:
            shown = scope_rails.scoped_suggestions(shown, scope_model)
        raise HTTPException(status_code=400, detail=f"warehouse error: {shown}") from exc

    truncated = len(result.rows) > body.limit
    rows = [list(r) for r in result.rows[: body.limit]]
    grain_lines: list[str] = []
    end_lines: list[str] = []
    clock_lines: list[str] = []
    checks: list[str] = []
    report = VerificationReport(grade="unverified")
    degraded: list[str] = []
    mode: FailureMode | None = None
    if scope_model is not None and (exposed := sql_guard.exposed_tables(guard.sql, scope_model)):
        readings, profiles, fresh = _grain_readings(scope_model, exposed, connector, caller)
        grain_lines = [r.line for r in readings]
        checks += [
            f"grain:exposed, {r.line}" if r.status == "unverified" else f"grain:{r.status}"
            for r in readings
        ]
        report = grain.fold(report, readings)
        for table in exposed:
            line, ok = _data_end_line(scope_model, table, profiles, fresh)
            end_lines.append(line)
            checks.append("data_end:pass" if ok else f"data_end:exposed, {line}")
            report.checks.append(
                VerificationCheck(name="data_end", status="pass" if ok else "skip", reason=line)
            )
        # Beyond the grain line: row conservation across the statement's
        # joins, the clock of every time column it reads, and its number
        # against a certified answer over the same tables and window. Each
        # is a line on the receipt; one that could not run says so.
        more = _explore_checks(guard.sql, rows, scope_model, exposed, connector, caller, wh)
        clock_lines = [c.detail for c in more if c.name == "clock"]
        checks += [c.line for c in more]
        mode = explore_checks.mode_of(more)
        report = explore_checks.fold(report, more)
        what = "a table" if len(exposed) == 1 else "tables"
        degraded = [
            f"EXPOSED: reads {', '.join(exposed)}, {what} with no governed definitions",
            *grain.lines(readings),
            *explore_checks.lines(more),
        ]
    if rows:
        checks.append("empty_result:pass")
        report.checks.append(VerificationCheck(name="empty_result", status="pass"))
    else:
        # No rows is not a zero: the filter values or the window may be what
        # is wrong, and nothing here can tell which.
        empty = (
            "the statement returned no rows; an empty result is not a zero — check the "
            "filter values and where the data ends before reporting one"
        )
        checks.append(f"empty_result:disclose: {empty}")
        report.checks.append(VerificationCheck(name="empty_result", status="fail", reason=empty))
    trace.confidence = "unverified"
    trace.valid, trace.row_count, trace.wh_bytes = True, len(rows), result.bytes_scanned
    if scope_model is not None:
        # An outside-lens read: its own lane in the request log, every check
        # on the row a review ticket reads, and the mode the receipt carries.
        trace.lane = "explore"
        trace.verification, trace.degraded, trace.mode = report, degraded, mode
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
        grain=grain_lines,
        data_end=end_lines,
        clock=clock_lines,
        checks=checks,
        sql=guard.sql,
        mode=mode,
        # The rows are the caller's own SQL over the lens's tables: nothing
        # governed their meaning, so the receipt never grades them verified.
        receipt=receipts.build(
            request_id=request_id,
            lens=scope,
            certification="none",
            cert_id=None,
            confidence="unverified",
            sql=body.sql,
            data_as_of=None,
            mode=mode,
            checks=checks,
        ),
    )


def _grain_lines(
    model: SemanticModel, tables: list[str], connector: Connector, caller: CallerIdentity
) -> list[str]:
    """The grain check over the exposed tables a lens-scope statement read: the
    same rule an EXPOSED `query` answer carries, from the stored profiles of
    the tables' connections while they are within probe.max_age."""
    return [r.line for r in _grain_readings(model, tables, connector, caller)[0]]


def _explore_checks(
    sql: str,
    rows: list[list[Any]],
    model: SemanticModel,
    exposed: list[str],
    connector: Connector,
    caller: CallerIdentity,
    connection: str,
) -> list[explore_checks.ExploreCheck]:
    """The checks beyond grain on an outside-lens read (runtime.explore_checks),
    the reconciliation reading the lens's active certified answers."""
    out: list[explore_checks.ExploreCheck] = []
    join = explore_checks.join_check(sql, model, connector, connection=connection)
    if join is not None:
        out.append(join)
    out += explore_checks.clock_checks(sql, model, exposed)
    out.append(
        explore_checks.reconcile_check(
            sql, rows, _certified_pairs(model.lens, caller), model, connector, connection=connection
        )
    )
    return out


def _certified_pairs(lens: str, caller: CallerIdentity) -> list[tuple[str, str]]:
    """The lens's active, slot-free certified pairs as ``(question, sql)``:
    the governed numbers an outside-lens read can be reconciled against."""
    from services.certify import store as certify_store
    from services.db.session import org_session

    try:
        with org_session(caller.org_id) as session:
            return [
                (a.question, a.sql)
                for a in certify_store.list_for_lens(session, lens)
                if certify_store.is_active(a.status) and not a.slots and a.sql
            ]
    except Exception:  # noqa: BLE001 — without the corpus the check says it could not close
        log.exception("certified lookup for the reconcile check failed")
        return []


def _data_end_line(
    model: SemanticModel, table: str, profiles: dict[str, TableProfile], fresh: bool
) -> tuple[str, bool]:
    """``(line, verified)``: where an exposed table's dated columns end, from
    its stored profile within probe.max_age, or why that could not be told."""
    entity = next((e for e in model.entities if e.source.table == table), None)
    columns = clock_columns(entity, model) if entity is not None else []
    if not columns:
        return f"could not verify the data end of {table}: it has no dated column", False
    profile = grain.profile_for(profiles, table)
    if profile is None:
        return f"could not verify the data end of {table}: no fresh stored profile", False
    ends = {
        c.name: c.max for c in profile.columns if any(c.name.lower() == x.lower() for x in columns)
    }
    shown = [f"{name} through {end[:10]}" for name, end in ends.items() if end]
    if not shown:
        return f"could not verify the data end of {table}: the probe holds no maximum", False
    probed = profile.profiled_at.date().isoformat()
    return f"DATA END {table}: {', '.join(shown)} (probe of {probed})", fresh


def _grain_readings(
    model: SemanticModel, tables: list[str], connector: Connector, caller: CallerIdentity
) -> tuple[list[grain.GrainReading], dict[str, TableProfile], bool]:
    """The grain readings, and the fresh stored profiles they read."""
    from services.db.session import org_session
    from services.lenses import profile_store
    from services.project import probe_settings
    from services.project.blueprints import fresh_within

    connections = {e.source.connection for e in model.entities if e.source.table in tables}
    # A profile past probe.max_age proves nothing about today's rows: it is
    # left out, and the check counts live.
    profiles: dict[str, TableProfile] = {}
    max_age = None
    try:
        with org_session(caller.org_id) as session:
            max_age = probe_settings.load(session).max_age_delta
            fresh = fresh_within(max_age)
            for connection in connections:
                for stored in profile_store.list_profiles(session, connection):
                    if fresh(stored.profile):
                        profiles[stored.profile.table] = stored.profile
    except Exception:  # noqa: BLE001 — without profiles the check counts live
        log.exception("profile lookup for the grain check failed")
    readings = grain.read_grain(
        model,
        tables,
        profiles=profiles,
        fresh=True,
        connector=connector,
        scope=str(caller.org_id),
        ttl=max_age,
    )
    return readings, profiles, True


@router.post("/sql", response_model=SqlResponse)
async def run_sql(
    body: SqlBody,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> SqlResponse:
    """Run read-only SQL against a connection (admin) or a lens's allow-list (caller)."""
    return await run_in_threadpool(_run, body, background, caller)
