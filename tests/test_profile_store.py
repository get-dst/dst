"""Table-profile store tests: upsert keeps the previous version for the
drift diff, staleness helper honours its TTL argument, join-candidate CRUD never
resets a status on re-discovery, and RLS isolates orgs (mirrors test_certify.py)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.contracts.profile import (
    ColumnProfile,
    JoinCandidate,
    PartitioningProfile,
    TableProfile,
)
from services.db.session import org_session
from services.lenses import profile_store


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


def _org() -> object:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        # org.name is UNIQUE (0047) — each org this helper mints needs its own.
        return c.execute(
            text("INSERT INTO org (name) VALUES (:n) RETURNING id"),
            {"n": f"ProfileStoreTest-{uuid.uuid4().hex[:8]}"},
        ).scalar_one()


def _cleanup(*orgs: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        for org in orgs:
            c.execute(text("DELETE FROM join_candidate WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM table_profile WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _profile(
    connection: str = "jaffle", table: str = "orders", row_count: int = 99
) -> TableProfile:
    return TableProfile(
        connection=connection,
        table=table,
        description="one row per order",
        row_count=row_count,
        partitioning=PartitioningProfile(column="order_date", kind="time"),
        clustering=["status"],
        last_updated_physical=datetime(2026, 6, 9, 8, 30, tzinfo=UTC),
        columns=[
            ColumnProfile(name="order_id", type="INTEGER", nullable=False),
            ColumnProfile(
                name="status",
                type="VARCHAR",
                description="order state",
                description_source="warehouse",
                null_rate=0.02,
                distinct_count=5,
                is_low_cardinality=True,
                top_values=["returned", "completed", "shipped"],
            ),
        ],
    )


def _candidate(left: str = "orders", right: str = "customers") -> JoinCandidate:
    return JoinCandidate(
        left_table=left,
        left_columns=["customer_id"],
        right_table=right,
        right_columns=["id"],
        evidence="fk",
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@needs_db
def test_upsert_round_trips_and_keeps_previous() -> None:
    org = _org()
    try:
        with org_session(org) as s:
            profile_store.upsert_profile(s, _profile(row_count=99))
        with org_session(org) as s:
            stored = profile_store.get_profile(s, "jaffle", "orders")
            assert stored is not None
            assert stored.previous is None
            assert stored.profile.row_count == 99
            assert stored.profile.partitioning is not None
            assert stored.profile.partitioning.column == "order_date"
            assert stored.profile.last_updated_physical == datetime(2026, 6, 9, 8, 30, tzinfo=UTC)
            status = next(c for c in stored.profile.columns if c.name == "status")
            assert status.top_values == ["returned", "completed", "shipped"]
            assert status.is_low_cardinality and status.distinct_count == 5

        # second upsert: the new payload wins, the old one survives as `previous`
        with org_session(org) as s:
            profile_store.upsert_profile(s, _profile(row_count=150))
        with org_session(org) as s:
            stored = profile_store.get_profile(s, "jaffle", "orders")
            assert stored is not None
            assert stored.profile.row_count == 150
            assert stored.previous is not None and stored.previous.row_count == 99

        # third upsert: `previous` tracks the immediately prior version only
        with org_session(org) as s:
            profile_store.upsert_profile(s, _profile(row_count=200))
        with org_session(org) as s:
            stored = profile_store.get_profile(s, "jaffle", "orders")
            assert stored is not None
            assert stored.profile.row_count == 200
            assert stored.previous is not None and stored.previous.row_count == 150
    finally:
        _cleanup(org)


@needs_db
def test_list_and_prune_scoped_to_connection() -> None:
    org = _org()
    try:
        with org_session(org) as s:
            profile_store.upsert_profile(s, _profile(table="orders"))
            profile_store.upsert_profile(s, _profile(table="customers"))
            profile_store.upsert_profile(s, _profile(connection="warehouse", table="orders"))
        with org_session(org) as s:
            jaffle = profile_store.list_profiles(s, "jaffle")
            assert [p.profile.table for p in jaffle] == ["customers", "orders"]
            assert profile_store.list_profiles(s, "nope") == []
        with org_session(org) as s:
            assert profile_store.prune_profiles(s, "jaffle", ["orders"]) == 1
        with org_session(org) as s:
            assert [p.profile.table for p in profile_store.list_profiles(s, "jaffle")] == ["orders"]
            # the other connection's profiles are untouched
            assert len(profile_store.list_profiles(s, "warehouse")) == 1
    finally:
        _cleanup(org)


def test_is_stale_honours_ttl() -> None:
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    fresh = now - timedelta(hours=1)
    old = now - timedelta(hours=25)
    assert profile_store.is_stale(old, now=now) is True
    assert profile_store.is_stale(fresh, now=now) is False
    # the TTL argument is honoured, both ways
    assert profile_store.is_stale(fresh, ttl_hours=0.5, now=now) is True
    assert profile_store.is_stale(old, ttl_hours=48, now=now) is False
    # naive timestamps are treated as UTC
    assert profile_store.is_stale(old.replace(tzinfo=None), now=now) is True


# ---------------------------------------------------------------------------
# Join candidates
# ---------------------------------------------------------------------------


@needs_db
def test_join_candidate_crud_and_dedupe() -> None:
    org = _org()
    try:
        with org_session(org) as s:
            n = profile_store.record_candidates(
                s, "jaffle", [_candidate(), _candidate(left="payments", right="orders")]
            )
            assert n == 2
        with org_session(org) as s:
            cands = profile_store.list_candidates(s, "jaffle")
            assert len(cands) == 2
            assert all(c.status == "candidate" and c.evidence == "fk" for c in cands)
            orders = next(c for c in cands if c.left_table == "orders")
            assert orders.id is not None
            assert orders.left_columns == ["customer_id"] and orders.right_columns == ["id"]
        with org_session(org) as s:
            assert profile_store.approve_candidate(s, str(orders.id)) == 1
            payments = next(
                c for c in profile_store.list_candidates(s, "jaffle") if c.left_table == "payments"
            )
            assert profile_store.reject_candidate(s, str(payments.id)) == 1
        # re-recording the same candidates inserts nothing and resets no status
        with org_session(org) as s:
            assert (
                profile_store.record_candidates(
                    s, "jaffle", [_candidate(), _candidate(left="payments", right="orders")]
                )
                == 0
            )
        with org_session(org) as s:
            assert {c.status for c in profile_store.list_candidates(s, "jaffle")} == {
                "approved",
                "rejected",
            }
            approved = profile_store.list_candidates(s, "jaffle", status="approved")
            assert len(approved) == 1 and approved[0].left_table == "orders"
    finally:
        _cleanup(org)


# ---------------------------------------------------------------------------
# RLS isolation
# ---------------------------------------------------------------------------


@needs_db
def test_rls_isolates_orgs() -> None:
    org_a = _org()
    org_b = _org()
    try:
        with org_session(org_a) as s:
            profile_store.upsert_profile(s, _profile())
            profile_store.record_candidates(s, "jaffle", [_candidate()])
        with org_session(org_a) as s:
            cand_id = profile_store.list_candidates(s, "jaffle")[0].id

        # org B sees none of org A's rows…
        with org_session(org_b) as s:
            assert profile_store.get_profile(s, "jaffle", "orders") is None
            assert profile_store.list_profiles(s, "jaffle") == []
            assert profile_store.list_candidates(s, "jaffle") == []
            # …and cannot flip A's candidate or prune A's profiles
            assert profile_store.approve_candidate(s, str(cand_id)) == 0
            assert profile_store.prune_profiles(s, "jaffle", ["nothing"]) == 0

        # org A's rows are intact
        with org_session(org_a) as s:
            assert profile_store.get_profile(s, "jaffle", "orders") is not None
            assert profile_store.list_candidates(s, "jaffle")[0].status == "candidate"
    finally:
        _cleanup(org_a, org_b)
