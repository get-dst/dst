"""Rows dated after the lens clock on an event date (the day a quote was
decided, an invoice paid) cannot have happened yet: the date is an event on
the evidence (its declaration, else what its name or description says, else
its future rows), every window on it is clipped to the clock, and the monitor
names that date. The invented world is the cube fake's: quotes sent on one
date and decided on another, some decisions dated after the clock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from services.contracts.cube_meta import MetaDoc
from services.contracts.cube_probe import date_meaning, time_kind
from services.contracts.cube_query import CubeQuery
from services.contracts.lens_config import LensConfig
from services.contracts.lens_model import LensModel, Overlay
from services.cube.lens_view import CubeLensView
from services.monitor import cube as monitor
from services.serve.door import load_probe
from services.verify import pre
from services.verify.base import Ask
from tests import _cube_fake as fake

UNDECLARED: dict[str, Any] = {**fake.OVERLAY, "cubes": {}}


def test_a_date_reads_as_what_its_name_or_prose_says() -> None:
    assert date_meaning("decided_at") == "event"
    assert date_meaning("paid_on") == "event"
    assert date_meaning("due_date") == "schedule"
    assert date_meaning("expires_on") == "schedule"
    assert date_meaning("month") is None
    assert date_meaning("stamp", "Stamp", "the day the quote was decided") == "event"
    assert date_meaning("stamp", "planned visit day") == "schedule"
    # declared wins; with nothing said, future rows make a schedule
    assert time_kind("schedule", True, "event") == "schedule"
    assert time_kind(None, True, "event") == "event"
    assert time_kind(None, True, None) == "schedule"
    assert time_kind(None, False, None) == "event"


def _project(tmp_path: Path) -> Path:
    probe, freshness = fake.monitor_files()
    root = tmp_path / "project"
    (root / "profiles").mkdir(parents=True)
    (root / "profiles" / "cube.probe.json").write_text(json.dumps(probe))
    (root / "profiles" / "cube.freshness.json").write_text(json.dumps(freshness))
    return root


def _view(tmp_path: Path) -> CubeLensView:
    overlay = Overlay.model_validate(UNDECLARED)
    return CubeLensView.build(
        fake.doc(), overlay=overlay, probe=load_probe(_project(tmp_path), overlay, fake.doc())
    )


def test_an_undeclared_event_date_with_future_rows_is_still_an_event(tmp_path: Path) -> None:
    view = _view(tmp_path)
    assert view.time_facts("quotes.decided_at").kind == "event"  # type: ignore[union-attr]
    assert view.time_facts("contracts.end_date").kind == "schedule"  # type: ignore[union-attr]


def _window(view: CubeLensView, question: str, body: dict[str, Any]) -> Any:
    query = CubeQuery.model_validate(body)
    a = Ask(
        question=question,
        lens=view,
        config=LensConfig(name="shop"),
        asked=[query],
        today=fake.TODAY,
    )
    return pre.window_misbound(a, [query])


def test_this_week_on_an_event_date_never_counts_the_rows_after_the_clock(
    tmp_path: Path,
) -> None:
    view = _view(tmp_path)
    # Tuesday 2026-09-01, data loaded through Monday 08-31.
    c = _window(
        view,
        "quote win rate this week",
        {
            "measures": ["quotes.won"],
            "timeDimensions": [
                {"dimension": "quotes.decided_at", "dateRange": ["2026-08-31", "2026-09-06"]}
            ],
        },
    )
    assert c.outcome == "disclose" and c.queries is not None
    assert c.queries[0].timeDimensions[0].dateRange == ["2026-08-31", "2026-08-31"]
    assert "clipped: quotes.decided_at" in c.detail


def test_an_event_date_read_without_a_window_or_through_a_filter_is_clipped(
    tmp_path: Path,
) -> None:
    view = _view(tmp_path)
    c = _window(
        view,
        "quotes won by week",
        {
            "measures": ["quotes.won"],
            "timeDimensions": [{"dimension": "quotes.decided_at", "granularity": "week"}],
        },
    )
    assert c.queries is not None
    (f,) = c.queries[0].filters
    assert (f.member, f.operator, f.values) == (
        "quotes.decided_at",
        "beforeOrOnDate",
        ["2026-08-31"],
    )
    c = _window(
        view,
        "quotes won by week",
        {
            "measures": ["quotes.won"],
            "dimensions": ["quotes.decided_at"],
            "filters": [
                {
                    "member": "quotes.decided_at",
                    "operator": "inDateRange",
                    "values": ["2026-08-01", "2026-09-30"],
                }
            ],
        },
    )
    assert c.queries is not None
    assert c.queries[0].filters[0].values == ["2026-08-01", "2026-08-31"]
    # a schedule date is read as asked
    c = _window(
        view,
        "contracts ending by month",
        {
            "measures": ["contracts.count"],
            "timeDimensions": [{"dimension": "contracts.end_date", "granularity": "month"}],
        },
    )
    assert c.outcome == "pass"


def _pipeline_model(overlay: dict[str, Any]) -> LensModel:
    payload = fake.meta_payload()
    payload["cubes"].append(
        {
            "name": "pipeline",
            "type": "view",
            "measures": [{"name": "pipeline.won", "type": "number", "aliasMember": "quotes.won"}],
            "dimensions": [
                {"name": "pipeline.sent_at", "type": "time", "aliasMember": "quotes.sent_at"},
                {"name": "pipeline.decided_at", "type": "time", "aliasMember": "quotes.decided_at"},
            ],
            "segments": [],
        }
    )
    return LensModel.build(
        MetaDoc.parse(payload), Overlay.model_validate(overlay), "pipeline", views=["pipeline"]
    )


def test_the_monitor_names_future_rows_on_the_event_date_not_the_clock() -> None:
    model = _pipeline_model({"cubes": {"quotes": {"default_time_dimension": "sent_at"}}})
    probed = {
        "clock_end": "2026-08-31",
        "queries": 0,
        "data_ends": {
            "quotes.sent_at": {"data_end": "2026-08-31", "grain": "day", "future_rows": False},
            "quotes.decided_at": {"data_end": "2026-08-31", "grain": "day", "future_rows": True},
        },
    }
    _p, fresh, findings = monitor.monitor(
        [model], lambda q: [], clock_end=fake.TODAY.replace(day=1), probed=probed
    )
    future = [f for f in findings if f["kind"] == "future_rows"]
    assert [f["member"] for f in future] == ["quotes.decided_at"]
    assert "event date" in future[0]["detail"] and "clipped" in future[0]["detail"]
    assert fresh["time_dimensions"]["quotes.decided_at"] == {
        "kind": "event",
        "basis": "meaning",
        "future_rows": True,
    }


def test_migrate_model_declares_the_kind_its_evidence_says(tmp_path: Path) -> None:
    from services.project.migrate_cube import Converter
    from tests.test_migrate_cube import _books

    root = _books(tmp_path / "books")
    entity = root / "semantic/entities/invoices.yaml"
    doc = yaml.safe_load(entity.read_text())
    doc["fields"].append({"name": "stamp", "type": "date", "description": "when it was paid"})
    entity.write_text(yaml.safe_dump(doc))
    probe_path = root / "profiles/warehouse.probe.json"
    probe = json.loads(probe_path.read_text())
    for t in probe["tables"]:
        if t["table"] == "fin.invoices":
            t["columns"] += [{"name": "stamp", "max": "2026-10-02"}]
            for c in t["columns"]:
                if c["name"] == "issued_on":
                    c["max"] = "2026-10-01"  # the clock, issued (an event) after the probe ran
    probe_path.write_text(json.dumps(probe))
    overlay = yaml.safe_load(Converter(root).files()["overlay.yaml"])
    assert overlay["cubes"]["invoices"]["clock_kind"] == {
        "issued_on": "event",
        "due_on": "schedule",
        "stamp": "event",
    }
