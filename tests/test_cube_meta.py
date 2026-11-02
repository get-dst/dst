"""The Cube meta document as dst reads it (the closed ``meta.dst`` overlay,
model_version, view alias resolution), the ``cube:`` config, ``dst cube
status`` and the ``/ready`` row. A live lane at the end runs against a real Cube
when ``DST_TEST_CUBE_URL`` names one."""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.config import settings
from services.contracts.cube_meta import MetaDoc, view_members
from services.contracts.cube_query import CubeQuery, from_intent
from services.contracts.query_intent import QueryIntent
from services.cube import client as cube_client
from services.cube.client import CubeClient
from services.cube.context import CubeContext
from services.project.schema import parse_project_yaml
from tests.test_cube_client import META, SECRET, FakeCube

# ── the overlay ───────────────────────────────────────────────────────────


def test_meta_dst_is_parsed_into_typed_overlays() -> None:
    doc = MetaDoc.parse(META)
    assert doc.overlay_errors == []
    orders = doc.cube("orders")
    assert orders is not None and orders.dst is not None
    assert orders.dst.grain == "one row per order" and orders.dst.key == ["id"]
    revenue = doc.member("orders.revenue")
    assert revenue is not None and revenue.dst is not None
    assert revenue.dst.additivity == "flow" and revenue.dst.aliases == ["sales"]  # type: ignore[union-attr]
    status = doc.member("orders.status")
    assert status is not None and status.dst.dictionary is True  # type: ignore[union-attr]
    paid = doc.member("orders.paid")
    assert paid is not None and paid.dst.meaning == "orders already paid"  # type: ignore[union-attr]


def test_the_free_form_meta_is_kept_verbatim() -> None:
    doc = MetaDoc.parse(META)
    revenue = doc.member("orders.revenue")
    assert revenue is not None and revenue.meta == META["cubes"][0]["measures"][1]["meta"]


def test_an_unknown_meta_dst_key_is_an_error_naming_member_and_key() -> None:
    meta = copy.deepcopy(META)
    meta["cubes"][0]["measures"][1]["meta"]["dst"]["bogus_key"] = 1
    meta["cubes"][0]["meta"]["dst"]["colour"] = "red"
    meta["cubes"][0]["dimensions"][0]["meta"]["dst"] = {"dictionary": "maybe"}
    doc = MetaDoc.parse(meta)
    assert "orders.revenue: meta.dst unknown key 'bogus_key'" in doc.overlay_errors
    assert "orders: meta.dst unknown key 'colour'" in doc.overlay_errors
    assert any(e.startswith("orders.status: meta.dst.dictionary:") for e in doc.overlay_errors)
    assert doc.member("orders.revenue").dst is None  # type: ignore[union-attr]


def test_meta_dst_must_be_a_mapping() -> None:
    meta = copy.deepcopy(META)
    meta["cubes"][1]["dimensions"][0]["meta"] = {"dst": ["aliases"]}
    assert MetaDoc.parse(meta).overlay_errors == ["customers.country: meta.dst must be a mapping"]


def test_a_measure_key_on_a_dimension_is_refused() -> None:
    meta = copy.deepcopy(META)
    meta["cubes"][0]["dimensions"][0]["meta"]["dst"] = {"additivity": "level"}
    assert MetaDoc.parse(meta).overlay_errors == [
        "orders.status: meta.dst unknown key 'additivity'"
    ]


# ── views, model_version, counts ──────────────────────────────────────────


def test_a_view_member_resolves_to_its_base_member() -> None:
    doc = MetaDoc.parse(META)
    assert doc.resolve("orders_view.revenue") == "orders.revenue"
    assert doc.resolve("orders_view.country") == "customers.country"
    assert doc.resolve("orders.revenue") == "orders.revenue"
    assert doc.resolve("nope.nothing") == "nope.nothing"


def test_model_version_ignores_labels_and_order_but_not_members() -> None:
    doc = MetaDoc.parse(META)
    relabelled = copy.deepcopy(META)
    relabelled["cubes"][0]["title"] = "Sales orders"
    relabelled["cubes"][0]["measures"].reverse()
    relabelled["cubes"].reverse()
    assert MetaDoc.parse(relabelled).model_version() == doc.model_version()
    changed = copy.deepcopy(META)
    changed["cubes"][0]["measures"][1]["aggType"] = "avg"
    assert MetaDoc.parse(changed).model_version() != doc.model_version()


def test_model_version_restricted_to_a_lens_views_moves_only_with_them() -> None:
    doc = MetaDoc.parse(META)
    other = copy.deepcopy(META)
    other["cubes"][1]["dimensions"].append({"name": "customers.city", "type": "string"})
    changed = MetaDoc.parse(other)
    assert changed.model_version(["orders_view"]) == doc.model_version(["orders_view"])
    assert changed.model_version() != doc.model_version()


def test_member_counts() -> None:
    doc = MetaDoc.parse(META)
    assert doc.member_counts(["orders_view"]) == {
        "cubes": 0,
        "views": 1,
        "measures": 2,
        "dimensions": 3,
        "segments": 1,
    }
    assert doc.member_counts()["cubes"] == 3


def test_view_members_feed_from_intent_with_the_declared_clock() -> None:
    members = view_members(MetaDoc.parse(META), "orders_view")
    assert members.clock == "orders_view.ordered_at"
    assert members.time_dimensions == ["orders_view.ordered_at"]
    q = from_intent(QueryIntent(entity="orders_view", metrics=["revenue"], grain="month"), members)
    assert q.timeDimensions[0].dimension == "orders_view.ordered_at"
    with pytest.raises(KeyError):
        view_members(MetaDoc.parse(META), "nope")


# ── config ────────────────────────────────────────────────────────────────


def test_the_cube_block_parses_with_its_defaults() -> None:
    cfg = parse_project_yaml("cube:\n  url: http://localhost:4000\n")
    assert cfg.cube is not None
    assert (cfg.cube.url, cfg.cube.api_secret_env, cfg.cube.meta_ttl) == (
        "http://localhost:4000",
        "CUBEJS_API_SECRET",
        300,
    )
    assert parse_project_yaml("name: x\n").cube is None


def test_an_unknown_cube_key_is_a_named_error() -> None:
    with pytest.raises(Exception, match="api_secret"):
        parse_project_yaml("cube:\n  url: http://x\n  api_secret: inline\n")


def _project(tmp_path: Path, yaml: str, env: str = "") -> Path:
    (tmp_path / "dst.yaml").write_text(yaml, encoding="utf-8")
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    return tmp_path


def test_configured_reads_dst_yaml_and_the_named_secret(tmp_path: Path) -> None:
    root = _project(
        tmp_path,
        "cube:\n  url: http://cube:4000/\n  api_secret_env: MY_CUBE_SECRET\n  meta_ttl: 60\n",
        "MY_CUBE_SECRET=s3cret\n",
    )
    cfg = cube_client.configured(root)
    assert cfg is not None
    assert cfg.url == "http://cube:4000" and cfg.meta_ttl == 60
    assert cfg.api_secret == "s3cret", "dst.yaml's api_secret_env applies when the env names none"


def test_the_env_wins_field_by_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path, "cube:\n  url: http://from-yaml:4000\n  meta_ttl: 60\n")
    monkeypatch.setattr(settings, "cube_url", "http://from-env:4000")
    cfg = cube_client.configured(root)
    assert cfg is not None and cfg.url == "http://from-env:4000" and cfg.meta_ttl == 60
    monkeypatch.setattr(settings, "cube_meta_ttl", 5)
    assert cube_client.configured(root).meta_ttl == 5  # type: ignore[union-attr]


def test_not_configured_without_a_url(tmp_path: Path) -> None:
    assert cube_client.configured(_project(tmp_path, "name: x\n")) is None
    assert cube_client.status(root=tmp_path) == "not configured"


# ── dst cube status, doctor, /ready ───────────────────────────────────────


@pytest.fixture
def fake_cube(monkeypatch: pytest.MonkeyPatch) -> FakeCube:
    """Every CubeClient built in the test talks to one fake Cube."""
    fake = FakeCube()
    real_init = CubeClient.__init__

    def init(self: CubeClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = fake.transport()
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(CubeClient, "__init__", init)
    return fake


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def _cube_project(tmp_path: Path) -> Path:
    return _project(tmp_path, "cube:\n  url: http://cube:4000\n", f"CUBEJS_API_SECRET={SECRET}\n")


def test_cube_status_prints_version_model_version_and_counts(
    tmp_path: Path,
    fake_cube: FakeCube,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _cube_project(tmp_path)
    assert _run_cli(monkeypatch, ["cube", "status", "--dir", str(root)]) == 0
    out = capsys.readouterr().out
    version = MetaDoc.parse(META).model_version()
    assert "url           http://cube:4000" in out
    assert "secret        set" in out
    assert "readyz        ok (version not stated)" in out
    assert f"model_version {version}" in out
    assert "members       1 view(s), 3 cube(s), 5 measure(s), 6 dimension(s), 2 segment(s)" in out
    assert "warehouse     ok (one row from customers" in out
    assert fake_cube.claims and fake_cube.claims[0]["dst"]["groups"] == []
    # The probe is the smallest query the model allows: one dimension of
    # the cube with the fewest members, limit 1, as the operator.
    load = [json.loads(r.content) for r in fake_cube.requests if r.url.path.endswith("/load")]
    assert load == [{"query": {"dimensions": ["customers.country"], "limit": 1}}]


def test_cube_status_fails_when_the_warehouse_does_not_answer_through_cube(
    tmp_path: Path,
    fake_cube: FakeCube,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The meta compiles with no warehouse call, so a driver that cannot
    reach the warehouse (an empty BigQuery location, a dead host) kept
    status, doctor and /ready green until the probe erred on every table.
    One `limit 1` query through Cube shows it here."""
    fake_cube.warehouse_error = "Error: connect ECONNREFUSED 10.0.0.9:443"
    root = _cube_project(tmp_path)
    assert _run_cli(monkeypatch, ["cube", "status", "--dir", str(root)]) == 1
    out = capsys.readouterr().out
    assert "model_version" in out
    assert (
        "warehouse     FAIL — Error: connect ECONNREFUSED 10.0.0.9:443 (limit 1 on customers)"
        in out
    )
    assert "CUBEJS_DB_BQ_LOCATION" in out
    assert _run_cli(monkeypatch, ["cube", "status", "--dir", str(root), "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["warehouse"] == {
        "ok": False,
        "cube": "customers",
        "detail": "Error: connect ECONNREFUSED 10.0.0.9:443",
        "provider": "warehouse",
    }
    from services.cli.cube import doctor_row

    assert doctor_row(root) == 1
    assert "warehouse Error: connect ECONNREFUSED" in capsys.readouterr().out


def test_cube_status_json(
    tmp_path: Path,
    fake_cube: FakeCube,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _cube_project(tmp_path)
    assert _run_cli(monkeypatch, ["cube", "status", "--dir", str(root), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["reachable"] is True and body["overlay_errors"] == 0
    assert body["members"]["views"] == 1


def test_cube_status_fails_when_cube_is_down(
    tmp_path: Path,
    fake_cube: FakeCube,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_cube.healthy = False
    root = _cube_project(tmp_path)
    assert _run_cli(monkeypatch, ["cube", "status", "--dir", str(root)]) == 1
    assert "readyz        FAIL — HTTP 503" in capsys.readouterr().out


def test_cube_status_says_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, "name: x\n")
    assert _run_cli(monkeypatch, ["cube", "status", "--dir", str(root)]) == 1
    assert "not configured" in capsys.readouterr().err


def test_doctor_row(
    tmp_path: Path, fake_cube: FakeCube, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.cli.cube import doctor_row

    assert doctor_row(_project(tmp_path, "name: x\n")) == 0
    assert "cube          not configured" in capsys.readouterr().out
    root = _cube_project(tmp_path)
    assert doctor_row(root) == 0
    assert "cube          ok (http://cube:4000, model " in capsys.readouterr().out
    fake_cube.healthy = False
    assert doctor_row(root) == 1


def test_ready_reports_cube_without_gating_on_it(
    fake_cube: FakeCube, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.app import _cube_status

    assert _cube_status() == "not configured"
    monkeypatch.setattr(settings, "cube_url", "http://cube:4000")
    monkeypatch.setenv("CUBEJS_API_SECRET", SECRET)
    status = _cube_status()
    assert isinstance(status, dict) and status["reachable"] is True
    assert status["model_version"] == MetaDoc.parse(META).model_version()
    assert status["warehouse"]["ok"] is True and status["warehouse"]["cube"] == "customers"
    fake_cube.warehouse_error = "Error: connect ECONNREFUSED 10.0.0.9:443"
    status = _cube_status()
    assert isinstance(status, dict) and status["warehouse"]["ok"] is False


# ── live lane: a real Cube ────────────────────────────────────────────────

LIVE_URL = os.environ.get("DST_TEST_CUBE_URL")
live = pytest.mark.skipif(not LIVE_URL, reason="DST_TEST_CUBE_URL names no Cube to test against")


@live
def test_live_meta_and_one_load() -> None:
    secret = os.environ.get("DST_TEST_CUBE_SECRET") or os.environ.get("CUBEJS_API_SECRET")
    c = CubeClient(str(LIVE_URL), secret)
    try:
        assert c.ready().reachable
        doc = c.meta(CubeContext.operator())
        assert doc.cubes, "a live Cube with an empty model proves nothing"
        assert len(doc.model_version()) == 64
        cube = next(c for c in doc.cubes if c.measures)
        out = c.load(CubeQuery(measures=[cube.measures[0].name], limit=1), CubeContext.operator())
        assert len(out.data) == 1 and cube.measures[0].name in out.data[0]
    except httpx.HTTPError as exc:  # pragma: no cover — the lane reports, not hides
        pytest.fail(f"live Cube at {LIVE_URL}: {exc}")
    finally:
        c.close()
