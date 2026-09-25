"""Public demo mode: every signed-in person is a demo caller in one org.

Pins the mapping on all three doors (bearer data plane, MCP OAuth grant, key
mint), that sign-in never reaches admin, that the self-serve key is one live
key per person, and that none of it exists when DST_DEMO_ORG_ID is unset.
Clerk verification is faked at `verify_token` — a token is not a test fixture.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api.oauth import _resolve_grant_identity
from services.auth import clerk, demo
from services.config import settings
from services.contracts.lens_config import AccessRule
from services.db.session import org_session
from services.governance import ratelimit
from services.lenses import store
from services.lenses.demo import LENS_NAME, jaffle_customer_value_bundle


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
_admin = create_engine(settings.database_admin_url)

VISITOR = "Visitor@Example.com"
CLAIMS = {"email": VISITOR, "sub": "user_1"}


@pytest.fixture(autouse=True)
def _clean_budget() -> Iterator[None]:
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture
def client(live_client: TestClient) -> TestClient:
    return live_client


@pytest.fixture
def demo_org(monkeypatch: pytest.MonkeyPatch) -> Iterator[uuid.UUID]:
    """Demo mode on, pointed at a fresh org with the demo lens published to
    group `demo`; Clerk verification faked to one visitor."""
    with _admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('DemoMode') RETURNING id")
        ).scalar_one()
    bundle = jaffle_customer_value_bundle()
    bundle.config.access.allow = [AccessRule(group="demo")]
    with org_session(org) as session:
        store.create_lens(session, bundle)
        store.publish(session, LENS_NAME)
    monkeypatch.setattr(settings, "demo_org_id", str(org))
    monkeypatch.setattr(clerk, "verify_token", lambda t: CLAIMS if t == "clerk-ok" else None)
    try:
        yield uuid.UUID(str(org))
    finally:
        with _admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@needs_db
def test_visitor_is_a_demo_caller_in_the_demo_org(demo_org: uuid.UUID) -> None:
    ident = demo.resolve("clerk-ok")
    assert ident is not None
    assert ident.org_id == demo_org
    assert ident.name == VISITOR.lower()
    assert ident.groups == ["demo"]
    assert not ident.is_admin
    # Idempotent: the same person is the same caller row.
    again = demo.resolve("clerk-ok")
    assert again is not None and again.caller_id == ident.caller_id
    with org_session(demo_org) as session:
        rows = session.execute(text("SELECT type FROM caller WHERE name = :n"), {"n": ident.name})
        assert [r[0] for r in rows] == ["demo"]


@needs_db
def test_bearer_data_plane_sees_the_demo_lens(client: TestClient, demo_org: uuid.UUID) -> None:
    r = client.get(f"/v1/lenses/{LENS_NAME}", headers=_auth("clerk-ok"))
    assert r.status_code == 200
    assert client.get("/v1/lenses", headers=_auth("clerk-bad")).status_code == 401


@needs_db
def test_sign_in_never_reaches_admin(client: TestClient, demo_org: uuid.UUID) -> None:
    r = client.get("/mgmt/callers", headers=_auth("clerk-ok"))
    assert r.status_code == 403
    assert "public demo" in r.json()["detail"]


@needs_db
def test_mcp_grant_lands_on_the_demo_caller(demo_org: uuid.UUID) -> None:
    resolved = _resolve_grant_identity("clerk-ok")
    assert resolved is not None
    org, cid = resolved
    assert org == demo_org
    ident = demo.resolve("clerk-ok")
    assert ident is not None and ident.caller_id == cid
    assert _resolve_grant_identity("clerk-bad") is None


@needs_db
def test_mint_issues_one_live_key_per_person(client: TestClient, demo_org: uuid.UUID) -> None:
    first = client.post("/auth/demo-key", headers=_auth("clerk-ok"))
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["caller"] == VISITOR.lower()
    assert body["key"].startswith("dst_")
    assert body["lenses"] == [LENS_NAME]
    # each lens comes with a question its own semantic layer declares, not a fixed one
    assert set(body["examples"]) == {LENS_NAME}
    assert body["expires_in_days"] == settings.demo_key_days
    # The minted key is a real caller key on the data plane.
    assert client.get(f"/v1/lenses/{LENS_NAME}", headers=_auth(body["key"])).status_code == 200
    # A re-mint retires the previous key.
    second = client.post("/auth/demo-key", headers=_auth("clerk-ok"))
    assert second.status_code == 201
    assert client.get(f"/v1/lenses/{LENS_NAME}", headers=_auth(body["key"])).status_code == 401
    assert (
        client.get(f"/v1/lenses/{LENS_NAME}", headers=_auth(second.json()["key"])).status_code
        == 200
    )


@needs_db
def test_mint_needs_a_session(client: TestClient, demo_org: uuid.UUID) -> None:
    assert client.post("/auth/demo-key").status_code == 401
    assert client.post("/auth/demo-key", headers=_auth("clerk-bad")).status_code == 401


@needs_db
def test_mint_is_budgeted_per_address(client: TestClient, demo_org: uuid.UUID) -> None:
    from services.api.demo import _MINT_IP_RPM

    for _ in range(_MINT_IP_RPM):
        assert client.post("/auth/demo-key", headers=_auth("clerk-ok")).status_code == 201
    r = client.post("/auth/demo-key", headers=_auth("clerk-ok"))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_nothing_exists_outside_demo_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "demo_org_id", None)
    assert client.get("/demo").status_code == 404
    assert client.post("/auth/demo-key", headers=_auth("clerk-ok")).status_code == 404
    assert demo.resolve("clerk-ok") is None


def test_page_needs_clerk(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "demo_org_id", str(uuid.uuid4()))
    monkeypatch.setattr(clerk, "issuer", lambda: None)
    assert client.get("/demo").status_code == 503


def test_page_boots_clerk_under_the_consent_policy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "demo_org_id", str(uuid.uuid4()))
    monkeypatch.setattr(settings, "clerk_publishable_key", "pk_test_x")
    monkeypatch.setattr(clerk, "issuer", lambda: "https://ex.clerk.accounts.dev")
    r = client.get("/demo")
    assert r.status_code == 200
    assert 'data-clerk-publishable-key="pk_test_x"' in r.text
    assert "ex.clerk.accounts.dev/npm/@clerk/clerk-js" in r.text
    assert "/auth/demo-key" in r.text
    assert r.headers["content-security-policy"].startswith("frame-ancestors 'none'")


def test_every_page_a_person_sees_carries_the_instance_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DST_INSTANCE_NAME names the deployment on the demo page and both consent
    pages, not only to the driver AI; a named deployment credits dst beneath it."""
    from services.api.oauth import _clerk_consent_html, _consent_html

    monkeypatch.setenv("DST_INSTANCE_NAME", "roshan")
    monkeypatch.setattr(settings, "demo_org_id", str(uuid.uuid4()))
    monkeypatch.setattr(settings, "clerk_publishable_key", "pk_test_x")
    monkeypatch.setattr(clerk, "issuer", lambda: "https://ex.clerk.accounts.dev")
    page = client.get("/demo").text
    assert "<title>roshan · demo</title>" in page
    assert "answers by dst (data serve tool)" in page
    assert "Sign in to roshan" in _clerk_consent_html({}, "pk", "host", "Claude")
    assert "Authorize roshan access" in _consent_html({})

    monkeypatch.delenv("DST_INSTANCE_NAME")
    page = client.get("/demo").text
    assert "<title>dst · demo</title>" in page and "answers by dst" not in page


def test_a_demo_oauth_token_lives_as_long_as_a_demo_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no refresh token, so a demo client's grant is the token's whole
    life: the week the demo promises, then the person reconnects. Outside demo
    mode the long default stands."""
    from datetime import timedelta

    from services.api.oauth import _token_ttl
    from services.auth import oauth

    monkeypatch.setattr(settings, "demo_org_id", str(uuid.uuid4()))
    monkeypatch.setattr(settings, "demo_key_days", 7)
    assert _token_ttl() == timedelta(days=7)
    monkeypatch.setattr(settings, "demo_org_id", None)
    assert _token_ttl() == oauth.OAUTH_TOKEN_TTL


@needs_db
def test_the_page_says_what_the_demo_is_about_before_sign_in(
    client: TestClient, demo_org: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visitor sees what they will be able to ask before signing in: each lens a
    demo caller may use, by its display name and description, with an example
    question its own semantic layer declares — and the name to give the connector."""
    monkeypatch.setenv("DST_INSTANCE_NAME", "roshan")
    monkeypatch.setattr(settings, "clerk_publishable_key", "pk_test_x")
    monkeypatch.setattr(clerk, "issuer", lambda: "https://ex.clerk.accounts.dev")
    cfg = jaffle_customer_value_bundle().config  # the lens the fixture published
    page = client.get("/demo").text
    assert "<h1>roshan</h1>" in page
    assert "What you can ask" in page
    assert f"<b>{cfg.display_name or cfg.name}</b>" in page
    assert "custom connector named <b>roshan</b>" in page
