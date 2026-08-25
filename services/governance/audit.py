"""Append-only authz audit log (records allow AND deny)."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from services.db.session import org_session


def record(
    org_id: uuid.UUID | str,
    caller: str,
    lens: str,
    decision: str,
    reason: str | None = None,
    *,
    request_id: str | None = None,
    agent: str | None = None,
) -> None:
    """One allow/deny row. `caller` is the principal (person), `agent` the acting
    client, `request_id` the join to `request_log` — an allowed query and the
    decision that let it through share it. A deny still records its request_id
    though no request_log row exists: null-joinable, and honest about the gap."""
    with org_session(org_id) as session:
        session.execute(
            text(
                "INSERT INTO audit_log (org_id, caller, lens, decision, reason, "
                "request_id, agent) VALUES "
                "(NULLIF(current_setting('app.current_org', true), '')::uuid, "
                ":c, :l, :d, :r, :rid, :a)"
            ),
            {"c": caller, "l": lens, "d": decision, "r": reason, "rid": request_id, "a": agent},
        )
