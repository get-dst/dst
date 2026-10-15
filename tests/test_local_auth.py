"""OSS pivot E3: local (Clerk-free) dashboard auth — passwords, sessions, dispatch."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth import local
from services.auth.deps import resolve_mcp_caller
from services.auth.tokens import SESSION_PREFIX, hash_token
from services.config import settings
from services.db.session import org_session

client = TestClient(app)


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


def test_password_hash_roundtrip() -> None:
    stored = local.hash_password("hunter2")
    assert stored.startswith("scrypt$")
    assert local.verify_password("hunter2", stored)
    assert not local.verify_password("hunter3", stored)
    assert not local.verify_password("hunter2", "not-a-hash")
    # per-user salt: same password, different hash
    assert local.hash_password("hunter2") != stored


@pytest.fixture
def org(request: pytest.FixtureRequest) -> Iterator[uuid.UUID]:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        org_id = c.execute(
            text("INSERT INTO org (name) VALUES ('LocalAuth') RETURNING id")
        ).scalar_one()
    yield uuid.UUID(str(org_id))
    with admin.begin() as c:
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org_id})  # cascades
    admin.dispose()


@needs_db
def test_login_session_lifecycle(org: uuid.UUID) -> None:
    with org_session(org) as session:
        local.create_user(session, "anna@example.com", "correct horse", role="admin")
        session.commit()

    assert local.login("anna@example.com", "wrong") is None
    assert local.login("nobody@example.com", "correct horse") is None

    raw = local.login("anna@example.com", "correct horse")
    assert raw is not None and raw.startswith(SESSION_PREFIX)

    ident = local.resolve(raw)
    assert ident is not None
    assert ident.org_id == org
    assert ident.user == "anna@example.com"
    assert ident.is_admin is True
    assert ident.groups == ["local:admin"]
    assert local.session_expiry(raw) is not None

    local.logout(raw)
    assert local.resolve(raw) is None


@needs_db
def test_login_is_rate_limited_per_email() -> None:
    """No lockout otherwise: online password guessing against a known email is unbounded
    (and each valid-email attempt costs a 32MiB scrypt). The limiter trips before the
    password check, so it caps guessing regardless of whether the email exists."""
    from services.api.auth_local import _LOGIN_RPM
    from services.governance import ratelimit

    ratelimit.reset()
    email = "brute@example.com"
    codes = [
        client.post("/auth/login", json={"email": email, "password": "x"}).status_code
        for _ in range(_LOGIN_RPM + 2)
    ]
    assert codes[:_LOGIN_RPM] == [401] * _LOGIN_RPM  # within budget: normal auth failure
    assert codes[_LOGIN_RPM:] == [429] * 2  # over budget: throttled
    blocked = client.post("/auth/login", json={"email": email, "password": "x"})
    assert blocked.status_code == 429 and "Retry-After" in blocked.headers
    ratelimit.reset()


@needs_db
def test_login_is_rate_limited_per_source_address() -> None:
    """The per-email budget is blind to a spray: one guess each against a thousand
    addresses never trips it, and since login is constant-work every one of those
    guesses now costs a full KDF. The per-address budget is what caps that."""
    from services.api.auth_local import _LOGIN_IP_RPM
    from services.governance import ratelimit

    ratelimit.reset()
    codes = [
        client.post(
            "/auth/login", json={"email": f"spray{i}@example.com", "password": "x"}
        ).status_code
        for i in range(_LOGIN_IP_RPM + 2)
    ]
    # Every email is distinct, so the per-email cap never fires — only the address one.
    assert codes[:_LOGIN_IP_RPM] == [401] * _LOGIN_IP_RPM
    assert codes[_LOGIN_IP_RPM:] == [429] * 2
    ratelimit.reset()


@needs_db
def test_login_work_does_not_reveal_whether_the_email_exists(org: uuid.UUID) -> None:
    """The account-enumeration oracle.

    scrypt is deliberately expensive, so running it only when a row matched made the
    response time answer "does this account exist?" — a known address cost the full
    KDF, an unknown one returned in about a fiftieth of that. Anyone could sort an
    address list into customers and strangers from across the network, unauthenticated.

    Pinned two ways. The KDF call count is the exact invariant and cannot flake; the
    timing bound is loose enough to survive a loaded machine while still catching the
    real regression, which was a twentyfold gap.
    """
    import statistics
    import time
    from unittest.mock import patch

    with org_session(org) as session:
        local.create_user(session, "known@example.com", "correct horse", role="admin")
        session.commit()

    calls: list[str] = []
    real_scrypt = local._scrypt

    def counting_scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
        calls.append(password)
        return real_scrypt(password, salt, n, r, p)

    local._absent_user_hash.cache_clear()
    local._absent_user_hash()  # build the decoy first, so it isn't counted below

    with patch.object(local, "_scrypt", counting_scrypt):
        assert local.login("known@example.com", "wrong") is None
        known_calls = len(calls)
        calls.clear()
        assert local.login("no-such-address@example.com", "wrong") is None
        absent_calls = len(calls)
    assert known_calls == 1  # the stored hash for the one candidate
    assert absent_calls == known_calls  # …and exactly one decoy when there is none

    def median_ms(email: str) -> float:
        samples = []
        for _ in range(7):
            start = time.perf_counter()
            local.login(email, "wrong")
            samples.append((time.perf_counter() - start) * 1000)
        return statistics.median(samples)

    known_ms = median_ms("known@example.com")
    absent_ms = median_ms("no-such-address@example.com")
    # Before the fix this ratio was ~0.05. Anything above a quarter means the two
    # paths are doing the same work and the difference is scheduling noise.
    assert absent_ms > known_ms * 0.25, f"timing oracle: {known_ms:.1f}ms vs {absent_ms:.1f}ms"


@needs_db
def test_expired_session_rejected(org: uuid.UUID) -> None:
    with org_session(org) as session:
        local.create_user(session, "bo@example.com", "pw", role="member")
        session.commit()
    raw = local.login("bo@example.com", "pw")
    assert raw is not None
    ident = local.resolve(raw)
    assert ident is not None and ident.is_admin is False

    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "UPDATE local_session SET expires_at = now() - interval '1s' WHERE token_hash = :h"
            ),
            {"h": hash_token(raw)},
        )
    admin.dispose()
    assert local.resolve(raw) is None


# --- dstsess_ dispatch through the auth deps -----------------------------------


@needs_db
def test_session_dispatch_admin_and_member(org: uuid.UUID) -> None:
    with org_session(org) as session:
        local.create_user(session, "root@example.com", "pw-admin", role="admin")
        local.create_user(session, "viewer@example.com", "pw-member", role="member")
        session.commit()

    admin_raw = local.login("root@example.com", "pw-admin")
    member_raw = local.login("viewer@example.com", "pw-member")
    assert admin_raw and member_raw

    # control plane: admin session works, member session is 403, garbage is 401
    r = client.get("/mgmt/ping", headers={"Authorization": f"Bearer {admin_raw}"})
    assert r.status_code == 200 and r.json()["org_id"] == str(org)
    assert (
        client.get("/mgmt/ping", headers={"Authorization": f"Bearer {member_raw}"}).status_code
        == 403
    )
    assert (
        client.get("/mgmt/ping", headers={"Authorization": "Bearer dstsess_nope"}).status_code
        == 401
    )

    # data plane / MCP: both roles resolve to CallerIdentity
    ident = resolve_mcp_caller(member_raw)
    assert ident is not None
    assert ident.org_id == org
    assert ident.name == "viewer@example.com"
    assert ident.is_admin is False
    assert ident.groups == ["local:member"]
    assert resolve_mcp_caller("dstsess_nope") is None


# --- /auth endpoints, cookie flow, /mgmt/users -------------------------------


@needs_db
def test_login_endpoint_cookie_and_bearer(org: uuid.UUID) -> None:
    with org_session(org) as session:
        local.create_user(session, "eva@example.com", "open sesame", role="admin")
        session.commit()

    c = TestClient(app)  # own cookie jar — don't pollute the module client
    assert (
        c.post("/auth/login", json={"email": "eva@example.com", "password": "no"}).status_code
        == 401
    )

    r = c.post("/auth/login", json={"email": "eva@example.com", "password": "open sesame"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith(SESSION_PREFIX)
    assert body["role"] == "admin" and body["org_id"] == str(org)
    assert body["expires_at"] is not None
    assert c.cookies.get("dst_session") == body["token"]

    # cookie alone signs the SPA in: /auth/me and the control plane both work
    assert c.get("/auth/me").json()["email"] == "eva@example.com"
    assert c.get("/mgmt/ping").json()["org_id"] == str(org)
    # an Authorization header always wins over the cookie
    assert c.get("/mgmt/ping", headers={"Authorization": "Bearer dstadm_bad"}).status_code == 401
    # the bearer token works without any cookie
    fresh = TestClient(app)
    assert (
        fresh.get("/auth/me", headers={"Authorization": f"Bearer {body['token']}"}).status_code
        == 200
    )

    assert c.post("/auth/logout").status_code == 204
    assert c.cookies.get("dst_session") is None
    assert c.get("/auth/me").status_code == 401  # session revoked server-side
    assert (
        fresh.get("/auth/me", headers={"Authorization": f"Bearer {body['token']}"}).status_code
        == 401
    )


@needs_db
def test_mgmt_users_admin_only(org: uuid.UUID) -> None:
    with org_session(org) as session:
        local.create_user(session, "boss@example.com", "pw", role="admin")
        session.commit()
    admin_raw = local.login("boss@example.com", "pw")
    ah = {"Authorization": f"Bearer {admin_raw}"}

    r = client.post("/mgmt/users", json={"email": "new@example.com", "password": "pw2"}, headers=ah)
    assert r.status_code == 201 and r.json()["role"] == "member"
    assert (
        client.post(
            "/mgmt/users", json={"email": "new@example.com", "password": "x"}, headers=ah
        ).status_code
        == 409
    )
    emails = [u["email"] for u in client.get("/mgmt/users", headers=ah).json()]
    assert emails == ["boss@example.com", "new@example.com"]

    # the freshly created member can log in but not manage users
    member_raw = local.login("new@example.com", "pw2")
    assert member_raw is not None
    assert (
        client.post(
            "/mgmt/users",
            json={"email": "x@example.com", "password": "x"},
            headers={"Authorization": f"Bearer {member_raw}"},
        ).status_code
        == 403
    )


@needs_db
def test_bootstrap_creates_first_admin() -> None:
    import argparse

    from services.cli.main import _bootstrap

    ns = argparse.Namespace(org="BootstrapLocal", email="own@example.com", password="pw")
    admin = create_engine(settings.database_admin_url)
    try:
        assert _bootstrap(ns) == 0
        raw = local.login("own@example.com", "pw")
        assert raw is not None
        ident = local.resolve(raw)
        assert ident is not None and ident.is_admin is True
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE name = 'BootstrapLocal'"))  # cascades
        admin.dispose()


@needs_db
def test_bootstrap_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rerun of bootstrap changes nothing: same org, the token .env already holds
    is kept (so .env is byte-identical), an existing user keeps its password.
    `--rotate-admin-token` is the only way to a second token, `--reset-password`
    the only way to a new password, and a different `--org` against the token in
    .env is refused rather than silently switching the project."""
    import argparse

    from services.cli.main import _bootstrap

    def ns(**kw: object) -> argparse.Namespace:
        base: dict[str, object] = {
            "org": "BootstrapTwice",
            "email": None,
            "password": None,
            "rotate_admin_token": False,
            "reset_password": False,
        }
        return argparse.Namespace(**{**base, **kw})

    monkeypatch.chdir(tmp_path)
    envfile = tmp_path / ".env"
    envfile.write_text("DST_URL=http://localhost:8000\n", encoding="utf-8")
    admin = create_engine(settings.database_admin_url)

    def token_count() -> int:
        with admin.connect() as c:
            org_id = c.execute(
                text("SELECT id FROM org WHERE name = 'BootstrapTwice'")
            ).scalar_one()  # exactly one org — scalar_one raises on duplicates
            return int(
                c.execute(
                    text(
                        "SELECT count(*) FROM admin_token WHERE org_id = :o AND label = 'bootstrap'"
                    ),
                    {"o": org_id},
                ).scalar_one()
            )

    def password_hash() -> str:
        with admin.connect() as c:
            return str(
                c.execute(
                    text("SELECT password_hash FROM local_user WHERE email = 'two@example.com'")
                ).scalar_one()
            )

    try:
        assert _bootstrap(ns(email="two@example.com", password="pw1")) == 0
        assert "created" in capsys.readouterr().out
        first_env = envfile.read_bytes()
        assert b"DST_ADMIN_TOKEN=dstadm_" in first_env
        assert token_count() == 1
        first_hash = password_hash()

        # Bare rerun: .env byte-identical, still one token.
        assert _bootstrap(ns()) == 0
        out = capsys.readouterr().out
        assert "reused" in out and "kept the one in .env" in out
        assert envfile.read_bytes() == first_env
        assert token_count() == 1

        # Rerun with --email on the existing user and no --reset-password: untouched.
        assert _bootstrap(ns(email="two@example.com", password="pw2")) == 0
        assert "left untouched" in capsys.readouterr().out
        assert password_hash() == first_hash
        assert local.login("two@example.com", "pw1") is not None

        # --reset-password is the explicit way to a new password.
        assert _bootstrap(ns(email="two@example.com", password="pw2", reset_password=True)) == 0
        assert password_hash() != first_hash
        assert local.login("two@example.com", "pw1") is None
        raw = local.login("two@example.com", "pw2")
        assert raw is not None
        ident = local.resolve(raw)
        assert ident is not None and ident.is_admin is True

        # A different --org against the token in .env is refused, naming both.
        assert _bootstrap(ns(org="BootstrapElsewhere")) == 1
        err = capsys.readouterr().err
        assert "BootstrapTwice" in err and "BootstrapElsewhere" in err
        assert envfile.read_bytes() == first_env
        with admin.connect() as c:
            assert (
                c.execute(
                    text("SELECT count(*) FROM org WHERE name = 'BootstrapElsewhere'")
                ).scalar_one()
                == 0
            )

        # --rotate-admin-token mints a second token and rewrites the line.
        assert _bootstrap(ns(rotate_admin_token=True)) == 0
        assert "rotated" in capsys.readouterr().out
        assert envfile.read_bytes() != first_env
        assert b"DST_ADMIN_TOKEN=dstadm_" in envfile.read_bytes()
        assert token_count() == 2
    finally:
        with admin.begin() as c:
            c.execute(
                text("DELETE FROM org WHERE name IN ('BootstrapTwice', 'BootstrapElsewhere')")
            )  # cascades
        admin.dispose()
