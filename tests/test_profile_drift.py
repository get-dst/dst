"""Profile drift — previous-vs-current diffs, stalled tables,
refresh endpoint, and lens-scoped drift naming eval cases for re-baseline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.profile import ColumnProfile, PartitioningProfile, TableProfile
from services.db.session import org_session
from services.evals import store as eval_store
from services.lenses import profile_drift, profile_store
from services.lenses import store as lens_store
from services.lenses.demo import jaffle_customer_value_bundle

client = TestClient(app)


def _profile(**overrides: object) -> TableProfile:
    base: dict = {
        "connection": "jaffle",
        "table": "customers",
        "columns": [
            ColumnProfile(name="customer_id", type="INTEGER", top_values=None),
            ColumnProfile(name="status", type="VARCHAR", top_values=["active", "churned"]),
        ],
        "partitioning": None,
    }
    base.update(overrides)
    return TableProfile(**base)


def test_diff_names_every_drift_kind() -> None:
    prev = _profile()
    cur = _profile(
        columns=[
            ColumnProfile(name="customer_id", type="VARCHAR", top_values=None),  # retyped
            ColumnProfile(name="status", type="VARCHAR", top_values=["active", "churned", "trial"]),
            ColumnProfile(name="segment", type="VARCHAR"),  # added
        ],
        partitioning=PartitioningProfile(column="created_at", kind="time"),
    )
    kinds = {(d.kind, d.detail) for d in profile_drift.diff_profiles(prev, cur)}
    assert ("column_added", "segment") in kinds
    assert ("column_retyped", "customer_id: INTEGER -> VARCHAR") in kinds
    assert ("enum_value_added", "status: trial") in kinds
    assert any(k == "partition_changed" for k, _ in kinds)

    dropped = profile_drift.diff_profiles(cur, prev)
    assert any(d.kind == "column_dropped" and d.detail == "segment" for d in dropped)


def test_identical_profiles_have_no_drift() -> None:
    assert profile_drift.diff_profiles(_profile(), _profile()) == []


def test_stalled_table_is_flagged() -> None:
    stale = _profile(last_updated_logical=datetime.now(UTC) - timedelta(days=30))
    item = profile_drift._staleness(stale, stale_after_days=7)
    assert item is not None and item.kind == "stale_table" and "30 days ago" in item.detail
    fresh = _profile(last_updated_logical=datetime.now(UTC) - timedelta(days=1))
    assert profile_drift._staleness(fresh, stale_after_days=7) is None


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


@needs_db
def test_drift_and_rebaseline_flow() -> None:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Drift') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    h = {"Authorization": f"Bearer {raw}"}
    try:
        with org_session(org) as s:
            lens_store.create_lens(s, jaffle_customer_value_bundle())
            case_id = eval_store.create_case(
                s,
                "customer_value",
                question="how many customers?",
                expected_sql="SELECT count(*) FROM customers",
                source="authored",
                status="approved",
                created_by="t",
            )
            profile_store.upsert_profile(s, _profile())
            # second upsert with a changed column → previous kept, drift detectable
            profile_store.upsert_profile(
                s,
                _profile(
                    columns=[
                        ColumnProfile(name="customer_id", type="INTEGER"),
                        ColumnProfile(
                            name="status", type="VARCHAR", top_values=["active", "churned", "trial"]
                        ),
                    ]
                ),
            )

        conn_drift = client.get("/mgmt/connections/jaffle/profile/drift", headers=h)
        assert conn_drift.status_code == 200, conn_drift.text
        assert any(d["kind"] == "enum_value_added" for d in conn_drift.json())

        lens_drift = client.get("/mgmt/lenses/customer_value/profile-drift", headers=h)
        assert lens_drift.status_code == 200, lens_drift.text
        body = lens_drift.json()
        assert any(d["kind"] == "enum_value_added" for d in body["drift"])
        # the approved case is named for re-baseline — never silently re-baselined
        assert case_id in body["eval_cases_needing_rebaseline"]

        refreshed = client.post("/mgmt/connections/jaffle/profile/refresh", headers=h)
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["tables"] > 0
        assert refreshed.json()["sampled"] is False

        # sample=true runs the guarded sampler after the catalog pass
        sampled = client.post("/mgmt/connections/jaffle/profile/refresh?sample=true", headers=h)
        assert sampled.status_code == 200, sampled.text
        assert sampled.json()["sampled"] is True
        with org_session(org) as s:
            orders = next(
                p.profile
                for p in profile_store.list_profiles(s, "jaffle")
                if p.profile.table == "orders"
            )
        status_col = next(c for c in orders.columns if c.name == "status")
        assert status_col.top_values  # real enum literals from the warehouse
    finally:
        with admin.begin() as c:
            for table in ("eval_case", "join_candidate", "table_profile", "lens", "admin_token"):
                c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})  # noqa: S608
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
