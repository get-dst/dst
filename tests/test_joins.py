"""Join-key inference — hypotheses validated by a push-down
overlap probe, candidates approved into the lens's semantic model."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.profile import JoinCandidate, profiles_from_snapshot
from services.db.session import org_session
from services.lenses import joins, profile_store
from services.lenses import store as lens_store
from services.lenses.demo import jaffle_customer_value_bundle

client = TestClient(app)


def _conn() -> DuckDBConnector:
    return DuckDBConnector(settings.duckdb_jaffle_path)


def _jaffle_profiles() -> list:
    return profiles_from_snapshot(_conn().introspect(), connection="jaffle")


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


def test_hypothesize_finds_customer_orders_pair() -> None:
    hyps = joins.hypothesize(_jaffle_profiles())
    assert ("orders", "customer_id", "customers", "customer_id") in hyps
    # no hypothesis invents columns that don't exist
    profiles = {p.table: {c.name for c in p.columns} for p in _jaffle_profiles()}
    for left_table, left_col, right_table, right_col in hyps:
        assert left_col in profiles[left_table]
        assert right_col in profiles[right_table]


def test_overlap_probe_returns_only_a_ratio() -> None:
    ratio = joins.overlap_ratio(_conn(), "orders", "customer_id", "customers", "customer_id")
    assert ratio is not None and ratio >= 0.99  # every order's customer exists


def test_low_overlap_hypothesis_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(joins, "overlap_ratio", lambda *a, **k: 0.30)

    class _Session:  # never reached for writes: nothing confirms
        pass

    recorded: list = []
    monkeypatch.setattr(
        joins.profile_store,
        "list_profiles",
        lambda s, c: [type("S", (), {"profile": p})() for p in _jaffle_profiles()],
    )
    monkeypatch.setattr(joins.profile_store, "list_candidates", lambda s, c: [])
    monkeypatch.setattr(
        joins.profile_store, "record_candidates", lambda s, c, cands: recorded.extend(cands)
    )
    out = joins.infer_join_candidates(_Session(), _conn(), "jaffle")  # type: ignore[arg-type]
    assert out == [] and recorded == []


def test_approve_into_model_maps_tables_to_entities() -> None:
    bundle = jaffle_customer_value_bundle()
    entity_tables = {e.source.table for e in bundle.semantic_model.entities}
    assert "customers" in entity_tables  # the demo lens scopes the customers table
    candidate = JoinCandidate(
        left_table="orders",
        left_columns=["customer_id"],
        right_table="customers",
        right_columns=["customer_id"],
        evidence="name+overlap",
        overlap_ratio=1.0,
    )
    if "orders" in entity_tables:
        before = len(bundle.semantic_model.joins)
        assert joins.approve_into_model(bundle, candidate) is True
        assert len(bundle.semantic_model.joins) == before + 1
        # idempotent on a second approval
        assert joins.approve_into_model(bundle, candidate) is True
        assert len(bundle.semantic_model.joins) == before + 1
    else:
        assert joins.approve_into_model(bundle, candidate) is False


@needs_db
def test_join_candidate_api_flow() -> None:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Joins') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    h = {"Authorization": f"Bearer {raw}"}
    try:
        with org_session(org) as s:
            lens_store.create_lens(s, jaffle_customer_value_bundle())
            for p in _jaffle_profiles():
                profile_store.upsert_profile(s, p)
        # infer over the built-in jaffle connection: the lens scopes only `customers`,
        # so the orders→customers candidate is out of scope and the call still 201s.
        r = client.post("/mgmt/lenses/customer_value/join-candidates/infer", headers=h)
        assert r.status_code == 201, r.text

        # record a candidate by hand and approve it through the API
        with org_session(org) as s:
            profile_store.record_candidates(
                s,
                "jaffle",
                [
                    JoinCandidate(
                        left_table="customers",
                        left_columns=["customer_id"],
                        right_table="customers",
                        right_columns=["customer_id"],
                        evidence="fk",
                    )
                ],
            )
            cands = profile_store.list_candidates(s, "jaffle")
        cid = next(c.id for c in cands if c.evidence == "fk")
        listed = client.get("/mgmt/lenses/customer_value/join-candidates", headers=h)
        assert listed.status_code == 200
        assert any(c["id"] == cid for c in listed.json())

        approved = client.post(
            f"/mgmt/lenses/customer_value/join-candidates/{cid}/approve", headers=h
        )
        assert approved.status_code == 200, approved.text
        with org_session(org) as s:
            after = profile_store.list_candidates(s, "jaffle", status="approved")
        assert any(c.id == cid for c in after)

        missing = client.post(
            "/mgmt/lenses/customer_value/join-candidates/00000000-0000-0000-0000-000000000000/approve",
            headers=h,
        )
        assert missing.status_code == 404
    finally:
        with admin.begin() as c:
            for table in ("join_candidate", "table_profile", "lens", "admin_token"):
                c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})  # noqa: S608
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
