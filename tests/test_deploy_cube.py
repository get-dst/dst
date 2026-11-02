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
    assert _env(cube)["CUBEJS_DEV_MODE"] == "false"
    assert any(v.endswith(":/cube/conf/model:ro") for v in cube["volumes"])
    assert services["app"]["depends_on"]["cube"] == {
        "condition": "service_healthy",
        "required": False,
    }
    assert "DST_CUBE_URL" in services["app"]["environment"]


def _env(service: dict[str, object]) -> dict[str, str | None]:
    """A service's environment, mapping or list form, as name → value
    (None for a bare list entry: passed only when the shell sets it)."""
    env = service["environment"]
    if isinstance(env, dict):
        return {k: str(v) for k, v in env.items()}
    assert isinstance(env, list)
    out: dict[str, str | None] = {}
    for item in env:
        name, eq, value = str(item).partition("=")
        out[name] = value.split("  #")[0].strip() if eq else None
    return out


def test_compose_passes_cube_warehouse_settings_only_when_set() -> None:
    """A mapping entry `${VAR:-}` hands Cube an EMPTY value, and its BigQuery
    driver fails every query on an empty CUBEJS_DB_BQ_LOCATION while the
    meta and /readyz stay green: the optional settings are bare list
    entries, passed only when the shell or .env sets them."""
    cube = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())["services"]["cube"]
    env = _env(cube)
    for name in (
        "CUBEJS_DB_BQ_LOCATION",
        "CUBEJS_DB_BQ_PROJECT_ID",
        "CUBEJS_DB_BQ_KEY_FILE",
        "CUBEJS_DB_HOST",
        "DST_DUCKDB_PATH",
    ):
        assert env[name] is None, name
    assert env["CUBEJS_DB_TYPE"] == "${CUBEJS_DB_TYPE:-duckdb}"
    # The secret is the one default-empty entry left: Cube refuses to start without it.
    assert [k for k, v in env.items() if v is not None and v.endswith(":-}")] == [
        "CUBEJS_API_SECRET"
    ]
    # `dst init --cube` scaffolds the same shape.
    from services.cli.init import _cube_service

    scaffolded = yaml.safe_load("services:\n" + _cube_service())["services"]["cube"]
    assert _env(scaffolded)["CUBEJS_DB_BQ_LOCATION"] is None
    assert _env(scaffolded)["CUBEJS_DB_TYPE"] == "${CUBEJS_DB_TYPE:-duckdb}"


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

    # An empty cube.env value is left out of the sidecar's env entirely.
    located = _template(
        "cube.enabled=true",
        "cube.model.configMap=model",
        "cube.env.CUBEJS_DB_TYPE=bigquery",
        "cube.env.CUBEJS_DB_BQ_LOCATION=",
    )
    assert located.returncode == 0, located.stderr
    deploy = next(
        d for d in (d for d in yaml.safe_load_all(located.stdout) if d) if d["kind"] == "Deployment"
    )
    sidecar = next(
        c for c in deploy["spec"]["template"]["spec"]["containers"] if c["name"] == "cube"
    )
    names = [e["name"] for e in sidecar["env"]]
    assert "CUBEJS_DB_TYPE" in names and "CUBEJS_DB_BQ_LOCATION" not in names

    external = _template("cube.external.url=http://cube.internal:4000")
    assert "cubejs/cube" not in external.stdout and "http://cube.internal:4000" in external.stdout

    assert (
        "keep replicaCount at 1"
        in _template("cube.enabled=true", "cube.model.configMap=model", "replicaCount=2").stderr
    )
    assert "needs the model" in _template("cube.enabled=true").stderr
