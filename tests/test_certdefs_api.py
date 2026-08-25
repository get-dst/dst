"""GET /mgmt/lenses/{name}/certified-definitions — the read endpoint.

The happy path needs a seeded lens (needs_db); the graceful-degradation contract
(no certified_dir → null + empty; bad path → dir echoed, empty metrics, never 500) and
the bundle→certified_dir resolution are exercised directly without a database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api.mgmt_lenses import _lens_certified_dir
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.lens_config import ModelConfig
from services.lenses.demo import jaffle_customer_value_bundle

client = TestClient(app)

CERTIFIED_PAGE = """---
metric: net_invoiced_revenue
summary: net invoiced revenue, recognised, after credit notes
owner: finance
grain: invoice_line
sources:
  - bronze.invoices
  - bronze.credit_notes
source_of_truth: NetSuite
usage_mode: auto
verified_value:
  value: 35020740
  as_of: '2026-06-15'
sql: SELECT SUM(amount) FROM bronze.invoices
---

Net of credit notes; recognised on issue date.
"""


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
        org = c.execute(
            text("INSERT INTO org (name) VALUES ('CertDefApi') RETURNING id")
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


def test_certdef_view_requires_auth() -> None:
    assert client.get("/mgmt/lenses/whatever/certified-definitions").status_code == 401


def test_resolve_certified_dir_prefers_published_then_draft() -> None:
    """The serving path reads the published bundle, so the view follows it."""
    draft = jaffle_customer_value_bundle()
    draft.config.model.certified_dir = "/certified/draft"
    published = jaffle_customer_value_bundle()
    published.config.model.certified_dir = "/certified/live"

    row = {"draft": draft.model_dump(mode="json"), "published": published.model_dump(mode="json")}
    assert _lens_certified_dir(row) == "/certified/live"

    draft_only = {"draft": draft.model_dump(mode="json"), "published": None}
    assert _lens_certified_dir(draft_only) == "/certified/draft"
    assert _lens_certified_dir({"draft": None, "published": None}) is None


@needs_db
def test_certdef_view_loads_pages_from_bound_directory(tmp_path: Path) -> None:
    (tmp_path / "01_revenue.md").write_text(CERTIFIED_PAGE, encoding="utf-8")
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    bundle = jaffle_customer_value_bundle()
    bundle.config.model = ModelConfig(model="claude-sonnet-4-6", certified_dir=str(tmp_path))
    try:
        created = client.post("/mgmt/lenses", json=bundle.model_dump(mode="json"), headers=h)
        assert created.status_code == 201

        view = client.get("/mgmt/lenses/customer_value/certified-definitions", headers=h).json()
        assert view["certified_dir"] == str(tmp_path)
        assert len(view["metrics"]) == 1
        m = view["metrics"][0]
        assert m["metric"] == "net_invoiced_revenue"
        assert m["usage_mode"] == "auto"
        assert m["verified_value"]["value"] == 35020740
        assert m["verified_value"]["as_of"] == "2026-06-15"
        assert "SELECT SUM(amount)" in m["sql"]
        assert m["sources"] == ["bronze.invoices", "bronze.credit_notes"]
    finally:
        _cleanup(org)


@needs_db
def test_certdef_view_empty_without_binding() -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    bundle = jaffle_customer_value_bundle()  # no certified_dir
    try:
        client.post("/mgmt/lenses", json=bundle.model_dump(mode="json"), headers=h)
        view = client.get("/mgmt/lenses/customer_value/certified-definitions", headers=h).json()
        assert view == {"certified_dir": None, "metrics": []}
    finally:
        _cleanup(org)


@needs_db
def test_certdef_view_tolerates_a_bad_path(tmp_path: Path) -> None:
    """A certified_dir pointing nowhere echoes the path but never 500s."""
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    missing = str(tmp_path / "does-not-exist")
    bundle = jaffle_customer_value_bundle()
    bundle.config.model = ModelConfig(model="claude-sonnet-4-6", certified_dir=missing)
    try:
        client.post("/mgmt/lenses", json=bundle.model_dump(mode="json"), headers=h)
        resp = client.get("/mgmt/lenses/customer_value/certified-definitions", headers=h)
        assert resp.status_code == 200
        assert resp.json() == {"certified_dir": missing, "metrics": []}
    finally:
        _cleanup(org)


@needs_db
def test_certdef_view_404_for_unknown_lens() -> None:
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        assert client.get("/mgmt/lenses/nope/certified-definitions", headers=h).status_code == 404
    finally:
        _cleanup(org)
