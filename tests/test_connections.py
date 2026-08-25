"""Encrypted credential storage + connection management.

Covers Fernet round-trip, the encrypted-at-rest store, resolve_connector fallback,
and the admin-authed /mgmt/connections API (secrets never returned).
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses.connections import resolve_connector
from services.security import crypto

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


@pytest.fixture
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


def test_crypto_round_trip(_crypto_key: None) -> None:
    secret = '{"type":"service_account","private_key":"x"}'
    token = crypto.encrypt(secret)
    assert token != secret
    assert crypto.decrypt(token) == secret


def test_crypto_unconfigured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", None)
    crypto._fernet.cache_clear()
    with pytest.raises(crypto.CryptoNotConfigured):
        crypto.encrypt("x")
    crypto._fernet.cache_clear()


def _make_org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('ConnTest') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM connection WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_store_encrypts_at_rest(_crypto_key: None) -> None:
    org, _ = _make_org_token()
    secret = '{"type":"service_account","x":1}'
    try:
        with org_session(org) as s:
            connection_store.create_connection(s, "wh", "bigquery", {"project": "p"}, secret)
        # raw column must be ciphertext, not the plaintext secret
        admin = create_engine(settings.database_admin_url)
        with admin.connect() as c:
            stored = c.execute(
                text("SELECT secret_encrypted FROM connection WHERE org_id = :o"), {"o": org}
            ).scalar_one()
        assert stored != secret
        assert "service_account" not in stored
        with org_session(org) as s:
            assert connection_store.get_secret(s, "wh") == secret
            rec = connection_store.get_connection(s, "wh")
            assert rec is not None and rec.has_secret and rec.config["project"] == "p"
    finally:
        _cleanup(org)


@needs_db
def test_resolve_falls_back_to_builtin_jaffle() -> None:
    org, _ = _make_org_token()
    try:
        # no DB connection row named 'jaffle' -> built-in DuckDB connector
        conn = resolve_connector("jaffle", org)
        assert conn.kind == "duckdb"
    finally:
        _cleanup(org)


@needs_db
def test_api_create_list_delete_hides_secret(_crypto_key: None) -> None:
    org, raw = _make_org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        # duckdb (local jaffle) so the create-time read probe passes offline; a secret is
        # still attached to exercise the encrypted-at-rest / never-returned behaviour.
        r = client.post(
            "/mgmt/connections",
            headers=h,
            json={"name": "warehouse", "type": "duckdb", "secret": "dummy-cred"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["has_secret"] is True
        assert r.json()["access"] == ["read"]

        lst = client.get("/mgmt/connections", headers=h)
        assert lst.status_code == 200
        item = next(c for c in lst.json() if c["name"] == "warehouse")
        assert item["has_secret"] is True
        assert "secret" not in item and "secret_encrypted" not in item
        assert item["config"]["access"] == ["read"]

        dup = client.post(
            "/mgmt/connections",
            headers=h,
            json={"name": "warehouse", "type": "duckdb"},
        )
        assert dup.status_code == 409

        d = client.delete("/mgmt/connections/warehouse", headers=h)
        assert d.status_code == 204
    finally:
        _cleanup(org)


@needs_db
def test_api_dependents_lists_lenses_using_connection(_crypto_key: None) -> None:
    org, raw = _make_org_token()
    h = {"Authorization": f"Bearer {raw}"}
    admin = create_engine(settings.database_admin_url)
    try:
        body = {"name": "warehouse", "type": "duckdb"}
        r = client.post("/mgmt/connections", headers=h, json=body)
        assert r.status_code == 201, r.text

        # Two lenses depend on the connection — one declares it in config.connections, the
        # other sources a published entity from it. A third, unrelated lens must NOT appear.
        rows = [
            ("by_config", '{"config": {"connections": ["warehouse"]}}', "{}"),
            (
                "by_source",
                "{}",
                '{"semantic_model": {"entities": [{"source": {"connection": "warehouse"}}]}}',
            ),
            ("unrelated", '{"config": {"connections": ["elsewhere"]}}', "{}"),
        ]
        with admin.begin() as c:
            for name, draft, published in rows:
                c.execute(
                    text(
                        "INSERT INTO lens (org_id, name, display_name, description, "
                        "status, draft_json, published_json) "
                        "VALUES (:o, :n, :n, '', 'draft', CAST(:d AS jsonb), CAST(:p AS jsonb))"
                    ),
                    {"o": org, "n": name, "d": draft, "p": published},
                )

        resp = client.get("/mgmt/connections/warehouse/dependents", headers=h)
        assert resp.status_code == 200, resp.text
        assert {ln["name"] for ln in resp.json()["lenses"]} == {"by_config", "by_source"}

        # unknown connection -> 404, never an empty success
        assert client.get("/mgmt/connections/missing/dependents", headers=h).status_code == 404
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        _cleanup(org)


@needs_db
def test_api_rejects_bad_bigquery_secret(_crypto_key: None) -> None:
    org, raw = _make_org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        r = client.post(
            "/mgmt/connections",
            headers=h,
            json={"name": "bad", "type": "bigquery", "secret": "not-json"},
        )
        assert r.status_code == 400
    finally:
        _cleanup(org)


@needs_db
def test_api_create_blocked_when_credential_cannot_connect(_crypto_key: None) -> None:
    # Valid-shaped service-account JSON but a bogus key — the connector cannot be built,
    # so the create-time evaluation blocks creation (offline; no BigQuery call).
    org, raw = _make_org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        r = client.post(
            "/mgmt/connections",
            headers=h,
            json={
                "name": "unreachable",
                "type": "bigquery",
                "config": {"project": "p", "dataset": "p.d"},
                "secret": '{"type":"service_account","project_id":"p","private_key":"nope"}',
            },
        )
        assert r.status_code == 400, r.text
        # nothing persisted
        lst = client.get("/mgmt/connections", headers=h).json()
        assert all(c["name"] != "unreachable" for c in lst)
    finally:
        _cleanup(org)
