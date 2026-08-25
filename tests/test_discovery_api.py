"""Caller-scoped lens discovery (GET /v1/lenses, GET /v1/lenses/{name}).

These back the MCP server's list_lenses / describe_lens tools.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.db.session import org_session
from services.lenses import store
from services.lenses.demo import jaffle_customer_value_bundle

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


def _make_org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('DiscTest') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _seed_published(org: object) -> None:
    with org_session(org) as session:
        if not store.lens_exists(session, "customer_value"):
            store.create_lens(session, jaffle_customer_value_bundle())
            store.publish(session, "customer_value")


def test_list_requires_auth() -> None:
    assert client.get("/v1/lenses").status_code == 401


def test_describe_requires_auth() -> None:
    assert client.get("/v1/lenses/customer_value").status_code == 401


@needs_db
def test_list_returns_only_published() -> None:
    org, raw = _make_org_token()
    _seed_published(org)
    # an unpublished draft should NOT appear
    with org_session(org) as session:
        bundle = jaffle_customer_value_bundle()
        bundle.config.name = "draft_only"
        bundle.config.display_name = "Draft Only"
        store.create_lens(session, bundle)
    try:
        r = client.get("/v1/lenses", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200, r.text
        names = {item["name"] for item in r.json()}
        assert "customer_value" in names
        assert "draft_only" not in names
    finally:
        _cleanup(org)


@needs_db
def test_describe_exposes_fields_and_definitions() -> None:
    org, raw = _make_org_token()
    _seed_published(org)
    try:
        r = client.get("/v1/lenses/customer_value", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["name"] == "customer_value"
        assert body["dialect"] in {"duckdb", "bigquery"}
        assert body["entities"], "expected at least one entity"
        assert all("fields" in e for e in body["entities"])
        # the demo lens defines repeat_customer
        terms = {d["term"] for d in body["definitions"]}
        assert "repeat_customer" in terms
    finally:
        _cleanup(org)


@needs_db
def test_describe_unknown_lens_404() -> None:
    org, raw = _make_org_token()
    try:
        r = client.get("/v1/lenses/nope", headers={"Authorization": f"Bearer {raw}"})
        assert r.status_code == 404
    finally:
        _cleanup(org)
