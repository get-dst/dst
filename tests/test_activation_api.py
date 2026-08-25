"""The activation spine derives the next funnel step from state that
already exists — connections, audit runs, lenses — with no new storage. Each test builds
one org at a given point in connect → audit → lens and asserts the derived ``step``."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import audit_store
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses import store as lens_store
from services.lenses.demo import jaffle_customer_value_bundle
from services.probe.drift import DriftFinding, DriftVariant

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
        org = c.execute(text("INSERT INTO org (name) VALUES ('ActT') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM audit_run WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM connection WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _activation(raw: str) -> dict[str, object]:
    r = client.get("/mgmt/activation", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text
    return r.json()


def _connect(org: object, name: str = "wh") -> None:
    with org_session(org) as session:
        connection_store.create_connection(session, name, "duckdb", {"access": ["read"]}, None)


_CONFLICT = DriftFinding(
    metric_intent="monthly revenue",
    severity="conflict",
    blast_radius=47,
    variants=[
        DriftVariant(
            statement="SELECT SUM(a)", source_tables=["t"], run_count=25, distinguishing="net"
        ),
        DriftVariant(
            statement="SELECT SUM(b)", source_tables=["t"], run_count=22, distinguishing="gross"
        ),
    ],
)


@needs_db
def test_activation_empty_with_no_connections() -> None:
    org, raw = _org_token()
    try:
        body = _activation(raw)
        assert body["step"] == "empty"
        assert body["connections"] == 0
        assert body["focus_connection"] is None
    finally:
        _cleanup(org)


@needs_db
def test_activation_connected_when_audit_found_nothing() -> None:
    org, raw = _org_token()
    try:
        _connect(org)  # connected, no audit yet (and none mid-flight)
        body = _activation(raw)
        assert body["step"] == "connected"
        assert body["connections"] == 1
        assert body["focus_connection"] == "wh"
        assert body["findings"] == 0
    finally:
        _cleanup(org)


@needs_db
def test_activation_auditing_while_the_audit_is_in_flight() -> None:
    org, raw = _org_token()
    try:
        _connect(org)
        with org_session(org) as session:
            audit_store.record_running(session, "wh", 30)
        body = _activation(raw)
        assert body["step"] == "auditing"
        assert body["auditing"] is True
    finally:
        _cleanup(org)


@needs_db
def test_activation_findings_ready_when_the_audit_found_contradictions() -> None:
    org, raw = _org_token()
    try:
        _connect(org)
        with org_session(org) as session:
            audit_store.record_run(session, "wh", 30, 100, [_CONFLICT])
        body = _activation(raw)
        assert body["step"] == "findings_ready"
        assert body["findings"] == 1
        assert body["focus_connection"] == "wh"
    finally:
        _cleanup(org)


@needs_db
def test_activation_drafting_then_activated_with_lenses() -> None:
    org, raw = _org_token()
    try:
        _connect(org)
        with org_session(org) as session:
            lens_store.create_lens(session, jaffle_customer_value_bundle())
        # a draft lens exists but none is published yet
        assert _activation(raw)["step"] == "drafting"

        with org_session(org) as session:
            lens_store.publish(session, "customer_value")
        activated = _activation(raw)
        assert activated["step"] == "activated"
        assert activated["published_lenses"] == 1
    finally:
        _cleanup(org)
