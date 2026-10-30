"""Which lane serves a lens: an entity-YAML lens stays typed, a lens apply
stamped against a Cube model serves through Cube, an explicit `serving:` or
DST_SERVING wins; plan prints the lane per lens, doctor and /ready per
published lens with what a Cube lane lacks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.cli.project import _render_plan
from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import CubeStamp, SemanticModel
from services.project import cube_plan
from services.serve import door, lanes

STAMP = CubeStamp(model_version="m", overlay_version="o", stamped_at="2026-10-01T00:00:00+00:00")


def _model(cube: bool) -> SemanticModel:
    return SemanticModel(lens="a", dialect="duckdb", cube=STAMP if cube else None)


def test_an_entity_lens_stays_typed_and_a_cube_backed_one_serves_through_cube() -> None:
    lens = LensConfig(name="a")
    assert lens.serving is None
    assert door.serving_core(lens, _model(cube=False)) == "typed"
    assert door.serving_core(lens) == "typed"
    assert door.serving_core(lens, _model(cube=True)) == "cube"
    assert lanes.why(lens, cube_backed=True) == "Cube-backed"
    assert lanes.why(lens, cube_backed=False) == "default"


def test_the_lens_setting_and_dst_serving_override_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    typed = LensConfig(name="a", serving="typed")
    assert door.serving_core(typed, _model(cube=True)) == "typed"
    assert lanes.why(typed, cube_backed=True) == "lens serving:"
    assert door.serving_core(LensConfig(name="a", serving="cube")) == "cube"
    monkeypatch.setattr(lanes.settings, "serving", "typed")
    assert door.serving_core(LensConfig(name="a"), _model(cube=True)) == "typed"
    assert lanes.why(LensConfig(name="a", serving="cube"), cube_backed=True) == "DST_SERVING"


def test_a_stored_bundle_without_serving_reads_unset() -> None:
    # Bundles stored by earlier builds carried serving: typed explicitly only
    # when the author wrote it; one with no key reads unset, so a Cube stamp
    # decides.
    assert LensConfig.model_validate({"name": "a"}).serving is None
    assert "serving" not in LensConfig(name="a").model_dump(exclude_none=True)


def test_the_cube_backed_lenses_of_a_push() -> None:
    files = {
        "cube/overlay.yaml": "version: 1\nlenses:\n  sales: {views: [orders]}\n",
        "lenses/sales/lens.yaml": "name: sales\n",
        "lenses/ops/lens.yaml": "name: ops\nviews: [tickets]\n",
        "lenses/legacy/lens.yaml": "name: legacy\n",
    }
    assert cube_plan.backed_lenses(files) == {"sales", "ops"}
    assert cube_plan.backed_lenses({"lenses/legacy/lens.yaml": "name: legacy\n"}) == set()
    # Apply stamps nothing without an overlay, or with one it refuses.
    no_overlay = {k: v for k, v in files.items() if k != "cube/overlay.yaml"}
    assert cube_plan.backed_lenses(no_overlay) == set()
    broken = {**files, "cube/overlay.yaml": "version: 1\nnot_a_key: 1\n"}
    assert cube_plan.backed_lenses(broken) == set()


def test_plan_prints_the_lane_per_lens() -> None:
    lines: list[str] = []
    row = {
        "scope": "serving",
        "lanes": {
            "legacy": {"lane": "typed", "why": "default"},
            "sales": {"lane": "cube", "why": "Cube-backed"},
        },
    }
    assert _render_plan([row], lines.append) == 0
    assert lines == ["serving: legacy → typed (default), sales → cube (Cube-backed)"]


def test_the_doctor_lines_name_what_a_cube_lane_lacks() -> None:
    rep = {
        "lenses": [
            {"org": "acme", "lens": "legacy", "lane": "typed", "why": "default"},
            {
                "org": "acme",
                "lens": "sales",
                "lane": "cube",
                "why": "Cube-backed",
                "cube": "not configured",
                "monitor": "never run",
                "chat_tier": "not configured",
            },
        ]
    }
    out = lanes.lines(rep)
    assert out[0] == "lanes"
    assert out[1].split() == ["acme/legacy", "typed", "(default)"]
    assert out[2].split() == ["acme/sales", "cube", "(Cube-backed)"]
    assert "cube not configured" in out[3]
    assert "monitor never run" in out[4]
    assert "no chat tier" in out[5] and "driver mode" in out[5]
    healthy = {
        **rep["lenses"][1],
        "cube": "configured",
        "monitor": "ran x",
        "chat_tier": "configured",
    }
    assert len(lanes.lines({"lenses": [healthy]})) == 2
    assert lanes.lines({"lenses": []}) == ["lanes         no published lens — `dst apply` first"]
    assert lanes.lines("unknown") == ["lanes         unknown"]


@pytest.mark.needs_db
def test_ready_and_doctor_report_the_lane_of_every_published_lens(
    monkeypatch: pytest.MonkeyPatch, live_client: TestClient
) -> None:
    from sqlalchemy import create_engine, text

    from services.config import settings
    from services.db.session import org_session
    from services.lenses import store
    from services.lenses.store import LensBundle

    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('LaneOrg') RETURNING id")).scalar_one()
    try:
        with org_session(org) as session:
            for name, cube in (("legacy", False), ("sales", True)):
                bundle = LensBundle(config=LensConfig(name=name), semantic_model=_model(cube))
                store.create_lens(session, bundle)
                store.publish(session, name)
            session.commit()
        monkeypatch.setattr(lanes.settings, "cube_url", None)
        rep = lanes.report()
        assert isinstance(rep, dict)
        mine = {r["lens"]: r for r in rep["lenses"] if r["org"] == "LaneOrg"}
        assert mine["legacy"] == {
            "org": "LaneOrg",
            "lens": "legacy",
            "lane": "typed",
            "why": "default",
        }
        assert mine["sales"]["lane"] == "cube" and mine["sales"]["why"] == "Cube-backed"
        assert mine["sales"]["monitor"] == "never run"

        body = live_client.get("/ready").json()
        rows = [r for r in body["serving"]["lenses"] if r["org"] == "LaneOrg"]
        assert [(r["lens"], r["lane"]) for r in rows] == [("legacy", "typed"), ("sales", "cube")]
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM lens_version WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def test_migrate_model_check_prints_the_diff_and_the_next_steps(tmp_path: Path) -> None:
    import shutil

    from tests.cube_shop import PROJECT

    root = tmp_path / "project"
    shutil.copytree(PROJECT, root, ignore=shutil.ignore_patterns("cube"))

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "services.cli.main", "migrate-model", "--dir", str(root), *args],
            capture_output=True,
            text=True,
        )

    check = run("--check")
    assert check.returncode == 2, check.stderr
    assert "(new," in check.stdout and "Next steps:" in check.stdout
    assert not (root / "cube").exists()  # a check writes nothing
    wrote = run()
    assert wrote.returncode == 0, wrote.stderr
    assert (root / "cube" / "cube.js").read_text().startswith("// dst's Cube Core config")
    for step in ("docker compose --profile cube up -d", "dst probe --cube", "dst plan"):
        assert step in wrote.stdout
    for step in ("dst apply", "dst monitor", "dst test"):
        assert step in wrote.stdout
    assert run("--check").returncode == 0
    orders = root / "semantic/entities/orders.yaml"
    orders.write_text(orders.read_text().replace("label: tips", "label: tip"))
    diff = run("--check")
    assert diff.returncode == 2
    assert "-    title: tips" in diff.stdout and "+    title: tip" in diff.stdout
