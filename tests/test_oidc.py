"""Generic OIDC: bring-your-own-IdP auth, beside Clerk.

Token *signature* verification needs a live IdP (JWKS), so — like the Clerk tests —
these monkeypatch `verify_token` to return claims and prove everything downstream: org
provisioning, group and admin mapping, and that an IdP group grants lens access through
the unchanged policy. The audience-is-enforced guard is checked directly.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from services.auth import oidc
from services.config import settings


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


def test_verify_refuses_without_an_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issuer set, audience unset ⇒ refuse. Verifying without an audience check is the
    documented multi-tenant-IdP hole (any app's token validates); fail closed."""
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.com")
    monkeypatch.setattr(settings, "oidc_audience", None)
    assert oidc.verify_token("any.jwt.here") is None


def test_inert_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", None)
    assert oidc.is_configured() is False
    assert oidc.resolve_identity("any.jwt") is None


def test_groups_map_and_admin_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.com")
    monkeypatch.setattr(settings, "oidc_groups_claim", "groups")
    monkeypatch.setattr(settings, "oidc_admin_group", "dst-admins")

    # A member of the admin group.
    monkeypatch.setattr(
        oidc, "org_for_oidc", lambda ref, name: uuid.UUID("00000000-0000-0000-0000-000000000001")
    )
    monkeypatch.setattr(
        oidc,
        "verify_token",
        lambda _t: {"email": "boss@corp.example", "groups": ["dst-admins", "finance"]},
    )
    ident = oidc.resolve_identity("jwt")
    assert ident is not None
    assert ident.user == "boss@corp.example"
    assert set(ident.groups) == {"dst-admins", "finance"}
    assert ident.is_admin is True

    # A member with no admin group is NOT admin — admin is opt-in, per config.
    monkeypatch.setattr(
        oidc, "verify_token", lambda _t: {"preferred_username": "analyst", "groups": ["finance"]}
    )
    member = oidc.resolve_identity("jwt")
    assert member is not None and member.user == "analyst" and member.is_admin is False


@needs_db
def test_org_provisioning_is_idempotent() -> None:
    ref = f"oidc:test:{uuid.uuid4()}"
    try:
        first = oidc.org_for_oidc(ref, "Acme")
        second = oidc.org_for_oidc(ref, "Acme")
        assert first == second
    finally:
        with create_engine(settings.database_admin_url).begin() as c:
            c.execute(text("DELETE FROM org WHERE oidc_ref = :r"), {"r": ref})


@needs_db
def test_oidc_group_grants_lens_access_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The payoff: a Keycloak group in the token grants a lens whose allow-list names
    that group — through the SAME policy.authorize every other identity uses."""
    from fastapi.testclient import TestClient

    from services.app import app
    from services.auth.tokens import hash_token, new_admin_token
    from services.contracts.lens_config import AccessRule
    from services.lenses.demo import jaffle_customer_value_bundle

    client = TestClient(app)
    admin_eng = create_engine(settings.database_admin_url)

    # An org provisioned as if OIDC users had signed into it.
    ref = f"oidc:test:{uuid.uuid4()}"
    admin_raw = new_admin_token()
    with admin_eng.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name, oidc_ref) VALUES ('OidcCo', :r) RETURNING id"), {"r": ref}
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(admin_raw)},
        )

    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.com")
    monkeypatch.setattr(settings, "oidc_audience", "dst")
    monkeypatch.setattr(settings, "oidc_groups_claim", "groups")
    # Resolve OIDC tokens to this exact org, with a group in the claim.
    monkeypatch.setattr(oidc, "org_for_oidc", lambda _ref, _name: uuid.UUID(str(org)))
    monkeypatch.setattr(
        oidc, "verify_token", lambda _t: {"email": "sara@idp.example", "groups": ["analysts"]}
    )

    ah = {"Authorization": f"Bearer {admin_raw}"}
    try:
        # A lens granted to the "analysts" group.
        bundle = jaffle_customer_value_bundle()
        bundle.config.access.allow = [AccessRule(group="analysts")]
        assert (
            client.post("/mgmt/lenses", json=bundle.model_dump(mode="json"), headers=ah).status_code
            == 201
        )
        assert client.post("/mgmt/lenses/customer_value/publish", headers=ah).status_code == 200

        # An OIDC token (any JWT-shaped string; verify is stubbed) resolves to sara,
        # whose "analysts" group is on the allow-list → she can list the lens.
        r = client.get("/v1/lenses", headers={"Authorization": "Bearer eyJhbGciOi.oidc.token"})
        assert r.status_code == 200, r.text
        assert any(x["name"] == "customer_value" for x in r.json()), (
            "OIDC group did not grant lens access through policy.authorize"
        )
    finally:
        with admin_eng.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
