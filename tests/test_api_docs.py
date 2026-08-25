"""The API docs are pinned to the app, not the other way around.

reference/api.md drifted without anyone noticing — "eight tools" while the MCP
server carried eleven, four /v1 routes missing from the data-plane table. A doc
only humans re-derive goes stale the week after it is split out; this suite makes
that drift a red build instead of a stranger's surprise. Same rule inward: /docs
is the reference of record for the control plane, so a route without a docstring
or a tag without a description is a hole in the product surface, not a style nit.

No DB, no server — the route table and the markdown file are both importable facts.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.routing import APIRoute

import services.mcp.server as mcp_server
from services.app import app

# The docs tree lives at docs/oss/docs/ (source layout) or docs/ (flattened) — this
# gate must hold in both, so resolve whichever exists.
_ROOT = Path(__file__).resolve().parent.parent
API_MD = next(
    p
    for p in (
        _ROOT / "docs" / "oss" / "docs" / "reference" / "api.md",
        _ROOT / "docs" / "reference" / "api.md",
    )
    if p.exists()
)


def _api_routes() -> list[APIRoute]:
    """The DOCUMENTED surface: routes hidden from /docs are not part of it.

    `include_in_schema=False` is how a route says it is not API surface — the
    SPA catch-all is the standing case. Filtering here is also what keeps these
    tests from depending on their environment: the catch-all only registers when
    a built dashboard is present, so without this the suite passed or failed on
    whether someone had run `pnpm build` into services/web_dist."""
    return [r for r in app.routes if isinstance(r, APIRoute) and r.include_in_schema]


def test_every_route_has_a_docstring() -> None:
    bare = [r.path for r in _api_routes() if not (r.summary or (r.endpoint.__doc__ or "").strip())]
    assert not bare, f"routes with nothing to show on /docs (add a docstring): {bare}"


def test_every_tag_is_described_and_used() -> None:
    used = {str(t) for r in _api_routes() for t in (r.tags or [])}
    described = {t["name"] for t in (app.openapi().get("tags") or [])}
    missing = sorted(used - described)
    stale = sorted(described - used)
    assert not missing, f"tags with no description in app.py's _openapi_tags: {missing}"
    assert not stale, f"described tags no route carries (delete the entry): {stale}"


def test_api_reference_lists_the_public_surface() -> None:
    # /v1, /auth, /oauth and meta routes are promised exhaustively — the literal
    # path must appear. /mgmt is deliberately grouped ("the rest is on /docs"),
    # so its two-segment prefix must appear: a new concern cannot ship untracked.
    doc = API_MD.read_text()
    missing: list[str] = []
    for r in _api_routes():
        path = r.path
        needle = "/".join(path.split("/")[:3]) if path.startswith("/mgmt") else path
        if needle not in doc:
            missing.append(f"{path} (looked for '{needle}')")
    assert not missing, "reference/api.md lost track of: " + "; ".join(missing)


def test_api_reference_lists_every_mcp_tool() -> None:
    doc = API_MD.read_text()
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert tools, "MCP server reports no tools — enumeration broke, not the doc"
    missing = [t.name for t in tools if f"`{t.name}`" not in doc]
    assert not missing, f"MCP tools absent from reference/api.md: {missing}"
