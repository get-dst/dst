"""Persisted routing decisions — the router-lens's standing record.

Every lens-less ``POST /v1/query`` produces a route (to a covering lens) or an
honest decline; both are recorded here so surface area = routed / asked and the
uncovered-question clusters can be computed per org over a window. The
decision is the surface signal — never an ungoverned catch-all. Org-scoped via
RLS (``routing_decision``, migration 0019), mirroring audit_store: record /
list-window / latest-window.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

_COLS = "id, question, routed_lens, score, covered, created_at, degraded, decision"


class RoutingDecision(BaseModel):
    """One persisted routing decision — a route to ``routed_lens`` or a decline."""

    id: str
    question: str
    routed_lens: str | None
    score: float
    covered: bool
    created_at: datetime
    # Outage marker: DECIDER_DOWN / ROUTER_DOWN. A degraded decline is
    # an availability signal, never a coverage signal — surface counts it apart.
    degraded: str | None = None
    # The decider's measured decision + policy verdict (0064): {provider, chosen,
    # p, runner_up, margin, verdict}. None on cosine paths, outages, pre-0064 rows.
    decision: dict[str, object] | None = None


def _row_to_decision(r: object) -> RoutingDecision:
    return RoutingDecision(
        id=str(r[0]),  # type: ignore[index]
        question=r[1],  # type: ignore[index]
        routed_lens=r[2],  # type: ignore[index]
        score=float(r[3]),  # type: ignore[index]
        covered=bool(r[4]),  # type: ignore[index]
        created_at=r[5],  # type: ignore[index]
        degraded=r[6],  # type: ignore[index]
        decision=r[7],  # type: ignore[index]
    )


def record(
    session: Session,
    question: str,
    routed_lens: str | None,
    score: float,
    covered: bool,
    *,
    degraded: str | None = None,
    decision: dict[str, object] | None = None,
) -> RoutingDecision:
    """Persist one routing decision; returns the stored row (id + created_at)."""
    row = session.execute(
        text(
            f"""
            INSERT INTO routing_decision
                (org_id, question, routed_lens, score, covered, degraded, decision)
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :q, :l, :s, :c, :dg, CAST(:dc AS jsonb)
            )
            RETURNING {_COLS}
            """
        ),
        {
            "q": question,
            "l": routed_lens,
            "s": score,
            "c": covered,
            "dg": degraded,
            "dc": json.dumps(decision) if decision is not None else None,
        },
    ).first()
    return _row_to_decision(row)


def by_id(session: Session, decision_id: str) -> RoutingDecision | None:
    """One recorded route of the session's org by its id; None for an id
    that is not one (a handle generated when the row could not be written)
    or one another org holds."""
    try:
        uuid.UUID(decision_id)
    except ValueError:
        return None
    row = session.execute(
        text(f"SELECT {_COLS} FROM routing_decision WHERE id = :i"), {"i": decision_id}
    ).first()
    return _row_to_decision(row) if row is not None else None


def amend(
    session: Session,
    decision_id: str,
    routed_lens: str,
    decision: dict[str, object] | None,
) -> None:
    """Point a recorded route at the lens that answered (a fallback after the
    routed lens refused), with the decision record that says why."""
    session.execute(
        text(
            "UPDATE routing_decision SET routed_lens = :l, decision = CAST(:dc AS jsonb) "
            "WHERE id = :i"
        ),
        {
            "i": decision_id,
            "l": routed_lens,
            "dc": json.dumps(decision) if decision is not None else None,
        },
    )


def list_window(session: Session, *, days: int = 30, limit: int = 1000) -> list[RoutingDecision]:
    """The org's routing decisions over the trailing ``days``, newest first."""
    rows = session.execute(
        text(
            f"SELECT {_COLS} FROM routing_decision "
            "WHERE created_at >= now() - make_interval(days => :d) "
            "ORDER BY created_at DESC LIMIT :l"
        ),
        {"d": days, "l": limit},
    ).all()
    return [_row_to_decision(r) for r in rows]


def latest(session: Session, limit: int = 50) -> list[RoutingDecision]:
    """The org's most recent routing decisions, newest first (for a runs timeline)."""
    rows = session.execute(
        text(f"SELECT {_COLS} FROM routing_decision ORDER BY created_at DESC LIMIT :l"),
        {"l": limit},
    ).all()
    return [_row_to_decision(r) for r in rows]
