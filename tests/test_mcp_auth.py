"""MCP transport auth + OAuth.

Covers the streamable-HTTP surface:
  - no key / bad key / revoked key -> 401 + WWW-Authenticate at the *transport*
    (not a working-looking session that fails inside every tool envelope);
  - admin token -> 403 with remediation (no superuser over MCP);
  - a valid caller key reaches JSON-RPC;
  - the OAuth metadata + full PKCE round-trip (self-contained consent, credential-based)
    mints a token bound to a real caller (attribution), and it clears the transport gate;
  - POST /mcp (no trailing slash) is answered directly, not 307-redirected.
No live BigQuery/LLM: these stop at the transport/authorize step.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.auth.tokens import hash_token, new_caller_key
from services.config import settings
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
_admin = create_engine(settings.database_admin_url)
_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}


@pytest.fixture
def client(live_client: TestClient) -> TestClient:
    # The session-wide live app (see conftest): one lifespan per process, so valid-key
    # requests can reach the inner JSON-RPC app.
    return live_client


@pytest.fixture
def seeded() -> Iterator[tuple[uuid.UUID, uuid.UUID, str]]:
    """An org + caller + a live dst_ key. Yields (org_id, caller_id, raw_key)."""
    raw = new_caller_key()
    with _admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('McpAuth') RETURNING id")).scalar_one()
        cid = c.execute(
            text(
                "INSERT INTO caller (org_id, name, type) VALUES (:o, 'svc@t', 'service') "
                "RETURNING id"
            ),
            {"o": org},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO api_key (org_id, caller_id, key_hash, prefix) VALUES (:o, :c, :h, :p)"
            ),
            {"o": org, "c": cid, "h": hash_token(raw), "p": raw[:12]},
        )
    try:
        yield uuid.UUID(str(org)), uuid.UUID(str(cid)), raw
    finally:
        with _admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})  # cascades keys+callers


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


# --- metadata (no DB) -----------------------------------------------------------


def test_protected_resource_metadata(client: TestClient) -> None:
    r = client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    body = r.json()
    assert body["resource"].endswith("/mcp")
    assert body["authorization_servers"]


def test_authorization_server_metadata(client: TestClient) -> None:
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    body = r.json()
    assert body["authorization_endpoint"].endswith("/oauth/authorize")
    assert body["token_endpoint"].endswith("/oauth/token")
    assert body["code_challenge_methods_supported"] == ["S256"]


# --- transport auth -------------------------------------------------------------


def test_no_token_is_401_with_www_authenticate(client: TestClient) -> None:
    r = client.post("/mcp", json=_INIT, follow_redirects=False)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers.get("www-authenticate", "")
    assert "auth_url" in r.json()


def test_admin_token_is_403_with_remediation(client: TestClient) -> None:
    r = client.post("/mcp", headers={"Authorization": "Bearer dstadm_x"}, json=_INIT)
    assert r.status_code == 403
    assert "caller key" in r.json()["hint"].lower()


def test_no_trailing_slash_is_not_redirected(client: TestClient) -> None:
    # The friction: documented URL is /mcp; Starlette mounts 307 to /mcp/ and raw POST
    # clients don't follow. The gate must answer /mcp directly.
    r = client.post("/mcp", json=_INIT, follow_redirects=False)
    assert r.status_code != 307


@needs_db
def test_bad_key_is_401(client: TestClient) -> None:
    r = client.post("/mcp", headers={"Authorization": "Bearer dst_bogus"}, json=_INIT)
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_token"


@needs_db
def test_valid_key_clears_the_gate(
    client: TestClient, seeded: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    _, _, raw = seeded
    r = client.post("/mcp", headers={"Authorization": f"Bearer {raw}"}, json=_INIT)
    assert r.status_code not in (401, 403)  # reached JSON-RPC, not blocked at the transport


@needs_db
def test_revoked_key_is_401(client: TestClient, seeded: tuple[uuid.UUID, uuid.UUID, str]) -> None:
    _, _, raw = seeded
    with _admin.begin() as c:
        c.execute(
            text("UPDATE api_key SET revoked_at = now() WHERE key_hash = :h"),
            {"h": hash_token(raw)},
        )
    r = client.post("/mcp", headers={"Authorization": f"Bearer {raw}"}, json=_INIT)
    assert r.status_code == 401


@needs_db
def test_expired_oauth_token_is_401(
    client: TestClient, seeded: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    org, cid, _ = seeded
    expired = credentials.mint_oauth_token(org, cid, timedelta(seconds=-1))
    r = client.post("/mcp", headers={"Authorization": f"Bearer {expired}"}, json=_INIT)
    assert r.status_code == 401


# --- full OAuth PKCE round-trip, self-contained consent -------------------------


@needs_db
def test_oauth_pkce_roundtrip_mints_token_bound_to_a_caller(
    client: TestClient, seeded: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    from urllib.parse import parse_qs, urlparse

    _, caller_id, caller_key = seeded
    cb = "http://127.0.0.1:9999/cb"
    reg = client.post("/oauth/register", json={"redirect_uris": [cb], "client_name": "Claude"})
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]

    verifier, challenge = _pkce()
    # The authorize endpoint renders a self-contained consent form (no SPA, no redirect).
    az = client.get(
        "/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": cb,
            "code_challenge": challenge,
            "state": "s1",
        },
        follow_redirects=False,
    )
    # Either consent page (Clerk sign-in when configured, else credential paste) is HTML.
    assert az.status_code == 200 and "Connect MCP client" in az.text

    # Submit the form with a real caller key as the credential → redirect to the client.
    comp = client.post(
        "/oauth/authorize/complete",
        data={
            "credential": caller_key,
            "client_id": client_id,
            "redirect_uri": cb,
            "code_challenge": challenge,
            "state": "s1",
        },
        follow_redirects=False,
    )
    assert comp.status_code == 302
    loc = comp.headers["location"]
    assert loc.startswith(cb)
    code = parse_qs(urlparse(loc).query)["code"][0]

    tok = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cb,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200
    access = tok.json()["access_token"]
    assert access.startswith("dsto_")

    # The minted token is bound to the seeded caller (attribution) and clears the gate.
    with _admin.begin() as c:
        bound = c.execute(
            text("SELECT caller_id FROM api_key WHERE key_hash = :h"),
            {"h": hash_token(access)},
        ).scalar_one()
    assert str(bound) == str(caller_id)
    gated = client.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json=_INIT)
    assert gated.status_code not in (401, 403)

    # A tampered PKCE verifier is rejected — checked on a FRESH code. Reusing the
    # redeemed one would now fail as a replay and the assertion would
    # pass without PKCE having been exercised at all.
    verifier2, challenge2 = _pkce()
    comp2 = client.post(
        "/oauth/authorize/complete",
        data={
            "credential": caller_key,
            "client_id": client_id,
            "redirect_uri": cb,
            "code_challenge": challenge2,
            "state": "s2",
        },
        follow_redirects=False,
    )
    code2 = parse_qs(urlparse(comp2.headers["location"]).query)["code"][0]
    bad = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code2,
            "redirect_uri": cb,
            "client_id": client_id,
            "code_verifier": "wrong",
        },
    )
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"

    # …and a failed PKCE attempt must NOT have spent the code: otherwise anyone
    # holding a stolen code could burn it and deny the legitimate client its one
    # redemption. The code is claimed only after every other check passes.
    good = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code2,
            "redirect_uri": cb,
            "client_id": client_id,
            "code_verifier": verifier2,
        },
    )
    assert good.status_code == 200, "a failed PKCE attempt consumed the code"


@needs_db
def test_authorize_complete_rejects_bad_credential(
    client: TestClient, seeded: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    cb = "http://127.0.0.1:9999/cb"
    reg = client.post("/oauth/register", json={"redirect_uris": [cb]})
    client_id = reg.json()["client_id"]
    _, challenge = _pkce()
    r = client.post(
        "/oauth/authorize/complete",
        data={
            "credential": "dst_bogus",
            "client_id": client_id,
            "redirect_uri": cb,
            "code_challenge": challenge,
            "state": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 401 and "recognized" in r.text


@needs_db
def test_consent_page_names_the_client_and_where_the_code_goes(client: TestClient) -> None:
    """Consent-phishing defense: an attacker can register an arbitrary redirect and send
    a victim a link on the real dst origin. The page must name the registered client and
    the destination host, and flag a non-loopback destination — otherwise the victim has
    no on-page signal that the token is about to leave to an unfamiliar host."""
    cb = "https://evil.example/cb"
    reg = client.post("/oauth/register", json={"redirect_uris": [cb], "client_name": "Sketchy"})
    client_id = reg.json()["client_id"]
    _, challenge = _pkce()
    r = client.get(
        "/oauth/authorize",
        params={"client_id": client_id, "redirect_uri": cb, "code_challenge": challenge},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Sketchy" in r.text  # the registered name, not a generic placeholder
    assert "evil.example" in r.text  # where the code will be sent
    assert "not a local address" in r.text  # non-loopback warning


@needs_db
def test_unknown_client_authorize_rejected(client: TestClient) -> None:
    _, challenge = _pkce()
    r = client.get(
        "/oauth/authorize",
        params={"client_id": "kc_nope", "redirect_uri": "http://x/cb", "code_challenge": challenge},
        follow_redirects=False,
    )
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_no_key_minting_in_web() -> None:
    """Caller-key MINTING is CLI-only
    (`dst keys create`); the OSS SPA only lists and revokes. The Connect modal
    now carries a documented placeholder instead of issuing keys inline — so the
    invariant to keep dead is the issuance/creation hooks, not the placeholder."""
    from pathlib import Path

    web_src = Path(__file__).resolve().parent.parent / "apps" / "web" / "src"
    hits = [
        p.name
        for p in web_src.rglob("*.[tj]s*")
        if any(sym in p.read_text(encoding="utf-8") for sym in ("useIssueKey", "useCreateCaller"))
    ]
    assert not hits, f"key-minting UI code still present in {hits}"


@needs_db
def test_authorization_code_is_single_use_and_reuse_kills_the_token(
    client: TestClient, seeded: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    """A code redeems once; replaying it revokes what the first redemption minted.

    The docstrings in services/auth/oauth.py called these codes "one-time" three
    times and nothing enforced it — verify_auth_code checked signature, typ and
    exp, so a code was redeemable repeatedly for its full 120s life. PKCE narrows
    who can exploit that, but OAuth 2.1 requires both single use AND revocation of
    the issued token on reuse detection, because a replayed code means the code
    leaked and the token from redemption #1 is therefore in doubt.
    """
    from urllib.parse import parse_qs, urlparse

    _, _caller_id, caller_key = seeded
    cb = "http://127.0.0.1:9998/cb"
    client_id = client.post(
        "/oauth/register", json={"redirect_uris": [cb], "client_name": "Replayer"}
    ).json()["client_id"]

    verifier, challenge = _pkce()
    comp = client.post(
        "/oauth/authorize/complete",
        data={
            "credential": caller_key,
            "client_id": client_id,
            "redirect_uri": cb,
            "code_challenge": challenge,
            "state": "s",
        },
        follow_redirects=False,
    )
    code = parse_qs(urlparse(comp.headers["location"]).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cb,
        "client_id": client_id,
        "code_verifier": verifier,
    }

    first = client.post("/oauth/token", data=form)
    assert first.status_code == 200
    access = first.json()["access_token"]
    # The token works before the replay.
    assert client.post(
        "/mcp", headers={"Authorization": f"Bearer {access}"}, json=_INIT
    ).status_code not in (401, 403)

    second = client.post("/oauth/token", data=form)
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_grant"
    assert "already used" in second.json()["error_description"]

    # And the first token is now dead — reuse detection is not just a refusal to
    # mint a second token, it withdraws the first.
    with _admin.begin() as c:
        revoked = c.execute(
            text("SELECT revoked_at FROM api_key WHERE key_hash = :h"), {"h": hash_token(access)}
        ).scalar_one()
    assert revoked is not None, "code reuse did not revoke the token it had already issued"
    assert (
        client.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json=_INIT).status_code
        == 401
    )


@needs_db
def test_scope_is_granted_not_discarded(
    client: TestClient, seeded: tuple[uuid.UUID, uuid.UUID, str]
) -> None:
    """`scope` used to be accepted at /oauth/authorize and thrown away.

    The consent screen named a grant the minted token did not carry: a client that
    asked for `read` got an unrestricted token. This asserts the whole chain —
    advertised, shown, stored on the token, and enforced at the one route that
    distinguishes read from write.
    """
    from urllib.parse import parse_qs, urlparse

    # Advertised in both metadata documents, so a client can discover them.
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert set(prm["scopes_supported"]) == {"read", "write"}
    assert set(
        client.get("/.well-known/oauth-authorization-server").json()["scopes_supported"]
    ) == {
        "read",
        "write",
    }

    _, _cid, caller_key = seeded
    cb = "http://127.0.0.1:9997/cb"
    client_id = client.post(
        "/oauth/register", json={"redirect_uris": [cb], "client_name": "Reader"}
    ).json()["client_id"]

    # An unknown scope is refused outright rather than silently dropped — handing
    # back a token while ignoring what was asked for is the same class of lie.
    verifier, challenge = _pkce()
    base_params = {
        "client_id": client_id,
        "redirect_uri": cb,
        "code_challenge": challenge,
        "state": "s",
    }
    bad = client.get("/oauth/authorize", params={**base_params, "scope": "admin"})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_scope"

    # The consent page states the actual grant.
    az = client.get("/oauth/authorize", params={**base_params, "scope": "read"})
    assert az.status_code == 200
    assert "read governed data" in az.text
    assert "file corrections" not in az.text

    comp = client.post(
        "/oauth/authorize/complete",
        data={"credential": caller_key, "scope": "read", **base_params},
        follow_redirects=False,
    )
    code = parse_qs(urlparse(comp.headers["location"]).query)["code"][0]
    tok = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cb,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200
    assert tok.json()["scope"] == "read", "the token endpoint must echo what was granted"
    access = tok.json()["access_token"]

    with _admin.begin() as c:
        stored = c.execute(
            text("SELECT scopes FROM api_key WHERE key_hash = :h"), {"h": hash_token(access)}
        ).scalar_one()
    assert list(stored) == ["read"], "scope reached the consent screen but not the token"

    # Enforced: the read-scoped token cannot file a correction, and the 403 names
    # the scope it lacks so a client can step up in one round trip.
    denied = client.post(
        "/v1/reviews",
        headers={"Authorization": f"Bearer {access}"},
        json={"request_id": "whatever", "correction": {"kind": "definition", "note": "x"}},
    )
    assert denied.status_code == 403
    assert 'scope="write"' in denied.headers.get("www-authenticate", "")

    # An unscoped credential keeps working — every key minted before scopes
    # existed carries none, and they must not silently acquire a restriction.
    legacy = client.post(
        "/v1/reviews",
        headers={"Authorization": f"Bearer {caller_key}"},
        json={"request_id": "whatever", "correction": {"kind": "definition", "note": "x"}},
    )
    assert legacy.status_code != 403, "an unscoped key was retroactively restricted"
