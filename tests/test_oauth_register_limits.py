"""`POST /oauth/register` is the one endpoint that writes for a stranger.

RFC 7591 dynamic client registration has to be anonymous — the client is
registering precisely because it holds no credential yet — which made it an
unauthenticated, unthrottled, unbounded INSERT into a table nothing ever pruned.
Three things bound it now, and each is pinned here:

  * a per-source budget, so registrations cost something to produce;
  * size caps, so one accepted registration cannot store an arbitrary row;
  * retention, so registrations that never became clients expire — while a
    client that actually connected is kept, whatever the pressure on the table.

The flow itself must keep working: a client that registers and authorizes sees
exactly what it saw before, which the round-trip in test_mcp_auth.py covers.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api.oauth import _MAX_NAME_CHARS, _MAX_REDIRECT_URIS, _REGISTER_RPM
from services.app import app
from services.auth import oauth
from services.auth.tokens import hash_token, new_caller_key
from services.config import settings
from services.governance import ratelimit


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
client = TestClient(app)

_CB = "http://127.0.0.1:9999/cb"


@pytest.fixture(autouse=True)
def _clean_budget() -> Iterator[None]:
    """The limiter is process-global, so this module both starts and leaves clean."""
    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture
def caller_key() -> Iterator[str]:
    """An org + caller + live dst_ key — the credential that authorizes a client,
    and so the thing that marks a registration as a real client."""
    raw = new_caller_key()
    with _admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('RegLimits') RETURNING id")
        ).scalar_one()
        cid = c.execute(
            text(
                "INSERT INTO caller (org_id, name, type) "
                "VALUES (:o, 'svc@t', 'service') RETURNING id"
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
        yield raw
    finally:
        with _admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _register(**body: object) -> httpx.Response:
    return client.post("/oauth/register", json={"redirect_uris": [_CB], **body})


def _authorize(client_id: str, credential: str) -> httpx.Response:
    """Complete a consent against a real credential — what marks the client used."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return client.post(
        "/oauth/authorize/complete",
        data={
            "credential": credential,
            "client_id": client_id,
            "redirect_uri": _CB,
            "code_challenge": challenge,
            "state": "",
        },
        follow_redirects=False,
    )


@needs_db
def test_registration_is_throttled_per_source() -> None:
    codes = [_register().status_code for _ in range(_REGISTER_RPM + 2)]
    assert codes[:_REGISTER_RPM] == [201] * _REGISTER_RPM  # the real flow is untouched
    assert codes[_REGISTER_RPM:] == [429] * 2  # a spray is not free

    blocked = client.post("/oauth/register", json={"redirect_uris": [_CB]})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    # Still discoverable cross-origin: an MCP client must be able to READ the refusal.
    assert blocked.headers["Access-Control-Allow-Origin"] == "*"
    assert blocked.json()["error"] == "temporarily_unavailable"


@needs_db
def test_one_registration_cannot_store_an_arbitrary_row() -> None:
    """Metadata validation checks shapes, not sizes — the free write measured in
    bytes rather than rows."""
    many = client.post(
        "/oauth/register",
        json={"redirect_uris": [f"http://127.0.0.1:{9000 + i}/cb" for i in range(50)]},
    )
    assert many.status_code == 400 and many.json()["error"] == "invalid_redirect_uri"

    long_name = client.post(
        "/oauth/register",
        json={"redirect_uris": [_CB], "client_name": "n" * (_MAX_NAME_CHARS + 1)},
    )
    assert long_name.status_code == 400

    oversize = client.post(
        "/oauth/register",
        content=b'{"redirect_uris": ["http://127.0.0.1/cb"], "client_name": "'
        + b"n" * 40_000
        + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert oversize.status_code == 400

    # …and the ordinary registration still succeeds.
    ok = client.post(
        "/oauth/register",
        json={"redirect_uris": [_CB] * _MAX_REDIRECT_URIS, "client_name": "Claude"},
    )
    assert ok.status_code == 201 and ok.json()["client_id"].startswith("dstc_")


@needs_db
def test_retention_reaps_abandoned_registrations_and_keeps_real_clients(
    caller_key: str,
) -> None:
    """The distinction retention rests on: a registration that reached an
    authorization request is a client and is never reaped; one that never did is
    abandoned. Getting this backwards would silently break working integrations."""
    abandoned = _register(client_name="Never used").json()["client_id"]
    real = _register(client_name="Real client").json()["client_id"]

    # A completed authorization against a real credential is what marks it used.
    # The anonymous GET deliberately does NOT: otherwise anyone could immunise their
    # own spam registrations against the cap by visiting the consent page.
    anon = client.get(
        "/oauth/authorize",
        params={"client_id": abandoned, "redirect_uri": _CB, "code_challenge": "x" * 43},
        follow_redirects=False,
    )
    assert anon.status_code == 200
    assert _authorize(real, caller_key).status_code == 302

    # Age both rows past the TTL; only the abandoned one should go.
    with _admin.begin() as c:
        c.execute(
            text(
                "UPDATE oauth_client SET created_at = now() - :ttl - interval '1 day' "
                "WHERE client_id IN (:a, :r)"
            ),
            {"ttl": oauth._CLIENT_TTL, "a": abandoned, "r": real},
        )

    _register(client_name="Sweeps on registration")  # prune runs here

    assert oauth.client_redirect_uris(abandoned) is None  # expired
    assert oauth.client_redirect_uris(real) == [_CB]  # used, therefore kept


@needs_db
def test_unused_registrations_are_capped(caller_key: str) -> None:
    """The backstop for a burst inside the TTL. It evicts oldest-unused-first and
    never touches a used row, so registration spam cannot push out a live client."""
    keep = _register(client_name="Live client").json()["client_id"]
    assert _authorize(keep, caller_key).status_code == 302
    oldest = _register(client_name="Oldest unused").json()["client_id"]

    # Simulate a table already at the cap of never-used rows, all older than `oldest`
    # is about to be, without paying for thousands of HTTP registrations.
    with _admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO oauth_client (client_id, redirect_uris, created_at) "
                "SELECT 'dstc_filler_' || g, ARRAY[:cb], now() - interval '1 minute' "
                "FROM generate_series(1, :n) g"
            ),
            {"cb": _CB, "n": oauth._MAX_UNUSED_CLIENTS},
        )
        # `oldest` must be the oldest unused row, so it is the one evicted.
        c.execute(
            text(
                "UPDATE oauth_client SET created_at = now() - interval '1 hour' "
                "WHERE client_id = :o"
            ),
            {"o": oldest},
        )

    _register(client_name="Trips the cap")

    assert oauth.client_redirect_uris(oldest) is None  # over the cap, unused → evicted
    assert oauth.client_redirect_uris(keep) == [_CB]  # used → kept regardless

    with _admin.begin() as c:
        c.execute(text("DELETE FROM oauth_client WHERE client_id LIKE 'dstc_filler_%'"))
        unused = c.execute(
            text("SELECT count(*) FROM oauth_client WHERE last_used_at IS NULL")
        ).scalar_one()
    assert unused <= oauth._MAX_UNUSED_CLIENTS
