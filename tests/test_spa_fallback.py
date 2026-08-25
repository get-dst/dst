"""The SPA catch-all must not swallow the API's namespace.

An MCP tool pointed at a bundled build that lacks its endpoint gets
`200 text/html` from the SPA fallback and dies inside `r.json()` with
"Expecting value: line 1 column 1 (char 0)". The client sees an
opaque parse error and blames dst; the truth was a wrong path. Only bundled
deploys mount the catch-all, so dev never sees this class — hence a test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services import app as app_module
from services.config import settings


@pytest.fixture
def bundled_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The app as a release image builds it: SPA bundled at web_dist.

    Mounts the REAL `_mount_spa` onto the real app and restores the route table
    afterwards — reloading the module runs the startup migration instead.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>dst</title>", encoding="utf-8")
    monkeypatch.setattr(settings, "web_dist", str(dist))

    saved = list(app_module.app.router.routes)
    app_module._mount_spa()
    try:
        yield TestClient(app_module.app)
    finally:
        app_module.app.router.routes[:] = saved


def test_spa_serves_client_routes(bundled_client: TestClient) -> None:
    r = bundled_client.get("/lenses/churn")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", ["/v1/nope", "/mgmt/nope", "/health/nope"])
def test_unregistered_api_paths_404_instead_of_serving_html(
    bundled_client: TestClient, path: str
) -> None:
    r = bundled_client.get(path)
    assert r.status_code == 404, f"{path} returned {r.status_code} {r.headers.get('content-type')}"
    assert not r.headers["content-type"].startswith("text/html")


def test_the_mcp_mount_answers_for_its_own_namespace(bundled_client: TestClient) -> None:
    """`/mcp` is an ASGI Mount, so it authenticates before routing and never reaches
    the catch-all: an unknown sub-path is 401 JSON, not 404 and not index.html. The
    invariant that matters to a client is the content type — HTML is what breaks
    r.json() with an opaque parse error."""
    r = bundled_client.get("/mcp/nope")
    assert r.status_code == 401
    assert not r.headers["content-type"].startswith("text/html")


def test_a_registered_api_path_still_reaches_its_router(bundled_client: TestClient) -> None:
    """The guard must not shadow real endpoints — unauthenticated is 401, never HTML."""
    r = bundled_client.get("/v1/lenses")
    assert r.status_code == 401
    assert not r.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
        "/..%2f..%2f..%2f..%2fetc%2fpasswd",
        "/%2e%2e/secret.txt",
    ],
)
def test_encoded_traversal_cannot_escape_the_build_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A percent-encoded `..` reaches the catch-all undecoded by the router; without a
    containment check `dist / full_path` followed it out of the build and served any
    file the process could read (env → DST_SECRET_KEY). Escapes must fall through to
    index.html — indistinguishable from an unknown client route — never the file."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>dst</title>", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("DST_SECRET_KEY=leaked", encoding="utf-8")
    monkeypatch.setattr(settings, "web_dist", str(dist))

    saved = list(app_module.app.router.routes)
    app_module._mount_spa()
    try:
        # raw_path is sent verbatim so %2e stays encoded through the router
        r = TestClient(app_module.app).get(path)
    finally:
        app_module.app.router.routes[:] = saved

    assert "leaked" not in r.text
    assert r.headers["content-type"].startswith("text/html")
