"""Persisted audit runs — the Definition Drift probe as standing results.

The audit is not a synchronous button-press: every run (a manual refresh or a
scheduled job — see ``run_and_store`` in ``services.api.mgmt_audit``)
records an ``AuditRun`` here, and the dashboard default-loads ``latest`` per
connection so the findings simply *stand there* to be examined. Org-scoped via
RLS (``audit_run``, migration 0018), mirroring the JoinCandidate / PatchCandidate
stores: record / latest / list, findings stored as the verbatim DriftFinding list.
"""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.probe.drift import DriftFinding

_COLS = "id, connection, days, records_scanned, findings, status, created_at"


class AuditRun(BaseModel):
    """One persisted audit of a connection — the standing Definition Drift result."""

    id: str
    connection: str
    days: int
    records_scanned: int
    findings: list[DriftFinding] = Field(default_factory=list)
    status: str = "ok"
    created_at: datetime


def _row_to_run(r: object) -> AuditRun:
    raw = r[4]  # type: ignore[index]
    findings = raw if isinstance(raw, list) else json.loads(raw)
    return AuditRun(
        id=str(r[0]),  # type: ignore[index]
        connection=r[1],  # type: ignore[index]
        days=r[2],  # type: ignore[index]
        records_scanned=r[3],  # type: ignore[index]
        findings=[DriftFinding.model_validate(f) for f in findings],
        status=r[5],  # type: ignore[index]
        created_at=r[6],  # type: ignore[index]
    )


def record_run(
    session: Session,
    connection: str,
    days: int,
    records_scanned: int,
    findings: list[DriftFinding],
    *,
    status: str = "ok",
) -> AuditRun:
    """Persist a completed audit run; returns the stored row (id + created_at)."""
    payload = json.dumps([f.model_dump(mode="json") for f in findings])
    row = session.execute(
        text(
            f"""
            INSERT INTO audit_run (
                org_id, connection, days, records_scanned, findings, status
            )
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :c, :d, :n, CAST(:f AS jsonb), :s
            )
            RETURNING {_COLS}
            """
        ),
        {"c": connection, "d": days, "n": records_scanned, "f": payload, "s": status},
    ).first()
    return _row_to_run(row)


def record_running(session: Session, connection: str, days: int) -> str:
    """Stamp an in-progress run so the UI can show "scanning…" while the audit works.

    The background audit records this the moment it starts, then ``finalize``
    flips it to ``ok`` / ``empty`` / ``unsupported`` / ``error``. ``latest`` skips
    ``running`` rows so the standing result keeps showing the last completed audit; the
    ``/status`` endpoint reads the running row to drive the connect→audit handoff.
    """
    row = session.execute(
        text(
            """
            INSERT INTO audit_run (org_id, connection, days, records_scanned, findings, status)
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :c, :d, 0, CAST('[]' AS jsonb), 'running'
            )
            RETURNING id
            """
        ),
        {"c": connection, "d": days},
    ).first()
    return str(row[0])  # type: ignore[index]


def finalize(
    session: Session,
    run_id: str,
    records_scanned: int,
    findings: list[DriftFinding],
    *,
    status: str = "ok",
) -> None:
    """Complete a ``running`` row in place — the audit's terminal state + its findings."""
    payload = json.dumps([f.model_dump(mode="json") for f in findings])
    session.execute(
        text(
            """
            UPDATE audit_run
               SET records_scanned = :n, findings = CAST(:f AS jsonb), status = :s
             WHERE id = CAST(:id AS uuid)
            """
        ),
        {"id": run_id, "n": records_scanned, "f": payload, "s": status},
    )


def latest(session: Session, connection: str) -> AuditRun | None:
    """The most recent run that produced a *result* (``ok``/``empty``), or None.

    Skips non-result rows — an in-flight ``running`` marker (so a refresh never blanks
    the standing result) and ``unsupported``/``error`` runs (so "couldn't audit this
    warehouse" never masquerades as "no drift found"). Use ``latest_any`` for the live
    audit state the connect→audit handoff polls (``/status``).
    """
    row = session.execute(
        text(
            f"SELECT {_COLS} FROM audit_run "
            f"WHERE connection = :c AND status IN ('ok', 'empty') ORDER BY created_at DESC LIMIT 1"
        ),
        {"c": connection},
    ).first()
    return _row_to_run(row) if row else None


def latest_any(session: Session, connection: str) -> AuditRun | None:
    """The most recent run for a connection *including* an in-flight one — drives the
    never-run / running / done status the connect→audit handoff polls."""
    row = session.execute(
        text(
            f"SELECT {_COLS} FROM audit_run WHERE connection = :c ORDER BY created_at DESC LIMIT 1"
        ),
        {"c": connection},
    ).first()
    return _row_to_run(row) if row else None


def list_runs(session: Session, connection: str, limit: int = 50) -> list[AuditRun]:
    """A connection's audit history, newest first (for a future runs timeline)."""
    rows = session.execute(
        text(
            f"SELECT {_COLS} FROM audit_run WHERE connection = :c ORDER BY created_at DESC LIMIT :l"
        ),
        {"c": connection, "l": limit},
    ).all()
    return [_row_to_run(r) for r in rows]
