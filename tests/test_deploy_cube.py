"""The Cube sidecar in the deploy shapes: one pinned tag everywhere, the model
mounted read-only, the read-only DuckDB attach shipped once, and the Helm
chart's sidecar rules."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
CHART = DEPLOY / "helm" / "dst"
PINNED = (DEPLOY / "cube.version").read_text().strip()


def test_one_pinned_cube_tag_everywhere() -> None:
    assert re.fullmatch(r"v\d+\.\d+\.\d+", PINNED)
    for compose in (DEPLOY / "docker-compose.yml", DEPLOY / "demo" / "docker-compose.yml"):
        image = yaml.safe_load(compose.read_text())["services"]["cube"]["image"]
        assert image == f"cubejs/cube:${{CUBE_TAG:-{PINNED}}}", compose
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["cube"]["image"]["tag"] == PINNED


def test_the_chart_ships_the_same_cube_config() -> None:
    assert (CHART / "files" / "cube.js").read_text() == (DEPLOY / "cube" / "cube.js").read_text()
    assert "READ_ONLY" in (DEPLOY / "cube" / "cube.js").read_text()
    assert "contextToGroups" in (DEPLOY / "cube" / "cube.js").read_text()


@pytest.mark.parametrize("compose", ["docker-compose.yml", "demo/docker-compose.yml"])
def test_compose_sidecar_is_optional_and_reads_the_model_read_only(compose: str) -> None:
    services = yaml.safe_load((DEPLOY / compose).read_text())["services"]
    cube = services["cube"]
    assert cube["profiles"] == ["cube"]
    assert cube["environment"]["CUBEJS_DEV_MODE"] == "false"
    assert any(v.endswith(":/cube/conf/model:ro") for v in cube["volumes"])
    assert services["app"]["depends_on"]["cube"] == {
        "condition": "service_healthy",
        "required": False,
    }
    assert "DST_CUBE_URL" in services["app"]["environment"]


helm = shutil.which("helm")


def _template(*sets: str) -> subprocess.CompletedProcess[str]:
    args = [str(helm), "template", "t", str(CHART), "--set", "publicBaseUrl=https://x"]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True)


@pytest.mark.skipif(helm is None, reason="helm is not installed")
def test_helm_sidecar_external_and_refusals() -> None:
    off = _template()
    assert (
        off.returncode == 0 and "cubejs/cube" not in off.stdout and "DST_CUBE_URL" not in off.stdout
    )
    on = _template("cube.enabled=true", "cube.model.configMap=model")
    assert on.returncode == 0, on.stderr
    docs = [d for d in yaml.safe_load_all(on.stdout) if d]
    deploy = next(d for d in docs if d["kind"] == "Deployment")
    containers = {c["name"]: c for c in deploy["spec"]["template"]["spec"]["containers"]}
    assert containers["cube"]["image"] == f"cubejs/cube:{PINNED}"
    env = {e["name"]: e for e in containers["dst"]["env"]}
    assert env["DST_CUBE_URL"]["value"] == "http://localhost:4000"
    mounts = {m["name"]: m for m in containers["cube"]["volumeMounts"]}
    assert mounts["cube-model"]["readOnly"] is True
    assert any(d["kind"] == "ConfigMap" and "cube.js" in d["data"] for d in docs)

    external = _template("cube.external.url=http://cube.internal:4000")
    assert "cubejs/cube" not in external.stdout and "http://cube.internal:4000" in external.stdout

    assert (
        "keep replicaCount at 1"
        in _template("cube.enabled=true", "cube.model.configMap=model", "replicaCount=2").stderr
    )
    assert "needs the model" in _template("cube.enabled=true").stderr
