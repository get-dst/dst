"""Group-based access bridge: group allow-lists, caller groups, Clerk role mapping."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from services.auth import clerk
from services.config import settings
from services.contracts.lens_config import AccessConfig, AccessRule, LensConfig
from services.db.session import org_session
from services.governance import credentials
from services.governance.credentials import CallerIdentity
from services.governance.policy import authorize


def _lens_allowing(rules: list[AccessRule]) -> LensConfig:
    return LensConfig(name="l", display_name="L", access=AccessConfig(allow=rules))


def test_authorize_by_group() -> None:
    cfg = _lens_allowing([AccessRule(group="org:analyst")])
    member = CallerIdentity(
        org_id=uuid.uuid4(), name="alice", is_admin=False, groups=["org:analyst"]
    )
    outsider = CallerIdentity(org_id=uuid.uuid4(), name="bob", is_admin=False, groups=["org:sales"])
    ok, reason = authorize(member, cfg)
    assert ok and "group" in reason
    assert authorize(outsider, cfg)[0] is False


def test_authorize_caller_name_still_works() -> None:
    cfg = _lens_allowing([AccessRule(caller="svc-1")])
    svc = CallerIdentity(org_id=uuid.uuid4(), name="svc-1", is_admin=False)
    assert authorize(svc, cfg)[0] is True
    other = CallerIdentity(org_id=uuid.uuid4(), name="svc-2", is_admin=False)
    assert authorize(other, cfg)[0] is False


def test_clerk_admin_role_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    oid = uuid.uuid4()
    monkeypatch.setattr(clerk, "org_for_clerk", lambda ref, name: oid)

    monkeypatch.setattr(
        clerk, "verify_token", lambda _t: {"org_id": "o1", "org_role": "org:admin", "sub": "u1"}
    )
    admin = clerk.resolve_identity("tok")
    assert admin is not None and admin.is_admin and admin.groups == ["org:admin"]

    monkeypatch.setattr(
        clerk, "verify_token", lambda _t: {"org_id": "o1", "org_role": "org:member", "sub": "u2"}
    )
    member = clerk.resolve_identity("tok")
    assert member is not None and not member.is_admin and member.groups == ["org:member"]

    # personal account (no org) => admin of own tenant
    monkeypatch.setattr(clerk, "verify_token", lambda _t: {"sub": "u3"})
    personal = clerk.resolve_identity("tok")
    assert personal is not None and personal.is_admin


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


@needs_db
def test_caller_groups_round_trip() -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('GrpTest') RETURNING id")).scalar_one()
    try:
        with org_session(org) as s:
            credentials.create_caller(s, "analyst-bot", "service", ["org:analyst"])
            raw = credentials.issue_key(s, credentials.caller_id_by_name(s, "analyst-bot"))
            listing = credentials.list_callers(s)
        assert listing[0]["groups"] == ["org:analyst"]
        ident = credentials.verify_caller_key(raw)
        assert ident is not None and ident.groups == ["org:analyst"]
        # the key is authorized for a lens that allows its group
        cfg = _lens_allowing([AccessRule(group="org:analyst")])
        assert authorize(ident, cfg)[0] is True
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM api_key WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM caller WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
