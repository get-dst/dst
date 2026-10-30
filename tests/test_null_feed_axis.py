"""A feed axis with no value on some rows. The invented world: a field
service whose jobs carry the crew that did them, except call-outs, which
carry none and stopped being logged on 2026-08-10. The probe writes those
rows as their own stream, `(none)`, read where the axis is not set; and a
monitor file written before that (a null on the axis) never takes a door
down: the loader skips the record and says so on that cube alone."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from services.contracts.cube_meta import MetaDoc
from services.contracts.cube_probe import NO_VALUE
from services.contracts.cube_query import CubeQuery
from services.contracts.lens_model import LensModel, Overlay
from services.monitor import cube as monitor
from services.serve.door import FRESHNESS_FILE, PROBE_FILE, load_probe

CLOCK = date(2026, 8, 31)


def _doc() -> MetaDoc:
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
                    "measures": [{"name": "jobs.count", "type": "number", "aggType": "count"}],
                    "dimensions": [
                        {"name": "jobs.completed_on", "type": "time", "title": "completed on"},
                        {"name": "jobs.crew", "type": "string", "title": "crew"},
                    ],
                    "segments": [],
                },
                {
                    "name": "field",
                    "type": "view",
                    "measures": [alias("count", "number", "count")],
                    "dimensions": [alias("completed_on", "time"), alias("crew", "string")],
                    "segments": [],
                },
            ]
        }
    )


def _overlay() -> Overlay:
    return Overlay.model_validate(
        {"cubes": {"jobs": {"default_time_dimension": "completed_on", "feed_axes": ["crew"]}}}
    )


def _model() -> LensModel:
    return LensModel.build(_doc(), _overlay(), "field", views=["field"])


ENDS = {"north": "2026-08-31", None: "2026-08-10"}


def _load(q: CubeQuery) -> list[dict[str, Any]]:
    if not q.timeDimensions:  # the feeds: one row per crew, call-outs carry none
        return [{"field.crew": "north"}, {"field.crew": None}]
    pins = {f.member: f for f in q.filters if f.member == "field.crew"}
    pin = pins.get("field.crew")
    if pin is None:  # unpinned: every row, so the newest day of any crew
        end = max(e for e in ENDS.values() if e)
    elif pin.operator == "notSet":
        end = ENDS[None]
    else:
        end = ENDS[(pin.values or [""])[0]]
    if any(f.operator == "afterDate" for f in q.filters):
        return []
    return [{"field.completed_on.day": f"{end}T00:00:00.000"}]


def test_the_probe_writes_a_null_axis_value_as_its_own_stream() -> None:
    doc = monitor.probe([_model()], _load, clock_end=CLOCK)
    streams = {s["values"]["crew"]: s["end"] for s in doc["streams"]["jobs"]}
    # read where the crew is not set, not over every row
    assert streams == {"north": "2026-08-31", NO_VALUE: "2026-08-10"}
    _probe, fresh, findings = monitor.monitor([_model()], _load, clock_end=CLOCK, probed=doc)
    stopped = [f for f in findings if f["kind"] == "feed_stopped"]
    assert [f["stream"] for f in stopped] == [{"crew": NO_VALUE}]
    assert all(
        v is not None for s in fresh["cubes"]["jobs"]["streams"] for v in s["values"].values()
    )


def _project(tmp_path: Path, fresh: dict[str, Any]) -> Path:
    (tmp_path / "profiles").mkdir()
    (tmp_path / PROBE_FILE).write_text(json.dumps({"clock_end": "2026-08-31"}))
    (tmp_path / FRESHNESS_FILE).write_text(json.dumps(fresh))
    return tmp_path


def test_a_null_record_in_the_monitor_file_never_takes_the_door_down(tmp_path: Path) -> None:
    fresh = {
        "clock_end": "2026-08-31",
        "cubes": {
            "jobs": {
                "clock": "jobs.completed_on",
                "streams": [
                    {"values": {"crew": "north"}, "end": "2026-08-31"},
                    {"values": {"crew": None}, "end": "2026-08-10", "stopped": True},
                ],
            },
            "visits": {
                "clock": "visits.visited_on",
                "streams": [{"values": {"area": "east"}, "end": "2026-08-30"}],
            },
        },
        "measures": {
            "jobs.billed": {
                "cube": "jobs",
                "clock": "jobs.completed_on",
                "partial_streams": [
                    {
                        "period": "2026-08",
                        "partial_from": "2026-08-01",
                        "partial_through": "2026-08-31",
                        "partial_ratio": 0.5,
                        "values": {"crew": None},
                    }
                ],
            },
            "visits.count": {"cube": "visits", "clock": "visits.visited_on"},
        },
    }
    probe = load_probe(_project(tmp_path, fresh))
    jobs = probe.cubes["jobs"]
    # the readable stream stays, the null one is skipped and said so
    assert [s.values for s in jobs.streams] == [{"crew": "north"}]
    assert len(jobs.warnings) == 2 and all("skipped" in w for w in jobs.warnings)
    assert probe.measures["jobs.billed"].partial_streams == []
    # the other cube is untouched, and says nothing
    assert probe.cubes["visits"].streams[0].values == {"area": "east"}
    assert probe.cubes["visits"].warnings == []
    assert "visits.count" in probe.measures


def test_the_skipped_record_is_disclosed_on_its_cube(tmp_path: Path) -> None:
    from services.cube.lens_view import CubeLensView
    from services.verify import post
    from services.verify.base import Ask

    fresh = {
        "clock_end": "2026-08-31",
        "cubes": {
            "jobs": {
                "clock": "jobs.completed_on",
                "streams": [
                    {"values": {"crew": "north"}, "end": "2026-08-31"},
                    {"values": {"crew": None}, "end": "2026-08-10"},
                ],
            }
        },
        "measures": {"jobs.count": {"cube": "jobs", "clock": "jobs.completed_on"}},
    }
    probe = load_probe(_project(tmp_path, fresh))
    view = CubeLensView.build(
        _doc(), overlay=_overlay(), probe=probe, lens="field", views=["field"]
    )
    q = CubeQuery.model_validate(
        {
            "measures": ["field.count"],
            "timeDimensions": [
                {"dimension": "field.completed_on", "dateRange": ["2026-08-01", "2026-08-31"]}
            ],
        }
    )
    c = post.data_gap(Ask(question="jobs in August", lens=view, asked=[q]), q)
    assert c.outcome == "disclose" and "stream 2 of jobs does not read" in c.detail
