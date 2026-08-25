"""Lens management CRUD + draft->publish via the API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
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
        org = c.execute(text("INSERT INTO org (name) VALUES ('LensApi') RETURNING id")).scalar_one()
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


def test_lens_list_requires_auth() -> None:
    assert client.get("/mgmt/lenses").status_code == 401


@needs_db
def test_lens_crud_publish_flow() -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    bundle = jaffle_customer_value_bundle().model_dump(mode="json")
    try:
        assert client.post("/mgmt/lenses", json=bundle, headers=h).status_code == 201
        assert client.post("/mgmt/lenses", json=bundle, headers=h).status_code == 409  # duplicate

        listing = {x["name"]: x["status"] for x in client.get("/mgmt/lenses", headers=h).json()}
        assert listing.get("customer_value") == "draft"

        pub = client.post("/mgmt/lenses/customer_value/publish", headers=h)
        assert pub.status_code == 200 and pub.json()["status"] == "live"

        got = client.get("/mgmt/lenses/customer_value", headers=h).json()
        assert got["status"] == "live" and got["published"] is not None

        assert client.delete("/mgmt/lenses/customer_value", headers=h).status_code == 204
        assert client.get("/mgmt/lenses/customer_value", headers=h).status_code == 404
    finally:
        _cleanup(org)


@needs_db
def test_lens_repo_versions_and_diff() -> None:
    # lens-as-repo: each publish snapshots a version; the lens materializes as a file
    # tree; two versions diff.
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    bundle = jaffle_customer_value_bundle().model_dump(mode="json")
    try:
        assert client.post("/mgmt/lenses", json=bundle, headers=h).status_code == 201

        pub1 = client.post("/mgmt/lenses/customer_value/publish", headers=h)
        assert pub1.status_code == 200 and pub1.json()["version"] == 1
        v1 = client.get("/mgmt/lenses/customer_value/versions", headers=h).json()
        assert [v["version"] for v in v1] == [1]  # exactly one version per publish

        # Change the draft, then publish again -> version 2.
        bundle2 = jaffle_customer_value_bundle().model_dump(mode="json")
        bundle2["config"]["display_name"] = "Customer Value v2"
        assert client.put("/mgmt/lenses/customer_value", json=bundle2, headers=h).status_code == 200
        pub2 = client.post("/mgmt/lenses/customer_value/publish", headers=h)
        assert pub2.json()["version"] == 2
        v = client.get("/mgmt/lenses/customer_value/versions", headers=h).json()
        assert [x["version"] for x in v] == [2, 1]  # newest first
        # The history names WHO published (0051): a raw admin token by its label.
        assert all(x["created_by"] == "token:t" for x in v)

        # The live tree lists files; one file's content is fetchable.
        repo = client.get("/mgmt/lenses/customer_value/repo", headers=h).json()
        paths = {f["path"] for f in repo["files"]}
        assert {"README.md", "queries.yaml", "compiled.yaml"} <= paths
        # the demo's definitions are shared-layer terms — no lens-local pages
        assert "semantic_model.yaml" not in paths
        f = client.get(
            "/mgmt/lenses/customer_value/repo/file", params={"path": "README.md"}, headers=h
        ).json()
        assert "Customer Value v2" in f["content"]

        # The README changed between versions, so the diff names it.
        d = client.get(
            "/mgmt/lenses/customer_value/diff", params={"from": 1, "to": 2}, headers=h
        ).json()
        assert any(x["path"] == "README.md" for x in d["files"])
    finally:
        _cleanup(org)


@needs_db
def test_put_blocks_entity_edits_on_shared_compiled_lens() -> None:
    # Clobber guard: the demo bundle is compiled from shared entity assets
    # (shared_provenance), so entity/join edits via PUT are derived-state clobbers → 409.
    # Config / sample-query edits keep working.
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    bundle = jaffle_customer_value_bundle().model_dump(mode="json")
    try:
        assert client.post("/mgmt/lenses", json=bundle, headers=h).status_code == 201

        entity_edit = jaffle_customer_value_bundle().model_dump(mode="json")
        entity_edit["semantic_model"]["entities"][0]["fields"].append(
            {"name": "sneaky", "type": "string"}
        )
        r = client.put("/mgmt/lenses/customer_value", json=entity_edit, headers=h)
        assert r.status_code == 409 and "/mgmt/semantic" in r.json()["detail"]

        join_edit = jaffle_customer_value_bundle().model_dump(mode="json")
        join_edit["semantic_model"]["joins"][0]["type"] = "inner"
        assert (
            client.put("/mgmt/lenses/customer_value", json=join_edit, headers=h).status_code == 409
        )

        # non-entity edits still land: config, sample queries, local definitions
        ok = jaffle_customer_value_bundle().model_dump(mode="json")
        ok["config"]["display_name"] = "Customer Value v2"
        ok["semantic_model"]["sample_queries"] = [{"question": "How many?", "sql": "SELECT 1"}]
        ok["semantic_model"]["definitions"].append(
            {"term": "local_extra", "body": "A lens-local term.", "source": "authored"}
        )
        assert client.put("/mgmt/lenses/customer_value", json=ok, headers=h).status_code == 200
        got = client.get("/mgmt/lenses/customer_value", headers=h).json()
        assert got["draft"]["config"]["display_name"] == "Customer Value v2"
    finally:
        _cleanup(org)


@needs_db
def test_put_allows_entity_edits_without_shared_provenance() -> None:
    # A hand-authored bundle (no shared_provenance) predates the shared layer —
    # its entities stay editable via PUT.
    from services.contracts.lens_config import LensConfig
    from services.contracts.semantic_model import SemanticModel

    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    sm = {
        "lens": "hand",
        "dialect": "duckdb",
        "entities": [
            {
                "name": "orders",
                "source": {"connection": "jaffle", "table": "orders"},
                "fields": [{"name": "order_id", "type": "integer"}],
            }
        ],
    }
    bundle = {
        "config": LensConfig(name="hand", display_name="Hand").model_dump(mode="json"),
        "semantic_model": SemanticModel.model_validate(sm).model_dump(mode="json"),
    }
    try:
        assert client.post("/mgmt/lenses", json=bundle, headers=h).status_code == 201
        bundle["semantic_model"]["entities"][0]["fields"].append(
            {"name": "amount", "type": "number"}
        )
        assert client.put("/mgmt/lenses/hand", json=bundle, headers=h).status_code == 200
    finally:
        _cleanup(org)
