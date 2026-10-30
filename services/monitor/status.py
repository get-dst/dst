"""The ``data_quality`` block: the latest `dst monitor` run, as applied.

`dst monitor` writes ``profiles/cube.freshness.json``; committed, it rides
``dst apply`` like every probe artifact and lands here, summarised, in the
org's ``setting`` row under one key. ``/ready`` reports it per org, with its
age: the monitor's own clock is the user's (a scheduled job), and a run older
than the project's ``probe.max_age`` says the job stopped. Never a readiness
gate; a missing or unreadable row reads as "not monitored".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.monitor.cube import summary

log = logging.getLogger("dst.monitor")

KEY = "data_quality"
FRESHNESS_PATH = "profiles/cube.freshness.json"


def apply(session: Session, files: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """``(applied, warnings)`` for a push carrying the freshness file."""
    raw = files.get(FRESHNESS_PATH)
    if raw is None:
        return [], []
    try:
        block = summary(json.loads(raw))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return [], [f"{FRESHNESS_PATH}: not a monitor artifact ({exc}) — skipped"]
    session.execute(
        text(
            """
            INSERT INTO setting (org_id, key, value)
            VALUES (NULLIF(current_setting('app.current_org', true), '')::uuid,
                    :k, CAST(:v AS jsonb))
            ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value
            """
        ),
        {"k": KEY, "v": json.dumps(block)},
    )
    return [f"data quality: {block['findings']} finding(s) as of {block['clock_end']}"], []


def load(session: Session) -> dict[str, Any] | None:
    try:
        value = session.execute(
            text("SELECT value FROM setting WHERE key = :k"), {"k": KEY}
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — a status read never fails a caller
        log.exception("data quality: the stored block could not be read")
        return None
    return value if isinstance(value, dict) else None


def ready_status() -> dict[str, Any] | str:
    """Per org: when the monitor ran, how old that is, its counts by mode."""
    from services.db.session import admin_engine, org_session
    from services.project import probe_settings

    out: dict[str, Any] = {}
    try:
        with admin_engine.connect() as conn:
            orgs = list(conn.execute(text("SELECT id, name FROM org ORDER BY created_at")))
        for org_id, org_name in orgs:
            with org_session(org_id) as session:
                block = load(session)
                max_age = probe_settings.load(session).max_age_delta
            if block is None:
                continue
            age_days: float | None = None
            if block.get("as_of"):
                ran = datetime.fromisoformat(str(block["as_of"]))
                age_days = round((datetime.now(UTC) - ran).total_seconds() / 86_400, 2)
            out[str(org_name)] = {
                "as_of": block.get("as_of"),
                "clock_end": block.get("clock_end"),
                "age_days": age_days,
                "stale": bool(
                    max_age is not None
                    and age_days is not None
                    and age_days > max_age.total_seconds() / 86_400
                ),
                "findings": block.get("findings", 0),
                "by_mode": block.get("by_mode", {}),
                "unread": block.get("unread", 0),
            }
    except Exception:  # noqa: BLE001 — readiness must never crash on a status read
        log.exception("readiness data-quality check failed")
        return "unknown"
    return out or "not monitored"
