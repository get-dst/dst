"""Persisted routing decisions — the router-lens's standing record.

Every lens-less ``POST /v1/query`` produces a route (to a covering lens) or an
honest decline; both are recorded here so surface area = routed / asked and the
uncovered-question clusters can be computed per org over a window. The
decision is the surface signal — never an ungoverned catch-all. Org-scoped via
RLS (``routing_decision``, migration 0019), mirroring audit_store: record /
list-window / latest-window.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

_COLS = "id, question, routed_lens, score, covered, created_at, degraded"


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


def _row_to_decision(r: object) -> RoutingDecision:
    return RoutingDecision(
        id=str(r[0]),  # type: ignore[index]
        question=r[1],  # type: ignore[index]
        routed_lens=r[2],  # type: ignore[index]
        score=float(r[3]),  # type: ignore[index]
        covered=bool(r[4]),  # type: ignore[index]
        created_at=r[5],  # type: ignore[index]
        degraded=r[6],  # type: ignore[index]
    )


def record(
    session: Session,
    question: str,
    routed_lens: str | None,
    score: float,
    covered: bool,
    *,
    degraded: str | None = None,
) -> RoutingDecision:
    """Persist one routing decision; returns the stored row (id + created_at)."""
    row = session.execute(
        text(
            f"""
            INSERT INTO routing_decision (org_id, question, routed_lens, score, covered, degraded)
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :q, :l, :s, :c, :dg
            )
            RETURNING {_COLS}
            """
        ),
        {"q": question, "l": routed_lens, "s": score, "c": covered, "dg": degraded},
    ).first()
    return _row_to_decision(row)


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
