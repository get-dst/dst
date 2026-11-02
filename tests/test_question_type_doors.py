"""The question-type step at the doors: the lens door, the routed door, driver
mode and the door with no driver, against the database.

A question that asks for no number is answered before any binding, from the
lens models and the project file, with no model call; the row it leaves in
the request log carries the type as its mode. The worlds are the fake shop
(a Cube lens) and the jaffle customer value lens (typed): invented.
"""

# ruff: noqa: F811 — the fixtures are imported by name, as pytest finds them
from __future__ import annotations

import json
from typing import Any

import pytest
import yaml
from sqlalchemy import create_engine, text

from services.config import settings
from services.contracts.failure_modes import FailureMode
from services.contracts.fakes import ScriptedLLM
from services.db.session import org_session
from services.llm.registry import ResolvedModel
from services.observability import observe
from services.serve import door
from services.serve import question as question_step
from tests import _cube_fake as fake
from tests.test_cube_frontier import Scripted
from tests.test_driver_mode import (  # noqa: F401 — the fixtures
    DRIVER,
    _ai_calls,
    _call,
    _drop,
    _scripted_typed_provider,
    _typed_org,
    client,
    org,
    world,
)


def _row(request_id: str) -> tuple[Any, ...] | None:
    admin = create_engine(settings.database_admin_url)
    with admin.connect() as c:
        return c.execute(
            text(
                "SELECT status, mode, question, lens, caller FROM request_log WHERE request_id = :r"
            ),
            {"r": request_id},
        ).first()


def test_route_query_in_driver_mode_defines_a_term_with_no_model(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    org_id, raw = org
    r = _call(raw, "POST", "/v1/query", json={"q": "what does revenue mean?"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["covered"] is True and out["routed_to"]["decided_by"] == "question_type", out
    answer = out["answer"]
    assert answer["status"] == "ok" and answer["mode"] == "definition"
    assert "shop.revenue" in answer["answer"] and "also called sales" in answer["answer"]
    assert answer.get("data") is None and answer.get("sql") is None
    row = _row(answer["request_id"])
    assert row is not None and (row[0], row[1], row[3]) == ("ok", "definition", "shop")
    assert _ai_calls(org_id) == 0
    assert not world.compiled  # nothing ran


def test_the_lens_door_answers_capability_from_the_model_and_its_refusals(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    _, raw = org
    r = _call(raw, "POST", "/v1/lenses/shop/query", json={"q": "do we have lifetime value?"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "refused" and out["mode"] == "capability"
    assert "'lifetime value' is declared not computable in shop" in out["answer"]
    r = _call(raw, "POST", "/v1/lenses/shop/query", json={"q": "do we track NPS"})
    assert r.json()["status"] == "ok" and r.json()["mode"] == "capability"
    assert r.json()["answer"].startswith("no: no lens holds 'nps'")
    r = _call(raw, "POST", "/v1/lenses/shop/query", json={"q": "is it possible to see revenue"})
    assert r.json()["status"] == "ok" and r.json()["answer"].startswith("yes:")
    # A data question still goes where a driver's question goes on a Cube lens.
    r = _call(raw, "POST", "/v1/lenses/shop/query", json={"q": "orders by status"})
    assert r.status_code == 400 and "query_cube" in r.json()["detail"]


def test_the_open_case_asks_the_model_once_on_the_no_driver_door_only(
    org: tuple[object, str], world: fake.FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, raw = org
    llm = ScriptedLLM(['{"type": "not_a_data_question", "subject": ""}'])
    monkeypatch.setattr(
        question_step, "fast_model", lambda caller, bundle=None: ResolvedModel(llm, "s", "fast-1")
    )
    auth = {"Authorization": f"Bearer {raw}"}
    # Decided by the rules: no model asked.
    r = client.post("/v1/query", json={"q": "what does revenue mean"}, headers=auth)
    assert r.status_code == 200 and r.json()["answer"]["mode"] == "definition", r.text
    assert llm._i == 0
    # Left open by the rules: the model reads it, once.
    r = client.post("/v1/query", json={"q": "blorp"}, headers=auth)
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert answer["status"] == "refused" and answer["mode"] == "not_a_data_question"
    assert "not a data question" in answer["answer"]
    assert llm._i == 1
    # The same open question in driver mode: the rules only, no model, a query.
    r = _call(raw, "POST", "/v1/query", json={"q": "blorp"})
    assert r.status_code == 200 and r.json()["driver"] is True, r.text
    assert r.json()["covered"] is False
    assert llm._i == 1


def test_a_sql_ask_on_the_no_driver_door_returns_the_sql_and_the_cube_query(
    org: tuple[object, str], world: fake.FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, raw = org
    reply = {
        "answer": "query",
        "queries": [{"measures": ["shop.count"], "dimensions": ["shop.status"]}],
        "ledger": [
            {"word": "orders", "to": "member", "ref": "shop.count"},
            {"word": "status", "to": "member", "ref": "shop.status"},
        ],
    }
    llm = Scripted(json.dumps(reply))
    monkeypatch.setattr(
        door, "frontier_model", lambda config: ResolvedModel(llm, "scripted", "fast-1")
    )
    auth = {"Authorization": f"Bearer {raw}"}
    asked = "give me the SQL for orders by status"
    r = client.post("/v1/lenses/shop/query", json={"q": asked}, headers=auth)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "ok" and out["mode"] == "sql_request", out
    assert out["answer"].startswith("SQL:\n") and "Cube query:" in out["answer"]
    assert '"shop.status"' in out["answer"] and out["data"]["rows"]
    assert len(llm.calls) == 1  # the frontier lane read the data question, nothing else
    row = _row(out["request_id"])
    assert row is not None and row[1] == "sql_request" and row[2] == asked

    # "my last request" reads this caller's log, and only theirs.
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(
            text(
                "INSERT INTO request_log (org_id, request_id, lens, caller, question, status) "
                "VALUES (:o, 'req_other', 'shop', 'someone-else', 'their secret question', 'ok')"
            ),
            {"o": org_id},
        )
    r = client.post("/v1/query", json={"q": "show me my last request"}, headers=auth)
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert answer["mode"] == "admin" and answer["status"] == "ok"
    assert asked in answer["answer"] and out["request_id"] in answer["answer"]
    assert "their secret question" not in answer["answer"]
    # "give me the sql" alone: the SQL of the last answer.
    r = client.post("/v1/query", json={"q": "give me the sql"}, headers=auth)
    answer = r.json()["answer"]
    assert answer["mode"] == "sql_request" and "SELECT" in answer["answer"].upper()

    with org_session(org_id) as session:
        kpis = observe.kpis(session, include_test=True)
    types = kpis["question_types"]
    assert isinstance(types, dict) and types.get("sql_request") == 2 and types.get("admin") == 1
    assert "sql_request" not in kpis["modes"] and "admin" not in kpis["modes"]  # type: ignore[operator]


def test_locate_cites_the_hand_off_declared_in_the_project_file(
    org: tuple[object, str], world: fake.FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, raw = org
    r = _call(raw, "POST", "/v1/query", json={"q": "what report shows revenue by month"})
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert answer["status"] == "refused" and answer["mode"] == "hand_off"
    assert "no hand_off is declared" in answer["answer"]
    root = door.project_root()
    (root / "dst.yaml").write_text(
        yaml.safe_dump(
            {
                "hand_off": [
                    {"name": "Sales dashboard", "kind": "dashboard", "url": "https://bi.example/s"},
                    {"name": "Helpdesk", "kind": "ticket", "mcp": "https://help.example/mcp"},
                ]
            }
        )
    )
    monkeypatch.setattr(settings, "project_file", str(root / "dst.yaml"))
    r = _call(raw, "POST", "/v1/query", json={"q": "what report shows revenue by month"})
    answer = r.json()["answer"]
    assert answer["status"] == "ok" and answer["mode"] == "hand_off", answer
    assert "Sales dashboard (dashboard) https://bi.example/s" in answer["answer"]
    assert "shop holds shop.revenue" in answer["answer"]
    r = _call(raw, "POST", "/v1/query", json={"q": "can you email this to the team"})
    answer = r.json()["answer"]
    assert answer["mode"] == "not_a_data_question" and "Helpdesk (ticket)" in answer["answer"]
    r = _call(raw, "POST", "/v1/query", json={"q": "which lens holds revenue"})
    answer = r.json()["answer"]
    assert answer["mode"] == "hand_off" and "shop as shop.revenue" in answer["answer"]


def test_a_typed_lens_defines_from_its_pages_and_serves_a_sql_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    _scripted_typed_provider(monkeypatch, [gen, "Some customers."])
    org_id, raw = _typed_org()
    try:
        auth = {"Authorization": f"Bearer {raw}"}
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "what does repeat customer mean?"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == "ok" and out["mode"] == "definition"
        assert "number_of_orders > 1" in out["answer"]
        assert FailureMode(out["mode"]) is FailureMode.definition
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "give me the SQL for how many customers"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == "ok" and out["mode"] == "sql_request", out
        assert out["answer"].upper().startswith("SQL:\nSELECT COUNT(*) AS N FROM CUSTOMERS")
        row = _row(out["request_id"])
        assert row is not None and row[2] == "give me the SQL for how many customers"
    finally:
        _drop(org_id)
