"""Plugin seam: entry-point plugins mount routes; a broken one never
stops the boot; and none of it happens quietly.

`pip install some-dst-plugin` changes what an install's API serves. That was
invisible from the outside — no line naming what mounted, nothing on the health
surface — so an operator could not answer "what is running here" without reading the
venv. Visibility is the fix; `DST_PLUGINS` is the optional lock on top of it.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import plugins


class _Dist:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version


class _EntryPoint:
    """Stand-in for importlib.metadata.EntryPoint (name + dist + load)."""

    def __init__(self, name: str, register: object, dist: _Dist | None = None) -> None:
        self.name = name
        self.dist = dist or _Dist(f"dst-{name}", "1.2.3")
        self._register = register

    def load(self) -> object:
        return self._register


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """`plugins.loaded` records what THIS process mounted at startup, so it outlives a
    test the way it outlives a boot. Cleared here, and open-by-default pinned rather
    than inherited, so each test states its own preconditions."""
    monkeypatch.setattr(plugins, "loaded", [])
    monkeypatch.setattr(plugins.settings, "plugins", None)


def _core_app() -> FastAPI:
    app = FastAPI()

    @app.get("/core")
    def core() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_plugin_route_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    def register(app: FastAPI) -> None:
        @app.get("/plugin/ping")
        def ping() -> dict[str, bool]:
            return {"pong": True}

    monkeypatch.setattr(plugins, "entry_points", lambda group: [_EntryPoint("cloud", register)])
    app = _core_app()
    plugins.load_plugins(app)
    assert TestClient(app).get("/plugin/ping").json() == {"pong": True}


def test_broken_plugin_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(app: FastAPI) -> None:
        raise RuntimeError("boom")

    def register(app: FastAPI) -> None:
        @app.get("/plugin/after-broken")
        def after() -> dict[str, bool]:
            return {"pong": True}

    monkeypatch.setattr(
        plugins,
        "entry_points",
        lambda group: [_EntryPoint("broken", broken), _EntryPoint("cloud", register)],
    )
    app = _core_app()
    plugins.load_plugins(app)  # must not raise
    client = TestClient(app)
    assert client.get("/core").json() == {"status": "ok"}
    assert client.get("/plugin/after-broken").json() == {"pong": True}
    # …and the one that blew up is not reported as running.
    assert [p.name for p in plugins.loaded] == ["cloud"]


def _two_routes(app: FastAPI) -> None:
    @app.get("/plugin/a")
    def a() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/plugin/b")
    def b() -> dict[str, bool]:
        return {"ok": True}


def test_every_mount_says_what_it_mounted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The defect: `pip install some-dst-plugin` altered the route table with no
    trace at all. Name, distribution, version, and how much of the table is now its —
    enough for an operator to go from a surprising route to the package that added it."""
    monkeypatch.setattr(
        plugins,
        "entry_points",
        lambda group: [_EntryPoint("cloud", _two_routes, _Dist("dst-cloud", "0.0.1"))],
    )
    with caplog.at_level(logging.INFO, logger="dst"):
        mounted = plugins.load_plugins(_core_app())
    assert mounted == [plugins.LoadedPlugin("cloud", "dst-cloud", "0.0.1", 2)]
    line = "".join(r.getMessage() for r in caplog.records)
    assert "cloud" in line and "dst-cloud 0.0.1" in line and "+2 routes" in line


def test_ready_reports_what_is_mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A log line is only seen by whoever has the logs. The health surface answers
    "what is running here" for anyone who can reach the server."""
    assert plugins.status() == "none"
    monkeypatch.setattr(
        plugins,
        "entry_points",
        lambda group: [_EntryPoint("cloud", _two_routes, _Dist("dst-cloud", "0.0.1"))],
    )
    plugins.load_plugins(_core_app())
    assert plugins.status() == "cloud@0.0.1"


def test_an_allowlist_pins_the_route_table(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Open by default keeps a plugin-based deployment working on a stock core
    with no extra env var to forget. Setting DST_PLUGINS turns the route table into a declared fact,
    and the plugin left out is refused out loud — never just absent."""
    monkeypatch.setattr(
        plugins,
        "entry_points",
        lambda group: [_EntryPoint("cloud", _two_routes), _EntryPoint("hitchhiker", _two_routes)],
    )
    monkeypatch.setattr(plugins.settings, "plugins", "cloud")
    app = _core_app()
    with caplog.at_level(logging.WARNING, logger="dst"):
        mounted = plugins.load_plugins(app)
    assert [p.name for p in mounted] == ["cloud"]
    assert TestClient(app).get("/plugin/a").status_code == 200
    assert "hitchhiker" in "".join(r.getMessage() for r in caplog.records)


def test_an_empty_allowlist_is_an_answer_not_an_accident(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`DST_PLUGINS=` is how an operator says "none" — and it cannot be silent
    either, because an empty value in a compose file is just as often a mistake."""
    monkeypatch.setattr(plugins, "entry_points", lambda group: [_EntryPoint("cloud", _two_routes)])
    monkeypatch.setattr(plugins.settings, "plugins", "")
    with caplog.at_level(logging.WARNING, logger="dst"):
        assert plugins.load_plugins(_core_app()) == []
    assert caplog.records
    assert plugins.status() == "none (DST_PLUGINS)"
