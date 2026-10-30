"""Which lane serves each lens, and what that lane needs to answer.

A lens serves through Cube when ``DST_SERVING`` says so, else when its own
``serving:`` says so, else when apply stamped it against a Cube model (a lens
of a Cube-backed project: ``cube/overlay.yaml`` lists it, or its lens.yaml
names ``views:``). Every other lens serves typed, as entity-YAML projects
always have. ``dst plan`` prints the lane per lens from the files;
``dst doctor`` and ``/ready`` print it per published lens, with whether the
Cube lane's dependencies are there: a reachable Cube is configured, the
monitor has run, and a chat tier answers the HTTP question door for callers
that are not in driver mode (an agent in driver mode needs none).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from services.config import settings
from services.contracts.lens_config import LensConfig

log = logging.getLogger("dst.serve.lanes")

Lane = Literal["typed", "cube"]


def lane(config: LensConfig, *, cube_backed: bool) -> Lane:
    return settings.serving or config.serving or ("cube" if cube_backed else "typed")


def why(config: LensConfig, *, cube_backed: bool) -> str:
    """What decided the lane, in the words the configuration uses."""
    if settings.serving:
        return "DST_SERVING"
    if config.serving:
        return "lens serving:"
    return "Cube-backed" if cube_backed else "default"


def report() -> dict[str, Any] | str:
    """Per published lens: its lane and why; for a Cube lane, whether Cube is
    configured, whether the org's monitor has run, and whether a chat tier
    serves the HTTP question door without a driver. Never raises."""
    from sqlalchemy import text

    from services.cube.client import configured
    from services.db.session import admin_engine, org_session
    from services.lenses.store import list_published
    from services.monitor import status as monitor_status
    from services.serve.door import frontier_model

    try:
        cube = "configured" if configured() is not None else "not configured"
        lenses: list[dict[str, Any]] = []
        with admin_engine.connect() as conn:
            orgs = list(conn.execute(text("SELECT id, name FROM org ORDER BY created_at")))
        for org_id, org_name in orgs:
            with org_session(org_id) as session:
                published = list_published(session)
                monitored = monitor_status.load(session)
            for name, _display, _desc, bundle in published:
                backed = bundle.semantic_model.cube is not None
                row: dict[str, Any] = {
                    "org": str(org_name),
                    "lens": name,
                    "lane": lane(bundle.config, cube_backed=backed),
                    "why": why(bundle.config, cube_backed=backed),
                }
                if row["lane"] == "cube":
                    row["cube"] = cube
                    row["monitor"] = f"ran {monitored.get('as_of')}" if monitored else "never run"
                    row["chat_tier"] = (
                        "configured"
                        if frontier_model(bundle.config) is not None
                        else "not configured"
                    )
                lenses.append(row)
    except Exception:  # noqa: BLE001 — readiness must never crash on a status read
        log.exception("serving lane report failed")
        return "unknown"
    return {"lenses": lenses}


def lines(rep: dict[str, Any] | str) -> list[str]:
    """``report()`` as the doctor prints it: one line per lens, then what a
    Cube lane is missing, each with what it costs."""
    if not isinstance(rep, dict):
        return [f"lanes         {rep}"]
    rows = rep.get("lenses") or []
    if not rows:
        return ["lanes         no published lens — `dst apply` first"]
    out = ["lanes"]
    width = max(len(f"{r['org']}/{r['lens']}") for r in rows)
    for r in rows:
        name = f"{r['org']}/{r['lens']}"
        out.append(f"  {name:<{width}}  {r['lane']} ({r['why']})")
        if r["lane"] != "cube":
            continue
        if r["cube"] != "configured":
            out.append(
                "    ! cube not configured: every door on this lens answers 503 — set "
                "DST_CUBE_URL or `cube: {url: …}` in dst.yaml"
            )
        if r["monitor"] == "never run":
            out.append(
                "    ! monitor never run: the data-end and feed checks skip — "
                "`dst monitor`, then `dst apply`"
            )
        if r["chat_tier"] != "configured":
            out.append(
                "    ! no chat tier: the HTTP question door without a driver answers 503; "
                "MCP agents in driver mode are served with no model"
            )
    return out
