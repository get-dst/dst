"""/mgmt requires a valid admin token and derives the org."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings

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


def test_mgmt_missing_token() -> None:
    assert client.get("/mgmt/ping").status_code == 401


@needs_db
def test_mgmt_bad_token() -> None:
    assert client.get("/mgmt/ping", headers={"Authorization": "Bearer nope"}).status_code == 401


@needs_db
def test_mgmt_valid_token() -> None:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('AuthTest') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 'test')"),
            {"o": org, "h": hash_token(raw)},
        )
    try:
        r = client.get("/mgmt/ping", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200
        assert r.json()["org_id"] == str(org)
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
