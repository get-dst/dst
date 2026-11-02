"""Data quality through Cube: the probe's dictionaries and data ends, the
monitor's findings, the CLI's files and exit codes, the data_quality block.

The fake Cube answers /v1/meta with the fixture's recorded meta and /v1/load
from a cassette of a real Cube Core's answers to the very queries the monitor
makes over the cube_shop warehouse (a web feed that stops on 2026-07-31, a
till September loading short). A query the cassette lacks is a 400, so a
change in what the monitor asks fails here instead of passing on a guess. The
live lane at the end runs the same against a real Cube."""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from services.contracts.lens_model import LensModel, Overlay
from services.cube.client import CubeClient
from services.cube.context import CubeContext
from services.monitor import cube as monitor
from services.monitor import status as monitor_status
from tests import cube_shop

CLOCK = date(2026, 9, 14)
CASSETTE = cube_shop.FIXTURE / "load_cassette.json"


class CassetteCube:
    def __init__(self) -> None:
        self.answers: dict[str, list[dict[str, Any]]] = json.loads(CASSETTE.read_text())
        self.asked: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/meta"):
            return httpx.Response(200, json=cube_shop.meta_payload())
        query = json.loads(request.content)["query"]
        self.asked.append(query)
        key = json.dumps(query, sort_keys=True)
        if key not in self.answers:
            return httpx.Response(400, json={"error": f"not in the cassette: {key}"})
        return httpx.Response(200, json={"query": query, "data": self.answers[key]})


def _overlay(feed_axes: list[str] | None = None, clock_kind: str | None = None) -> Overlay:
    data = yaml.safe_load((cube_shop.PROJECT / "cube" / "overlay.yaml").read_text())
    if feed_axes:
        data["cubes"]["orders"]["feed_axes"] = feed_axes
    if clock_kind:
        data["cubes"]["orders"]["clock_kind"] = {"ordered_at": clock_kind}
    return Overlay.model_validate(data)


def _run(
    feed_axes: list[str] | None = None, clock_kind: str | None = None, **kwargs: Any
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    fake = CassetteCube()
    client = CubeClient("http://cube.test", None, transport=httpx.MockTransport(fake.handle))
    ctx = CubeContext.operator()
    overlay = _overlay(feed_axes, clock_kind)
    model = LensModel.build(client.meta(ctx, extended=True), overlay, "sales")
    return monitor.monitor([model], lambda q: client.load(q, ctx).data, clock_end=CLOCK, **kwargs)


def _kinds(findings: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return sorted((f["kind"], f["member"]) for f in findings)


def test_probe_reads_dictionaries_and_data_ends_through_cube() -> None:
    probe, _fresh, _findings = _run()
    assert probe["dictionaries"]["orders.channel"] == ["counter", "web"]
    assert probe["dictionaries"]["stores.region"] == ["Coast", "North", "South"]
    assert "stores.manager_email" not in probe["dictionaries"]  # hidden: never collected
    assert probe["data_ends"]["orders.ordered_at"] == {
        "data_end": "2026-09-14",
        "grain": "day",
        "future_rows": False,
    }
    # a text YYYY-MM month cast to a date ends on its month's last day
    assert probe["data_ends"]["stock_monthly.month"]["grain"] == "month"
    assert probe["data_ends"]["stock_monthly.month"]["data_end"] == "2026-08-31"
    # opening dates that happen to be first-of-month days are not a month grain
    assert probe["data_ends"]["stores.opened_on"]["grain"] == "day"
    assert probe["unread"] == [] and probe["errors"] == []


def test_monitor_names_the_stopped_feed_and_the_short_month_per_feed() -> None:
    _probe, fresh, findings = _run(feed_axes=["channel"])
    stopped = [f for f in findings if f["kind"] == "feed_stopped"]
    assert [(f["member"], f["stream"]) for f in stopped] == [
        ("orders.ordered_at", {"channel": "web"})
    ]
    assert stopped[0]["mode"] == "data_gap"
    partial = {f["member"]: f for f in findings if f["kind"] == "partial_period"}
    assert set(partial) == {
        "orders.revenue",
        "orders.order_count",
        "orders.discount_total",
        "orders.tips_eur",
    }
    assert partial["orders.revenue"]["stream"] == {"channel": "counter"}
    assert fresh["measures"]["orders.revenue"]["partial_streams"][0]["period"] == "2026-09"
    # the level (stock on hand) is never judged; its monthly clock is current
    assert "stock_monthly.stock_on_hand" not in fresh["measures"]
    assert not [f for f in findings if f["kind"] == "stale"]


def test_without_declared_feeds_the_series_is_judged_whole() -> None:
    _probe, _fresh, findings = _run()
    assert not [f for f in findings if f["kind"] == "feed_stopped"]
    # the web-only measure's September is empty: the stopped feed shows here
    assert ("partial_period", "orders.shipped_revenue") in _kinds(findings)


def test_future_rows_on_an_event_clock_are_a_finding_and_judged_as_an_event() -> None:
    probe, _fresh, _findings = _run()
    probe["data_ends"]["orders.ordered_at"]["future_rows"] = True
    # undeclared, "ordered" says the date is an event: the rows after the
    # clock are a finding, and loading is still judged on it
    _p, fresh, findings = _run(probed=probe)
    assert fresh["cubes"]["orders"]["kind"] == "event"
    assert fresh["time_dimensions"]["orders.ordered_at"]["basis"] == "meaning"
    (future,) = [f for f in findings if f["kind"] == "future_rows"]
    assert "event date" in future["detail"] and "clipped" in future["detail"]
    assert ("partial_period", "orders.revenue") in _kinds(findings)
    # declared a schedule, nothing is said and no loading judgment is read
    # on a due or end date (shipped_revenue counts on its own clock,
    # shipped_on, and is still judged)
    _p, fresh, findings = _run(clock_kind="schedule", probed=probe)
    assert fresh["cubes"]["orders"]["kind"] == "schedule"
    assert not [f for f in findings if f["kind"] == "future_rows"]
    judged = {f["member"] for f in findings if f["kind"] in ("partial_period", "stale")}
    assert judged == {"orders.shipped_revenue"}


def test_the_budget_says_what_it_left_unread() -> None:
    probe, fresh, _findings = _run(max_queries=4)
    assert probe["queries"] == 4
    assert probe["unread"] and fresh["unread"]
    assert any(u.startswith("data end ") for u in fresh["unread"])


def test_a_declared_dictionary_that_moved_is_a_finding() -> None:
    fake = CassetteCube()
    client = CubeClient("http://cube.test", None, transport=httpx.MockTransport(fake.handle))
    ctx = CubeContext.operator()
    meta = client.meta(ctx, extended=True)
    model = LensModel.build(meta, _overlay(), "sales")
    for m in model.members:
        if m.base == "orders.channel" and m.dimension is not None:
            m.dimension.dictionary = ["counter", "phone"]
    _p, _f, findings = monitor.monitor([model], lambda q: client.load(q, ctx).data, clock_end=CLOCK)
    moved = {(f["kind"], f["detail"]) for f in findings if f["member"] == "orders.channel"}
    assert moved == {("unbound_value_new", "web"), ("unbound_value_vanished", "phone")}


def _months(values: list[float]) -> dict[str, float]:
    out, d = {}, date(2025, 1, 1)
    for v in values:
        out[d.strftime("%Y-%m")] = v
        d = monitor._month_add(d, 1)
    return out


@pytest.mark.parametrize(
    ("series", "partial"),
    [
        ([100.0] * 15 + [30.0], True),  # steady, then a short month
        ([100.0, 40.0] * 7 + [100.0, 45.0], False),  # lumpy: dips like this every other month
        ([100.0] * 6 + [30.0], False),  # too little history to judge
        ([5.0] * 15 + [0.0], False),  # too sparse to judge
        # deeper than any month before it, but the same month a year ago dipped
        # alike: a seasonal low, not a partial load
        ([100.0] * 7 + [45.0] + [100.0] * 11 + [40.0], False),
    ],
)
def test_the_partial_rule(series: list[float], partial: bool) -> None:
    last = date(2025, 1, 1)
    for _ in range(len(series) - 1):
        last = monitor._month_add(last, 1)
    through = monitor._month_end(last)
    got = monitor._partial(_months(series), date(2025, 1, 1), through)
    assert (got is not None) is partial


def test_the_cli_writes_the_artifacts_and_gates_on_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    from services.cli import main as cli_main
    from services.cli import monitor as cli_monitor

    root = tmp_path / "project"
    shutil.copytree(cube_shop.PROJECT, root)
    fake = CassetteCube()

    class Client(CubeClient):
        def __init__(self, url: str, secret: str | None, **kw: Any) -> None:
            super().__init__(url, secret, transport=httpx.MockTransport(fake.handle), **kw)

    monkeypatch.setattr("services.cube.client.CubeClient", Client)
    monkeypatch.setattr(
        sys, "argv", ["dst", "monitor", "--dir", str(root), "--as-of", "2026-09-14"]
    )
    assert cli_main.main() == 2
    out = capsys.readouterr().out
    assert "partial_period" in out and "Monitor: 5 finding(s)" in out
    fresh = json.loads((root / cli_monitor.FRESHNESS).read_text())
    assert fresh["clock_end"] == "2026-09-14"
    assert (root / "profiles" / "cube.probe.json").exists()
    per_lens = json.loads((root / "lenses/sales/results/monitor-2026-09-14.json").read_text())
    assert per_lens["model_version"] == cube_shop.meta().model_version(
        [
            v
            for v in yaml.safe_load((root / "cube/overlay.yaml").read_text())["lenses"]["sales"][
                "views"
            ]
        ]
    )
    assert len(per_lens["findings"]) == 5

    monkeypatch.setattr(
        sys, "argv", ["dst", "probe", "--cube", "--dir", str(root), "--as-of", "2026-09-14"]
    )
    assert cli_main.main() == 0


def test_the_data_quality_block_summarises_by_mode() -> None:
    _p, fresh, _f = _run(feed_axes=["channel"])
    block = monitor.summary(fresh)
    assert block["by_mode"] == {"data_gap": 5}
    assert block["by_kind"] == {"feed_stopped": 1, "partial_period": 4}
    assert monitor_status.FRESHNESS_PATH == "profiles/cube.freshness.json"


LIVE_URL = os.environ.get("DST_TEST_CUBE_URL")


@pytest.mark.skipif(not LIVE_URL, reason="DST_TEST_CUBE_URL names no Cube to test against")
def test_live_monitor_over_the_fixture_cube_finds_what_the_cassette_does() -> None:
    """A live Cube serving tests/fixtures/cube_shop (make_data.py says how)."""
    secret = os.environ.get("DST_TEST_CUBE_SECRET") or os.environ.get("CUBEJS_API_SECRET")
    client = CubeClient(str(LIVE_URL), secret)
    ctx = CubeContext.operator()
    try:
        meta = client.meta(ctx, extended=True)
        if meta.cube("sales_orders") is None:
            pytest.skip("the live Cube does not serve the cube_shop fixture model")
        model = LensModel.build(meta, _overlay(["channel"]), "sales")
        _p, _f, live = monitor.monitor([model], lambda q: client.load(q, ctx).data, clock_end=CLOCK)
    finally:
        client.close()
    _p, _f, recorded = _run(feed_axes=["channel"])
    assert _kinds(live) == _kinds(recorded)


# ── a low month is not a partial load ───────────────────────────────────────
#
# One invented feed of deliveries, a steady ten a day since January 2025, and
# its August 2026 written three ways. The volume rule flags each; only the
# evidence of loading decides.


def _deliveries(
    august: dict[int, float], loaded_at: bool = False, matures_after: dict[str, int] | None = None
) -> LensModel:
    from services.contracts.cube_meta import MetaDoc

    dims = [{"name": "deliveries.delivered_at", "type": "time"}]
    if loaded_at:
        dims.append({"name": "deliveries.synced_at", "type": "time"})
    meta = MetaDoc.parse(
        {
            "cubes": [
                {
                    "name": "deliveries",
                    "type": "cube",
                    "measures": [
                        {
                            "name": "deliveries.count",
                            "type": "number",
                            "aggType": "count",
                            "meta": {"dst": {"clock": "delivered_at"}},
                        }
                    ],
                    "dimensions": dims,
                },
                {
                    "name": "ops",
                    "type": "view",
                    "measures": [
                        {
                            "name": "ops.count",
                            "type": "number",
                            "aggType": "count",
                            "aliasMember": "deliveries.count",
                        }
                    ],
                    "dimensions": [
                        {
                            "name": f"ops.{d['name'].split('.')[1]}",
                            "type": "time",
                            "aliasMember": d["name"],
                        }
                        for d in dims
                    ],
                },
            ]
        }
    )
    cube: dict[str, Any] = {"loaded_at": "synced_at"} if loaded_at else {}
    if matures_after:
        cube["matures_after"] = matures_after
    overlay = Overlay.model_validate({"cubes": {"deliveries": cube}, "lenses": {"ops": {}}})
    return LensModel.build(meta, overlay, "ops", views=["ops"])


def _daily(august: dict[int, float]) -> dict[date, float]:
    out: dict[date, float] = {}
    d = date(2025, 1, 1)
    while d < date(2026, 8, 1):
        out[d] = 10.0
        d = date.fromordinal(d.toordinal() + 1)
    out.update({date(2026, 8, k): v for k, v in august.items()})
    return out


def _load(days: dict[date, float], synced: date | None = None) -> Any:
    def load(q: Any) -> list[dict[str, Any]]:
        (td,) = q.timeDimensions
        if td.dimension == "ops.synced_at":
            return [{"ops.synced_at.day": f"{synced}T00:00:00.000"}] if synced else []
        lo, hi = (date.fromisoformat(x[:10]) for x in td.dateRange)
        grain = td.granularity
        rows: dict[str, float] = {}
        for d, v in days.items():
            if lo <= d <= hi and v:
                k = (d.replace(day=1) if grain == "month" else d).isoformat()
                rows[k] = rows.get(k, 0.0) + v
        return [
            {f"ops.delivered_at.{grain}": f"{k}T00:00:00.000", "ops.count": v}
            for k, v in sorted(rows.items())
        ]

    return load


PROBED = {
    "data_ends": {
        "deliveries.delivered_at": {"data_end": "2026-08-31", "grain": "day", "future_rows": False}
    },
    "queries": 0,
}
LOW = {k: 3.0 for k in range(1, 32)}  # a genuinely quiet month, loaded to its last day
HALF = {**{k: 10.0 for k in range(1, 16)}, 31: 5.0}  # loaded to mid-month, one late row


def _partials(model: LensModel, load: Any) -> list[dict[str, Any]]:
    _p, _f, findings = monitor.monitor([model], load, clock_end=date(2026, 8, 31), probed=PROBED)
    return [f for f in findings if f["kind"] == "partial_period"]


def _recorded(model: LensModel, load: Any) -> dict[str, Any]:
    _p, fresh, _f = monitor.monitor([model], load, clock_end=date(2026, 8, 31), probed=PROBED)
    return dict(fresh["measures"]["deliveries.count"].get("partial") or {})


def test_a_complete_low_month_is_not_a_partial_period() -> None:
    assert _partials(_deliveries(LOW), _load(_daily(LOW))) == []


def test_a_half_loaded_month_is_a_partial_period_and_says_why() -> None:
    (found,) = _partials(_deliveries(HALF), _load(_daily(HALF)))
    assert found["member"] == "deliveries.count" and "2026-08" in found["detail"]
    assert "second half" in found["detail"]


def test_a_complete_month_still_maturing_is_said_as_complete_not_partially_loaded() -> None:
    """A low month whose load evidence says complete (the stamp is after the
    period closed) is recorded only where the overlay declares the measure's
    values mature after the period, and then as complete with its values
    still maturing: never as partially loaded. Load evidence of a short load
    still says partially loaded, maturing declared or not."""
    model = _deliveries(LOW, loaded_at=True, matures_after={"count": 30})
    (found,) = _partials(model, _load(_daily(LOW), synced=date(2026, 9, 1)))
    assert "complete; values still maturing (deliveries.count" in found["detail"]
    recorded = _recorded(model, _load(_daily(LOW), synced=date(2026, 9, 1)))
    assert recorded["maturing"] == "deliveries.count" and recorded["load_evidence"] is False
    short = _recorded(model, _load(_daily(LOW), synced=date(2026, 8, 30)))
    assert short["load_evidence"] is True and "maturing" not in short
    assert short["evidence"].startswith("last loaded 2026-08-30")
    # Matured: thirty days after August ends is past the clock of mid-October.
    _p, fresh, _f = monitor.monitor(
        [model],
        _load(_daily(LOW), synced=date(2026, 9, 1)),
        clock_end=date(2026, 10, 15),
        probed=PROBED,
    )
    assert "partial" not in fresh["measures"]["deliveries.count"]


def test_the_feed_s_last_load_decides_where_it_is_declared() -> None:
    """A declared load stamp outranks the day pattern: loaded after the
    period closed, a front-loaded month is complete; last loaded before it
    closed, an even month is not."""
    model = _deliveries(HALF, loaded_at=True)
    assert _partials(model, _load(_daily(HALF), synced=date(2026, 9, 1))) == []
    (found,) = _partials(model, _load(_daily(LOW), synced=date(2026, 8, 30)))
    assert "last loaded 2026-08-30" in found["detail"]
