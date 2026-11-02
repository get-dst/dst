"""The MCP consent step's sign-in at the company IdP, against a fake issuer.

The fake IdP is a real local HTTP server: discovery, a JWKS, a token endpoint
that hands back whatever ID token the test loaded, and a users listing for the
deprovisioning sync. So signature verification runs for real (PyJWKClient
fetches the keys over HTTP), and every refusal below is the product's own
check, not a stub's.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import create_engine, text

from services.auth import oidc, oidc_login
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.governance import ratelimit
from services.project import caller_groups
from services.project.schema import parse_project_yaml


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
AUDIENCE = "dst-mcp"


class FakeIdp:
    """Discovery + JWKS + token endpoint + users listing on a loopback port."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.next_id_token: str | None = None
        self.token_requests: list[dict[str, str]] = []
        self.users: Any = []
        self.users_status = 200
        self.users_requests = 0
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:  # quiet
                pass

            def _json(self, body: Any, status: int = 200, headers: dict[str, str] | None = None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/.well-known/openid-configuration":
                    self._json(
                        {
                            "issuer": idp.issuer,
                            "authorization_endpoint": f"{idp.issuer}/authorize",
                            "token_endpoint": f"{idp.issuer}/token",
                            "jwks_uri": f"{idp.issuer}/jwks",
                        }
                    )
                elif path == "/jwks":
                    jwk = json.loads(RSAAlgorithm.to_jwk(idp.key.public_key()))
                    self._json({"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]})
                elif path == "/users":
                    idp.users_requests += 1
                    page = parse_qs(urlparse(self.path).query).get("page", ["1"])[0]
                    if isinstance(idp.users, dict) and "pages" in idp.users:
                        pages = idp.users["pages"]
                        n = int(page)
                        headers = (
                            {"Link": f'<{idp.issuer}/users?page={n + 1}>; rel="next"'}
                            if n < len(pages)
                            else {}
                        )
                        self._json(pages[n - 1], idp.users_status, headers)
                    else:
                        self._json(idp.users, idp.users_status)
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self) -> None:
                if urlparse(self.path).path != "/token":
                    self._json({"error": "not found"}, 404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                idp.token_requests.append(form)
                if idp.next_id_token is None:
                    self._json({"error": "invalid_grant"}, 400)
                    return
                self._json({"id_token": idp.next_id_token, "token_type": "Bearer"})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def mint(self, sent_nonce: str, **overrides: Any) -> str:
        """An ID token as this issuer would sign it; overrides poison one claim or
        swap the key (`key="other"`) for the refusal tests."""
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": AUDIENCE,
            "sub": "00u-sara",
            "email": "sara@corp.example",
            "groups": ["cs-team"],
            "nonce": sent_nonce,
            "iat": now,
            "exp": now + 300,
        }
        key = self.key
        if overrides.pop("key", None) == "other":
            key = self.other_key
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(scope="module")
def idp() -> Iterator[FakeIdp]:
    fake = FakeIdp()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def configured(idp: FakeIdp, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeIdp]:
    monkeypatch.setattr(settings, "oidc_issuer", idp.issuer)
    monkeypatch.setattr(settings, "oidc_audience", AUDIENCE)
    monkeypatch.setattr(settings, "oidc_client_id", None)
    monkeypatch.setattr(settings, "oidc_client_secret", None)
    monkeypatch.setattr(settings, "oidc_users_url", None)
    monkeypatch.setattr(settings, "oidc_users_token", None)
    monkeypatch.setattr(settings, "oidc_groups_claim", "groups")
    monkeypatch.setattr(settings, "oidc_token_days", 30)
    monkeypatch.setattr(settings, "oidc_org", f"oidc-test-{uuid.uuid4().hex[:6]}")
    idp.next_id_token = None
    idp.token_requests.clear()
    idp.users = []
    idp.users_status = 200
    oidc.reset_cache()
    try:
        yield idp
    finally:
        oidc.reset_cache()
        with _admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE oidc_ref = :r"), {"r": f"oidc:{idp.issuer}"})


@pytest.fixture
def client(live_client: TestClient) -> TestClient:
    live_client.cookies.clear()
    return live_client


def _org_with_mapping(mapping: dict[str, list[str]], groups: list[str]) -> uuid.UUID:
    """The org the issuer's users land in, with a `groups:` mapping applied as
    `dst apply` would store it."""
    org = oidc.org_id()
    with _admin.begin() as c:
        c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org)})
        for key, value in ((caller_groups.KEY, groups), (caller_groups.IDP_KEY, mapping)):
            c.execute(
                text(
                    "INSERT INTO setting (org_id, key, value) VALUES (:o, :k, CAST(:v AS jsonb)) "
                    "ON CONFLICT (org_id, key) DO UPDATE SET value = EXCLUDED.value"
                ),
                {"o": org, "k": key, "v": json.dumps(value)},
            )
    return org


def _caller(org: uuid.UUID, name: str) -> tuple[uuid.UUID, list[str], str | None] | None:
    with _admin.begin() as c:
        c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org)})
        row = c.execute(
            text("SELECT id, groups, idp_subject FROM caller WHERE name = :n"), {"n": name}
        ).first()
    return (uuid.UUID(str(row[0])), list(row[1] or []), row[2]) if row else None


def _start(client: TestClient, cb: str = "http://127.0.0.1:9999/cb") -> dict[str, str]:
    """Register a client and press the IdP button: returns the pending request
    (state, nonce, client params) the browser now holds in its cookie."""
    ratelimit.reset()  # registration is throttled per address; these tests register a lot
    reg = client.post("/oauth/register", json={"redirect_uris": [cb], "client_name": "Claude"})
    assert reg.status_code == 201
    params = {
        "client_id": reg.json()["client_id"],
        "redirect_uri": cb,
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "state": "client-state",
        "scope": "read",
    }
    page = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert page.status_code == 200
    assert "Continue with" in page.text and 'action="/oauth/authorize/idp"' in page.text
    assert 'name="credential"' not in page.text  # no key field on the IdP page
    r = client.post("/oauth/authorize/idp", data=params, follow_redirects=False)
    assert r.status_code == 302, r.text
    to = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(to.query).items()}
    assert to.path == "/authorize" and q["code_challenge_method"] == "S256"
    assert q["client_id"] == AUDIENCE and q["redirect_uri"].endswith("/oauth/callback")
    assert "nonce" in q and "state" in q and "code_challenge" in q
    assert oidc_login.PENDING_COOKIE in r.cookies
    return {**params, "idp_state": q["state"], "nonce": q["nonce"]}


def _callback(client: TestClient, pending: dict[str, str], **kw: Any):
    return client.get(
        "/oauth/callback",
        params={"code": "idp-code-1", "state": kw.pop("state", pending["idp_state"])},
        follow_redirects=False,
    )


# --- the flow ---------------------------------------------------------------------


@needs_db
def test_idp_sign_in_mints_a_short_lived_token_bound_to_a_mapped_caller(
    client: TestClient, configured: FakeIdp
) -> None:
    org = _org_with_mapping({"support": ["cs-team"]}, ["support", "finance"])
    pending = _start(client)
    configured.next_id_token = configured.mint(pending["nonce"])

    back = _callback(client, pending)
    assert back.status_code == 302, back.text
    loc = urlparse(back.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == pending["redirect_uri"]
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert q["state"] == "client-state"
    # The exchange carried our PKCE verifier and the client id, no secret.
    sent = configured.token_requests[-1]
    assert sent["code"] == "idp-code-1" and sent["code_verifier"] and "client_secret" not in sent

    # The caller: created for the subject, named by email, groups through the mapping.
    found = _caller(org, "sara@corp.example")
    assert found is not None
    cid, groups, subject = found
    assert subject == "00u-sara" and groups == ["support"]

    # The dst token: the client's own PKCE still applies; the lifetime is the IdP one.
    tok = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"],
            "redirect_uri": pending["redirect_uri"],
            "client_id": pending["client_id"],
            "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        },
    )
    assert tok.status_code == 200, tok.text
    body = tok.json()
    assert body["access_token"].startswith("dsto_") and body["scope"] == "read"
    assert body["expires_in"] == 30 * 86400
    with _admin.begin() as c:
        bound = c.execute(
            text("SELECT caller_id FROM api_key WHERE key_hash = :h"),
            {"h": hash_token(body["access_token"])},
        ).scalar_one()
    assert uuid.UUID(str(bound)) == cid
    # The pending cookie is gone with the grant.
    assert oidc_login.PENDING_COOKIE not in client.cookies


@needs_db
@pytest.mark.parametrize(
    "poison",
    [
        {"aud": "some-other-app"},
        {"iss": "https://idp.example.com"},
        {"key": "other"},
        {"nonce": "not-the-one-we-sent"},
        {"exp": int(time.time()) - 60},
        {"sub": None},
    ],
    ids=["audience", "issuer", "signature", "nonce", "expired", "no-subject"],
)
def test_an_unverifiable_id_token_grants_nothing(
    client: TestClient, configured: FakeIdp, poison: dict[str, Any]
) -> None:
    org = _org_with_mapping({"support": ["cs-team"]}, ["support"])
    pending = _start(client)
    configured.next_id_token = configured.mint(pending["nonce"], **poison)
    back = _callback(client, pending)
    assert back.status_code == 401 and "could not be verified" in back.text
    assert "location" not in back.headers
    assert _caller(org, "sara@corp.example") is None  # no caller, no code, no token


@needs_db
def test_a_callback_without_its_cookie_or_with_the_wrong_state_is_refused(
    client: TestClient, configured: FakeIdp
) -> None:
    _org_with_mapping({}, [])
    pending = _start(client)
    configured.next_id_token = configured.mint(pending["nonce"])
    wrong = _callback(client, pending, state="forged")
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_request"
    assert not configured.token_requests  # never reached the IdP
    client.cookies.clear()
    bare = _callback(client, pending)
    assert bare.status_code == 400
    assert not configured.token_requests


@needs_db
def test_an_idp_decline_clears_the_pending_request(client: TestClient, configured: FakeIdp) -> None:
    _org_with_mapping({}, [])
    pending = _start(client)
    r = client.get(
        "/oauth/callback",
        params={"error": "access_denied", "state": pending["idp_state"]},
        follow_redirects=False,
    )
    assert r.status_code == 401 and "declined" in r.text
    assert oidc_login.PENDING_COOKIE not in client.cookies
    assert not configured.token_requests


@needs_db
def test_an_unreachable_issuer_fails_closed_on_the_consent_page(
    client: TestClient, configured: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", "http://127.0.0.1:1")  # nothing listens
    oidc.reset_cache()
    cb = "http://127.0.0.1:9999/cb"
    reg = client.post("/oauth/register", json={"redirect_uris": [cb]})
    params = {
        "client_id": reg.json()["client_id"],
        "redirect_uri": cb,
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    }
    r = client.post("/oauth/authorize/idp", data=params, follow_redirects=False)
    assert r.status_code == 503 and "could not be reached" in r.text


def test_the_idp_button_is_absent_when_no_issuer_is_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", None)
    r = client.post(
        "/oauth/authorize/idp", data={"client_id": "x", "redirect_uri": "y", "code_challenge": "z"}
    )
    assert r.status_code == 404


# --- groups: mapping, refresh, precedence ---------------------------------------


def test_group_mapping_and_refresh_rules() -> None:
    mapping = {"support": ["cs-team", "cs-leads"], "finance": ["fin"]}
    assert caller_groups.map_idp_groups(mapping, ["cs-leads", "Everyone"]) == ["support"]
    assert caller_groups.map_idp_groups({}, ["cs-team"]) == []  # no mapping, no group
    # An admin-set unmapped group survives; a mapped one follows the IdP both ways.
    assert caller_groups.refresh_idp_groups(mapping, ["ops", "finance"], ["cs-team"]) == [
        "ops",
        "support",
    ]
    assert caller_groups.refresh_idp_groups(mapping, ["ops"], []) == ["ops"]


def test_dst_yaml_accepts_the_mapping_form_of_groups() -> None:
    cfg = parse_project_yaml("groups:\n  support:\n    idp: [cs-team]\n  sales:\n")
    assert cfg.groups == ["support", "sales"] and cfg.idp_groups == {"support": ["cs-team"]}
    assert parse_project_yaml("groups: [a, b]").idp_groups == {}
    with pytest.raises(ValueError, match="takes `idp:"):
        parse_project_yaml("groups:\n  support:\n    members: [x]\n")


@needs_db
def test_every_sign_in_refreshes_groups_through_the_mapping(
    client: TestClient, configured: FakeIdp
) -> None:
    org = _org_with_mapping(
        {"support": ["cs-team"], "finance": ["fin"]}, ["support", "finance", "ops"]
    )
    # First sign-in: support via cs-team. An unmapped IdP group grants nothing.
    pending = _start(client)
    configured.next_id_token = configured.mint(pending["nonce"], groups=["cs-team", "Everyone"])
    assert _callback(client, pending).status_code == 302
    found = _caller(org, "sara@corp.example")
    assert found is not None and found[1] == ["support"]
    cid = found[0]
    # An admin adds an unmapped group by hand.
    with _admin.begin() as c:
        c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org)})
        c.execute(
            text("UPDATE caller SET groups = :g WHERE id = :i"),
            {"g": ["support", "ops"], "i": cid},
        )
    # Second sign-in, moved teams at the IdP, email changed too: same subject, same
    # caller; support is dropped, finance granted, the admin's ops kept.
    pending = _start(client)
    configured.next_id_token = configured.mint(
        pending["nonce"], groups=["fin"], email="sara.n@corp.example"
    )
    assert _callback(client, pending).status_code == 302
    assert _caller(org, "sara.n@corp.example") is None  # the row is reused, not renamed
    found = _caller(org, "sara@corp.example")
    assert found is not None and found[0] == cid and found[1] == ["finance", "ops"]


@needs_db
def test_a_name_held_by_another_subject_is_refused_and_an_unbound_one_is_claimed(
    client: TestClient, configured: FakeIdp
) -> None:
    org = _org_with_mapping({"support": ["cs-team"]}, ["support"])
    with _admin.begin() as c:
        c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org)})
        c.execute(
            text(
                "INSERT INTO caller (org_id, name, type, groups) "
                "VALUES (:o, 'sara@corp.example', 'user', '{ops}')"
            ),
            {"o": org},
        )
    pending = _start(client)
    configured.next_id_token = configured.mint(pending["nonce"])
    assert _callback(client, pending).status_code == 302
    found = _caller(org, "sara@corp.example")
    assert found is not None and found[2] == "00u-sara" and found[1] == ["ops", "support"]
    # Someone else with the same email claim but another subject: refused.
    pending = _start(client)
    configured.next_id_token = configured.mint(pending["nonce"], sub="00u-impostor")
    r = _callback(client, pending)
    assert r.status_code == 401
    assert _caller(org, "sara@corp.example") == found


# --- deprovisioning ---------------------------------------------------------------


def _admin_headers(org: uuid.UUID) -> dict[str, str]:
    raw = new_admin_token()
    with _admin.begin() as c:
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    return {"Authorization": f"Bearer {raw}"}


def _active_keys(cid: uuid.UUID) -> int:
    with _admin.begin() as c:
        return int(
            c.execute(
                text("SELECT count(*) FROM api_key WHERE caller_id = :c AND revoked_at IS NULL"),
                {"c": cid},
            ).scalar_one()
        )


@needs_db
def test_sync_idp_revokes_the_callers_the_idp_no_longer_lists(
    client: TestClient, configured: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = _org_with_mapping({}, [])
    monkeypatch.setattr(settings, "oidc_users_url", f"{configured.issuer}/users")
    headers = _admin_headers(org)
    # Two people signed in; an admin-created service caller with no subject beside them.
    subjects = {}
    for sub, email in (("00u-sara", "sara@corp.example"), ("00u-tom", "tom@corp.example")):
        pending = _start(client)
        configured.next_id_token = configured.mint(pending["nonce"], sub=sub, email=email)
        back = _callback(client, pending)
        q = {k: v[0] for k, v in parse_qs(urlparse(back.headers["location"]).query).items()}
        tok = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": q["code"],
                "redirect_uri": pending["redirect_uri"],
                "client_id": pending["client_id"],
                "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
            },
        )
        assert tok.status_code == 200
        found = _caller(org, email)
        assert found is not None
        subjects[sub] = found[0]
    svc = client.post("/mgmt/callers", json={"name": "etl-bot"}, headers=headers).json()
    client.post("/mgmt/callers/etl-bot/keys", headers=headers)

    # Tom left: the listing carries sara only (Okta shape, paged).
    configured.users = {"pages": [[{"id": "00u-sara", "status": "ACTIVE"}], [{"id": "00u-x"}]]}
    dry = client.post("/mgmt/callers/sync-idp", params={"dry_run": "true"}, headers=headers)
    assert dry.status_code == 200, dry.text
    assert dry.json()["revoked"] == ["tom@corp.example"] and dry.json()["dry_run"] is True
    assert _active_keys(subjects["00u-tom"]) == 1  # a dry run revokes nothing
    assert configured.users_requests == 2  # both pages read

    real = client.post("/mgmt/callers/sync-idp", headers=headers)
    assert real.status_code == 200, real.text
    body = real.json()
    assert body["revoked"] == ["tom@corp.example"] and body["keys_revoked"] == 1
    assert body["listed"] == 2 and body["callers"] == 2
    assert _active_keys(subjects["00u-tom"]) == 0
    assert _active_keys(subjects["00u-sara"]) == 1
    assert _active_keys(uuid.UUID(svc["id"])) == 1  # never bound to the IdP, never touched
    # Idempotent: a second run has nothing left to revoke.
    assert client.post("/mgmt/callers/sync-idp", headers=headers).json()["keys_revoked"] == 0


@needs_db
@pytest.mark.parametrize("shape", ["unset", "down", "empty", "not-json"])
def test_sync_idp_revokes_nothing_on_a_listing_it_cannot_trust(
    client: TestClient, configured: FakeIdp, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    org = _org_with_mapping({}, [])
    headers = _admin_headers(org)
    pending = _start(client)
    configured.next_id_token = configured.mint(pending["nonce"])
    back = _callback(client, pending)
    q = {k: v[0] for k, v in parse_qs(urlparse(back.headers["location"]).query).items()}
    client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"],
            "redirect_uri": pending["redirect_uri"],
            "client_id": pending["client_id"],
            "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        },
    )
    found = _caller(org, "sara@corp.example")
    assert found is not None
    if shape == "unset":
        monkeypatch.setattr(settings, "oidc_users_url", None)
    else:
        monkeypatch.setattr(settings, "oidc_users_url", f"{configured.issuer}/users")
    if shape == "down":
        configured.users_status = 500
    elif shape == "not-json":
        configured.users = "just a string"
    r = client.post("/mgmt/callers/sync-idp", headers=headers)
    assert r.status_code == 503, r.text
    assert _active_keys(found[0]) == 1


@needs_db
def test_the_dashboard_oidc_path_maps_groups_the_same_way(
    configured: FakeIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One mapping rule on every door: a token presented straight to the API
    (the dashboard path) gets the mapped groups, not the raw claim."""
    _org_with_mapping({"support": ["cs-team"]}, ["support"])
    token = configured.mint("", groups=["cs-team", "Everyone"], nonce=None)
    ident = oidc.resolve_identity(token)
    assert ident is not None
    assert ident.user == "sara@corp.example" and ident.groups == ["support"]
    assert oidc.verify_id_token(token, "anything") is None  # no nonce, no ID-token trust
