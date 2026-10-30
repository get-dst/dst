"""`dst monitor` and `dst probe --cube` — data quality read through Cube.

Both read the project under --dir: dst.yaml names the Cube (and the probe
budget), cube/overlay.yaml the lenses and their views. `dst probe --cube`
writes profiles/cube.probe.json (value dictionaries and data ends through
Cube's /v1/load); `dst monitor` writes it too, then profiles/cube.freshness.json
(per cube clock its data end and feeds, per measure a partial latest month)
and one lenses/<lens>/results/monitor-<date>.json per lens with the findings
on its cubes. Committed and applied, the freshness file is the `data_quality`
block on /ready.

Exit codes gate a scheduled run: 0 clean, 2 findings, 1 Cube or the project
could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from services.cli import style
from services.cli._common import _read_project

FRESHNESS = "profiles/cube.freshness.json"


def _lens_models(root: Path) -> tuple[list[Any], Any, dict[str, Any]] | int:
    """``(models, load, settings)`` for the project, or an exit code."""
    from services.contracts.lens_model import OVERLAY_PATH, parse_overlay
    from services.cube.client import CubeClient, CubeError, configured
    from services.cube.context import CubeContext
    from services.project import cube_plan
    from services.project.schema import ProjectConfig, parse_project_yaml

    files = _read_project(root)
    if OVERLAY_PATH not in files:
        print(
            f"error: no {OVERLAY_PATH} in {root.resolve()} — not a Cube-backed project "
            "(`dst migrate-model` writes one)",
            file=sys.stderr,
        )
        return 1
    try:
        overlay = parse_overlay(files[OVERLAY_PATH])
        project = parse_project_yaml(files["dst.yaml"]) if "dst.yaml" in files else ProjectConfig()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    cfg = configured(root)
    if cfg is None:
        print(
            "error: no Cube configured — `cube: {url: …}` in dst.yaml or DST_CUBE_URL",
            file=sys.stderr,
        )
        return 1
    client = CubeClient(cfg.url, cfg.api_secret, meta_ttl=0)
    groups = sorted(project.cube.plan_context.groups) if project.cube else []
    ctx = CubeContext(org="", caller="dst-monitor", groups=groups)
    try:
        meta = client.meta(ctx, extended=True)
    except CubeError as exc:
        print(style.bad(f"error: provider_down: cube — {exc.detail}"), file=sys.stderr)
        return 1
    from services.contracts.lens_model import LensModel

    models = [
        LensModel.build(meta, overlay, lens, views=views)
        for lens, views in cube_plan.cube_lenses(overlay, files).items()
    ]
    stale = [p.stale_after_days for p in overlay.lenses.values() if p.stale_after_days]

    def load(query: Any) -> list[dict[str, Any]]:
        return client.load(query, ctx).data

    return (
        models,
        load,
        {
            "max_queries": project.probe.cube.max_queries,
            "max_values": project.probe.cube.max_values,
            "stale_after_days": min(stale) if stale else 3,
        },
    )


def _clock_end(args: argparse.Namespace) -> date:
    if getattr(args, "as_of", None):
        return date.fromisoformat(args.as_of)
    return datetime.now(UTC).date() - timedelta(days=1)


def _write(root: Path, rel: str, payload: dict[str, Any]) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def probe_cube(args: argparse.Namespace) -> int:
    """`dst probe --cube`."""
    from services.monitor import cube as monitor
    from services.project.probe import CUBE_PROBE

    root = Path(args.dir)
    got = _lens_models(root)
    if isinstance(got, int):
        return got
    models, load, cfg = got
    doc = monitor.probe(
        models,
        load,
        clock_end=_clock_end(args),
        max_queries=cfg["max_queries"],
        max_values=cfg["max_values"],
    )
    _write(root, CUBE_PROBE, doc)
    print(
        f"cube probe: {len(doc['dictionaries'])} dictionar(ies), {len(doc['data_ends'])} data "
        f"end(s), {doc['queries']}/{doc['max_queries']} queries → {root / CUBE_PROBE}"
    )
    for what in doc["unread"]:
        print(style.warn(f"  unread (budget): {what}"))
    for error in doc["errors"]:
        print(style.bad(f"  error: {error}"))
    return 1 if doc["errors"] else 0


def _monitor(args: argparse.Namespace) -> int:
    from services.monitor import cube as monitor
    from services.project.probe import CUBE_PROBE

    root = Path(args.dir)
    got = _lens_models(root)
    if isinstance(got, int):
        return got
    models, load, cfg = got
    clock_end = _clock_end(args)
    probe_doc, freshness, findings = monitor.monitor(
        models,
        load,
        clock_end=clock_end,
        stale_after_days=cfg["stale_after_days"],
        max_queries=cfg["max_queries"],
        max_values=cfg["max_values"],
    )
    _write(root, CUBE_PROBE, probe_doc)
    _write(root, FRESHNESS, freshness)
    for model in models:
        _write(
            root,
            f"lenses/{model.lens}/results/monitor-{clock_end.isoformat()}.json",
            {
                "lens": model.lens,
                "as_of": freshness["as_of"],
                "clock_end": clock_end.isoformat(),
                "model_version": model.model_version,
                "findings": monitor.lens_findings(model, findings),
            },
        )
    if args.as_json:
        print(json.dumps(monitor.summary(freshness), indent=2, ensure_ascii=False))
    else:
        for f in findings:
            stream = f" {f['stream']}" if f.get("stream") else ""
            print(
                f"  {style.warn(f['kind']):<16} [{f['mode']}] {f['member']}{stream} — {f['detail']}"
            )
        unread = freshness["unread"]
        print(
            style.bold(f"Monitor: {len(findings)} finding(s)")
            + f" over {len(freshness['cubes'])} clock(s), clock {clock_end.isoformat()}; "
            f"{freshness['queries']}/{freshness['max_queries']} queries"
            + (style.warn(f", {len(unread)} item(s) unread (budget)") if unread else "")
        )
    if any(f["kind"] == "provider_down" for f in findings):
        return 1
    return 2 if findings else 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "monitor",
        help="data quality through Cube: data ends per clock and feed, partial latest "
        "months, stale and stopped feeds, dictionary changes — written to profiles/ and "
        "lenses/<lens>/results/. Exit 0 clean, 2 findings, 1 Cube unreadable",
    )
    p.add_argument("--dir", default=".", help="project root (dst.yaml, cube/overlay.yaml)")
    p.add_argument(
        "--as-of",
        dest="as_of",
        help="the clock: the last day data is expected through (default: yesterday, UTC)",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="the summary as JSON")
    p.set_defaults(fn=_monitor)
