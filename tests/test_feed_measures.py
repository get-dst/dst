"""A feed is judged for partial periods by the measures its declaration
names (overlay ``feed_measures``), else by its row counts, and the verdict
rides every measure read on that feed's clock. The invented world: a field
service whose jobs are all logged for August, but only half of August's
work is billed yet."""

from __future__ import annotations

from datetime import date
from typing import Any

from services.contracts.cube_meta import MetaDoc
from services.contracts.cube_probe import CubeProbe
from services.contracts.cube_query import CubeQuery
from services.contracts.lens_model import LensModel, Overlay
from services.cube.lens_view import CubeLensView
from services.monitor import cube as monitor
from services.verify import post
from services.verify.base import Ask

CLOCK = date(2026, 8, 31)


def _meta() -> MetaDoc:
    def m(name: str, agg: str) -> dict[str, Any]:
        return {"name": f"jobs.{name}", "type": "number", "aggType": agg, "title": name}

    def alias(name: str, kind: str, agg: str | None = None) -> dict[str, Any]:
        out = {"name": f"field.{name}", "type": kind, "aliasMember": f"jobs.{name}"}
        return {**out, "aggType": agg} if agg else out

    return MetaDoc.parse(
        {
            "cubes": [
                {
                    "name": "jobs",
                    "type": "cube",
                    "title": "Field jobs",
                    "measures": [m("count", "count"), m("billed", "sum")],
                    "dimensions": [
                        {"name": "jobs.completed_on", "type": "time", "title": "completed on"}
                    ],
                    "segments": [],
                },
                {
                    "name": "field",
                    "type": "view",
                    "measures": [
                        alias("count", "number", "count"),
                        alias("billed", "number", "sum"),
                    ],
                    "dimensions": [alias("completed_on", "time")],
                    "segments": [],
                },
            ]
        }
    )


def _model(**jobs: Any) -> LensModel:
    overlay = Overlay.model_validate(
        {"cubes": {"jobs": {"default_time_dimension": "completed_on", **jobs}}}
    )
    return LensModel.build(_meta(), overlay, "field", views=["field"])


def _load(q: CubeQuery) -> list[dict[str, Any]]:
    """Twenty months of 100 jobs billed 1,000 a month; August's 100 jobs
    are all logged, and billed 500."""
    member = q.measures[0]
    rows = []
    d = date(2025, 1, 1)
    while d <= CLOCK:
        last = d.month == CLOCK.month and d.year == CLOCK.year
        value = 100.0 if member == "field.count" else (500.0 if last else 1000.0)
        rows.append({"field.completed_on.month": f"{d.isoformat()}T00:00:00.000", member: value})
        d = monitor._month_add(d, 1)
    return rows


PROBED = {
    "clock_end": CLOCK.isoformat(),
    "queries": 0,
    "data_ends": {
        "jobs.completed_on": {"data_end": "2026-08-31", "grain": "day", "future_rows": False}
    },
}


def _monitor(model: LensModel) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _p, fresh, findings = monitor.monitor([model], _load, clock_end=CLOCK, probed=PROBED)
    return fresh, findings


def test_complete_rows_with_half_billed_amounts_are_a_partial_feed_when_declared() -> None:
    fresh, findings = _monitor(_model(feed_measures=["billed"]))
    assert ("partial_period", "jobs.billed") in {(f["kind"], f["member"]) for f in findings}
    # the row count is complete on its own, but the feed is judged by billing
    count = fresh["measures"]["jobs.count"]["partial"]
    assert count["period"] == "2026-08" and count["judged_by"] == "jobs.billed"
    # and the lane discloses it on a query that reads only the count
    view = CubeLensView.build(
        _meta(),
        overlay=Overlay.model_validate({}),
        probe=CubeProbe.model_validate({"clock_end": "2026-08-31", "measures": fresh["measures"]}),
    )
    q = CubeQuery.model_validate(
        {
            "measures": ["jobs.count"],
            "timeDimensions": [
                {"dimension": "jobs.completed_on", "dateRange": ["2026-01-01", "2026-08-31"]}
            ],
        }
    )
    c = post.partial_period(Ask(question="jobs this year", lens=view, asked=[q]), [q])
    assert c.outcome == "disclose" and "judged by jobs.billed" in c.detail


def test_undeclared_a_feed_is_judged_by_its_row_counts() -> None:
    fresh, _findings = _monitor(_model())
    assert "partial" not in fresh["measures"]["jobs.count"]  # every job is logged
    # the billed amount is still judged on its own
    assert fresh["measures"]["jobs.billed"]["partial"]["period"] == "2026-08"
