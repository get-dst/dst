"""The activation spine — where is this org in the funnel?

The connect → audit → lens arc is four surfaces that don't otherwise know
the org just arrived. This derives the single next step from data already persisted
(warehouse connections, audit runs, lenses) — no new state, no new table. The first-run
home reads ``step`` and renders that one next action; an org with a published lens is
``activated`` and the home shows nothing.

Steps:
- ``empty``          — no warehouse connected yet → connect one
- ``connected``      — connected, audit ran clean (or can't audit) → build a lens by hand
- ``auditing``       — a drift audit is in flight → "scanning your query history…"
- ``findings_ready`` — the audit found contradictions → review them / build a lens to fix
- ``drafting``       — a draft lens exists but none is published → finish and publish it
- ``activated``      — at least one published lens → the funnel is complete
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.api import audit_store
from services.auth.deps import get_app_session
from services.lenses import connection_store
from services.lenses import store as lens_store

router = APIRouter(prefix="/mgmt/activation", tags=["activation"])

ActivationStep = Literal[
    "empty", "connected", "auditing", "findings_ready", "drafting", "activated"
]


class Activation(BaseModel):
    """The org's place in the funnel + the counts the first-run home needs to phrase the
    next step. ``focus_connection`` is the warehouse that step acts on (the one with open
    contradictions, else the one being audited, else the first connected)."""

    step: ActivationStep
    connections: int
    lenses: int
    published_lenses: int
    auditing: bool
    findings: int  # open conflicts on the focus connection
    focus_connection: str | None


@router.get("")
def activation(session: Session = Depends(get_app_session)) -> Activation:
    """Derive the activation step from already-persisted state (no new storage)."""
    warehouses = [c for c in connection_store.list_connections(session)]
    lenses = lens_store.list_lenses(session)
    published = [ln for ln in lenses if ln.status == "live"]

    # Focus the step on the connection with the most open contradictions; fall back to one
    # mid-audit, then to the first connected warehouse. One pass over the connections.
    auditing = False
    focus: str | None = None
    findings = 0
    for c in warehouses:
        live = audit_store.latest_any(session, c.name)
        if live is not None and live.status == "running":
            auditing = True
        completed = audit_store.latest(session, c.name)
        conflicts = (
            sum(1 for f in completed.findings if f.severity == "conflict") if completed else 0
        )
        if conflicts > findings:
            findings, focus = conflicts, c.name
    if focus is None and warehouses:
        focus = warehouses[0].name

    if published:
        step: ActivationStep = "activated"
    elif lenses:
        step = "drafting"
    elif not warehouses:
        step = "empty"
    elif findings > 0:
        step = "findings_ready"
    elif auditing:
        step = "auditing"
    else:
        step = "connected"

    return Activation(
        step=step,
        connections=len(warehouses),
        lenses=len(lenses),
        published_lenses=len(published),
        auditing=auditing,
        findings=findings,
        focus_connection=focus,
    )
