"""semantic_asset store — upsert/hash/RLS/delete-guard, standards view."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.contracts.shared_semantic import asset_hash
from services.db.session import org_session
from services.definitions import standards as std_store
from services.semantic import store


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


@pytest.fixture()
def org_id():
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('SemStoreT') RETURNING id")
        ).scalar_one()
    yield oid
    with admin.begin() as c:
        c.execute(text("DELETE FROM semantic_asset WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


ENTITY = {"name": "orders", "source": {"connection": "wh", "table": "t.orders"}}


def test_upsert_get_list_hash(org_id: uuid.UUID) -> None:
    with org_session(org_id) as s:
        stored = store.upsert_asset(s, "entity", "orders", ENTITY)
        assert stored.content_hash == asset_hash(ENTITY)
        got = store.get_asset(s, "entity", "orders")
        assert got is not None and got.body["name"] == "orders"
        # update changes the hash
        store.upsert_asset(s, "entity", "orders", {**ENTITY, "grain": "one row per order"})
        assert store.asset_hashes(s)["entity/orders"] != stored.content_hash
        assert [a.name for a in store.list_assets(s, "entity")] == ["orders"]
        s.commit()


def test_rls_isolates_orgs(org_id: uuid.UUID) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        other = c.execute(
            text("INSERT INTO org (name) VALUES ('SemStoreT2') RETURNING id")
        ).scalar_one()
    try:
        with org_session(org_id) as s:
            store.upsert_asset(s, "definition", "ltv", {"term": "ltv", "body": "x"})
            s.commit()
        with org_session(other) as s:
            assert store.list_assets(s) == []
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM semantic_asset WHERE org_id = :o"), {"o": other})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": other})
        admin.dispose()


def test_delete_guard_names_dependent_lenses(org_id: uuid.UUID) -> None:
    with org_session(org_id) as s:
        store.upsert_asset(s, "entity", "orders", ENTITY)
        s.execute(
            text(
                "INSERT INTO lens (org_id, name, display_name, status, draft_json, "
                "published_json) VALUES "
                "(NULLIF(current_setting('app.current_org', true), '')::uuid, 'board', 'B', "
                "'live', CAST(:b AS jsonb), CAST(:b AS jsonb))"
            ),
            {"b": '{"config": {"select": {"entities": [{"name": "orders"}]}}}'},
        )
        with pytest.raises(ValueError, match="board"):
            store.delete_asset(s, "entity", "orders")
        s.execute(text("DELETE FROM lens WHERE name = 'board'"))
        assert store.delete_asset(s, "entity", "orders") == 1
        s.commit()


def test_standards_view_over_shared_definitions(org_id: uuid.UUID) -> None:
    with org_session(org_id) as s:
        std_store.upsert_standard(s, "net_revenue", "Revenue net of refunds.", "amount > 0")
        assert [x.term for x in std_store.list_standards(s)] == ["net_revenue"]
        got = std_store.get_standard(s, "net_revenue")
        assert got is not None and got.sql_expr == "amount > 0"
        # the standard IS a shared definition row
        asset = store.get_asset(s, "definition", "net_revenue")
        assert asset is not None and asset.body["status"] == "active"
        # upsert preserves shared-only fields (status/possible_mappings)
        asset.body["status"] = "ambiguous"
        store.upsert_asset(s, "definition", "net_revenue", asset.body)
        std_store.upsert_standard(s, "net_revenue", "new prose", None)
        again = store.get_asset(s, "definition", "net_revenue")
        assert again is not None and again.body["status"] == "ambiguous"
        assert std_store.delete_standard(s, "net_revenue") == 1
        s.commit()
