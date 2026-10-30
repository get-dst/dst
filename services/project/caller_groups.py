"""The project's ``groups:`` (dst.yaml), stored per org.

The caller groups a lens may name in a `hide` entry's ``except_groups``. It
rides ``dst apply`` the way ``probe:`` does and lands in the org's ``setting``
row under one key, so every compile — a pushed lens, a stale recompile, the
plan's dry run — checks the names against the same list. Files win: a pushed
dst.yaml without the section stores none. Declaring a group grants nothing:
membership is what an admin attaches to a caller, or what an identity
provider asserts in a verified token.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger("dst.groups")

KEY = "groups"


def load(session: Session) -> list[str]:
    """The org's declared groups, none when none were applied."""
    try:
        value = session.execute(
            text("SELECT value FROM setting WHERE key = :k"), {"k": KEY}
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — an unreadable row declares nothing
        log.exception("groups: the stored `groups:` section could not be read")
        return []
    return [str(g) for g in value] if isinstance(value, list) else []


def apply(session: Session, groups: list[str]) -> list[str]:
    """``applied`` lines for a pushed dst.yaml's ``groups:``: stored when it
    differs from what is stored, else nothing."""
    wanted = sorted(set(groups))
    if wanted == sorted(load(session)):
        return []
    session.execute(
        text(
            """
            INSERT INTO setting (org_id, key, value)
            VALUES (NULLIF(current_setting('app.current_org', true), '')::uuid,
                    :k, CAST(:v AS jsonb))
            ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value
            """
        ),
        {"k": KEY, "v": json.dumps(wanted)},
    )
    return [f"groups: {', '.join(wanted) or 'none'}"]
