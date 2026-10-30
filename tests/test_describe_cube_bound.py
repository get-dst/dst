"""describe_lens on a Cube-backed lens stays small enough for an agent to
read inline: a summary per view by default (the one view in full only when
the lens serves one and it fits), the facts of the lens's own cubes only,
under 40 KB, and any view in full on request. The invented world: a
wholesaler's warehouse with several wide views and a project of other cubes."""

from __future__ import annotations

from typing import Any

from services.contracts.cube_meta import MetaDoc
from services.contracts.cube_probe import CubeProbe
from services.contracts.lens_model import Overlay
from services.cube.lens_view import CubeLensView
from services.lenses.describe_cube import DRIVER_DESCRIBE_MAX_CHARS, describe_view

LONG = "a column a wholesaler's analysts read every week, described at some length " * 3


def _payload(views: int) -> dict[str, Any]:
    cubes: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for v in range(views):
        cube = f"stock_{v}"
        dims = [
            {"name": f"{cube}.col_{i}", "type": "string", "title": f"col {i}", "description": LONG}
            for i in range(120)
        ]
        dims.append({"name": f"{cube}.counted_on", "type": "time", "title": "counted on"})
        cubes.append(
            {
                "name": cube,
                "type": "cube",
                "measures": [
                    {"name": f"{cube}.units", "type": "number", "aggType": "sum", "title": "units"}
                ],
                "dimensions": dims,
                "segments": [],
            }
        )
        out.append(
            {
                "name": f"view_{v}",
                "type": "view",
                "measures": [
                    {
                        "name": f"view_{v}.units",
                        "type": "number",
                        "aggType": "sum",
                        "aliasMember": f"{cube}.units",
                    }
                ],
                "dimensions": [
                    {
                        "name": f"view_{v}.{d['name'].split('.')[1]}",
                        "type": d["type"],
                        "aliasMember": d["name"],
                    }
                    for d in dims
                ],
                "segments": [],
            }
        )
    return {"cubes": cubes + out}


def _view(views: int) -> CubeLensView:
    doc = MetaDoc.parse(_payload(views))
    lens_cubes = {f"stock_{v}" for v in range(views)}
    other = {
        f"elsewhere_{i}": {"time_dimensions": {"day": {"data_end": "2026-08-31"}}}
        for i in range(400)
    }
    probe = CubeProbe.model_validate(
        {
            "clock_end": "2026-08-31",
            "cubes": {
                **other,
                **{
                    c: {"time_dimensions": {"counted_on": {"data_end": "2026-08-31"}}}
                    for c in lens_cubes
                },
            },
            "dictionaries": {
                f"stock_{v}.col_{i}": [f"value number {j}" for j in range(40)]
                for v in range(views)
                for i in range(120)
            },
        }
    )
    overlay = Overlay.model_validate(
        {"lenses": {"stock": {"views": [f"view_{v}" for v in range(views)]}}}
    )
    return CubeLensView.build(doc, overlay=overlay, probe=probe, lens="stock")


def test_a_wide_lens_describes_as_a_summary_per_view_under_the_bound() -> None:
    out = describe_view(_view(3), name="stock")
    assert len(out.model_dump_json()) < DRIVER_DESCRIBE_MAX_CHARS <= 40_000
    assert {v.detail for v in out.views} == {"summary"}
    assert all(v.measures for v in out.views)
    # only the lens's own cubes' facts, never the whole project's
    assert set(out.freshness.time_dimensions) == {f"stock_{v}.counted_on" for v in range(3)}
    assert "describe again with `view`" in out.note


def test_a_view_asked_for_is_listed_in_full_and_still_bounded() -> None:
    out = describe_view(_view(3), name="stock", full_view="view_1")
    full = [v for v in out.views if v.detail == "full"]
    assert [v.name for v in full] == ["view_1"] and full[0].members
    assert len(out.model_dump_json()) < DRIVER_DESCRIBE_MAX_CHARS


def test_a_lens_of_one_view_that_fits_is_listed_in_full() -> None:
    doc = MetaDoc.parse(
        {
            "cubes": [
                {
                    "name": "stock",
                    "type": "cube",
                    "measures": [{"name": "stock.units", "type": "number", "aggType": "sum"}],
                    "dimensions": [],
                    "segments": [],
                },
                {
                    "name": "small",
                    "type": "view",
                    "measures": [
                        {"name": "small.units", "type": "number", "aliasMember": "stock.units"}
                    ],
                    "dimensions": [],
                    "segments": [],
                },
            ]
        }
    )
    view = CubeLensView.build(
        doc, overlay=Overlay.model_validate({"lenses": {"s": {"views": ["small"]}}}), lens="s"
    )
    out = describe_view(view, name="s")
    assert [v.detail for v in out.views] == ["full"]
