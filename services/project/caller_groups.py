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
# dst group -> the identity-provider groups that grant it (the `idp:` half of
# the mapping form). Read on every IdP sign-in; an IdP group named nowhere in
# it grants nothing.
IDP_KEY = "groups_idp"


def _read(session: Session, key: str) -> object:
    try:
        return session.execute(
            text("SELECT value FROM setting WHERE key = :k"), {"k": key}
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — an unreadable row declares nothing
        log.exception("groups: the stored `groups:` section could not be read")
        return None


def _write(session: Session, key: str, value: object) -> None:
    session.execute(
        text(
            """
            INSERT INTO setting (org_id, key, value)
            VALUES (NULLIF(current_setting('app.current_org', true), '')::uuid,
                    :k, CAST(:v AS jsonb))
            ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value
            """
        ),
        {"k": key, "v": json.dumps(value)},
    )


def load(session: Session) -> list[str]:
    """The org's declared groups, none when none were applied."""
    value = _read(session, KEY)
    return [str(g) for g in value] if isinstance(value, list) else []


def load_idp_mapping(session: Session) -> dict[str, list[str]]:
    """dst group -> IdP groups, empty when no mapping was applied."""
    value = _read(session, IDP_KEY)
    if not isinstance(value, dict):
        return {}
    return {str(k): [str(g) for g in v] for k, v in value.items() if isinstance(v, list)}


def map_idp_groups(mapping: dict[str, list[str]], idp_groups: list[str]) -> list[str]:
    """The dst groups a verified token's IdP groups grant, under ``mapping``.
    Exact names; an IdP group the mapping never names grants nothing."""
    asserted = set(idp_groups)
    return sorted(g for g, idps in mapping.items() if asserted & set(idps))


def refresh_idp_groups(
    mapping: dict[str, list[str]], current: list[str], idp_groups: list[str]
) -> list[str]:
    """A caller's groups after an IdP sign-in: the IdP owns every mapped dst
    group (granted when the token carries one of its IdP groups, dropped when
    not), and a group with no mapping keeps whatever an admin set."""
    kept = [g for g in current if g not in mapping]
    return sorted(set(kept) | set(map_idp_groups(mapping, idp_groups)))


def apply(
    session: Session, groups: list[str], idp_groups: dict[str, list[str]] | None = None
) -> list[str]:
    """``applied`` lines for a pushed dst.yaml's ``groups:``: stored when it
    differs from what is stored, else nothing."""
    out: list[str] = []
    wanted = sorted(set(groups))
    if wanted != sorted(load(session)):
        _write(session, KEY, wanted)
        out.append(f"groups: {', '.join(wanted) or 'none'}")
    mapping = {g: sorted(set(v)) for g, v in sorted((idp_groups or {}).items()) if v}
    if mapping != load_idp_mapping(session):
        _write(session, IDP_KEY, mapping)
        out.append(
            "groups from the identity provider: "
            + ("; ".join(f"{g} <- {', '.join(v)}" for g, v in mapping.items()) or "none")
        )
    return out
