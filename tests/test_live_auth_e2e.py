"""Live end-to-end auth: a REAL uvicorn server, real HTTP, the whole auth chain.

Unlike the rest of the suite (in-process TestClient), this launches an actual server
subprocess against the scratch Postgres and drives it over the wire with httpx. It
proves the stack boots and serves auth for real:

  health (unauth) → caller-key gate (401/200) → MCP transport challenge →
  OAuth discovery → PKCE round-trip → single-use code (replay rejected) →
  scope enforcement (read token refused on write) → revocation kills the key

Attribution rows and the warehouse tag are covered by test_identity_triple and the
live-Postgres test_warehouse_attribution; this one's unique job is the transport.

Opt-in — it costs a subprocess boot, so it is gated behind DST_TEST_LIVE_E2E=1
(the harness's live-lane convention) rather than run on every `make ci`:

    DST_TEST_LIVE_E2E=1 uv run pytest tests/test_live_auth_e2e.py
"""

from __future__ import annotations

import base64
import contextlib
import os
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import create_engine, text

from services.auth.tokens import hash_token, new_admin_token, new_caller_key
from services.config import settings

pytestmark = pytest.mark.skipif(
    os.environ.get("DST_TEST_LIVE_E2E") != "1",
    reason="live server e2e — set DST_TEST_LIVE_E2E=1 to run",
)

# A FIXED Fernet key: the encryption sentinel persists in the scratch DB, so a key
# that changed per run would make the second run's server refuse to boot. 32 bytes,
# url-safe-b64 encoded — a valid Fernet key derived visibly rather than random.
_SECRET_KEY = base64.urlsafe_b64encode(b"dst-live-e2e-fixed-secret-32").decode()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "DATABASE_ADMIN_URL": settings.database_admin_url,
        "DST_SECRET_KEY": _SECRET_KEY,
        "DST_PUBLIC_BASE_URL": base,
        "DST_PROVIDERS": "",
        "DST_DUCKDB_JAFFLE_PATH": settings.duckdb_jaffle_path,
    }
    # A prior run may have planted a sentinel under a different key; clear it so this
    # server plants its own. (Fixed key means this is belt-and-suspenders.)
    admin = create_engine(settings.database_admin_url)
    with contextlib.suppress(Exception), admin.begin() as c:
        c.execute(text("DELETE FROM dst_meta WHERE key = 'encryption-check'"))

    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "services.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early with code {proc.returncode}")
            with contextlib.suppress(Exception):
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            time.sleep(0.4)
        else:
            raise RuntimeError("server did not become healthy within 45s")
        yield base
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        if proc.poll() is None:
            proc.kill()


@pytest.fixture(scope="module")
def org_and_key(server: str) -> Iterator[tuple[str, str, str]]:
    """A live org with an admin token and a caller key. Yields (base, admin, key)."""
    admin = create_engine(settings.database_admin_url)
    admin_raw = new_admin_token()
    caller_raw = new_caller_key()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('LiveE2E') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 'e2e')"),
            {"o": org, "h": hash_token(admin_raw)},
        )
        cid = c.execute(
            text(
                "INSERT INTO caller (org_id, name, type) VALUES (:o, 'antti', 'user') RETURNING id"
            ),
            {"o": org},
        ).scalar_one()
        c.execute(
            text("INSERT INTO api_key (org_id, caller_id, key_hash, prefix) VALUES (:o,:c,:h,:p)"),
            {"o": org, "c": cid, "h": hash_token(caller_raw), "p": caller_raw[:12]},
        )
    try:
        yield server, admin_raw, caller_raw
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _pkce() -> tuple[str, str]:
    import hashlib

    verifier = (
        base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).rstrip(b"=").decode()
    )
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def test_health_is_open(server: str) -> None:
    assert httpx.get(f"{server}/health", timeout=5).status_code == 200


def test_caller_key_gate(org_and_key: tuple[str, str, str]) -> None:
    base, _admin, key = org_and_key
    assert httpx.get(f"{base}/v1/lenses", timeout=10).status_code == 401
    assert (
        httpx.get(
            f"{base}/v1/lenses", headers={"Authorization": "Bearer dst_bogus"}, timeout=10
        ).status_code
        == 401
    )
    ok = httpx.get(f"{base}/v1/lenses", headers={"Authorization": f"Bearer {key}"}, timeout=10)
    assert ok.status_code == 200, ok.text  # a real caller reaches the data plane over the wire


def test_mcp_transport_challenge(server: str) -> None:
    no_auth = httpx.post(
        f"{server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=10
    )
    assert no_auth.status_code == 401
    assert "resource_metadata=" in no_auth.headers.get("www-authenticate", "")
    admin_tok = httpx.post(
        f"{server}/mcp",
        headers={"Authorization": "Bearer dstadm_x"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        timeout=10,
    )
    assert admin_tok.status_code == 403  # admin tokens are refused over MCP


def test_oauth_discovery_advertises_scopes(server: str) -> None:
    prm = httpx.get(f"{server}/.well-known/oauth-protected-resource/mcp", timeout=10).json()
    assert prm["resource"].endswith("/mcp")
    assert set(prm["scopes_supported"]) == {"read", "write"}
    asm = httpx.get(f"{server}/.well-known/oauth-authorization-server", timeout=10).json()
    assert asm["code_challenge_methods_supported"] == ["S256"]


def test_full_oauth_roundtrip_scope_and_replay(org_and_key: tuple[str, str, str]) -> None:
    base, _admin, caller_key = org_and_key
    cb = "http://127.0.0.1:9999/cb"
    client_id = httpx.post(
        f"{base}/oauth/register", json={"redirect_uris": [cb], "client_name": "e2e"}, timeout=10
    ).json()["client_id"]

    verifier, challenge = _pkce()
    # Consent: paste the caller key, request only `read`.
    comp = httpx.post(
        f"{base}/oauth/authorize/complete",
        data={
            "credential": caller_key,
            "client_id": client_id,
            "redirect_uri": cb,
            "code_challenge": challenge,
            "state": "s",
            "scope": "read",
        },
        follow_redirects=False,
        timeout=10,
    )
    assert comp.status_code == 302
    code = httpx.URL(comp.headers["location"]).params["code"]

    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cb,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    tok = httpx.post(f"{base}/oauth/token", data=form, timeout=10)
    assert tok.status_code == 200, tok.text
    body = tok.json()
    assert body["scope"] == "read"
    access = body["access_token"]
    assert access.startswith("dsto_")

    # The minted token works over the wire.
    assert (
        httpx.get(
            f"{base}/v1/lenses", headers={"Authorization": f"Bearer {access}"}, timeout=10
        ).status_code
        == 200
    )

    # Scope enforced BEFORE the replay: a read token cannot file a correction (the
    # only write door), and the 403 names the scope it lacks. Ordered first because
    # the replay below detects reuse and REVOKES this token — so checking scope after
    # it would see a 401 (revoked), not the 403 we mean to prove.
    denied = httpx.post(
        f"{base}/v1/reviews",
        headers={"Authorization": f"Bearer {access}"},
        json={"request_id": "whatever", "correction": {"kind": "definition", "note": "x"}},
        timeout=10,
    )
    assert denied.status_code == 403
    assert 'scope="write"' in denied.headers.get("www-authenticate", "")

    # Single-use: replaying the code is refused, AND reuse detection withdraws the
    # token the first redemption minted (OAuth 2.1) — the token is now dead.
    replay = httpx.post(f"{base}/oauth/token", data=form, timeout=10)
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert (
        httpx.get(
            f"{base}/v1/lenses", headers={"Authorization": f"Bearer {access}"}, timeout=10
        ).status_code
        == 401
    ), "code reuse did not revoke the issued token"


def test_revocation_kills_the_key_over_the_wire(org_and_key: tuple[str, str, str]) -> None:
    base, _admin, caller_key = org_and_key
    # Works now.
    assert (
        httpx.get(
            f"{base}/v1/lenses", headers={"Authorization": f"Bearer {caller_key}"}, timeout=10
        ).status_code
        == 200
    )
    # Revoke via the CLI verb, against the same live DB.
    rc = subprocess.run(
        ["uv", "run", "dst", "revoke-token", caller_key],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={
            **os.environ,
            "DATABASE_ADMIN_URL": settings.database_admin_url,
            "DATABASE_URL": settings.database_url,
        },
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, rc.stderr
    # The next call over the wire is 401 — revocation is immediate, no caching window.
    assert (
        httpx.get(
            f"{base}/v1/lenses", headers={"Authorization": f"Bearer {caller_key}"}, timeout=10
        ).status_code
        == 401
    )
