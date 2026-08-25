"""Request-log drill-down + context chunks + lens drift endpoints."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.context.ingest import ingest_text
from services.contracts.fakes import HashEmbedder
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


def _org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('DrillT') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _seed_request(org: object, rid: str) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, sql, valid, "
                "row_count, answer, definition_used, confidence, verification, certification, "
                "latency, ai_cost_usd, wh_cost_usd, status) "
                "VALUES (:o, :r, 'customer_value', 'a', 'q?', 'SELECT 1', "
                "true, 3, 'ans', 'repeat_customer', 'verified', CAST(:ver AS jsonb), 'none', "
                "CAST(:lat AS jsonb), 0.01, 0.0, 'ok')"
            ),
            {
                "o": org,
                "r": rid,
                "lat": '{"total": 12}',
                "ver": '{"grade": "verified", "checks": '
                '[{"name": "numeric_grounding", "status": "pass", "reason": null}]}',
            },
        )


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM context_chunk WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM request_log WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_request_list_and_detail() -> None:
    org, raw = _org_token()
    rid = f"req-{uuid.uuid4()}"
    _seed_request(org, rid)
    h = {"Authorization": f"Bearer {raw}"}
    try:
        lst = client.get("/mgmt/observe/requests", headers=h)
        assert lst.status_code == 200
        assert any(r["request_id"] == rid for r in lst.json())

        detail = client.get(f"/mgmt/observe/requests/{rid}", headers=h)
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["sql"] == "SELECT 1"
        assert body["definition_used"] == "repeat_customer"
        assert body["latency"] == {"total": 12}
        # The verification breakdown is part of the browsable trace
        assert body["verification"]["grade"] == "verified"
        assert body["verification"]["checks"][0]["name"] == "numeric_grounding"
        assert body["certification"] == "none"

        assert client.get("/mgmt/observe/requests/nope", headers=h).status_code == 404
    finally:
        _cleanup(org)


@needs_db
def test_context_chunks_endpoint() -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        with org_session(org) as s:
            store.create_lens(s, jaffle_customer_value_bundle())
            ingest_text(
                s, "customer_value", "note:x", "Churn is 90 days inactive. " * 30, HashEmbedder()
            )
        r = client.get("/mgmt/lenses/customer_value/context/chunks?source=note:x", headers=h)
        assert r.status_code == 200, r.text
        chunks = r.json()["chunks"]
        assert chunks and all(c["source"] == "note:x" for c in chunks)
        assert "Churn" in chunks[0]["text"]
    finally:
        _cleanup(org)


@needs_db
def test_lens_drift_endpoint() -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        with org_session(org) as s:
            store.create_lens(s, jaffle_customer_value_bundle())
        client.put(
            "/mgmt/standards/repeat_customer",
            headers=h,
            json={"term": "repeat_customer", "body": "a different meaning"},
        )
        r = client.get("/mgmt/lenses/customer_value/drift", headers=h)
        assert r.status_code == 200
        assert any(c["term"] == "repeat_customer" for c in r.json()["conflicts"])
    finally:
        with create_engine(settings.database_admin_url).begin() as c:
            c.execute(text("DELETE FROM semantic_asset WHERE org_id = :o"), {"o": org})
        _cleanup(org)
