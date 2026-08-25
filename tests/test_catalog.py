"""Wizard catalog endpoints: available connections + connection tables — served
from the stored table profile when fresh, introspected live otherwise."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import mgmt_catalog
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.profile import TableProfile
from services.db.session import org_session
from services.lenses import profile_store

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


def test_available_requires_auth() -> None:
    assert client.get("/mgmt/connections/available").status_code == 401


def _org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('CatT') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM table_profile WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_available_includes_builtin_jaffle() -> None:
    org, raw = _org_token()
    try:
        r = client.get("/mgmt/connections/available", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200
        names = {c["name"] for c in r.json()}
        assert "jaffle" in names
        jaffle = next(c for c in r.json() if c["name"] == "jaffle")
        assert jaffle["type"] == "duckdb" and jaffle["builtin"] is True
    finally:
        _cleanup(org)


@needs_db
def test_jaffle_tables_listed() -> None:
    org, raw = _org_token()
    try:
        r = client.get(
            "/mgmt/connections/jaffle/tables", headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.status_code == 200, r.text
        tables = {t["name"] for t in r.json()["tables"]}
        assert "customers" in tables and "orders" in tables
        cust = next(t for t in r.json()["tables"] if t["name"] == "customers")
        assert cust["columns"] > 0
    finally:
        _cleanup(org)


@needs_db
def test_tables_persisted_then_served_from_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """First call introspects live and persists profiles; while those are fresh,
    the next call serves the store and never touches the connector — same shape."""
    org, raw = _org_token()
    try:
        h = {"Authorization": f"Bearer {raw}"}
        first = client.get("/mgmt/connections/jaffle/tables", headers=h)
        assert first.status_code == 200, first.text

        with org_session(org) as s:
            stored = profile_store.list_profiles(s, "jaffle")
        assert {p.profile.table for p in stored} >= {"customers", "orders"}

        def _boom(name: str, org_id: object) -> object:
            raise AssertionError("introspected live despite a fresh stored profile")

        monkeypatch.setattr(mgmt_catalog, "resolve_connector", _boom)
        second = client.get("/mgmt/connections/jaffle/tables", headers=h)
        assert second.status_code == 200, second.text
        assert second.json() == first.json()  # backward-compatible, byte-for-byte
        cust = next(t for t in second.json()["tables"] if t["name"] == "customers")
        assert cust["rows"] == 100 and cust["columns"] > 0
    finally:
        _cleanup(org)


@needs_db
def test_tables_reintrospects_when_profile_stale() -> None:
    """A past-TTL profile triggers a live refresh: profiles are re-persisted fresh
    and tables that disappeared from the warehouse are pruned from the store."""
    org, raw = _org_token()
    try:
        stale_at = datetime.now(UTC) - timedelta(hours=48)
        with org_session(org) as s:
            profile_store.upsert_profile(
                s,
                TableProfile(connection="jaffle", table="dropped_table", profiled_at=stale_at),
            )

        r = client.get(
            "/mgmt/connections/jaffle/tables", headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.status_code == 200, r.text
        names = {t["name"] for t in r.json()["tables"]}
        assert "customers" in names and "dropped_table" not in names

        with org_session(org) as s:
            assert profile_store.get_profile(s, "jaffle", "dropped_table") is None  # pruned
            refreshed = profile_store.get_profile(s, "jaffle", "customers")
            assert refreshed is not None
            assert not profile_store.is_stale(refreshed.profiled_at)
    finally:
        _cleanup(org)
