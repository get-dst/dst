"""The lens doors on ``serving: cube``: the question door runs the frontier
lane and the checks, the structured door takes a Cube query (or today's
intent, translated member for member), and a lens with no Cube to compile
answers 503 naming it. Cube is the fake; the model is scripted."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import query as query_api
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.cube.client import CubeConfig
from services.db.session import org_session
from services.lenses import store
from services.lenses.demo import jaffle_customer_value_bundle
from services.llm.registry import ResolvedModel
from services.serve import door
from tests import _cube_fake as fake
from tests.test_cube_frontier import Scripted

client = TestClient(app)
LENS = "customer_value"


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
def org() -> Iterator[tuple[object, str]]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org_id = c.execute(
            text("INSERT INTO org (name) VALUES ('Cube door') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org_id, "h": hash_token(raw)},
        )
    with org_session(org_id) as session:
        store.create_lens(session, jaffle_customer_value_bundle())
        store.publish(session, LENS)
    yield org_id, raw
    with admin.begin() as c:
        for table in ("request_log", "audit_log", "admin_token"):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org_id})  # noqa: S608
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org_id})


@pytest.fixture()
def cube(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> fake.FakeCube:
    """The cube lane wired to the fake Cube, the fake world and a project
    holding the overlay and the probe file."""
    root = tmp_path / "project"
    (root / "cube").mkdir(parents=True)
    (root / "profiles").mkdir()
    (root / "cube" / "overlay.yaml").write_text(yaml.safe_dump(fake.OVERLAY))
    probe, freshness = fake.monitor_files()
    (root / "profiles" / "cube.probe.json").write_text(json.dumps(probe))
    (root / "profiles" / "cube.freshness.json").write_text(json.dumps(freshness))
    world = fake.world(tmp_path / "world.duckdb")
    handler = fake.FakeCube()
    monkeypatch.setattr(settings, "serving", "cube")
    monkeypatch.setattr(door, "project_root", lambda: root)
    monkeypatch.setattr(door, "configured", lambda: CubeConfig("http://cube.test", fake.SECRET))
    monkeypatch.setattr(
        door.CubeClient, "from_config", classmethod(lambda cls, cfg: fake.client(handler))
    )
    monkeypatch.setattr(
        query_api,
        "_authorized_connector",
        lambda bundle, caller: DuckDBConnector(str(world), profile=False),
    )
    return handler


def _post(raw: str, path: str, body: dict[str, Any]) -> Any:
    return client.post(
        f"/v1/lenses/{LENS}/{path}", json=body, headers={"Authorization": f"Bearer {raw}"}
    )


def test_the_question_door_answers_through_the_cube_lane(
    org: tuple[object, str], cube: fake.FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = {
        "answer": "query",
        "queries": [
            {
                "measures": ["orders.revenue"],
                "filters": [
                    {"member": "orders.company", "operator": "equals", "values": ["South AB"]}
                ],
                "timeDimensions": [
                    {"dimension": "orders.ordered_at", "dateRange": ["2026-08-01", "2026-08-31"]}
                ],
            }
        ],
        "note": "South AB webshop revenue in August",
        "ledger": [
            {"word": "south", "to": "filter", "ref": "South AB"},
            {"word": "revenue", "to": "member", "ref": "orders.revenue"},
            {"word": "August", "to": "period", "ref": "2026-08"},
        ],
    }
    llm = Scripted(json.dumps(reply))
    monkeypatch.setattr(
        door, "frontier_model", lambda config: ResolvedModel(llm, "scripted", "fast-1")
    )
    _, raw = org
    r = _post(raw, "query", {"q": "south revenue in August", "format": "structured"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok", body
    assert body["data"]["rows"] == [[510.0]]
    assert body["receipt"]["query_sha256"] and body["receipt"]["result_sha256"]
    assert "words:pass" in body["receipt"]["checks"]
    assert len(llm.calls) == 1 and cube.compiled


class _Recording(Scripted):
    """A provider that reports its usage the way the real clients do,
    prompt-cache writes included."""

    def complete(self, **kw: Any) -> Any:
        from services.observability import usage as ai_usage

        out = super().complete(**kw)
        ai_usage.record(
            "anthropic",
            kw["model"],
            out.input_tokens,
            out.output_tokens,
            cache_write_tokens=24_000,
            cache_write_long_tokens=24_000,
        )
        return out


def test_a_cube_lane_answer_s_row_carries_the_cost_its_calls_billed(
    org: tuple[object, str], cube: fake.FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = {
        "answer": "query",
        "queries": [{"measures": ["orders.count"], "dimensions": ["orders.status"]}],
        "ledger": [
            {"word": "orders", "to": "member", "ref": "orders.count"},
            {"word": "status", "to": "member", "ref": "orders.status"},
        ],
    }
    llm = _Recording(json.dumps(reply))
    monkeypatch.setattr(
        door,
        "frontier_model",
        lambda config: ResolvedModel(llm, "anthropic", "claude-haiku-4-5"),
    )
    org_id, raw = org
    r = _post(raw, "query", {"q": "orders by status", "format": "structured"})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    rid = r.json()["request_id"]
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(org_id)})
        row = c.execute(
            text("SELECT ai_input_tokens, ai_cost_usd FROM request_log WHERE request_id = :r"),
            {"r": rid},
        ).one()
        billed = c.execute(
            text(
                "SELECT SUM(cost_usd) FROM ai_call WHERE request_id = :r AND purpose = 'generate'"
            ),
            {"r": rid},
        ).scalar_one()
    assert row.ai_input_tokens == 1200
    assert row.ai_cost_usd is not None and billed is not None
    assert float(row.ai_cost_usd) == pytest.approx(float(billed))  # observe and ai_call agree


def test_the_question_door_refuses_before_any_model_call(
    org: tuple[object, str], cube: fake.FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = Scripted("{}")
    monkeypatch.setattr(
        door, "frontier_model", lambda config: ResolvedModel(llm, "scripted", "fast-1")
    )
    # The overlay's not_for entries are the lens's own; this lens is not "shop",
    # so declare it on the lens through the overlay under its name.
    overlay = {**fake.OVERLAY, "lenses": {LENS: fake.OVERLAY["lenses"]["shop"]}}
    (door.project_root() / "cube" / "overlay.yaml").write_text(yaml.safe_dump(overlay))
    _, raw = org
    r = _post(raw, "query", {"q": "what is the customer LTV", "format": "structured"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "refused" and r.json()["mode"] == "not_declared"
    assert llm.calls == []


def test_the_structured_door_takes_a_cube_query(
    org: tuple[object, str], cube: fake.FakeCube
) -> None:
    _, raw = org
    r = _post(
        raw,
        "metrics",
        {
            "cube_query": {
                "measures": ["orders.count"],
                "dimensions": ["orders.status"],
                "order": {"orders.count": "desc"},
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok", body
    assert body["data"]["columns"] == ["orders.status", "orders.count"]
    assert body["data"]["rows"][0] == ["Paid", 6]


def test_the_structured_door_translates_an_intent_on_a_cube_lens(
    org: tuple[object, str], cube: fake.FakeCube
) -> None:
    _, raw = org
    r = _post(raw, "metrics", {"entity": "shop", "metrics": ["count"], "dimensions": ["status"]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok", r.json()
    assert cube.compiled[-1]["measures"] == ["shop.count"]


def test_a_cube_lens_with_no_cube_answers_503(
    org: tuple[object, str], cube: fake.FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(door, "configured", lambda: None)
    _, raw = org
    r = _post(raw, "metrics", {"cube_query": {"measures": ["orders.count"]}})
    assert r.status_code == 503 and "Cube" in r.json()["detail"]
