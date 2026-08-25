"""Patch-candidate persistence — drafted fixes awaiting the human gate.

Org-scoped via RLS (``patch_candidate``, migration 0017), mirroring the JoinCandidate
store in ``services/lenses/profile_store.py``: record / list / approve / reject, where
approval just flips ``status`` — *applying* an approved patch is the API layer's job
(``services/api/reviews.py``).

Shared seam: the corpus distiller writes its candidates here too,
with ``ticket_id=None`` — one approval surface for every drafted change.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.reviews.patch import PatchCandidate

_COLS = "id, ticket_id, lens, kind, target, owner, diff_before, diff_after, status, rejection_note"


def _row_to_candidate(r: object) -> PatchCandidate:
    return PatchCandidate(
        id=str(r[0]),  # type: ignore[index]
        ticket_id=r[1],  # type: ignore[index]
        lens=r[2],  # type: ignore[index]
        kind=r[3],  # type: ignore[index]
        target=r[4],  # type: ignore[index]
        owner=r[5],  # type: ignore[index]
        diff_before=r[6],  # type: ignore[index]
        diff_after=r[7],  # type: ignore[index]
        status=r[8],  # type: ignore[index]
        rejection_note=r[9],  # type: ignore[index]
    )


def record(session: Session, candidate: PatchCandidate) -> str:
    """Persist a drafted candidate; returns its generated id."""
    row = session.execute(
        text(
            """
            INSERT INTO patch_candidate (
                org_id, ticket_id, lens, kind, target, owner,
                diff_before, diff_after, status
            )
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :ticket_id, :lens, :kind, :target, :owner,
                :diff_before, :diff_after, :status
            )
            RETURNING id
            """
        ),
        {
            "ticket_id": candidate.ticket_id,
            "lens": candidate.lens,
            "kind": candidate.kind,
            "target": candidate.target,
            "owner": candidate.owner,
            "diff_before": candidate.diff_before,
            "diff_after": candidate.diff_after,
            "status": candidate.status,
        },
    ).first()
    return str(row[0])  # type: ignore[index]


def get(session: Session, patch_id: str) -> PatchCandidate | None:
    row = session.execute(
        text(f"SELECT {_COLS} FROM patch_candidate WHERE id = :i"), {"i": patch_id}
    ).first()
    return _row_to_candidate(row) if row else None


def list_for_lens(session: Session, lens: str, status: str | None = None) -> list[PatchCandidate]:
    sql = f"SELECT {_COLS} FROM patch_candidate WHERE lens = :l"
    params: dict[str, object] = {"l": lens}
    if status is not None:
        sql += " AND status = :s"
        params["s"] = status
    rows = session.execute(text(sql + " ORDER BY created_at DESC"), params).all()
    return [_row_to_candidate(r) for r in rows]


def list_for_ticket(session: Session, ticket_id: str) -> list[PatchCandidate]:
    rows = session.execute(
        text(f"SELECT {_COLS} FROM patch_candidate WHERE ticket_id = :t ORDER BY created_at DESC"),
        {"t": ticket_id},
    ).all()
    return [_row_to_candidate(r) for r in rows]


def approve(session: Session, patch_id: str) -> int:
    return _set_status(session, patch_id, "approved")


def reject(session: Session, patch_id: str, note: str | None = None) -> int:
    """Decline a candidate, recording why — the note is drafter feedback."""
    return _set_status(session, patch_id, "rejected", note=note)


def _set_status(session: Session, patch_id: str, status: str, note: str | None = None) -> int:
    res = session.execute(
        text(
            "UPDATE patch_candidate SET status = :s, rejection_note = :n, updated_at = now() "
            "WHERE id = :i AND status = 'candidate'"
        ),
        {"s": status, "n": note, "i": patch_id},
    )
    return int(res.rowcount)  # type: ignore[attr-defined]
