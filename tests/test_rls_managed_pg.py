"""Managed-Postgres parity: the admin engine cannot assume BYPASSRLS.

Local dev and CI run the admin engine as a docker superuser, which silently
bypasses RLS. Cloud SQL / RDS / Neon admin roles are NOT superusers: FORCE-RLS
tables (caller, local_user) are invisible to them unless the org GUC is set.
The symptom: every freshly minted caller key comes back "invalid caller key"
while the row sits plainly in api_key.

These tests re-run the auth resolution paths through a role with superuser and
BYPASSRLS stripped — the managed-PG shape — and pin that they still resolve.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from services.auth import local
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.db.session import org_session
from services.governance import credentials


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


@pytest.fixture
def probe_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """An engine whose role mirrors a managed-PG admin: LOGIN, no SUPERUSER, no
    BYPASSRLS — swapped in for the module-level admin engines under test.

    Roles are cluster-global while scratch DBs are per-worktree, so the name is
    derived from the DB name to keep parallel suite runs off each other.
    """
    url = make_url(settings.database_admin_url)
    role = f"rls_probe_{url.database}"[:63]
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text(f"DROP ROLE IF EXISTS {role}"))
        c.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'probe' NOSUPERUSER NOBYPASSRLS"))
        c.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        c.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
        )
    probe = create_engine(url.set(username=role, password="probe"))
    monkeypatch.setattr(credentials, "admin_engine", probe)
    monkeypatch.setattr(local, "admin_engine", probe)
    yield probe
    probe.dispose()
    with admin.begin() as c:
        c.execute(text(f"DROP OWNED BY {role}"))
        c.execute(text(f"DROP ROLE {role}"))
    admin.dispose()


def _mk_org(name: str) -> tuple[str, str]:
    """org + admin token via the real (superuser) admin engine — setup, not the path
    under test."""
    raw = new_admin_token()
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES (:n) RETURNING id"), {"n": name}
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    admin.dispose()
    return str(org), raw


@needs_db
def test_probe_role_is_rls_bound(probe_engine: Engine) -> None:
    """The fixture's role must actually be subject to RLS — otherwise every test
    here is vacuously green and pins nothing."""
    org, _ = _mk_org("RLS Probe Guard")
    with org_session(org) as s:
        credentials.create_caller(s, "guard-caller")
        s.commit()
    with probe_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM caller")).scalar() == 0


@needs_db
def test_caller_key_resolves_without_bypassrls(probe_engine: Engine) -> None:
    org, _ = _mk_org("RLS Probe Keys")
    with org_session(org) as s:
        cid = credentials.create_caller(s, "probe-bot", "service", ["org:analyst"])
        raw = credentials.issue_key(s, cid)
        s.commit()
    ident = credentials.verify_caller_key(raw)
    assert ident is not None, "minted key must verify under a non-BYPASSRLS admin role"
    assert ident.org_id == uuid.UUID(org)
    assert ident.name == "probe-bot"
    assert ident.groups == ["org:analyst"]
    assert credentials.verify_caller_key("dst_bogus") is None


@needs_db
def test_local_login_and_resolve_without_bypassrls(probe_engine: Engine) -> None:
    org, _ = _mk_org("RLS Probe Login")
    with org_session(org) as s:
        local.create_user(s, "probe@example.com", "hunter2hunter2", role="admin")
        s.commit()
    token = local.login("probe@example.com", "hunter2hunter2")
    assert token is not None, "login must find the FORCE-RLS local_user row"
    ident = local.resolve(token)
    assert ident is not None and ident.is_admin and ident.user == "probe@example.com"
    assert local.login("probe@example.com", "wrong-password") is None
    assert local.resolve("dstsess_bogus") is None
