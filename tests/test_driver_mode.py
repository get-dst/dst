"""Driver mode: an agent caller gets no model call from dst, and every check.

The whole driver flow (route_query → describe_lens → query_cube, and the
explore route → sql) runs against the fake Cube and the fake world with no
provider configured at all, and leaves no ai_call row. A provider call made
anyway would raise (the no-model tripwire), so a green run is the proof. The
HTTP door without driver mode still reads the question with the frontier lane.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import driver as driver_api
from services.api import query as query_api
from services.api import route as route_api
from services.api import sql as sql_api
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.lens_config import LensConfig
from services.contracts.lens_model import LensModel, Overlay
from services.contracts.semantic_model import CubeStamp, Entity, SemanticModel
from services.cube.client import CubeConfig
from services.db.session import org_session
from services.governance import credentials
from services.lenses import store
from services.lenses.store import LensBundle
from services.llm import registry
from services.llm.registry import ResolvedModel
from services.observability import usage as ai_usage
from services.serve import door
from services.serve.driver import retry
from tests import _cube_fake as fake
from tests.test_cube_frontier import Scripted

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

DRIVER = {"X-DST-Driver": "1"}


STAFF_TABLE = {
    "name": "staff_monthly",
    "source": {"connection": "wh", "table": "hr.staff_monthly"},
    "exposed": True,
    "fields": [
        {"name": "month", "type": "date"},
        {"name": "company", "type": "string"},
        {"name": "headcount", "type": "number"},
    ],
}


def _shop() -> LensBundle:
    """The shop lens served through Cube, with the LensModel apply stores,
    and one exposed table that governs nothing."""
    model = LensModel.build(fake.doc(), Overlay.model_validate(fake.OVERLAY), "shop")
    stamp = CubeStamp(
        model_version=model.model_version,
        overlay_version=model.overlay_version,
        stamped_at="2026-09-01T00:00:00+00:00",
        views=["shop"],
        lens_model=model.model_dump(mode="json"),
    )
    return LensBundle(
        config=LensConfig(name="shop", connections=["wh"], serving="cube"),
        semantic_model=SemanticModel.model_validate(
            {
                "lens": "shop",
                "dialect": "duckdb",
                "entities": [STAFF_TABLE],
                "cube": stamp.model_dump(mode="json"),
            }
        ),
    )


@pytest.fixture()
def org() -> Iterator[tuple[object, str]]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org_id = c.execute(
            text("INSERT INTO org (name) VALUES ('Driver mode') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org_id, "h": hash_token(raw)},
        )
    with org_session(org_id) as session:
        for bundle in (_shop(),):
            store.create_lens(session, bundle)
            store.publish(session, bundle.config.name)
    yield org_id, raw
    with admin.begin() as c:
        for table in (
            "ai_call",
            "routing_decision",
            "request_log",
            "audit_log",
            "admin_token",
            "api_key",
            "caller",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id})  # noqa: S608
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org_id})


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> fake.FakeCube:
    """The fake Cube, the fake world, a project holding the overlay and the
    monitor's files, and NO provider: no key, no embedder."""
    root = tmp_path / "project"
    (root / "cube").mkdir(parents=True)
    (root / "profiles").mkdir()
    (root / "cube" / "overlay.yaml").write_text(yaml.safe_dump(fake.OVERLAY))
    probe, freshness = fake.monitor_files()
    (root / "profiles" / "cube.probe.json").write_text(json.dumps(probe))
    (root / "profiles" / "cube.freshness.json").write_text(json.dumps(freshness))
    (root / "lenses" / "shop").mkdir(parents=True)
    (root / "lenses" / "shop" / "certified_answers.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "question": "orders by status",
                    "query": {"measures": ["shop.count"], "dimensions": ["shop.status"]},
                }
            ]
        )
    )
    path = fake.world(tmp_path / "world.duckdb")
    handler = fake.FakeCube()
    monkeypatch.setattr(registry, "specs", lambda: {})
    monkeypatch.setattr(registry, "resolve_embedder", lambda: None)
    monkeypatch.setattr(door, "project_root", lambda: root)
    monkeypatch.setattr(door, "configured", lambda: CubeConfig("http://cube.test", fake.SECRET))
    monkeypatch.setattr(
        door.CubeClient, "from_config", classmethod(lambda cls, cfg: fake.client(handler))
    )

    def connector(bundle: Any, caller: Any) -> DuckDBConnector:
        return DuckDBConnector(str(path), profile=False)

    for module in (query_api, driver_api, sql_api):
        monkeypatch.setattr(module, "_authorized_connector", connector)
    monkeypatch.setattr(route_api, "_cube_probe", lambda: door.load_probe(root))
    return handler


def _call(raw: str, method: str, path: str, **kw: Any) -> Any:
    headers = {"Authorization": f"Bearer {raw}", **DRIVER, **kw.pop("headers", {})}
    return client.request(method, path, headers=headers, **kw)


def _ai_calls(org_id: object) -> int:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org_id)})
        return int(
            c.execute(
                text("SELECT count(*) FROM ai_call WHERE org_id = :o"), {"o": org_id}
            ).scalar_one()
        )


def test_the_whole_driver_flow_runs_with_no_provider_and_no_ai_call(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    org_id, raw = org
    assert registry.resolve(registry.tier("fast")) is None  # nothing to call, by setup

    # 1. route: coverage only, the members and values each word matched.
    r = _call(raw, "POST", "/v1/query", json={"q": "paid orders by status"})
    assert r.status_code == 200, r.text
    route = r.json()
    assert route["driver"] is True and route["covered"] is True, route
    assert [c["lens"] for c in route["candidates"]] == ["shop"]
    shop = route["candidates"][0]
    assert shop["serving"] == "cube" and shop["no_model"] is True
    assert route["no_model"] == {"shop": True}
    matched = {m["word"]: m["targets"] for m in shop["matched"]}
    assert "shop.status = Paid" in matched["paid"]
    assert "shop.status" in matched["status"]

    # 2. describe: the view in full, values, data ends, refusals, certified pairs.
    r = _call(raw, "GET", "/v1/lenses/shop")
    assert r.status_code == 200, r.text
    lens = r.json()
    assert lens["serving"] == "cube" and lens["views"][0]["detail"] == "full"
    members = {m["name"]: m for m in lens["views"][0]["members"]}
    assert members["shop.status"]["values"] == ["Open", "Paid", "Refunded"]
    assert members["shop.revenue"]["clock"] == "orders.ordered_at"
    assert members["shop.ordered_at"]["data_end"] == "2026-08-31"
    assert lens["freshness"]["clock_end"] == "2026-08-31"
    assert lens["freshness"]["partial_periods"]["orders.count"]["period"] == "2026-08"
    assert any(e["measure"] == "lifetime value" for e in lens["not_computable"])
    assert lens["certified"][0]["question"] == "orders by status"

    # 3. the driver's own query, every check, rows and a receipt.
    body = {
        "query": {
            "measures": ["shop.count"],
            "dimensions": ["shop.status"],
            "filters": [{"member": "shop.status", "operator": "equals", "values": ["Paid"]}],
        },
        "ledger": [
            {"word": "paid", "to": "filter", "ref": "Paid"},
            {"word": "orders", "to": "member", "ref": "shop.count"},
            {"word": "status", "to": "member", "ref": "shop.status"},
        ],
        "question": "paid orders by status",
    }
    r = _call(raw, "POST", "/v1/lenses/shop/cube", json=body)
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["status"] == "ok", answer
    assert answer["data"]["rows"] == [["Paid", 6]]
    assert "words:pass" in answer["receipt"]["checks"]
    assert answer["retry"] is None
    assert world.compiled  # Cube compiled it; dst ran it

    # 4. values and data_end, from the probe and the monitor.
    r = _call(raw, "GET", "/v1/lenses/shop/values", params={"member": "shop.status", "prefix": "p"})
    assert r.status_code == 200 and r.json()["values"] == ["Paid"], r.text
    r = _call(raw, "GET", "/v1/lenses/shop/data_end", params={"member": "shop.revenue"})
    assert r.status_code == 200, r.text
    assert r.json()["clock"] == "orders.ordered_at" and r.json()["data_end"] == "2026-08-31"

    # 5. a question no lens covers: the explore kit, then sql over the exposed table.
    r = _call(raw, "POST", "/v1/query", json={"q": "how many shipments went out"})
    assert r.status_code == 200, r.text
    route = r.json()
    assert route["covered"] is False and route["mode"] == "unroutable", route
    kit = route["explore_kit"]
    assert [t["table"] for t in kit["tables"]] == ["hr.staff_monthly"]
    assert "no blueprint" in kit["tables"][0]["flags"] and kit["tools"]
    r = _call(
        raw,
        "POST",
        "/v1/sql",
        json={"lens": "shop", "sql": "SELECT company, headcount FROM hr.staff_monthly"},
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert rows["row_count"] == 3
    assert any(c.startswith("grain:exposed, could not verify") for c in rows["checks"])
    assert any(c.startswith("data_end:exposed, could not verify") for c in rows["checks"])
    assert rows["receipt"]["checks"] == rows["checks"]
    assert rows["receipt"]["confidence"] == "unverified"
    r = _call(raw, "GET", "/v1/grain", params={"table": "hr.staff_monthly"})
    assert r.status_code == 200, r.text
    assert r.json()["lens"] == "shop" and r.json()["grain"]

    assert _ai_calls(org_id) == 0


def test_a_failing_check_returns_the_mode_and_what_to_change(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    _, raw = org
    body = {
        "query": {"measures": ["shop.count"]},
        "ledger": [{"word": "orders", "to": "member", "ref": "shop.count"}],
        "question": "paid orders",
    }
    r = _call(raw, "POST", "/v1/lenses/shop/cube", json=body)
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["status"] == "clarification" and answer["mode"] == "uncovered_word"
    assert answer["retry"]["check"] == "words"
    assert "ledger" in answer["retry"]["change"]
    assert "'paid'" in answer["retry"]["detail"]
    # A member outside the lens's views is refused, naming the views.
    body = {"query": {"measures": ["staff.headcount"]}, "ledger": []}
    r = _call(raw, "POST", "/v1/lenses/shop/cube", json=body)
    assert r.json()["status"] == "refused" and r.json()["retry"]["check"] == "scope"


def test_without_the_question_the_word_checks_report_untested(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    _, raw = org
    body = {"query": {"measures": ["shop.count"], "dimensions": ["shop.status"]}, "ledger": []}
    r = _call(raw, "POST", "/v1/lenses/shop/cube", json=body)
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    checks = r.json()["receipt"]["checks"]
    assert "words:skip" in checks and "unasked_scope:skip" in checks
    reasons = [c["reason"] for c in r.json()["verification"]["checks"] if c["name"] == "words"]
    assert reasons and reasons[0].startswith("UNTESTED")


def test_a_driver_question_on_a_cube_lens_is_sent_to_query_cube(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    _, raw = org
    r = _call(raw, "POST", "/v1/lenses/shop/query", json={"q": "orders by status"})
    assert r.status_code == 400 and "query_cube" in r.json()["detail"]


def test_the_http_door_without_a_driver_still_reads_the_question_with_the_frontier_lane(
    org: tuple[object, str], world: fake.FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    _, raw = org
    r = client.post(
        "/v1/lenses/shop/query",
        json={"q": "orders by status", "format": "structured"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok", r.json()
    assert len(llm.calls) == 1  # the frontier lane read it


def test_a_caller_an_admin_marks_a_driver_is_one_without_the_header(
    org: tuple[object, str],
) -> None:
    org_id, raw = org
    admin = {"Authorization": f"Bearer {raw}"}
    assert client.post("/mgmt/callers", json={"name": "agent"}, headers=admin).status_code == 201
    r = client.put("/mgmt/callers/agent/driver", json={"driver": True}, headers=admin)
    assert r.status_code == 200 and r.json() == {"name": "agent", "driver": True}
    key = client.post("/mgmt/callers/agent/keys", headers=admin).json()["key"]
    ident = credentials.verify_caller_key(key)
    assert ident is not None and ident.driver
    listed = client.get("/mgmt/callers", headers=admin).json()
    assert [c["driver"] for c in listed if c["name"] == "agent"] == [True]
    assert (
        client.put("/mgmt/callers/nobody/driver", json={"driver": True}, headers=admin).status_code
        == 404
    )


def test_a_model_call_in_driver_mode_fails_loud() -> None:
    with ai_usage.no_models(), pytest.raises(ai_usage.ModelCallInDriverMode):
        ai_usage.record("anthropic", "claude-haiku-4-5", 10, 10)
    with ai_usage.no_models():
        assert registry.resolve_decider() is None
        assert registry.resolve_embedder() is None


def test_retry_names_the_stopping_check() -> None:
    from services.contracts.failure_modes import FailureMode
    from services.contracts.response import QueryResponse
    from services.contracts.verification import VerificationCheck, VerificationReport

    resp = QueryResponse(
        lens="shop",
        status="refused",
        answer="shop.x is withheld from this caller",
        mode=FailureMode.hidden_column,
        request_id="r",
        verification=VerificationReport(
            grade="unverified",
            checks=[
                VerificationCheck(name="scope", status="pass"),
                VerificationCheck(name="hidden", status="fail", reason="withheld"),
            ],
        ),
    )
    hint = retry(resp)
    assert hint is not None and hint.check == "hidden" and "drop" in hint.change


def test_a_declared_refusal_is_the_route_s_answer(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    _, raw = org
    # The overlay declares lifetime value (ltv) not computable on shop.
    r = _call(raw, "POST", "/v1/query", json={"q": "customer ltv"})
    route = r.json()
    assert route["covered"] is False and route["candidates"] == [], route
    assert route["refusal"]["status"] == "refused" and route["mode"] == "not_declared"
    assert route["explore_kit"] is None
    # An ask the lens says is another lens's, that lens absent: its sentence.
    r = _call(raw, "POST", "/v1/query", json={"q": "contract churn by month"})
    route = r.json()
    assert route["mode"] == "misrouted" and "ask service" in route["refusal"]["answer"], route


def test_lenses_covering_a_question_equally_are_returned_for_the_driver_to_choose(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    org_id, raw = org
    twin = _shop()
    twin.config = twin.config.model_copy(update={"name": "shop_twin"})
    twin.semantic_model = twin.semantic_model.model_copy(update={"lens": "shop_twin"})
    with org_session(org_id) as session:
        store.create_lens(session, twin)
        store.publish(session, "shop_twin")
    r = _call(raw, "POST", "/v1/query", json={"q": "orders by status"})
    route = r.json()
    assert route["choose"] is True, route
    assert [c["lens"] for c in route["candidates"]] == ["shop", "shop_twin"]


# ── typed lenses: driver mode changes nothing ───────────────────────────────


def _typed_org() -> tuple[object, str]:
    from services.lenses.demo import jaffle_customer_value_bundle

    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org_id = c.execute(
            text("INSERT INTO org (name) VALUES ('Typed') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org_id, "h": hash_token(raw)},
        )
    with org_session(org_id) as session:
        store.create_lens(session, jaffle_customer_value_bundle())
        store.publish(session, "customer_value")
    return org_id, raw


def _drop(org_id: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        for table in ("ai_call", "routing_decision", "request_log", "audit_log", "admin_token"):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id})  # noqa: S608
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org_id})


def _scripted_typed_provider(monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> None:
    from services.contracts.fakes import ScriptedLLM, fake_llm_providers
    from services.runtime.generator import GroundedSQLGenerator

    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0, **_: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: ScriptedLLM(list(replies)),
    )


def _same(a: dict[str, Any], b: dict[str, Any]) -> None:
    for key in ("status", "answer", "sql", "confidence", "certification", "mode"):
        assert a.get(key) == b.get(key), key
    assert (a.get("data") or {}).get("rows") == (b.get("data") or {}).get("rows")


def test_a_typed_lens_query_with_the_driver_header_answers_as_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    _scripted_typed_provider(monkeypatch, [gen, "Some customers."] * 2)
    org_id, raw = _typed_org()
    try:
        auth = {"Authorization": f"Bearer {raw}"}
        q = {"q": "how many customers?"}
        plain = client.post("/v1/lenses/customer_value/query", json=q, headers=auth)
        driven = client.post("/v1/lenses/customer_value/query", json=q, headers={**auth, **DRIVER})
        assert plain.status_code == driven.status_code == 200, driven.text
        assert driven.json()["status"] == "ok" and driven.json()["answer"] == "Some customers."
        _same(plain.json(), driven.json())
    finally:
        _drop(org_id)


def test_route_query_over_typed_lenses_ignores_the_driver_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.router import RouteDecision

    gen = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    _scripted_typed_provider(monkeypatch, [gen, "Some customers."] * 2)
    asked: list[str] = []

    def routed(caller: Any, q: str) -> RouteDecision:
        asked.append(q)  # today's router, decider and all, is what ran
        return RouteDecision(True, "customer_value", 0.8, [], decided_by="decider")

    monkeypatch.setattr(route_api, "_route", routed)
    org_id, raw = _typed_org()
    try:
        auth = {"Authorization": f"Bearer {raw}"}
        q = {"q": "how many customers?"}
        plain = client.post("/v1/query", json=q, headers=auth).json()
        driven = client.post("/v1/query", json=q, headers={**auth, **DRIVER}).json()
        assert "driver" not in driven and driven["routed_to"] == plain["routed_to"]
        _same(plain["answer"], driven["answer"])
        assert len(asked) == 2
    finally:
        _drop(org_id)


def test_a_mixed_catalogue_routes_cube_lenses_by_coverage_and_typed_lenses_as_today(
    org: tuple[object, str], world: fake.FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.lenses.demo import jaffle_customer_value_bundle
    from services.router import RouteDecision

    org_id, raw = org
    with org_session(org_id) as session:
        store.create_lens(session, jaffle_customer_value_bundle())
        store.publish(session, "customer_value")
    excluded: list[frozenset[str]] = []

    def routed(caller: Any, q: str, exclude: frozenset[str] = frozenset()) -> RouteDecision:
        excluded.append(exclude)
        return RouteDecision(False, None, 0.0, [], "no governed lens covers this")

    monkeypatch.setattr(route_api, "_route", routed)
    # A question the Cube lens covers: coverage, no model, no typed routing.
    route = _call(raw, "POST", "/v1/query", json={"q": "paid orders by status"}).json()
    assert [c["lens"] for c in route["candidates"]] == ["shop"], route
    assert route["no_model"] == {"customer_value": False, "shop": True}
    assert excluded == []
    # One it does not: the typed lenses route it as today, Cube lenses left out.
    route = _call(raw, "POST", "/v1/query", json={"q": "how many shipments went out"}).json()
    assert route["routed"]["covered"] is False, route
    assert route["no_model"] == {"customer_value": False, "shop": True}
    assert excluded == [frozenset({"shop"})]


def test_query_cube_carries_the_overlay_caveats_on_what_it_read(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    _, raw = org
    root = door.project_root()
    overlay = {
        **fake.OVERLAY,
        "cubes": {
            **fake.OVERLAY["cubes"],
            "orders": {"member_caveats": {"count": ["allocated: one order counted per line"]}},
        },
    }
    (root / "cube" / "overlay.yaml").write_text(yaml.safe_dump(overlay))
    body = {
        "query": {"measures": ["shop.count"], "dimensions": ["shop.status"]},
        "ledger": [
            {"word": "orders", "to": "member", "ref": "shop.count"},
            {"word": "status", "to": "member", "ref": "shop.status"},
        ],
        "question": "orders by status",
    }
    r = _call(raw, "POST", "/v1/lenses/shop/cube", json=body)
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["status"] == "ok", answer
    assert answer["caveats"] == ["orders.count: allocated: one order counted per line"]
    assert answer["answer"].startswith("CAVEAT orders.count: allocated")
    assert answer["receipt"]["caveats"] == answer["caveats"]


def test_run_certified_serves_the_approved_rows_to_a_driver_with_no_model(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    from services.certify import store as certify_store

    org_id, raw = org
    with org_session(org_id) as session:
        cert_id = certify_store.create(
            session,
            "shop",
            "headcount by company",
            "SELECT company, headcount FROM hr.staff_monthly",
            None,
            "maija",
        )
    try:
        # the default format asks for prose; a driver gets the rows, no composer
        r = _call(raw, "POST", f"/v1/lenses/shop/certified/{cert_id}/run", json={})
        assert r.status_code == 200, r.text
        answer = r.json()
        assert answer["certification"] == "certified"
        assert answer["data"]["row_count"] == 3
        assert _ai_calls(org_id) == 0
    finally:
        admin = create_engine(settings.database_admin_url)
        with admin.begin() as c:
            c.execute(text("DELETE FROM certified_answer WHERE org_id = :o"), {"o": org_id})


def _lens_over(name: str, cube: str, measure: str, title: str, description: str) -> LensBundle:
    """A one-view lens over one invented cube, stamped as apply stores it."""
    from services.contracts.cube_meta import MetaDoc

    view = f"{name}_view"
    meta = MetaDoc.parse(
        {
            "cubes": [
                {
                    "name": cube,
                    "type": "cube",
                    "measures": [
                        {
                            "name": f"{cube}.{measure}",
                            "type": "number",
                            "aggType": "sum",
                            "title": title,
                            "description": description,
                        }
                    ],
                    "dimensions": [],
                    "segments": [],
                },
                {
                    "name": view,
                    "type": "view",
                    "measures": [
                        {
                            "name": f"{view}.{measure}",
                            "type": "number",
                            "aggType": "sum",
                            "aliasMember": f"{cube}.{measure}",
                        }
                    ],
                    "dimensions": [],
                    "segments": [],
                },
            ]
        }
    )
    model = LensModel.build(meta, Overlay(), name, views=[view])
    stamp = CubeStamp(
        model_version=model.model_version,
        overlay_version=model.overlay_version,
        stamped_at="2026-09-01T00:00:00+00:00",
        views=[view],
        lens_model=model.model_dump(mode="json"),
    )
    return LensBundle(
        config=LensConfig(name=name, connections=["wh"], serving="cube", views=[view]),
        semantic_model=SemanticModel.model_validate(
            {"lens": name, "dialect": "duckdb", "cube": stamp.model_dump(mode="json")}
        ),
    )


def test_route_ranks_lenses_by_the_words_they_name_not_their_prose(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    org_id, raw = org
    lenses = [
        # holds 'tracking' as a name, 'budget' only in prose: it covers
        _lens_over(
            "health",
            "health",
            "customers_tracked",
            "customers tracked",
            "customers we follow, each against their budget tier",
        ),
        # names 'budget', lacks 'tracking': as many words named, so a candidate
        _lens_over("finance", "plan", "budget_eur", "board budget", "the approved plan"),
        # holds both words in prose only: covers, names none
        _lens_over(
            "notes", "memo", "memo_count", "memos", "notes on tracking spend against budget"
        ),
    ]
    with org_session(org_id) as session:
        for b in lenses:
            store.create_lens(session, b)
            store.publish(session, b.config.name)
    r = _call(raw, "POST", "/v1/query", json={"q": "how are we tracking against budget"})
    assert r.status_code == 200, r.text
    route = r.json()
    got = {c["lens"]: c for c in route["candidates"]}
    assert set(got) == {"health", "finance"}, route
    assert got["health"]["score"] == 1 and got["finance"]["score"] == 1
    assert got["finance"]["uncovered"] == ["tracking"]
    assert route["choose"] is True


def test_run_certified_on_a_cube_lens_serves_through_the_pair_s_cube_query(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    from services.certify import store as certify_store

    org_id, raw = org
    root = door.project_root()
    fixed_sql = "SELECT status, COUNT(*) AS n FROM shop.orders GROUP BY 1"
    template_sql = "SELECT COUNT(*) AS n FROM shop.orders WHERE ordered_at >= {period.start}"
    (root / "lenses" / "shop" / "certified_answers.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "question": "orders by status",
                    "sql": fixed_sql,
                    "query": {"measures": ["shop.count"], "dimensions": ["shop.status"]},
                },
                {
                    "question": "orders in {period}",
                    "sql": template_sql,
                    "slots": {"period": {"type": "date_range"}},
                    "sample_bindings": [{"period": "2026-08"}],
                    "query": {
                        "measures": ["shop.count"],
                        "timeDimensions": [
                            {"dimension": "shop.ordered_at", "dateRange": "{period}"}
                        ],
                    },
                },
            ]
        )
    )
    with org_session(org_id) as session:
        fixed = certify_store.create(session, "shop", "orders by status", fixed_sql, None, "maija")
        template = certify_store.create(
            session,
            "shop",
            "orders in {period}",
            template_sql,
            None,
            "maija",
            slots={"period": {"type": "date_range"}},
            sample_bindings=[{"period": "2026-08"}],
        )
    try:
        r = _call(raw, "POST", f"/v1/lenses/shop/certified/{fixed}/run", json={})
        assert r.status_code == 200, r.text
        answer = r.json()
        assert answer["certification"] == "certified" and answer["certified_match"] == "exact"
        assert answer["receipt"]["certification"] == "certified"
        assert answer["receipt"]["cert_id"] == fixed
        assert "window:pass" in answer["receipt"]["checks"]  # every lane check ran
        assert world.compiled  # Cube compiled the pair's query; dst ran it
        statuses = {row[0] for row in answer["data"]["rows"]}
        assert "Paid" in statuses

        r = _call(
            raw,
            "POST",
            f"/v1/lenses/shop/certified/{template}/run",
            json={"bindings": {"period": "2026-08"}},
        )
        assert r.status_code == 200, r.text
        answer = r.json()
        assert answer["certified_match"] == "parameterized"
        assert answer["certified_provenance"]["bound_values"] == {"period": "2026-08"}
        assert "2026-08-01" in answer["sql"] and "2026-08-31" in answer["sql"]
        assert _ai_calls(org_id) == 0
    finally:
        admin = create_engine(settings.database_admin_url)
        with admin.begin() as c:
            c.execute(text("DELETE FROM certified_answer WHERE org_id = :o"), {"o": org_id})


def test_a_tie_at_the_cut_keeps_every_lens_tied(
    org: tuple[object, str], world: fake.FakeCube
) -> None:
    """Four lenses name every word of the question alike: the score is a
    tie, and the lens a tie-break by name would have cut is still offered."""
    org_id, raw = org
    lenses = [
        _lens_over("accounts", "ledger", "customer_invoiced_orders", "invoiced", "x"),
        _lens_over("billing", "bills", "customer_invoice_orders", "billed", "x"),
        _lens_over("crew", "jobs", "invoiced_customer_work_orders", "work", "x"),
        _lens_over("storefront", "orders", "invoice_customer_orders", "sold", "x"),
    ]
    with org_session(org_id) as session:
        for b in lenses:
            store.create_lens(session, b)
            store.publish(session, b.config.name)
    r = _call(raw, "POST", "/v1/query", json={"q": "invoice customer orders this week"})
    assert r.status_code == 200, r.text
    route = r.json()
    got = {c["lens"]: c["score"] for c in route["candidates"]}
    # the fixture's own shop lens may tie too; the invented four all do
    assert {n: got.get(n) for n in ("accounts", "billing", "crew", "storefront")} == {
        "accounts": 3,
        "billing": 3,
        "crew": 3,
        "storefront": 3,
    }, route


def test_a_covered_ask_also_sees_an_exposed_table_holding_it(
    org: tuple[object, str], world: fake.FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lens covering 'quota' names one word of the question; the exposed
    table carries two: the kit is offered beside the candidates."""
    org_id, raw = org
    monkeypatch.setattr(
        "services.runtime.assembly.profile_facts",
        lambda bundle, org: ([], None, None, {}),
    )
    lens = _lens_over("sales", "deals", "quota_eur", "quota", "the sales plan")
    exposed = {
        "name": "rep_quotas",
        "source": {"connection": "wh", "table": "crm.rep_quotas"},
        "exposed": True,
        "fields": [{"name": c, "type": "string"} for c in ("rep", "quota_eur", "behind_by")],
    }
    lens.semantic_model.entities.append(Entity.model_validate(exposed))
    with org_session(org_id) as session:
        store.create_lens(session, lens)
        store.publish(session, "sales")
    r = _call(raw, "POST", "/v1/query", json={"q": "which reps are behind on quota"})
    assert r.status_code == 200, r.text
    route = r.json()
    assert "sales" in [c["lens"] for c in route["candidates"]], route
    kit = route["explore_kit"]
    assert kit is not None and [t["table"] for t in kit["tables"]] == ["crm.rep_quotas"]
