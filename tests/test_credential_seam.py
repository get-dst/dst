"""The credential seam: per-user warehouse identity is *possible*, not walled off.

dst does not build IdP-to-warehouse federation (that is customer-specific state
that belongs in the customer's own system). What it guarantees is the seam — the
caller identity reaches the point where the credential is chosen, and the choice is
overridable. If the fake resolver below cannot be written against the public
surface, we have walled it off without meaning to, which is the thing this file
exists to catch.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.governance.credentials import CallerIdentity
from services.lenses import connections, credential_resolver


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


@pytest.fixture(autouse=True)
def _restore_default_resolver() -> object:
    yield
    credential_resolver.reset_resolver()


def test_default_resolver_returns_the_org_secret() -> None:
    """The managed default: one service account per org, nothing to configure."""
    req = credential_resolver.CredentialRequest(
        caller=None,
        connection="wh",
        connection_type="snowflake",
        config={"account": "a"},
        org_secret="org-service-account-secret",
    )
    assert credential_resolver.resolve(req) == "org-service-account-secret"


def test_a_resolver_receives_the_caller_and_can_key_on_it() -> None:
    """The whole point — an operator's automation gets the person and can return a
    per-user credential. Fall-through to org_secret for callers it does not handle
    is the correct shape: an override is additive, never a cliff."""
    seen: list[str | None] = []

    def per_user(req: credential_resolver.CredentialRequest) -> str | None:
        seen.append(req.caller.name if req.caller else None)
        if req.caller and req.caller.name == "antti":
            return f"keypair-for-{req.caller.name}"
        return req.org_secret

    credential_resolver.set_resolver(per_user)

    antti = credential_resolver.CredentialRequest(
        caller=CallerIdentity(org_id=uuid.uuid4(), name="antti", is_admin=False),
        connection="wh",
        connection_type="snowflake",
        config={},
        org_secret="org-default",
    )
    other = credential_resolver.CredentialRequest(
        caller=CallerIdentity(org_id=uuid.uuid4(), name="someone-else", is_admin=False),
        connection="wh",
        connection_type="snowflake",
        config={},
        org_secret="org-default",
    )
    assert credential_resolver.resolve(antti) == "keypair-for-antti"
    assert credential_resolver.resolve(other) == "org-default", (
        "fall-through must reach the default"
    )
    assert seen == ["antti", "someone-else"]


@needs_db
def test_the_caller_reaches_the_seam_through_resolve_connector() -> None:
    """End to end: resolve_connector(..., caller=X) actually delivers X to the
    resolver. This is the plumbing guarantee — the identity is not discarded at the
    org boundary the way it was before this shipped.

    A secretless DuckDB connection, deliberately: this asserts the *identity*
    reaches the seam, which does not depend on there being a stored secret to
    decrypt (and so runs whether or not DST_SECRET_KEY is configured)."""
    from services.db.session import org_session
    from services.lenses import connection_store

    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        oid = c.execute(text("INSERT INTO org (name) VALUES ('Seam') RETURNING id")).scalar_one()

    captured: dict[str, object] = {}

    def spy(req: credential_resolver.CredentialRequest) -> str | None:
        captured["caller_name"] = req.caller.name if req.caller else None
        captured["connection_type"] = req.connection_type
        return req.org_secret

    credential_resolver.set_resolver(spy)
    try:
        with org_session(oid) as s:
            connection_store.create_connection(
                s, "wh", "duckdb", {"path": settings.duckdb_jaffle_path}, secret=None
            )
            s.commit()
        caller = CallerIdentity(org_id=uuid.UUID(str(oid)), name="antti", is_admin=False)
        connections.resolve_connector("wh", oid, caller=caller)
        assert captured["caller_name"] == "antti", "the caller did not reach the seam"
        assert captured["connection_type"] == "duckdb"
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})


@needs_db
def test_mgmt_plane_resolution_has_no_caller() -> None:
    """The admin plane runs as the org, not a person — the seam must see caller=None
    there, so a per-user resolver correctly falls through to the org account for
    introspection/profiling rather than trying to map a person who isn't present."""
    from services.db.session import org_session
    from services.lenses import connection_store

    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('SeamMgmt') RETURNING id")
        ).scalar_one()

    seen: dict[str, object] = {}

    def spy(req: credential_resolver.CredentialRequest) -> str | None:
        seen["caller"] = req.caller
        return req.org_secret

    credential_resolver.set_resolver(spy)
    try:
        with org_session(oid) as s:
            connection_store.create_connection(
                s, "wh", "duckdb", {"path": settings.duckdb_jaffle_path}, secret=None
            )
            s.commit()
        # No caller= : this is how every /mgmt/* site calls it.
        connections.resolve_connector("wh", oid)
        assert seen["caller"] is None
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
