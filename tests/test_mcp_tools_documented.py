"""The MCP guide lists every tool's arguments by the names the tools declare.

An agent reads the guide, not the server source; an argument renamed in one and
not the other is a tool call that fails validation. The server is the source
of truth here, as the parser is for the CLI gate."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from services.mcp import server

_ROOT = Path(__file__).resolve().parents[1]
DOC = (
    next(
        p
        for p in (_ROOT / "docs" / "oss" / "docs" / "guides", _ROOT / "docs" / "guides")
        if p.is_dir()
    )
    / "agents-mcp.md"
)


def _tools() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, fn in vars(server).items():
        if name.startswith("_") or not inspect.iscoroutinefunction(fn):
            continue
        if not (fn.__doc__ and getattr(fn, "__module__", "") == server.__name__):
            continue
        params = [p for p in inspect.signature(fn).parameters if p != "ctx"]
        out[name] = params
    return out


def test_every_tool_argument_is_in_the_guide() -> None:
    text = DOC.read_text()
    rows = {
        m.group(1): m.group(2)
        for m in re.finditer(r"^\| `(\w+)` \| (.+) \|$", text, flags=re.MULTILINE)
    }
    tools = _tools()
    assert {"route_query", "query", "query_metrics", "send_for_review"} <= set(tools)
    for tool, params in tools.items():
        assert tool in rows, f"{tool} has no arguments row in {DOC}"
        for param in params:
            assert f"`{param}`" in rows[tool], f"{tool}: `{param}` missing from its row"
        if not params:
            assert rows[tool].strip() == "none"
