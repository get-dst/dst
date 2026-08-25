"""DST_URL must reach the MCP server on any port, however it was declared.

The bug class: the MCP server resolves its own API's base URL from a bare
import-time os.environ read, `dst serve` never exports what it resolved from
the project .env or a --port flag, and every MCP tool on a non-:8000 install
proxies to a dead port. Two seams, both pinned here: the server resolves
through resolve_env_ref (process env first, then the project .env), and _serve
exports the URL it actually serves.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import services.cli.main as cli
import services.config
from services.config import settings
from services.db import schema_state as schema
from services.mcp.server import _resolve_base_url


def test_mcp_base_url_reads_the_project_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DST_URL", raising=False)
    (tmp_path / ".env").write_text("DST_URL=http://localhost:8077/\n")
    monkeypatch.setattr(services.config, "_project_dirs", [tmp_path])
    assert _resolve_base_url() == "http://localhost:8077"


def test_mcp_base_url_process_env_wins_and_default_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services.config, "_project_dirs", [tmp_path])  # no .env
    monkeypatch.delenv("DST_URL", raising=False)
    assert _resolve_base_url() == "http://localhost:8000"
    monkeypatch.setenv("DST_URL", "https://dst.example.com/")
    assert _resolve_base_url() == "https://dst.example.com"


def _run_serve(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    import uvicorn

    monkeypatch.setattr(schema, "schema_state", lambda: schema.SchemaState("ok", "1", "1"))
    monkeypatch.setattr(cli, "_announce_ready", lambda url: None)
    monkeypatch.setattr(settings, "web_dist", "already-set")
    monkeypatch.setattr(uvicorn, "run", lambda target, **kw: None)
    assert cli._serve(argparse.Namespace(host="127.0.0.1", port=port, reload=False)) == 0


def test_serve_exports_the_port_it_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DST_URL", raising=False)
    monkeypatch.setattr(services.config, "_project_dirs", [])  # no .env either
    _run_serve(monkeypatch, port=18077)
    import os

    assert os.environ["DST_URL"] == "http://localhost:18077"


def test_serve_keeps_a_declared_url_whose_port_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env URL is kept verbatim (host/scheme intent), but only when it names
    the port actually served — an explicit --port elsewhere beats a stale URL."""
    monkeypatch.delenv("DST_URL", raising=False)
    (tmp_path / ".env").write_text("DST_URL=http://127.0.0.1:18077\n")
    monkeypatch.setattr(services.config, "_project_dirs", [tmp_path])
    _run_serve(monkeypatch, port=18077)
    import os

    assert os.environ["DST_URL"] == "http://127.0.0.1:18077"


def test_serve_never_clobbers_an_exported_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DST_URL", "https://dst.example.com")
    _run_serve(monkeypatch, port=18078)
    import os

    assert os.environ["DST_URL"] == "https://dst.example.com"
