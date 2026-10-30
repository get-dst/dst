"""Plan and apply on a Cube-backed project: the overlay checked against Cube's
/v1/meta (a recorded one, served by a fake), errors refusing the apply, and
the model_version stamped on every lens the overlay serves."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.cube_meta import MetaDoc
from services.contracts.failure_modes import FailureMode
from services.cube.client import CubeError
from services.db.session import org_session
from services.lenses import store as lens_store
from services.project import cube_plan
from tests import cube_shop
from tests.fixtures.cube_shop.make_data import build

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


@pytest.fixture(scope="module")
def warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cube_shop") / "shop.duckdb"
    build(path)
    return path


@pytest.fixture()
def org(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('CubeProjT') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    yield oid, {"Authorization": f"Bearer {raw}"}
    with admin.begin() as c:
        for table in (
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "semantic_asset",
            "table_profile",
            "connection",
            "caller_group",
            "admin_token",
        ):
            try:
                with c.begin_nested():
                    c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
            except Exception:  # noqa: BLE001 — a table this schema names otherwise
                pass
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


def _serve(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None = None) -> None:
    doc = MetaDoc.parse(payload or cube_shop.meta_payload())
    public = MetaDoc.parse(cube_shop.public_meta_payload()) if payload is None else doc
    monkeypatch.setattr(cube_plan, "fetch_meta", lambda _files: (doc, public))


def _cube_rows(out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in out if r.get("scope") == "cube"]


@needs_db
def test_plan_reads_the_overlay_against_cube_and_prints_the_keys_read(
    org, monkeypatch, warehouse
) -> None:
    _oid, headers = org
    _serve(monkeypatch)
    plan = client.post(
        "/mgmt/project/plan", headers=headers, json={"files": cube_shop.project_files(warehouse)}
    ).json()
    rows = _cube_rows(plan)
    assert not [r for r in rows if r["status"] == "invalid"], rows
    keys = next(r for r in rows if r["status"] == "keys")
    assert keys["lens"] == "sales"
    assert keys["model_version"] == cube_shop.meta().model_version(keys["views"])
    read = {(k["on"], k["key"]): k for k in keys["keys"]}
    assert read[("measure", "additivity")]["members"] >= 1
    assert "probe --cube" in read[("dimension", "dictionary")]["reader"]
    # The views list their members and grant the hidden manager_email to ops
    # only: Cube withholds it from a caller in no group, so plan has no warning.
    assert not [r for r in rows if r.get("code") == "hide_unenforced_in_cube"]
    lens = next(r for r in plan if r.get("lens") == "sales" and "scope" not in r)
    assert lens["status"] == "create" and not lens["errors"]


@needs_db
def test_plan_names_cube_down_and_an_unknown_view_as_errors(org, monkeypatch, warehouse) -> None:
    _oid, headers = org
    files = cube_shop.project_files(warehouse)

    def down(_files: dict[str, str]) -> tuple[MetaDoc, MetaDoc]:
        raise CubeError("ConnectError: refused", mode=FailureMode.provider_down, provider="cube")

    monkeypatch.setattr(cube_plan, "fetch_meta", down)
    rows = _cube_rows(
        client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    )
    assert [(r["status"], r["code"]) for r in rows] == [("invalid", "provider_down")]
    assert "provider_down: cube" in rows[0]["error"]

    _serve(monkeypatch)
    files["cube/overlay.yaml"] = files["cube/overlay.yaml"].replace(
        "    - sales_stock_monthly", "    - sales_stock_monthly\n    - sales_ghost"
    )
    rows = _cube_rows(
        client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    )
    bad = [r for r in rows if r["status"] == "invalid"]
    assert [(r["code"], r["mode"]) for r in bad] == [("unknown_view", "not_declared")]
    assert "sales_ghost" in bad[0]["error"]


@needs_db
def test_apply_stamps_the_model_version_and_restamps_when_cube_moves(
    org, monkeypatch, warehouse
) -> None:
    oid, headers = org
    _serve(monkeypatch)
    files = cube_shop.project_files(warehouse)
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert not [r for r in out if r.get("errors")], out
    cube_row = next(r for r in out if r.get("scope") == "cube")
    assert cube_row["applied"][0].startswith("sales: model ")
    with org_session(oid) as s:
        bundle = lens_store.resolve_published(s, "sales")
    assert bundle is not None and bundle.semantic_model.cube is not None
    stamp = bundle.semantic_model.cube
    assert stamp.model_version == cube_shop.meta().model_version(stamp.views)
    assert bundle.semantic_model.cube_views == stamp.views
    stored = cube_plan.stored_lens_model(stamp)
    assert stored is not None and stored.member("sales_orders.orders_revenue") is not None

    again = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert next(r for r in again if r.get("lens") == "sales")["action"] == "unchanged"

    moved = copy.deepcopy(cube_shop.meta_payload())
    view = next(c for c in moved["cubes"] if c["name"] == "sales_orders")
    view["measures"] = [m for m in view["measures"] if m["name"] != "sales_orders.orders_tips_eur"]
    _serve(monkeypatch, moved)
    third = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert next(r for r in third if r.get("lens") == "sales")["action"] == "updated"
    with org_session(oid) as s:
        restamped = lens_store.resolve_published(s, "sales")
    assert restamped is not None and restamped.semantic_model.cube is not None
    assert restamped.semantic_model.cube.model_version != stamp.model_version


@needs_db
def test_apply_refuses_a_cube_error_and_lands_nothing(org, monkeypatch, warehouse) -> None:
    oid, headers = org
    _serve(monkeypatch)
    files = cube_shop.project_files(warehouse)
    files["cube/overlay.yaml"] = files["cube/overlay.yaml"].replace(
        "default_time_dimension: ordered_at", "default_time_dimension: paid_at"
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    cube_row = next(r for r in out if r.get("scope") == "cube")
    assert cube_row["action"] == "rejected"
    assert any("paid_at" in e for e in cube_row["errors"])
    with org_session(oid) as s:
        assert lens_store.resolve_published(s, "sales") is None


@needs_db
def test_a_monitor_run_rides_apply_into_the_ready_data_quality_block(
    org, monkeypatch, warehouse
) -> None:
    _oid, headers = org
    _serve(monkeypatch)
    files = cube_shop.project_files(warehouse)
    ready = client.get("/ready").json()
    assert "data_quality" in ready
    freshness = {
        "as_of": "2026-09-15T02:00:00+00:00",
        "clock_end": "2026-09-14",
        "findings": [
            {
                "mode": "data_gap",
                "kind": "feed_stopped",
                "member": "orders.ordered_at",
                "detail": "ends 2026-07-31 while the newest feed of orders runs to 2026-09-14",
            }
        ],
        "unread": [],
    }
    files["profiles/cube.freshness.json"] = json.dumps(freshness)
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert not [r for r in out if r.get("errors")], out
    row = next(r for r in out if r.get("scope") == "data_quality")
    assert row["applied"] == ["data quality: 1 finding(s) as of 2026-09-14"]
    block = client.get("/ready").json()["data_quality"]
    mine = block["CubeProjT"]
    assert mine["findings"] == 1 and mine["by_mode"] == {"data_gap": 1}
    assert mine["clock_end"] == "2026-09-14" and mine["stale"] is True  # ran long ago


def _lanes(plan: list[dict[str, Any]]) -> dict[str, str]:
    row = next(r for r in plan if r.get("scope") == "serving")
    return {lens: v["lane"] for lens, v in row["lanes"].items()}


@needs_db
def test_plan_reports_the_lane_apply_stamps(org, monkeypatch, warehouse) -> None:
    # One decision for both: with the overlay the lens is stamped and served
    # through Cube; without it nothing is stamped, `views:` or not, and plan
    # says typed.
    _oid, headers = org
    _serve(monkeypatch)
    files = cube_shop.project_files(warehouse)
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    assert _lanes(plan)["sales"] == "cube"
    bare = {k: v for k, v in files.items() if k != "cube/overlay.yaml"}
    bare["lenses/sales/lens.yaml"] += "\nviews: [sales_orders]\n"
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": bare}).json()
    assert _lanes(plan)["sales"] == "typed"


@needs_db
def test_a_push_without_the_overlay_clears_the_stamp_and_serves_typed(
    org, monkeypatch, warehouse
) -> None:
    from services.serve.door import serving_core

    oid, headers = org
    _serve(monkeypatch)
    files = cube_shop.project_files(warehouse)
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert not [r for r in out if r.get("errors")], out
    with org_session(oid) as s:
        bundle = lens_store.resolve_published(s, "sales")
    assert bundle is not None and serving_core(bundle.config, bundle.semantic_model) == "cube"

    bare = {k: v for k, v in files.items() if k != "cube/overlay.yaml"}
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": bare}).json()
    assert not [r for r in out if r.get("errors")], out
    assert next(r for r in out if r.get("lens") == "sales")["action"] == "updated"
    with org_session(oid) as s:
        bundle = lens_store.resolve_published(s, "sales")
    assert bundle is not None and bundle.semantic_model.cube is None
    assert serving_core(bundle.config, bundle.semantic_model) == "typed"


@needs_db
def test_init_cube_then_apply_lands_the_scaffolded_lens(
    org, monkeypatch, warehouse, tmp_path
) -> None:
    # `dst init --cube` over a DuckDB warehouse, the scaffolded lens pointed
    # at the shop's views and nothing else edited: apply publishes it, served
    # through Cube. The scaffold names the connection init declared.
    import argparse

    from services.cli._common import _read_project
    from services.cli.init import run_init
    from services.serve.door import serving_core

    oid, headers = org
    _serve(monkeypatch)
    root = tmp_path / "proj"
    ns = argparse.Namespace(
        dir=str(root),
        name=None,
        warehouse="duckdb",
        duckdb_path=str(warehouse),
        example=False,
        cube=True,
        yes=True,
    )
    assert run_init(ns) == 0
    lens_yaml = root / "lenses" / "main" / "lens.yaml"
    assert "connections: [warehouse]" in lens_yaml.read_text()
    lens_yaml.write_text(
        lens_yaml.read_text().replace("views: []", "views: [sales_orders]"), encoding="utf-8"
    )
    files = _read_project(root)
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(r for r in out if r.get("lens") == "main")
    assert not row.get("errors"), out
    with org_session(oid) as s:
        bundle = lens_store.resolve_published(s, "main")
    assert bundle is not None and bundle.semantic_model.cube is not None
    assert serving_core(bundle.config, bundle.semantic_model) == "cube"
