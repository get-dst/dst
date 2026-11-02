"""The outside-lens path, end to end: a `sql` read inside a lens over exposed
tables carries every check (grain, data end, join conservation, clocks,
reconciliation) on its receipt and lands in the request log as lane
``explore`` with the question it answered; a review ticket on it carries the
SQL, the tables, the checks and the question; an approval certifies the pair
pinned to the tables' blueprints, or seeds a lens over the tables read; observe
and `dst test` count the path apart. DuckDB, no provider, no model call.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import duckdb
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import query as query_api
from services.api import sql as sql_api
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.cli.test import _explore_scorecard
from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.eval import EvalCase
from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import SemanticModel
from services.db.session import org_session
from services.evals.runner import run_behavioral
from services.lenses import store
from services.lenses.store import LensBundle
from services.llm import registry
from services.observability import observe
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import run_query

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

LENS = "ledger"
REVENUE_SQL = "SELECT SUM(amount) AS revenue FROM shop.orders WHERE ordered_on <= DATE '2026-03-03'"


def _model() -> SemanticModel:
    return SemanticModel.model_validate(
        {
            "lens": LENS,
            "dialect": "duckdb",
            "entities": [
                {
                    "name": "orders",
                    "description": "one row per order",
                    "source": {"connection": "wh", "table": "shop.orders"},
                    "exposed": True,
                    "fields": [
                        {"name": "order_id", "type": "number"},
                        {"name": "ordered_on", "type": "date"},
                        {"name": "amount", "type": "number"},
                    ],
                },
                {
                    "name": "items",
                    "source": {"connection": "wh", "table": "shop.items"},
                    "exposed": True,
                    "fields": [
                        {"name": "order_id", "type": "number"},
                        {"name": "sku", "type": "string"},
                    ],
                },
            ],
            "blueprints": {
                "shop.orders": {
                    "connection": "wh",
                    "table": "shop.orders",
                    "grain": "one order",
                    "key": ["order_id"],
                    "clocks": {
                        "ordered_on": {"meaning": "when the order was placed", "timezone": "UTC"}
                    },
                }
            },
            "shared_provenance": {
                "compiled_at": "2026-11-01T00:00:00+00:00",
                "assets": {"entity/orders": "e1", "blueprint/shop.orders": "b1"},
            },
        }
    )


def _world(path: Path) -> str:
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA shop")
    con.execute(
        "CREATE TABLE shop.orders AS SELECT * FROM (VALUES "
        "(1, DATE '2026-03-01', 10.0), (2, DATE '2026-03-02', 20.0), (3, DATE '2026-03-03', 30.0)"
        ") AS t(order_id, ordered_on, amount)"
    )
    con.execute(
        "CREATE TABLE shop.items AS SELECT * FROM (VALUES "
        "(1, 'a'), (1, 'b'), (2, 'c'), (3, 'd'), (3, 'e')) AS t(order_id, sku)"
    )
    con.close()
    return str(path)


@pytest.fixture()
def org(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, str]]:
    """An org with the ledger lens published, a certified pair to reconcile
    against, a DuckDB world behind every door, and no provider at all."""
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org_id = c.execute(
            text("INSERT INTO org (name) VALUES ('Explore') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org_id, "h": hash_token(raw)},
        )
    bundle = LensBundle(
        config=LensConfig(name=LENS, connections=["wh"], untyped_fallback=True),
        semantic_model=_model(),
    )
    with org_session(org_id) as session:
        store.create_lens(session, bundle)
        store.publish(session, LENS)
        certify_store.create(
            session, LENS, "revenue through march 3", REVENUE_SQL, None, created_by="test"
        )
    path = _world(tmp_path / "world.duckdb")
    monkeypatch.setattr(registry, "specs", lambda: {})
    monkeypatch.setattr(registry, "resolve_embedder", lambda: None)
    for module in (query_api, sql_api):
        monkeypatch.setattr(
            module, "_authorized_connector", lambda b, c: DuckDBConnector(path, profile=False)
        )
    yield org_id, raw
    with admin.begin() as c:
        for table in ("review", "certified_answer", "request_log", "audit_log", "admin_token"):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id})  # noqa: S608
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org_id})


def _sql(raw: str, sql: str, **extra: Any) -> Any:
    return client.post(
        "/v1/sql",
        json={"lens": LENS, "sql": sql, **extra},
        headers={"Authorization": f"Bearer {raw}"},
    )


def _log_row(org_id: Any, rid: str) -> Any:
    with create_engine(settings.database_admin_url).connect() as c:
        return c.execute(
            text("SELECT lane, question, degraded, mode FROM request_log WHERE request_id = :r"),
            {"r": rid},
        ).first()


def test_a_read_over_exposed_tables_carries_every_check_and_lands_in_the_explore_lane(
    org: tuple[Any, str],
) -> None:
    org_id, raw = org
    joined = (
        "SELECT SUM(o.amount) AS revenue FROM shop.orders o JOIN shop.items i "
        "ON i.order_id = o.order_id WHERE o.ordered_on <= DATE '2026-03-03'"
    )
    r = _sql(raw, joined, question="revenue through march 3, with items")
    assert r.status_code == 200, r.text
    d = r.json()
    # The SQL is shown, the join's row multiplication is disclosed with its
    # mode, the clock is named, and the reconciliation says why it could not
    # close: no certified answer reads both tables.
    assert d["sql"] and d["mode"] == "wrong_grain"
    by_name = {c.split(":", 1)[0]: c for c in d["checks"]}
    assert by_name["join"].startswith(
        "join:disclose (wrong_grain): shop.orders joined to shop.items gives 5 rows from 3 rows"
    )
    assert (
        by_name["clock"]
        == "clock:pass: shop.orders.ordered_on records when the order was placed, stored in UTC"
    )
    assert by_name["reconcile"].startswith(
        "reconcile:skip (unverifiable): could not close: no certified answer"
    )
    assert d["clock"] == ["shop.orders.ordered_on records when the order was placed, stored in UTC"]
    assert by_name["grain"] == "grain:one_per_key"
    assert d["receipt"]["checks"] == d["checks"] and d["receipt"]["mode"] == "wrong_grain"
    assert d["receipt"]["confidence"] == "unverified"
    row = _log_row(org_id, d["request_id"])
    assert row is not None
    assert row[0] == "explore" and row[1] == "revenue through march 3, with items"
    assert any(line.startswith("JOIN: ") for line in row[2]) and row[3] == "wrong_grain"

    # The same tables and window as a certified answer: the delta is disclosed.
    doubled = REVENUE_SQL.replace("SUM(amount)", "SUM(amount) * 2")
    d = _sql(raw, doubled, question="twice the revenue through march 3").json()
    reconcile = next(c for c in d["checks"] if c.startswith("reconcile:"))
    assert reconcile.startswith(
        "reconcile:disclose (unverifiable): differs from the certified answer to "
        "'revenue through march 3'"
    )
    assert "this statement 120, certified 60, delta +60 (+100.0%)" in reconcile
    assert d["mode"] == "unverifiable"


def test_a_review_on_an_outside_lens_answer_carries_the_sql_tables_checks_and_question(
    org: tuple[Any, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, raw = org
    h = {"Authorization": f"Bearer {raw}"}
    rid = _sql(raw, REVENUE_SQL, question="revenue through march 3").json()["request_id"]
    r = client.post("/v1/reviews", headers=h, json={"request_id": rid})
    assert r.status_code == 201, r.text
    ticket = r.json()
    # No judge is configured, so the ticket waits for a human, carrying what
    # the human rules on.
    assert ticket["state"] == "needs_human"
    explore = ticket["explore"]
    assert explore["question"] == "revenue through march 3" and explore["sql"] == REVENUE_SQL
    assert explore["tables"] == ["shop.orders"]
    assert any(c.startswith("reconcile:pass") for c in explore["checks"])
    assert any(c.startswith("grain:") or "rows" in c for c in explore["checks"])
    assert any(line.startswith("EXPOSED: reads shop.orders") for line in explore["lines"])
    tid = ticket["ticket_id"]
    assert client.get(f"/v1/reviews/{tid}", headers=h).json()["explore"]["sql"] == REVENUE_SQL

    # Approval writes a kind: sql pair, pinned to the table's blueprint.
    ruled = client.post(f"/mgmt/reviews/{tid}/rule", headers=h, json={"verdict": "approve"})
    assert (
        ruled.status_code == 200
        and ruled.json()["explore"]["question"] == "revenue through march 3"
    )
    cert = client.post(f"/mgmt/lenses/{LENS}/certified/from-request/{rid}", headers=h, json={})
    assert cert.status_code == 201, cert.text
    assert cert.json()["pair"] is None
    with org_session(org_id) as session:
        pair = certify_store.get(session, cert.json()["id"])
    assert pair is not None and pair.question == "revenue through march 3"
    assert pair.bindings == {"entity/orders": "e1", "blueprint/shop.orders": "b1"}

    # A read with no question has no pair to certify: said, not guessed.
    bare = _sql(raw, "SELECT order_id FROM shop.orders").json()["request_id"]
    refused = client.post(f"/mgmt/lenses/{LENS}/certified/from-request/{bare}", headers=h, json={})
    assert refused.status_code == 409 and "no question" in refused.json()["detail"]

    # Or seeds a lens: the slot map over the tables read, then the files.
    seed = client.get(f"/mgmt/reviews/{tid}/seed", headers=h)
    assert seed.status_code == 200, seed.text
    (table,) = seed.json()["tables"]
    assert table["table"] == "shop.orders" and table["key"] == ["order_id"]
    assert table["clocks"]["ordered_on"]["meaning"] == "when the order was placed"
    assert [f["name"] for f in table["fields"]] == ["order_id", "ordered_on", "amount"]

    import httpx

    from services.cli.main import main as cli_main

    def fake_get(url: str, headers: Any = None, params: Any = None, timeout: Any = None) -> Any:
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        return client.get(parts.path, headers=headers, params={**query, **(params or {})})

    monkeypatch.setattr(httpx, "get", fake_get)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        sys, "argv", ["dst", "seed-lens", tid, "--dir", str(project), "--token", raw]
    )
    assert cli_main() == 0
    entity = (project / "semantic" / "entities" / "orders.yaml").read_text()
    assert "table: shop.orders" in entity and "primary_key:\n- order_id" in entity
    assert "TODO" in entity and "# Question: revenue through march 3" in entity
    lens_yaml = (project / "lenses" / "orders_seed" / "lens.yaml").read_text()
    assert "- name: orders" in lens_yaml and "connections:\n- wh" in lens_yaml
    cases = (project / "lenses" / "orders_seed" / "evals" / "cases.yaml").read_text()
    assert "question: revenue through march 3" in cases and "expect: answer" in cases

    # A ticket on a governed answer seeds nothing: it has a lens already.
    assert client.get("/mgmt/reviews/rev_nothing/seed", headers=h).status_code == 404


def test_observe_counts_the_explore_lane_apart_from_governed_traffic(org: tuple[Any, str]) -> None:
    org_id, raw = org
    _sql(raw, REVENUE_SQL, question="revenue through march 3")
    _sql(raw, "SELECT order_id FROM shop.orders")
    with org_session(org_id) as session:
        kpis = observe.kpis(session)
    assert kpis["explore"]["queries"] == 2 and kpis["explore"]["answered"] == 2
    assert kpis["explore"]["warehouse_cost_usd"] == 0.0
    assert kpis["queries"] == 0, "an outside-lens read is not a governed query"


def test_an_answer_from_an_exposed_table_is_an_explore_answer_in_the_pipeline_and_the_suite(
    tmp_path: Path,
) -> None:
    path = _world(tmp_path / "world.duckdb")
    pairs = [("revenue through march 3", REVENUE_SQL)]

    def serve(question: str) -> Any:
        return run_query(
            question=question,
            lens_name=LENS,
            org_id="org-1",
            caller="analyst",
            semantic_model=_model(),
            connector=DuckDBConnector(path),
            generator=FixedSQLGenerator(REVENUE_SQL.replace("SUM(amount)", "SUM(amount) * 2")),
            composer=None,
            certified_pairs=lambda: pairs,
        )

    res = serve("twice the revenue through march 3")
    assert res.response.status == "ok", res.response.answer
    assert res.trace.lane == "explore"
    lines = {line.split(":", 1)[0] for line in res.response.degraded}
    assert {"EXPOSED", "GRAIN", "CLOCK", "RECONCILE"} <= lines, res.response.degraded
    reconcile = next(c for c in res.response.verification.checks if c.name == "reconcile")
    assert reconcile.status == "fail" and "delta +60" in (reconcile.reason or "")
    assert res.response.verification.grade != "verified"

    outcome = run_behavioral(
        lens=LENS,
        cases=[
            EvalCase(
                id="c1", lens=LENS, question="twice the revenue", expect="answer", source="authored"
            )
        ],
        serve=serve,
        concurrency=1,
    )
    (result,) = outcome.results
    assert result.passed and result.checks == {"lane": "explore", "answered": True}
    rows = [
        {"verdict": "pass", "lane": "explore", "answered": True, "ai_cost_usd": 0.0},
        {"verdict": "pass", "lane": "explore", "answered": False, "ai_cost_usd": 0.0},
        {"verdict": "fail", "lane": "explore", "answered": True, "ai_cost_usd": 0.001},
        {"verdict": "pass", "lane": None},
    ]
    assert _explore_scorecard(rows) == {
        "cases": 3,
        "right": 1,
        "honest": 1,
        "wrong": 1,
        "ai_cost_usd": 0.001,
    }
    assert _explore_scorecard([{"verdict": "pass"}]) is None
