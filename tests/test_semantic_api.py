"""/mgmt/semantic — asset CRUD + the drafter surface."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.semantic_model import FIELD_TYPES

client = TestClient(app)


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
def org_token():
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('SemApi') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    yield org, raw
    with admin.begin() as c:
        c.execute(text("DELETE FROM semantic_asset WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
    admin.dispose()


ENTITY_BODY = {
    "description": "One row per order.",
    "grain": "one row per order",
    "source": {"connection": "jaffle", "table": "orders"},
    "primary_key": ["order_id"],
    "fields": [{"name": "order_id", "type": "integer"}],
}


def test_semantic_requires_auth() -> None:
    assert client.get("/mgmt/semantic").status_code == 401


def test_asset_crud(org_token: tuple[uuid.UUID, str]) -> None:
    _org, raw = org_token
    h = {"Authorization": f"Bearer {raw}"}

    r = client.put("/mgmt/semantic/entity/orders", json=ENTITY_BODY, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "orders" and r.json()["source"] == "authored"

    r = client.put(
        "/mgmt/semantic/definition/net_revenue",
        json={"body": "Revenue net of refunds.", "sql_expr": "amount > 0"},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["body"]["term"] == "net_revenue"

    listing = client.get("/mgmt/semantic", headers=h).json()
    assert {(a["kind"], a["name"]) for a in listing} == {
        ("entity", "orders"),
        ("definition", "net_revenue"),
    }

    # Read-back of ONE asset, body included (otherwise confirming a
    # declaration landed takes a direct Postgres query).
    one = client.get("/mgmt/semantic/definition/net_revenue", headers=h)
    assert one.status_code == 200 and one.json()["body"]["sql_expr"] == "amount > 0"
    assert client.get("/mgmt/semantic/entity/nope", headers=h).status_code == 404
    only_defs = client.get("/mgmt/semantic", params={"kind": "definition"}, headers=h).json()
    assert [a["name"] for a in only_defs] == ["net_revenue"]

    # invalid bodies never store
    assert (
        client.put("/mgmt/semantic/entity/bad", json={"fields": "nope"}, headers=h).status_code
        == 422
    )
    assert client.get("/mgmt/semantic", params={"kind": "entity"}, headers=h).json()[0][
        "content_hash"
    ]

    assert client.delete("/mgmt/semantic/definition/net_revenue", headers=h).status_code == 204
    assert client.delete("/mgmt/semantic/definition/net_revenue", headers=h).status_code == 404


def test_delete_guarded_by_dependent_lens(org_token: tuple[uuid.UUID, str]) -> None:
    _org, raw = org_token
    h = {"Authorization": f"Bearer {raw}"}
    assert (
        client.put("/mgmt/semantic/entity/orders", json=ENTITY_BODY, headers=h).status_code == 200
    )
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO lens (org_id, name, display_name, status, draft_json, published_json)"
                " VALUES (:o, 'board', 'B', 'live', CAST(:b AS jsonb), CAST(:b AS jsonb))"
            ),
            {"o": _org, "b": '{"config": {"select": {"entities": [{"name": "orders"}]}}}'},
        )
    admin.dispose()
    r = client.delete("/mgmt/semantic/entity/orders", headers=h)
    assert r.status_code == 409 and "board" in r.json()["detail"]


_DRAFT_JSON = json.dumps(
    {
        "entities": [
            {
                "name": "the orders",
                "description": "Orders.",
                "grain": "one row per order",
                "source": {"connection": "ignored", "table": "orders"},
                "primary_key": ["order_id"],
                "fields": [
                    {"name": "order_id", "type": "integer"},
                    {"name": "amount", "type": "number"},
                ],
                "metrics": [{"name": "total_amount", "agg": "sum", "expr": "orders.amount"}],
                "joins": [],
            }
        ],
        "definitions": [
            {
                "term": "value",
                "body": "ASK the user: which value?",
                "status": "ambiguous",
                "possible_mappings": ["order amount - orders.amount"],
            }
        ],
    }
)


def _fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM([_DRAFT_JSON]),
    )


def test_introspect_returns_agent_legible_schema(org_token: tuple[uuid.UUID, str]) -> None:
    """The OSS authoring path: no LLM involved, just schema + profile facts."""
    _org, raw = org_token
    h = {"Authorization": f"Bearer {raw}"}
    r = client.get("/mgmt/semantic/introspect", params={"connection": "jaffle"}, headers=h)
    assert r.status_code == 200, r.text
    text = r.json()["text"]
    assert "orders" in text and "customers" in text
    assert "amount" in text  # columns present
    # ...and the same listing as structure, so the CLI's --json has a source here
    payload = r.json()["json"]
    listed = {t["name"]: t for t in payload["tables"]}
    assert "amount" in {c["name"] for c in listed["orders"]["columns"]}
    assert all(c["type"] in FIELD_TYPES for c in listed["orders"]["columns"])
    # unknown connection is an actionable 400
    r = client.get("/mgmt/semantic/introspect", params={"connection": "ghost"}, headers=h)
    assert r.status_code == 400


def test_introspect_never_answers_nothing_with_a_200(
    org_token: tuple[uuid.UUID, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty listing behind a success code is the silent-nothing failure
    shape. No tables is a 404 naming what was searched."""
    _org, raw = org_token
    h = {"Authorization": f"Bearer {raw}"}
    empty = str(tmp_path / "empty.duckdb")
    duckdb.connect(empty).close()
    monkeypatch.setattr(
        "services.api.mgmt_semantic.resolve_connector",
        lambda *_a, **_k: DuckDBConnector(empty),
    )
    r = client.get("/mgmt/semantic/introspect", params={"connection": "empty"}, headers=h)
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "no tables" in detail and "main" in detail  # the schemas actually searched
    assert "schema:" in detail  # and the knob that fixes a mis-scoped connection


def test_introspect_says_so_when_the_table_filter_matches_nothing(
    org_token: tuple[uuid.UUID, str],
) -> None:
    _org, raw = org_token
    h = {"Authorization": f"Bearer {raw}"}
    r = client.get(
        "/mgmt/semantic/introspect",
        params={"connection": "jaffle", "tables": "nope"},
        headers=h,
    )
    assert r.status_code == 404
    assert "matched none" in r.json()["detail"] and "orders" in r.json()["detail"]
    # a filter that DOES match is an ordinary listing, scoped to it
    r = client.get(
        "/mgmt/semantic/introspect",
        params={"connection": "jaffle", "tables": "orders"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert "- orders" in r.json()["text"] and "- customers" not in r.json()["text"]
