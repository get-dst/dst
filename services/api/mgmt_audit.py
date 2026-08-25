"""The wrongness probe surfaced in the app. Admin-authed.

The audit is no longer a button-press: runs are **persisted** and the dashboard
treats the latest one as *standing results* you examine, with refresh / timeframe
as secondary controls — audits happen regularly and the results sit there.

``GET /mgmt/connections/{name}/audit/latest`` → the most recent persisted run for
the connection (``{"found": false}`` if it has never run). This is what the page
default-loads.

``POST /mgmt/connections/{name}/audit`` runs the Definition Drift audit against
one of the org's warehouse connections — query history → drift miner →
optional read-only execution of each variant — **records the run** (audit_run,
migration 0018), and returns it. The run path is factored into a thin
``run_and_store(session, connection, days, execute)`` so a scheduled job can drive
the SAME flow. (This module does not schedule anything itself.) Connectors without
a history catalog are a clear 400, not a silent empty report.

``POST /mgmt/connections/{name}/audit/draft-definition`` is the bridge: one
conflict finding + a chosen lens → the drafted canon definition
(``services/probe/report.draft_definition`` — deterministic canon choice,
rejected readings kept in the body), recorded as a ticket-less
``PatchCandidate(kind="definition")`` through the patch store. Audit
findings ride the SAME approval rail as corrections and distillation:
``GET /mgmt/lenses/{lens}/patches`` and the Reviews UI.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from google.api_core.exceptions import Forbidden
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.api import audit_store
from services.api.audit_store import AuditRun
from services.api.mgmt_gap_map import compute_gap_map
from services.auth.deps import get_admin_org, get_app_session
from services.contracts.protocols import Connector
from services.contracts.query_history import HistoryReader
from services.definitions import standards as std_store
from services.lenses import store as lens_store
from services.lenses.connections import resolve_connector
from services.llm import registry
from services.observability import observe
from services.probe.correctness import annotate_correctness
from services.probe.drift import DriftFinding, execute_variants, mine_drift
from services.probe.gap_map import Gap
from services.probe.report import annotate_against_canon, draft_definition
from services.reviews import patch_store
from services.reviews.patch import PatchCandidate

router = APIRouter(prefix="/mgmt/connections/{name}/audit", tags=["audit"])
log = logging.getLogger("dst")

# The history catalog exists but the connection's credentials can't read it (BigQuery
# raises 403 Forbidden when the service account lacks bigquery.jobs.listAll).
PERMISSION_HINT = (
    "the connection's credentials cannot read query history — grant BigQuery "
    "Resource Viewer at project level to enable query-history drift audits"
)


def _resolve_history_connector(connection: str, org_id: uuid.UUID | None) -> Connector:
    """The connection's connector, proven to read query history — the audit's precondition.

    Returns the connector (a ``Connector`` that is also a ``HistoryReader``); raises
    HTTPException(404) for an unresolvable connection and 400 for a connector with no
    history catalog (MySQL, object stores, …), so callers map those to a clear status
    instead of an infinite "scanning…".
    """
    try:
        connector = resolve_connector(connection, org_id)
    except Exception as exc:  # noqa: BLE001 — unknown name / bad creds surface to the caller
        raise HTTPException(
            status_code=404, detail=f"connection '{connection}' could not be resolved: {exc}"
        ) from exc
    if not isinstance(connector, HistoryReader):
        raise HTTPException(
            status_code=400,
            detail=(
                f"connection '{connection}' ({connector.kind}) cannot read its warehouse's "
                "query history — the audit needs a history catalog (e.g. Snowflake "
                "ACCOUNT_USAGE, BigQuery INFORMATION_SCHEMA.JOBS)"
            ),
        )
    return connector


def _mine(
    session: Session, connector: Connector, days: int, *, execute: bool
) -> tuple[int, list[DriftFinding]]:
    """Mine (and optionally execute) the drift findings for one connector.

    The pure miner is in ``services.probe.drift``; this wires it to the org's LLM policy
    (whatever ``registry.tier("fast")`` resolves to — no vendor is assumed) and the
    skill-aware correctness pass. ``execute=false`` skips running variants — faster,
    zero warehouse reads beyond the history catalog. The connector is a
    ``HistoryReader`` by construction (``_resolve_history_connector``).
    """
    assert isinstance(connector, HistoryReader)  # guaranteed by _resolve_history_connector
    records = connector.query_history(days=days)
    # `tier()` returns a REF ("deepseek/deepseek-v4-flash"); the wire wants the
    # model half. Passing the ref through put "provider/model" in the request
    # body — a 404 on every install, Anthropic included — which the standing
    # path (`run_standing_audits`) swallowed, so scheduled audits produced
    # nothing but fallback names. The name always comes from the pair that
    # produced the client.
    resolved = registry.resolve(registry.tier("fast"))
    llm, model = (resolved.llm, resolved.name) if resolved else (None, "")
    findings = mine_drift(records, llm, dialect=connector.kind, model=model)
    if execute:
        findings = [execute_variants(connector, finding) for finding in findings]
    # Convention-aware correctness: tier each variant and crown the right reading
    # (medallion gold > silver > bronze, not a vote). Runs after execute so the LLM
    # tiebreak — only on a genuine tie — sees the observed numbers.
    findings = annotate_correctness(findings, llm=llm, model=model)
    # Declared-canon match: when the org has declared standard definitions
    # (authored or compiled from dbt), tell each finding whether its popular reading is
    # the one the org declared. No standards → findings unchanged (no declared canon).
    declared = std_store.list_standards(session)
    if declared:
        findings = annotate_against_canon(findings, declared)
    return len(records), findings


def run_and_store(
    session: Session,
    connection: str,
    days: int = 30,
    *,
    execute: bool = True,
    org_id: uuid.UUID | None = None,
) -> AuditRun:
    """Mine + (optionally) execute the audit, then persist it — the synchronous flow.

    This is the seam the manual refresh and a scheduled job both drive: open an
    org-scoped session, call ``run_and_store(session, connection, days, org_id=...)``,
    commit. (Scheduling itself lives outside this module.)
    Raises HTTPException(404) for an unresolvable connection, 400 for a
    connector with no history catalog, and 403 with the grant-this-role hint when the
    credentials can't read the catalog — the background path (``run_tracked``) catches
    those and records a status instead.
    """
    connector = _resolve_history_connector(connection, org_id)
    try:
        records_scanned, findings = _mine(session, connector, days, execute=execute)
    except Forbidden as exc:
        raise HTTPException(status_code=403, detail=PERMISSION_HINT) from exc
    return audit_store.record_run(session, connection, days, records_scanned, findings)


def run_tracked(
    session: Session, connection: str, days: int = 30, *, org_id: uuid.UUID | None = None
) -> None:
    """Background audit that records its whole lifecycle.

    Stamps a ``running`` marker the instant it starts, then finalizes it to a terminal
    status — ``ok`` (findings), ``empty`` (none), ``unsupported`` (no history catalog,
    or credentials that can't read it), or ``error``. Never raises: a connector that
    can't be audited or a mining failure becomes a status the connect→audit handoff can
    show, not an infinite spinner and not a vanished log line. The standing result
    (``latest``) is untouched until completion.
    """
    run_id = audit_store.record_running(session, connection, days)
    try:
        connector = _resolve_history_connector(connection, org_id)
    except HTTPException as exc:
        audit_store.finalize(
            session, run_id, 0, [], status="unsupported" if exc.status_code == 400 else "error"
        )
        return
    try:
        records_scanned, findings = _mine(session, connector, days, execute=True)
    except Forbidden:
        # Actionable, no traceback: recorded per run, so the scheduler retries once
        # per audit interval instead of spamming every cycle.
        log.warning("audit skipped for %s: %s", connection, PERMISSION_HINT)
        audit_store.finalize(session, run_id, 0, [], status="unsupported")
        return
    except Exception:
        log.exception("audit mining failed for connection %s", connection)
        audit_store.finalize(session, run_id, 0, [], status="error")
        return
    audit_store.finalize(
        session, run_id, records_scanned, findings, status="ok" if findings else "empty"
    )


@router.get("/latest")
def latest_audit(
    name: str,
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """The most recent persisted run — what the dashboard default-loads.

    ``{"found": false}`` when no audit has ever run for this connection (the
    page's empty state); otherwise ``{"found": true, ...AuditRun}``.
    """
    run = audit_store.latest(session, name)
    if run is None:
        return {"found": False}
    return {"found": True, **run.model_dump(mode="json")}


AuditState = Literal["never_run", "running", "done", "empty", "unsupported", "error"]


class AuditStatus(BaseModel):
    """The live state of a connection's audit — what the connect→audit handoff polls.

    ``never_run`` (no audit ever), ``running`` (in flight — show "scanning…"),
    ``done`` (completed with findings — show "found N contradictions"), ``empty`` (ran
    clean), ``unsupported`` (connector has no query-history catalog), ``error`` (the run
    failed). The terminal states carry ``findings`` / ``conflicts`` so the handoff can
    headline the count without a second fetch.
    """

    connection: str
    state: AuditState
    findings: int = 0
    conflicts: int = 0
    created_at: datetime | None = None


@router.get("/status")
def audit_status(
    name: str,
    session: Session = Depends(get_app_session),
) -> AuditStatus:
    """Never-run / running / done(+counts) for one connection's audit.

    Reads the most recent run *including* an in-flight one (``latest_any``), so right
    after a connect the UI can show "scanning your query history…" and then flip to the
    finding count when the background audit finalizes — instead of polling ``/latest``
    forever (which only ever shows completed runs and can't distinguish "still running"
    from "this warehouse can't be audited").
    """
    run = audit_store.latest_any(session, name)
    if run is None:
        return AuditStatus(connection=name, state="never_run")
    if run.status in ("running", "unsupported", "error"):
        return AuditStatus(
            connection=name,
            state=cast(AuditState, run.status),  # constrained set, mirrors run_tracked
            created_at=run.created_at,
        )
    conflicts = sum(1 for f in run.findings if f.severity == "conflict")
    return AuditStatus(
        connection=name,
        state="done" if run.findings else "empty",
        findings=len(run.findings),
        conflicts=conflicts,
        created_at=run.created_at,
    )


class AuditSummary(BaseModel):
    """The Audit's headline KPIs for one connection — answer accuracy, the
    governed share of warehouse traffic, and the open-conflict count. The data leader's
    OKRs made the hero of the page; drift is the leading indicator of the first.
    """

    connection: str
    # The accuracy section's honesty stamp: "scored" =
    # answer_accuracy is a real mean over ≥1 lens eval score; "unscored" = no
    # eval runs yet (answer_accuracy null); "unsupported" = this connector has
    # no query history to audit, and the endpoint OMITS answer_accuracy
    # entirely — an accuracy over 0 records is a vacuous pass, the worst state
    # in the system.
    accuracy_status: Literal["scored", "unscored", "unsupported"]
    # Answer accuracy: mean of the latest per-lens eval scores, scoped to the lenses that
    # govern this connection's metrics (falls back to all scored lenses). None = no runs.
    answer_accuracy: float | None
    accuracy_lenses: int  # how many scored lenses fed the number
    # Governed coverage from the gap-map: metrics a lens governs vs ungoverned gaps, and
    # the share weighted by traffic (audit blast radius + router declines).
    governed_metrics: int
    ungoverned_metrics: int
    governed_share: float | None
    # Open drift from the latest audit run (0 when never run).
    conflicts: int
    duplications: int
    has_run: bool
    # Per-metric governance: the latest run's metric → the lens that governs
    # it (or null for an ungoverned gap), so each exhibit can show "covered by X".
    metric_governance: dict[str, str | None]


@router.get("/summary")
def audit_summary(
    name: str,
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """The KPI band atop the Audit: accuracy + governed coverage + open conflicts.

    Composes three already-persisted surfaces — the eval-run trend (answer accuracy),
    the gap-map (governed coverage), and the latest audit run (open drift) — into one
    connection-scoped read. Accuracy is tied to the connection through the gap-map: only
    the lenses that govern this connection's metrics count, so the number tracks *this*
    warehouse's governed answers, not the org at large (with an all-lenses fallback when
    nothing is governed yet).
    """
    gap_map = compute_gap_map(session, name)
    governing = {g.governed_by for g in gap_map.governed if g.governed_by}

    trends = observe.eval_trend(session)
    relevant = [t for t in trends if t["lens"] in governing] or trends
    scores: list[float] = []
    for trend in relevant:
        score = trend["latest_score"]
        if isinstance(score, int | float) and not isinstance(score, bool):
            scores.append(float(score))
    accuracy = sum(scores) / len(scores) if scores else None

    def weight(g: Gap) -> int:
        return g.blast_radius + g.decline_count

    governed_weight = sum(weight(g) for g in gap_map.governed)
    total_weight = sum(weight(g) for g in gap_map.gaps)
    share = governed_weight / total_weight if total_weight else None

    run = audit_store.latest(session, name)
    conflicts = sum(1 for f in run.findings if f.severity == "conflict") if run else 0
    duplications = (len(run.findings) - conflicts) if run else 0

    # Per-metric governance from the same binder — keyed on the audit's metric labels.
    governance = {g.metric: g.governed_by for g in gap_map.gaps if "audit" in g.sources}

    # The connector's own verdict decides whether accuracy may exist at all: a
    # connection whose latest run recorded `unsupported` has no query history to
    # score against (DuckDB), and serving a number there — an `answer_accuracy:
    # 1.0` over 0 records — is a vacuous pass wearing a KPI.
    run_any = audit_store.latest_any(session, name)
    unsupported = run_any is not None and run_any.status == "unsupported"
    summary = AuditSummary(
        connection=name,
        accuracy_status="unsupported" if unsupported else ("scored" if scores else "unscored"),
        answer_accuracy=None if unsupported else accuracy,
        accuracy_lenses=0 if unsupported else len(scores),
        governed_metrics=gap_map.governed_count,
        ungoverned_metrics=gap_map.ungoverned_count,
        governed_share=share,
        metric_governance=governance,
        conflicts=conflicts,
        duplications=duplications,
        has_run=run is not None,
    )
    payload = summary.model_dump(mode="json")
    if unsupported:
        # OMITTED, not null: a key that is absent cannot be averaged, charted or
        # quoted; a null one gets coalesced somewhere and comes back as a number.
        payload.pop("answer_accuracy")
    return payload


@router.post("")
def run_audit(
    name: str,
    days: int = Query(default=30, ge=1, le=365),
    execute: bool = True,
    org_id: uuid.UUID = Depends(get_admin_org),
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """Refresh the audit: mine the connection's history, persist, return the run.

    A manual refresh (the page's secondary action) and the future scheduled job
    share ``run_and_store``. The response is the persisted ``AuditRun`` (id +
    created_at + findings), so the page can re-render as standing results.
    """
    run = run_and_store(session, name, days, execute=execute, org_id=org_id)
    return run.model_dump(mode="json")


class DraftDefinitionBody(BaseModel):
    """One finding from the audit response + the lens that should own the canon."""

    finding: DriftFinding
    lens: str


def _existing_definition_body(session: Session, lens: str, term: str) -> str | None:
    """The lens's current body for ``term`` (diff_before), 404 if the lens is unknown."""
    row = lens_store.get_lens(session, lens)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{lens}' not found")
    raw = row.get("draft") or row.get("published")
    if raw is None:
        return None
    bundle = lens_store.LensBundle.model_validate(raw)
    match = next(
        (d for d in bundle.semantic_model.definitions if d.term.lower() == term.lower()), None
    )
    return match.body if match is not None else None


@router.post("/draft-definition", status_code=201)
def draft_definition_patch(
    name: str,
    body: DraftDefinitionBody,
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """Turn one drift finding into a definition PatchCandidate on the chosen lens.

    The proposal reuses the report's canon-choice logic verbatim; the candidate
    is ticket-less (like distilled ones) and lands on the lens's existing patch
    queue — approval and apply stay human.
    """
    if not body.finding.variants:
        raise HTTPException(status_code=400, detail="finding has no variants to draft from")
    diff_before = _existing_definition_body(session, body.lens, body.finding.metric_intent)
    draft = draft_definition(body.finding)
    candidate = PatchCandidate(
        ticket_id=None,
        lens=body.lens,
        kind="definition",
        target=draft["term"],
        diff_before=diff_before,
        diff_after=draft["body"],
        status="candidate",
    )
    candidate.id = patch_store.record(session, candidate)
    return candidate.model_dump()
