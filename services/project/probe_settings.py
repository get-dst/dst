"""The project's ``probe:`` section (dst.yaml), stored per org.

It rides ``dst apply`` the way ``demo:`` does and lands in the org's ``setting``
row under one key, so the serving side (assembly, verification, observe,
readiness, the test sweep) reads the age limit the project declared. Files
win: a pushed dst.yaml without the section stores the defaults. A missing or
unreadable row reads as the defaults — the rails must never fail a serve over
a settings read.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.project.schema import ProbeConfig

log = logging.getLogger("dst.probe")

KEY = "probe"


def _stored(session: Session) -> str | None:
    value = session.execute(
        text("SELECT value FROM setting WHERE key = :k"), {"k": KEY}
    ).scalar_one_or_none()
    return value if isinstance(value, str) else None


def _dump(probe: ProbeConfig) -> str:
    return probe.model_dump_json()


def load(session: Session) -> ProbeConfig:
    """The org's probe settings, defaults when none were applied."""
    try:
        stored = _stored(session)
        if stored is not None:
            return ProbeConfig.model_validate_json(stored)
    except Exception:  # noqa: BLE001 — see the module docstring
        log.exception("probe settings: the stored `probe:` section could not be read")
    return ProbeConfig()


def apply(session: Session, probe: ProbeConfig) -> tuple[list[str], list[str]]:
    """``(applied, warnings)`` for a pushed dst.yaml's ``probe:`` section:
    stored when it differs from what is stored, else nothing."""
    if _dump(probe) == (_stored(session) or _dump(ProbeConfig())):
        return [], []
    session.execute(
        text(
            """
            INSERT INTO setting (org_id, key, value)
            VALUES (NULLIF(current_setting('app.current_org', true), '')::uuid,
                    :k, CAST(:v AS jsonb))
            ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value
            """
        ),
        {"k": KEY, "v": json.dumps(_dump(probe))},
    )
    age = "age rails off" if probe.max_age_delta is None else f"max_age {probe.max_age}"
    window = (
        "whole-table sampling"
        if probe.recent_partitions == 0
        else f"{probe.recent_partitions} recent partitions"
    )
    return [f"probe: {age}, {window}"], []
