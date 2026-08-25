"""Control-plane CRUD for eval cases and runs.

Mirrors ``services/certify/store.py``: raw SQL via SQLAlchemy ``text()``, org-scoped
via the ``app.current_org`` GUC that RLS policies read, no ORM models.

Tables: ``eval_case``, ``eval_run`` (migration 0013_eval), ``eval_result``
(migration 0046 — per-case outcomes, the drill-down behind the trend). These
control-plane rows are the canonical eval history; the warehouse-side ``__dst``
plane was removed. The ``snapshot_ref`` column stays, inert.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from services.contracts.eval import EvalResult

# ---------------------------------------------------------------------------
# Dataclasses (lightweight structs — mirrors certify/store.py)
# ---------------------------------------------------------------------------


@dataclass
class EvalCaseRow:
    id: str
    lens: str
    question: str
    expected_sql: str | None
    expected_answer: str | None
    snapshot_ref: str | None
    source: str
    status: str
    created_by: str
    # Behavioral expectations. Defaulted so callers
    # that build a value-shaped row keep their existing keyword call.
    expect: str | None = None
    term: str | None = None
    # Free-form classification (persona:…, intent:…) — see EvalCase.tags.
    tags: list[str] = field(default_factory=list)


@dataclass
class EvalResultRow:
    id: str
    run_id: str
    case_id: str
    question: str
    passed: bool
    grade: str | None
    checks: dict[str, Any] | None
    actual_sql: str | None
    actual_value: str | None
    reason: str | None


@dataclass
class EvalRunRow:
    id: str
    lens: str
    lens_version: str | None
    mode: str
    score: float | None
    passed: int
    failed: int
    errored: int
    telemetry_ref: str | None
    started_at: str | None


# ---------------------------------------------------------------------------
# eval_case
# ---------------------------------------------------------------------------


def create_case(
    session: Session,
    lens: str,
    question: str,
    source: str,
    *,
    expected_sql: str | None = None,
    expected_answer: str | None = None,
    expect: str | None = None,
    term: str | None = None,
    snapshot_ref: str | None = None,
    status: str = "candidate",
    created_by: str = "",
    tags: list[str] | None = None,
) -> str:
    """Insert a new eval case; returns the generated UUID string."""
    row = session.execute(
        text(
            """
            INSERT INTO eval_case (
                org_id, lens, question, source,
                expected_sql, expected_answer, expect, term, snapshot_ref,
                status, created_by, tags
            )
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :lens, :question, :source,
                :expected_sql, :expected_answer, :expect, :term, :snapshot_ref,
                :status, :created_by, :tags
            )
            RETURNING id
            """
        ),
        {
            "lens": lens,
            "question": question,
            "source": source,
            "expected_sql": expected_sql,
            "expected_answer": expected_answer,
            "expect": expect,
            "term": term,
            "snapshot_ref": snapshot_ref,
            "status": status,
            "created_by": created_by,
            "tags": tags or [],
        },
    ).first()
    return str(row[0])  # type: ignore[index]


def list_cases(
    session: Session,
    lens: str,
    status: str | None = None,
) -> list[EvalCaseRow]:
    """Return eval cases for a lens, optionally filtered by status."""
    if status is not None:
        rows = session.execute(
            text(
                "SELECT id, lens, question, expected_sql, expected_answer, "
                "snapshot_ref, source, status, created_by, expect, term, tags "
                "FROM eval_case WHERE lens = :l AND status = :s ORDER BY created_at DESC"
            ),
            {"l": lens, "s": status},
        ).all()
    else:
        rows = session.execute(
            text(
                "SELECT id, lens, question, expected_sql, expected_answer, "
                "snapshot_ref, source, status, created_by, expect, term, tags "
                "FROM eval_case WHERE lens = :l ORDER BY created_at DESC"
            ),
            {"l": lens},
        ).all()
    return [
        EvalCaseRow(
            str(r[0]),
            r[1],
            r[2],
            r[3],
            r[4],
            r[5],
            r[6],
            r[7],
            r[8],
            r[9],
            r[10],
            list(r[11] or []),
        )
        for r in rows
    ]


def update_case(
    session: Session,
    case_id: str,
    *,
    expected_sql: str | None,
    expected_answer: str | None,
    expect: str | None = None,
    term: str | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
) -> int:
    """Update a case's expectations in place (apply's upsert path — the question
    is the key). Returns rows affected.

    ``status`` rides too: the file offers the field — export writes it, the
    scaffold documents it — so an edit there must land, or the
    eval gate can never be armed from the files. None keeps the stored value
    (API callers that only touch expectations).

    Writes all the columns, so re-applying a case whose behavioral expectation
    still sits in a legacy ``expected_answer`` marker heals it into the real
    columns (migration 0031 does the bulk pass; this catches stragglers)."""
    res = session.execute(
        text(
            "UPDATE eval_case SET expected_sql = :sql, expected_answer = :ans, "
            "expect = :expect, term = :term, tags = :tags, "
            "status = COALESCE(:status, status) WHERE id = :i"
        ),
        {
            "sql": expected_sql,
            "ans": expected_answer,
            "expect": expect,
            "term": term,
            "tags": tags or [],
            "status": status,
            "i": case_id,
        },
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def approve_case(session: Session, case_id: str) -> int:
    """Transition a case from 'candidate' to 'approved'. Returns rows affected."""
    res = session.execute(
        text("UPDATE eval_case SET status = 'approved' WHERE id = :i AND status = 'candidate'"),
        {"i": case_id},
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def retire_case(session: Session, case_id: str) -> int:
    """Transition a case to 'retired'. Returns rows affected."""
    res = session.execute(
        text("UPDATE eval_case SET status = 'retired' WHERE id = :i"),
        {"i": case_id},
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def delete_case(session: Session, case_id: str) -> int:
    """Hard-delete a case. Returns rows affected."""
    res = session.execute(
        text("DELETE FROM eval_case WHERE id = :i"),
        {"i": case_id},
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# eval_run
# ---------------------------------------------------------------------------


def create_run(
    session: Session,
    lens: str,
    mode: str,
    *,
    lens_version: str | None = None,
    score: float | None = None,
    passed: int = 0,
    failed: int = 0,
    errored: int = 0,
    telemetry_ref: str | None = None,
) -> str:
    """Insert a new eval run record; returns the generated UUID string."""
    row = session.execute(
        text(
            """
            INSERT INTO eval_run (
                org_id, lens, mode, lens_version,
                score, passed, failed, errored, telemetry_ref
            )
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :lens, :mode, :lens_version,
                :score, :passed, :failed, :errored, :telemetry_ref
            )
            RETURNING id
            """
        ),
        {
            "lens": lens,
            "mode": mode,
            "lens_version": lens_version,
            "score": score,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "telemetry_ref": telemetry_ref,
        },
    ).first()
    return str(row[0])  # type: ignore[index]


def get_run(session: Session, run_id: str) -> EvalRunRow | None:
    """One eval run by id — None when it doesn't exist (or RLS hides it)."""
    r = session.execute(
        text(
            "SELECT id, lens, lens_version, mode, score, passed, failed, errored, telemetry_ref, "
            "started_at FROM eval_run WHERE id = :i"
        ),
        {"i": run_id},
    ).first()
    if r is None:
        return None
    return EvalRunRow(
        str(r[0]),
        r[1],
        r[2],
        r[3],
        float(r[4]) if r[4] is not None else None,
        int(r[5]),
        int(r[6]),
        int(r[7]),
        r[8],
        r[9].isoformat() if r[9] else None,
    )


def last_run_score(session: Session, lens: str, mode: str) -> float | None:
    """Score of the most recent run for this lens+mode (the regression gate's baseline)."""
    row = session.execute(
        text(
            "SELECT score FROM eval_run WHERE lens = :l AND mode = :m "
            "ORDER BY started_at DESC LIMIT 1"
        ),
        {"l": lens, "m": mode},
    ).first()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def list_runs(session: Session, lens: str) -> list[EvalRunRow]:
    """Return all eval runs for a lens ordered newest first."""
    rows = session.execute(
        text(
            "SELECT id, lens, lens_version, mode, score, passed, failed, errored, telemetry_ref, "
            "started_at FROM eval_run WHERE lens = :l ORDER BY started_at DESC"
        ),
        {"l": lens},
    ).all()
    return [
        EvalRunRow(
            str(r[0]),
            r[1],
            r[2],
            r[3],
            float(r[4]) if r[4] is not None else None,
            int(r[5]),
            int(r[6]),
            int(r[7]),
            r[8],
            r[9].isoformat() if r[9] else None,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# eval_result
# ---------------------------------------------------------------------------


def record_results(session: Session, run_id: str, results: list[EvalResult]) -> None:
    """Persist a run's per-case outcomes (the drill-down behind the aggregates)."""
    for r in results:
        session.execute(
            text(
                """
                INSERT INTO eval_result (
                    org_id, run_id, case_id, question, passed,
                    grade, checks, actual_sql, actual_value, reason
                )
                VALUES (
                    NULLIF(current_setting('app.current_org', true), '')::uuid,
                    :run_id, :case_id, :question, :passed,
                    :grade, CAST(:checks AS jsonb), :actual_sql, :actual_value, :reason
                )
                """
            ),
            {
                "run_id": run_id,
                "case_id": r.case_id,
                "question": r.question,
                "passed": r.passed,
                "grade": r.grade,
                "checks": json.dumps(r.checks) if r.checks else None,
                "actual_sql": r.actual_sql,
                "actual_value": r.actual_value,
                "reason": r.reason,
            },
        )


def list_results(session: Session, run_id: str) -> list[EvalResultRow]:
    """A run's per-case outcomes, failing first (what the drill-down leads with)."""
    rows = session.execute(
        text(
            "SELECT id, run_id, case_id, question, passed, grade, checks, "
            "actual_sql, actual_value, reason "
            "FROM eval_result WHERE run_id = :r ORDER BY passed, question, case_id"
        ),
        {"r": run_id},
    ).all()
    return [
        EvalResultRow(
            str(r[0]),
            str(r[1]),
            r[2],
            r[3],
            bool(r[4]),
            r[5],
            r[6],
            r[7],
            r[8],
            r[9],
        )
        for r in rows
    ]
