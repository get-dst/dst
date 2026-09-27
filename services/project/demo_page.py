"""A public demo page's content (dst.yaml ``demo:``), stored per org.

The section rides ``dst apply`` like connections do and lands in the org's
``setting`` row under one key, so the page (services/api/demo.py) reads what
the project declared instead of anything written into dst. Files win: a pushed
dst.yaml without the section clears the row.
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.auth import demo as demo_mode
from services.db.session import org_session as session_for
from services.governance.credentials import CallerIdentity
from services.project.schema import DemoConfig

log = logging.getLogger("dst.demo")

KEY = "demo"


def _stored(session: Session) -> str | None:
    # The section is kept as JSON TEXT inside the jsonb value: jsonb reorders
    # object keys, and a receipt's lines are ordered.
    value = session.execute(
        text("SELECT value FROM setting WHERE key = :k"), {"k": KEY}
    ).scalar_one_or_none()
    return value if isinstance(value, str) else None


def _dump(demo: DemoConfig) -> str:
    return demo.model_dump_json(exclude_none=True)


def load(session: Session) -> DemoConfig | None:
    """The org's demo-page content, or None when none was applied. A stored row
    that no longer validates is logged and read as absent: the page it feeds is
    informational and must never go down over it."""
    stored = _stored(session)
    if stored is None:
        return None
    try:
        return DemoConfig.model_validate_json(stored)
    except ValueError:
        log.exception("demo page: the stored `demo:` section no longer validates")
        return None


def consumer_view(caller: CallerIdentity) -> bool:
    """Whether this caller's answers are cut to what a consumer reads: a demo
    caller, in the demo org, on a project whose `demo.audience` is consumer.
    Anyone else, and every other deployment, gets the whole answer; a failed
    read of the section does too, logged, since a serve never fails over a page
    knob and the whole answer is the safe side."""
    if not (
        demo_mode.enabled()
        and demo_mode.GROUP in caller.groups
        and caller.org_id == demo_mode.org_id()
    ):
        return False
    try:
        with session_for(caller.org_id) as session:
            cfg = load(session)
    except Exception:  # noqa: BLE001 — see the docstring
        log.exception("demo page: reading the `demo:` section for a serve failed")
        return False
    return cfg is not None and cfg.audience == "consumer"


def apply(
    session: Session, demo: DemoConfig | None, *, org_id: uuid.UUID
) -> tuple[list[str], list[str]]:
    """``(applied, warnings)`` for a pushed dst.yaml's ``demo:`` section: set,
    cleared, or nothing when it matches what is stored."""
    stored = _stored(session)
    if (None if demo is None else _dump(demo)) == stored:
        return [], []
    if demo is None:
        session.execute(text("DELETE FROM setting WHERE key = :k"), {"k": KEY})
        return ["demo page: cleared"], []
    session.execute(
        text(
            """
            INSERT INTO setting (org_id, key, value)
            VALUES (NULLIF(current_setting('app.current_org', true), '')::uuid,
                    :k, CAST(:v AS jsonb))
            ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value
            """
        ),
        {"k": KEY, "v": json.dumps(_dump(demo))},
    )
    parts = [
        *(["tagline"] if demo.tagline else []),
        *([f"example of {len(demo.example)} turn(s)"] if demo.example else []),
        *(["privacy link"] if demo.privacy_url else []),
        *([f"log kept {demo.log_days} days"] if demo.log_days else []),
        *(["consumer answers"] if demo.audience == "consumer" else []),
    ]
    applied = [f"demo page: {', '.join(parts) or 'empty'}"]
    # Stored either way (the org may become the demo org later), but a section
    # nothing on this server will show must not read as live.
    warnings = (
        []
        if demo_mode.enabled() and demo_mode.org_id() == org_id
        else [
            "`demo:` is stored but not shown: only a deployment in demo mode "
            "whose DST_DEMO_ORG_ID is this org renders it at /demo"
        ]
    )
    return applied, warnings
