"""Review ticket persistence + trace lookup. Org-scoped via RLS.

A ticket reviews one traced request (request_log row). States:
  open -> needs_human -> approved | changes_requested | rejected
(the AI judge may resolve straight to `approved`).
"""

from __future__ import annotations

import secrets

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.contracts.correction import CorrectionDelta

VERDICT_TO_STATE = {
    "approve": "approved",
    "changes": "changes_requested",
    "reject": "rejected",
}


class Trace(BaseModel):
    request_id: str
    lens: str
    caller: str
    question: str
    sql: str | None = None
    answer: str | None = None
    definition_used: str | None = None
    confidence: str | None = None
    row_count: int | None = None
    # Patch-drafter routing inputs: which generation tier actually ran
    # ("certified" | "none" | null) and the graded verification breakdown.
    certification: str | None = None
    verification: dict[str, object] | None = None


class Ticket(BaseModel):
    ticket_id: str
    request_id: str
    lens: str
    caller: str
    state: str
    # Who raised it: 'human' (caller/dashboard) or 'ai' (auto_review flagged it).
    origin: str = "human"
    ai_verdict: str | None = None
    ai_reasoning: str | None = None
    human_verdict: str | None = None
    human_reasoning: str | None = None
    # The wrong-vs-right delta, when this ticket is a correction.
    correction: CorrectionDelta | None = None
    # Actor trail — verdicts someone stands behind. `judged_by` is the machine
    # actor behind ai_verdict (the judge's resolved model name); `ruled_by` is
    # who issued human_verdict (`human:<email>` session or `token:<label>`).
    # '' = that verdict hasn't happened, a fact rather than missing data.
    judged_by: str = ""
    ruled_by: str = ""
    # Deterministic incident identity for drift/serve-error tickets:
    # the unique index on (org_id, fingerprint) makes one-ticket-per-incident a
    # database guarantee. None on ordinary per-request tickets.
    fingerprint: str | None = None


def new_ticket_id() -> str:
    return "rev_" + secrets.token_hex(5)


def get_trace(session: Session, request_id: str) -> Trace | None:
    row = session.execute(
        text(
            "SELECT request_id, lens, caller, question, sql, answer, definition_used, "
            "confidence, row_count, certification, verification "
            "FROM request_log WHERE request_id = :r"
        ),
        {"r": request_id},
    ).first()
    if row is None:
        return None
    return Trace(
        request_id=row[0],
        lens=row[1],
        caller=row[2],
        question=row[3],
        sql=row[4],
        answer=row[5],
        definition_used=row[6],
        confidence=row[7],
        row_count=row[8],
        certification=row[9],
        verification=row[10],
    )


def _row_to_ticket(row: object) -> Ticket:
    r = row  # tuple
    correction = CorrectionDelta.model_validate(r[9]) if r[9] is not None else None  # type: ignore[index]
    return Ticket(
        ticket_id=r[0],  # type: ignore[index]
        request_id=r[1],  # type: ignore[index]
        lens=r[2],  # type: ignore[index]
        caller=r[3],  # type: ignore[index]
        state=r[4],  # type: ignore[index]
        ai_verdict=r[5],  # type: ignore[index]
        ai_reasoning=r[6],  # type: ignore[index]
        human_verdict=r[7],  # type: ignore[index]
        human_reasoning=r[8],  # type: ignore[index]
        correction=correction,
        origin=r[10],  # type: ignore[index]
        judged_by=r[11],  # type: ignore[index]
        ruled_by=r[12],  # type: ignore[index]
        fingerprint=r[13],  # type: ignore[index]
    )


_COLS = (
    "ticket_id, request_id, lens, caller, state, ai_verdict, ai_reasoning, "
    "human_verdict, human_reasoning, correction, origin, judged_by, ruled_by, fingerprint"
)


def create_ticket(
    session: Session,
    trace: Trace,
    correction: CorrectionDelta | None = None,
    origin: str = "human",
) -> Ticket:
    ticket_id = new_ticket_id()
    session.execute(
        text(
            """
            INSERT INTO review
                (org_id, ticket_id, request_id, lens, caller, state, correction, origin)
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :tid, :rid, :lens, :caller, 'open', CAST(:corr AS jsonb), :origin
            )
            """
        ),
        {
            "tid": ticket_id,
            "rid": trace.request_id,
            "lens": trace.lens,
            "caller": trace.caller,
            "corr": correction.model_dump_json() if correction is not None else None,
            "origin": origin,
        },
    )
    return Ticket(
        ticket_id=ticket_id,
        request_id=trace.request_id,
        lens=trace.lens,
        caller=trace.caller,
        state="open",
        correction=correction,
        origin=origin,
    )


def create_incident_ticket(
    session: Session,
    *,
    fingerprint: str,
    request_id: str,
    lens: str,
    caller: str,
    origin: str,
    correction: CorrectionDelta | None = None,
) -> tuple[Ticket, bool]:
    """One ticket per standing incident — (ticket, created).

    INSERT … ON CONFLICT on the (org_id, fingerprint) unique index: a second
    identical failure ATTACHES to the existing ticket instead of filing a
    sibling (dedup — the guarantee is the index, not a
    check-before-insert race). `request_id` is the first request that surfaced
    the incident; later ones are in the request log, joined by the trace's own
    error. State starts at needs_human: a schema break is a data-team act, and
    routing it through the AI judge would be a verdict on nothing."""
    ticket_id = new_ticket_id()
    inserted = session.execute(
        text(
            """
            INSERT INTO review
                (org_id, ticket_id, request_id, lens, caller, state, correction,
                 origin, fingerprint)
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :tid, :rid, :lens, :caller, 'needs_human', CAST(:corr AS jsonb),
                :origin, :fp
            )
            ON CONFLICT (org_id, fingerprint) WHERE fingerprint IS NOT NULL DO NOTHING
            """
        ),
        {
            "tid": ticket_id,
            "rid": request_id,
            "lens": lens,
            "caller": caller,
            "corr": correction.model_dump_json() if correction is not None else None,
            "origin": origin,
            "fp": fingerprint,
        },
    )
    if int(inserted.rowcount):  # type: ignore[attr-defined]
        return (
            Ticket(
                ticket_id=ticket_id,
                request_id=request_id,
                lens=lens,
                caller=caller,
                state="needs_human",
                correction=correction,
                origin=origin,
                fingerprint=fingerprint,
            ),
            True,
        )
    row = session.execute(
        text(f"SELECT {_COLS} FROM review WHERE fingerprint = :fp"), {"fp": fingerprint}
    ).first()
    if row is None:  # pragma: no cover — the conflict row vanished mid-flight
        raise RuntimeError(f"review fingerprint {fingerprint} conflicted but is not readable")
    return _row_to_ticket(row), False


def set_ai_result(
    session: Session,
    ticket_id: str,
    verdict: str,
    reasoning: str,
    state: str,
    judged_by: str = "",
) -> None:
    session.execute(
        text(
            "UPDATE review SET ai_verdict = :v, ai_reasoning = :r, state = :s, "
            "judged_by = :j, updated_at = now() WHERE ticket_id = :t"
        ),
        {"v": verdict, "r": reasoning, "s": state, "j": judged_by, "t": ticket_id},
    )


def set_human_ruling(
    session: Session,
    ticket_id: str,
    verdict: str,
    reasoning: str,
    correction: CorrectionDelta | None = None,
    ruled_by: str = "",
) -> int:
    state = VERDICT_TO_STATE.get(verdict, "needs_human")
    res = session.execute(
        text(
            "UPDATE review SET human_verdict = :v, human_reasoning = :r, state = :s, "
            "ruled_by = :who, correction = COALESCE(CAST(:corr AS jsonb), correction), "
            "updated_at = now() WHERE ticket_id = :t"
        ),
        {
            "v": verdict,
            "r": reasoning,
            "s": state,
            "who": ruled_by,
            "corr": correction.model_dump_json() if correction is not None else None,
            "t": ticket_id,
        },
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def get_ticket(session: Session, ticket_id: str) -> Ticket | None:
    row = session.execute(
        text(f"SELECT {_COLS} FROM review WHERE ticket_id = :t"), {"t": ticket_id}
    ).first()
    return _row_to_ticket(row) if row else None


def list_for_caller(session: Session, caller: str, state: str | None = None) -> list[Ticket]:
    """The tickets raised on ONE caller's own requests, newest first.

    ``review.caller`` is the trace's caller — who ASKED, not who filed — so this
    is "tickets about my questions". A correction the data team raised on a
    business user's answer therefore shows up in that user's list, which is the
    point: they are the person waiting to hear whether the number was fixed."""
    sql = f"SELECT {_COLS} FROM review WHERE caller = :c"
    params: dict[str, object] = {"c": caller}
    if state:
        sql += " AND state = :s"
        params["s"] = state
    rows = session.execute(text(sql + " ORDER BY created_at DESC"), params).all()
    return [_row_to_ticket(r) for r in rows]


def list_queue(session: Session, state: str | None = None) -> list[Ticket]:
    if state:
        rows = session.execute(
            text(f"SELECT {_COLS} FROM review WHERE state = :s ORDER BY created_at DESC"),
            {"s": state},
        ).all()
    else:
        rows = session.execute(text(f"SELECT {_COLS} FROM review ORDER BY created_at DESC")).all()
    return [_row_to_ticket(r) for r in rows]
