"""The cube lane end to end, with no Cube and no model: a fake Cube compiles
each query the way Cube does, dst runs the compiled SQL through its own DuckDB
connector under the guard, and the checks shape what is served."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from services.connectors.duckdb import DuckDBConnector
from services.contracts.cube_query import Binding, Candidate, CubeQuery
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_config import LensConfig
from services.contracts.lens_model import Overlay
from services.cube.client import CubeClient
from services.cube.context import CubeContext
from services.serve.cube_lane import CubeLane, inline_params
from tests import _cube_fake as fake


@pytest.fixture()
def lane(tmp_path: Path) -> CubeLane:
    return _lane(tmp_path)


def _lane(tmp_path: Path, client: CubeClient | None = None, **view: Any) -> CubeLane:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = fake.world(tmp_path / "world.duckdb")
    return CubeLane(
        lens="shop",
        config=LensConfig(name="shop"),
        view=fake.view(**view),
        client=client or fake.client(),
        ctx=CubeContext.operator(lens="shop"),
        connector=DuckDBConnector(str(path), profile=False),
        dialect="duckdb",
        today=fake.TODAY,
        org_id="org-test",
        caller="analyst",
        request_id="req_test",
    )


def _serve(
    lane: CubeLane, question: str, body: dict[str, Any], ledger: list[tuple[str, str, str]]
) -> Any:
    led = [Binding(word=w, kind=k, target=t, source="test") for w, k, t in ledger]  # type: ignore[arg-type]
    query = CubeQuery.model_validate(body)
    return lane.serve(
        question, [Candidate(query=query, ledger=led, lane="frontier")], ledger=led, lane="frontier"
    )


SOUTH = {"member": "orders.company", "operator": "equals", "values": ["South AB"]}
AUG = {"dimension": "orders.ordered_at", "dateRange": ["2026-08-01", "2026-08-31"]}


def test_inline_params_writes_literals_outside_quotes() -> None:
    sql = "SELECT '?' AS q, \"a?\" FROM t WHERE x = ? AND y = $2 AND z = 'it''s ?'"
    assert inline_params(sql, ["o'k", 3]) == (
        "SELECT '?' AS q, \"a?\" FROM t WHERE x = 'o''k' AND y = 3 AND z = 'it''s ?'"
    )


def test_an_answer_carries_the_ask_the_sql_and_the_rows_on_its_receipt(lane: CubeLane) -> None:
    resp, trace = _serve(
        lane,
        "south revenue in August",
        {"measures": ["orders.revenue"], "timeDimensions": [AUG], "filters": [SOUTH]},
        [
            ("south", "value", "South AB"),
            ("revenue", "member", "orders.revenue"),
            ("August", "period", "2026-08"),
        ],
    )
    assert resp.status == "ok", resp.answer
    assert resp.data is not None and resp.data.rows == [[510.0]]
    r = resp.receipt
    assert r is not None
    assert r.query_sha256 and r.sql_sha256 and r.result_sha256 and r.model_version
    assert r.checks and "words:pass" in r.checks and "freshness:pass" in r.checks
    # dst ran Cube's compiled SQL itself, parameters written in.
    assert resp.sql and "'South AB'" in resp.sql and "?" not in resp.sql
    assert trace.cube_query and trace.cube_query[0]["measures"] == ["orders.revenue"]
    assert trace.ledger and trace.serving == "cube:frontier"


def test_the_same_ask_hashes_the_same_every_time(tmp_path: Path) -> None:
    seen = set()
    for i in range(3):
        resp, _ = _serve(
            _lane(tmp_path / str(i)),
            "orders by status",
            {"measures": ["orders.count"], "dimensions": ["orders.status"]},
            [("orders", "member", "orders.count"), ("status", "member", "orders.status")],
        )
        r = resp.receipt
        assert r is not None
        seen.add((r.query_sha256, r.sql_sha256, r.result_sha256))
    assert len(seen) == 1


def test_a_group_figure_from_a_stopped_feed_is_served_with_the_gap_named(lane: CubeLane) -> None:
    resp, _ = _serve(
        lane,
        "group revenue in August",
        {"measures": ["orders.revenue"], "timeDimensions": [AUG]},
        [
            ("group", "member", "orders.revenue"),
            ("revenue", "member", "orders.revenue"),
            ("August", "period", "2026-08"),
        ],
    )
    assert resp.status == "ok"
    assert resp.confidence == "partial" and resp.mode == FailureMode.data_gap
    assert "North Oy" in resp.answer and "2026-06-05" in resp.answer


def test_a_ranking_names_ties_and_never_ranks_the_empty_bucket(lane: CubeLane) -> None:
    resp, _ = _serve(
        lane,
        "top 2 customers by orders in August",
        {
            "measures": ["orders.count"],
            "dimensions": ["orders.customer_id"],
            "timeDimensions": [AUG],
            "order": {"orders.count": "desc"},
            "limit": 2,
        },
        [
            ("customers", "member", "orders.customer_id"),
            ("orders", "member", "orders.count"),
            ("August", "period", "2026-08"),
        ],
    )
    assert resp.status == "ok", resp.answer
    assert resp.data is not None
    ids = [row[0] for row in resp.data.rows]
    assert None not in ids
    assert ids[0] == "C1" and len(ids) == 4  # C1 (2), then three customers tied at 1
    assert "unassigned bucket" in resp.answer and "tie at the limit" in resp.answer


def test_a_level_over_a_quarter_is_the_last_snapshot_not_the_sum(lane: CubeLane) -> None:
    resp, _ = _serve(
        lane,
        "headcount by company at the end of Q2",
        {
            "measures": ["staff.headcount"],
            "dimensions": ["staff.company"],
            "timeDimensions": [
                {"dimension": "staff.month", "dateRange": ["2026-04-01", "2026-06-30"]}
            ],
        },
        [
            ("headcount", "member", "staff.headcount"),
            ("company", "member", "staff.company"),
            ("end of Q2", "period", "2026-04-01..2026-06-30"),
        ],
    )
    assert resp.status == "ok", resp.answer
    assert resp.data is not None
    i = resp.data.columns.index("staff.headcount")
    assert [row[i] for row in resp.data.rows] == [43]


@pytest.mark.parametrize(
    ("measure", "latest", "month_bucket"),
    [("staff_daily.active_accounts", 1, 3), ("staff_daily.headcount", 10, 290)],
)
def test_a_level_with_no_period_is_the_latest_day_not_the_open_month(
    lane: CubeLane, measure: str, latest: int, month_bucket: int
) -> None:
    """No period word on a stock: one row per company, read on the day the
    clock's data ends. The open month's bucket would union its accounts
    (3, not 1) or add its 29 daily snapshots (290, not 10)."""
    resp, trace = _serve(
        lane,
        "active accounts by company",
        {"measures": [measure], "dimensions": ["staff_daily.company"]},
        [("active accounts", "member", measure), ("company", "member", "staff_daily.company")],
    )
    assert resp.status == "ok", resp.answer
    assert resp.data is not None
    i = resp.data.columns.index(measure)
    assert sorted(row[i] for row in resp.data.rows) == [latest, latest]
    assert [latest, latest] != [month_bucket, month_bucket]
    assert resp.confidence == "verified"
    assert "2026-08-29" in resp.answer and "day grain" in resp.answer
    assert trace.sql and "2026-08-29" in trace.sql and "date_trunc('day'" in trace.sql.lower()


def test_today_on_a_level_serves_the_latest_snapshot_as_partial(lane: CubeLane) -> None:
    resp, _ = _serve(
        lane,
        "headcount today",
        {
            "measures": ["staff_daily.headcount"],
            "timeDimensions": [
                {"dimension": "staff_daily.day", "dateRange": ["2026-09-01", "2026-09-01"]}
            ],
        },
        [("headcount", "member", "staff_daily.headcount"), ("today", "period", "2026-09-01")],
    )
    assert resp.status == "ok", resp.answer
    assert resp.data is not None and resp.data.row_count == 1
    assert resp.data.rows[0][resp.data.columns.index("staff_daily.headcount")] == 20
    assert resp.confidence == "partial" and resp.mode == FailureMode.data_gap
    assert "holds no snapshot" in resp.answer and "2026-08-29" in resp.answer


def test_a_pin_to_a_data_end_with_no_snapshot_is_refused_not_zero_rows(tmp_path: Path) -> None:
    """The probe says the roster's data ends on a day that holds no row:
    the stock is not served from nothing, and the answer says to re-run
    the monitor."""
    probe = {
        "cubes": {
            **fake.PROBE["cubes"],
            "staff_daily": {
                "clock": "staff_daily.day",
                "time_dimensions": {"day": {"kind": "event", "data_end": "2026-08-30"}},
            },
        }
    }
    lane = _lane(tmp_path, probe=probe)
    resp, _ = _serve(
        lane,
        "headcount",
        {"measures": ["staff_daily.headcount"]},
        [("headcount", "member", "staff_daily.headcount")],
    )
    assert resp.status == "refused" and resp.mode == FailureMode.data_gap
    assert "no snapshot of staff_daily.headcount on 2026-08-30" in resp.answer


def test_a_flow_asked_for_today_with_nothing_loaded_is_a_gap_not_a_zero(tmp_path: Path) -> None:
    """The lens clock reached today (another feed loaded), orders are loaded
    through yesterday: 'orders today' comes back with 0 rows, which is
    refused as a gap, never served as a count of 0."""
    lane = _lane(tmp_path, probe={"clock_end": "2026-09-01"})
    resp, _ = _serve(
        lane,
        "orders today",
        {
            "measures": ["orders.count"],
            "timeDimensions": [
                {"dimension": "orders.ordered_at", "dateRange": ["2026-09-01", "2026-09-01"]}
            ],
        },
        [("orders", "member", "orders.count"), ("today", "period", "2026-09-01")],
    )
    assert resp.status == "refused", resp.answer
    assert resp.mode == FailureMode.data_gap
    assert "nothing is loaded for 2026-09-01..2026-09-01" in resp.answer
    assert "not a measured 0" in resp.answer


def test_a_comparison_runs_each_window_and_sets_them_side_by_side(lane: CubeLane) -> None:
    resp, _ = _serve(
        lane,
        "south orders in August vs July by status",
        {
            "measures": ["orders.count"],
            "dimensions": ["orders.status"],
            "filters": [SOUTH],
            "timeDimensions": [
                {
                    "dimension": "orders.ordered_at",
                    "compareDateRange": [
                        ["2026-08-01", "2026-08-31"],
                        ["2026-07-01", "2026-07-31"],
                    ],
                }
            ],
        },
        [
            ("south", "value", "South AB"),
            ("orders", "member", "orders.count"),
            ("status", "member", "orders.status"),
            ("August", "period", "x"),
            ("July", "period", "x"),
        ],
    )
    # August is a partial month against a complete July: both windows are
    # served side by side, the partial one said, and the answer capped.
    assert resp.status == "ok" and resp.mode == FailureMode.data_gap, resp.answer
    assert resp.confidence == "partial"
    assert "the comparison sets 2026-08 against complete periods" in resp.answer
    assert resp.data is not None and len(resp.data.columns) > 2


def test_a_relative_window_is_read_against_the_lens_clock(lane: CubeLane) -> None:
    resp, trace = _serve(
        lane,
        "south revenue last month",
        {
            "measures": ["orders.revenue"],
            "filters": [SOUTH],
            "timeDimensions": [{"dimension": "orders.ordered_at", "dateRange": "last month"}],
        },
        [
            ("south", "value", "South AB"),
            ("revenue", "member", "orders.revenue"),
            ("last month", "period", "last month"),
        ],
    )
    assert resp.status == "ok", resp.answer
    assert trace.cube_query is not None
    assert trace.cube_query[0]["timeDimensions"][0]["dateRange"] == ["2026-08-01", "2026-08-31"]


def test_a_compiled_statement_the_guard_refuses_never_runs(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/meta"):
            return httpx.Response(200, json=fake.meta_payload())
        return httpx.Response(
            200, json={"sql": {"sql": ["DELETE FROM shop.orders", []], "aliasNameToMember": {}}}
        )

    client = CubeClient("http://cube.test", fake.SECRET, transport=httpx.MockTransport(handler))
    lane = _lane(tmp_path, client=client)
    resp, _ = _serve(
        lane,
        "orders by status",
        {"measures": ["orders.count"], "dimensions": ["orders.status"]},
        [("orders", "member", "orders.count"), ("status", "member", "orders.status")],
    )
    assert resp.status in ("refused", "error")
    assert lane.executed == []


def test_a_refused_ask_names_its_mode_and_executes_nothing(lane: CubeLane) -> None:
    resp, trace = _serve(
        lane,
        "headcount by salary",
        {"measures": ["staff.headcount"], "dimensions": ["staff.salary"]},
        [("headcount", "member", "staff.headcount"), ("salary", "member", "staff.salary")],
    )
    assert resp.status == "refused" and resp.mode == FailureMode.hidden_column
    assert lane.executed == [] and trace.mode == FailureMode.hidden_column


def test_a_structured_query_skips_the_question_word_checks(lane: CubeLane) -> None:
    query = CubeQuery.model_validate(
        {"measures": ["orders.count"], "dimensions": ["orders.status"], "filters": [SOUTH]}
    )
    resp, trace = lane.serve(
        "count by status",
        [Candidate(query=query, lane="structured")],
        ledger=[],
        lane="structured",
        structured=True,
    )
    assert resp.status == "ok", resp.answer
    assert resp.receipt is not None and resp.receipt.checks is not None
    assert not any(c.startswith(("words:", "unasked_scope:")) for c in resp.receipt.checks)
    assert trace.serving == "cube:structured"


def test_the_probe_and_monitor_files_read_as_one_set_of_facts(tmp_path: Path) -> None:
    import json

    from services.serve.door import load_probe

    probe, freshness = fake.monitor_files()
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "cube.probe.json").write_text(json.dumps(probe))
    (tmp_path / "profiles" / "cube.freshness.json").write_text(json.dumps(freshness))
    facts = load_probe(tmp_path, Overlay.model_validate(fake.OVERLAY))
    assert facts.clock_end == "2026-08-31"
    assert facts.dictionaries["orders.status"] == ["Open", "Paid", "Refunded"]
    assert facts.cubes["orders"].streams[0].stopped
    decided = facts.cubes["quotes"].time_dimensions["decided_at"]
    assert (decided.kind, decided.future_rows) == ("event", 1)  # declared an event
    assert facts.measures["orders.count"].partial is not None
    assert load_probe(tmp_path / "nowhere").clock_end is None


def _project(tmp_path: Path) -> Path:
    import json

    probe, freshness = fake.monitor_files()
    root = tmp_path / "project"
    (root / "profiles").mkdir(parents=True)
    (root / "profiles" / "cube.probe.json").write_text(json.dumps(probe))
    (root / "profiles" / "cube.freshness.json").write_text(json.dumps(freshness))
    return root


def test_a_date_with_future_rows_is_what_its_name_or_declaration_says(tmp_path: Path) -> None:
    from services.serve.door import load_probe

    root = _project(tmp_path)
    # A measure's clock is not an event date by that alone: contracts are
    # counted on the day they end, and the rows after the clock are ends to come.
    plain = load_probe(root, Overlay.model_validate({}))
    assert plain.cubes["contracts"].time_dimensions["end_date"].kind == "schedule"
    # a decision cannot lie ahead: its future rows are clipped, never read as due
    assert plain.cubes["quotes"].time_dimensions["decided_at"].kind == "event"
    assert plain.cubes["orders"].time_dimensions["ordered_at"].kind == "event"
    declared = load_probe(
        root,
        Overlay.model_validate(
            {
                "cubes": {
                    "contracts": {"clock_kind": {"end_date": "event"}},
                    "orders": {"clock_kind": {"ordered_at": "schedule"}},
                }
            }
        ),
    )
    assert declared.cubes["contracts"].time_dimensions["end_date"].kind == "event"
    assert declared.cubes["orders"].time_dimensions["ordered_at"].kind == "schedule"


def test_the_overlay_names_only_event_or_schedule() -> None:
    with pytest.raises(ValueError):
        Overlay.model_validate({"cubes": {"contracts": {"clock_kind": {"end_date": "due"}}}})


def test_a_schedule_date_in_a_month_after_the_clock_is_read_as_asked(tmp_path: Path) -> None:
    from services.cube.lens_view import CubeLensView
    from services.serve.door import load_probe

    root = _project(tmp_path)
    overlay = Overlay.model_validate(fake.OVERLAY)
    view = CubeLensView.build(fake.doc(), overlay=overlay, probe=load_probe(root, overlay))
    lane = _lane(tmp_path / "w")
    lane.view = view
    resp, _ = _serve(
        lane,
        "contracts ending in October",
        {
            "measures": ["contracts.count"],
            "timeDimensions": [
                {"dimension": "contracts.end_date", "dateRange": ["2026-10-01", "2026-10-31"]}
            ],
        },
        [
            ("contracts", "member", "contracts.count"),
            ("ending", "member", "contracts.end_date"),
            ("October", "period", "2026-10"),
        ],
    )
    assert resp.status == "ok", resp.answer
    assert resp.data is not None and resp.data.rows == [[3]]
    assert "clipped" not in resp.answer and "data_gap" not in resp.answer


def test_a_shape_cube_cannot_join_is_refused_as_not_expressible(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/meta"):
            return httpx.Response(200, json=fake.meta_payload())
        return httpx.Response(400, json={"error": "Can't find join path to join 'orders', 'staff'"})

    client = CubeClient("http://cube.test", fake.SECRET, transport=httpx.MockTransport(handler))
    lane = _lane(tmp_path, client=client)
    resp, _ = _serve(
        lane,
        "orders by staff company",
        {"measures": ["orders.count"], "dimensions": ["staff.company"]},
        [("orders", "member", "orders.count"), ("staff company", "member", "staff.company")],
    )
    assert resp.status == "refused" and resp.mode == FailureMode.not_expressible
    assert lane.executed == []


def test_last_week_reads_the_week_dst_resolved_not_the_one_the_model_wrote(
    tmp_path: Path,
) -> None:
    from datetime import date

    lane = _lane(tmp_path)
    lane.today = date(2026, 8, 11)  # a Tuesday: last week is Mon 08-03 .. Sun 08-09
    resp, _ = _serve(
        lane,
        "how many orders last week",
        {
            "measures": ["orders.count"],
            "timeDimensions": [
                {"dimension": "orders.ordered_at", "dateRange": ["2026-07-27", "2026-08-02"]}
            ],
        },
        [("orders", "member", "orders.count"), ("last week", "period", "2026-07-27")],
    )
    assert resp.status == "ok", resp.answer
    assert resp.data is not None and resp.data.rows == [[6]]
    assert "'last week' is 2026-08-03..2026-08-09" in resp.answer


def test_an_overlay_caveat_on_what_the_query_reads_rides_the_answer(tmp_path: Path) -> None:
    cubes = {
        **fake.OVERLAY["cubes"],
        "orders": {
            "caveats": ["orders placed by staff are test orders"],
            "member_caveats": {
                "revenue": ["allocated: refunds are spread over lines by their share of the order"]
            },
        },
    }
    lane = _lane(tmp_path, cubes=cubes)
    resp, _ = _serve(
        lane,
        "south revenue in August",
        {"measures": ["orders.revenue"], "timeDimensions": [AUG], "filters": [SOUTH]},
        [
            ("south", "value", "South AB"),
            ("revenue", "member", "orders.revenue"),
            ("August", "period", "2026-08"),
        ],
    )
    assert resp.status == "ok", resp.answer
    assert resp.caveats == [
        "orders: orders placed by staff are test orders",
        "orders.revenue: allocated: refunds are spread over lines by their share of the order",
    ]
    # prominent: the answer text leads with them, and the receipt signs them
    assert resp.answer.startswith("CAVEAT orders: orders placed by staff")
    assert resp.receipt is not None and resp.receipt.caveats == resp.caveats
    assert "caveats:disclose" in (resp.receipt.checks or [])
    # a query that reads none of them carries none
    resp, _ = _serve(
        lane,
        "headcount",
        {"measures": ["staff.headcount"]},
        [("headcount", "member", "staff.headcount")],
    )
    assert resp.caveats == [] and "CAVEAT" not in resp.answer


def test_a_comparison_with_a_partial_month_serves_and_states_the_fact(lane: CubeLane) -> None:
    """The partial period rides the answer text, the checks and the signed
    receipt; the figure is served."""
    jul = {"dimension": "orders.ordered_at", "dateRange": ["2026-07-01", "2026-07-31"]}
    led = [Binding(word="orders", kind="member", target="orders.count", source="test")]  # type: ignore[arg-type]
    candidates = [
        Candidate(query=CubeQuery.model_validate(body), ledger=led, lane="frontier")
        for body in (
            {"measures": ["orders.count"], "timeDimensions": [AUG]},
            {"measures": ["orders.count"], "timeDimensions": [jul]},
        )
    ]
    resp, _ = lane.serve(
        "orders in August vs July", candidates, ledger=led, lane="frontier", structured=True
    )
    assert resp.status == "ok", resp.answer
    assert "partial period" in resp.answer and "judged by orders.count" in resp.answer
    assert resp.receipt is not None and "partial_period:disclose" in (resp.receipt.checks or [])
    assert any("judged by orders.count" in d for d in resp.receipt.disclosures or [])
    check = next(c for c in resp.verification.checks if c.name == "partial_period")
    assert "2026-08" in (check.reason or "")


def _serve_many(
    lane: CubeLane, question: str, bodies: list[dict[str, Any]], words: list[str]
) -> Any:
    led = [Binding(word=w, kind="member", target="orders.count", source="test") for w in words]
    cands = [
        Candidate(query=CubeQuery.model_validate(b), ledger=led, lane="frontier") for b in bodies
    ]
    return lane.serve(question, cands, ledger=led, lane="frontier")


PAID = {"member": "orders.status", "operator": "equals", "values": ["Paid"]}


def test_a_share_asked_as_two_queries_is_divided_and_leads_the_answer(lane: CubeLane) -> None:
    """The lane answers a share with the numerator and the denominator; dst
    divides them (the narrower query is the numerator, whichever came
    first), leads with the figure, and keeps both parts under result 1, 2."""
    resp, _ = _serve_many(
        lane,
        "share of orders that were paid in August",
        [
            {"measures": ["orders.count"], "timeDimensions": [AUG]},
            {"measures": ["orders.count"], "filters": [PAID], "timeDimensions": [AUG]},
        ],
        ["share", "orders", "paid", "August"],
    )
    assert resp.status == "ok", resp.answer
    assert resp.answer.startswith("share: 83.3% (5 / 6: orders.count / orders.count")
    assert resp.data is not None
    assert resp.data.columns[:2] == ["result", "share"]
    assert resp.data.rows[0][:2] == [0, pytest.approx(5 / 6)]
    assert [r[0] for r in resp.data.rows] == [0, 1, 2]
    assert resp.receipt is not None and "derived:pass" in (resp.receipt.checks or [])


def test_a_share_by_group_divides_each_group_by_the_total(lane: CubeLane) -> None:
    resp, _ = _serve_many(
        lane,
        "share of August orders by company",
        [
            {
                "measures": ["orders.count"],
                "dimensions": ["orders.company"],
                "timeDimensions": [AUG],
            },
            {"measures": ["orders.count"], "timeDimensions": [AUG]},
        ],
        ["share", "orders", "company", "August"],
    )
    assert resp.status == "ok", resp.answer
    assert resp.data is not None
    company, share = (resp.data.columns.index(c) for c in ("orders.company", "share"))
    lead = {r[company]: r[share] for r in resp.data.rows if r[0] == 0}
    assert lead == {"North Oy": pytest.approx(1 / 6), "South AB": pytest.approx(5 / 6)}
    assert resp.answer.startswith("share: ")
    assert "North Oy: 16.7%" in resp.answer and "South AB: 83.3%" in resp.answer


def test_a_per_day_average_divides_the_total_by_the_days_of_the_window_that_ran(
    lane: CubeLane,
) -> None:
    """'divide by 298' is never left to the caller: the total over the
    window is divided by the days of the window the SQL read."""
    resp, _ = _serve_many(
        lane,
        "average orders per day in August",
        [{"measures": ["orders.count"], "timeDimensions": [AUG]}],
        ["average", "orders", "per day", "August"],
    )
    assert resp.status == "ok", resp.answer
    assert resp.answer.startswith("per day: 0.19 (6 / 31 days, 2026-08-01..2026-08-31)")
    assert resp.data is not None
    assert resp.data.columns == ["result", "per day", "orders.count"]
    assert resp.data.rows[0] == [0, pytest.approx(6 / 31), None]
    assert resp.data.rows[1] == [1, None, 6]


def test_parts_over_different_windows_are_not_divided_and_the_answer_says_why(
    lane: CubeLane,
) -> None:
    resp, _ = _serve_many(
        lane,
        "share of August orders in all orders",
        [
            {"measures": ["orders.count"], "timeDimensions": [AUG]},
            {"measures": ["orders.count"]},
        ],
        ["share", "orders", "August", "all"],
    )
    assert resp.status == "ok", resp.answer
    assert resp.answer.startswith("share: not computed - the parts read different windows")
    assert "2026-08-01..2026-08-31" in resp.answer and "start..2026-08-31" in resp.answer
    assert resp.data is not None and [r[0] for r in resp.data.rows] == [1, 2]
    assert resp.receipt is not None and "derived:skip" in (resp.receipt.checks or [])


class _Metered:
    """A connector that reports what it scanned, like BigQuery's does."""

    kind = "bigquery"

    def __init__(self, inner: DuckDBConnector) -> None:
        self.inner = inner

    def execute(self, sql: str, *, read_only: bool = True, row_limit: int | None = None) -> Any:
        out = self.inner.execute(sql, read_only=read_only, row_limit=row_limit)
        return out.model_copy(update={"bytes_scanned": 1_000_000})


def test_a_request_row_carries_the_bytes_the_connector_reported(tmp_path: Path) -> None:
    """The typed lane stamps wh_bytes / wh_cost_usd from QueryResult.bytes_scanned; the
    cube lane does the same, summed over every statement the ask ran. Tripwire: a row
    with SQL and rows has wh_bytes whenever the connector reports it."""
    from services.observability import cost

    lane = _lane(tmp_path)
    lane.connector = _Metered(lane.connector)  # type: ignore[assignment]
    resp, trace = _serve_many(
        lane,
        "share of orders that were paid in August",
        [
            {"measures": ["orders.count"], "timeDimensions": [AUG]},
            {"measures": ["orders.count"], "filters": [PAID], "timeDimensions": [AUG]},
        ],
        ["share", "orders", "paid", "August"],
    )
    assert resp.status == "ok", resp.answer
    assert trace.sql and trace.row_count
    assert trace.wh_bytes == 2_000_000
    assert trace.wh_cost_usd == cost.warehouse_cost_usd(2_000_000, "bigquery") > 0
    # A connector that reports nothing leaves the bytes unknown and prices
    # a free warehouse at zero, as the typed lane does.
    plain = _lane(tmp_path / "plain")
    _resp, trace = _serve(
        plain,
        "orders in August",
        {"measures": ["orders.count"], "timeDimensions": [AUG]},
        [("orders", "member", "orders.count"), ("August", "period", "2026-08")],
    )
    assert trace.sql and trace.wh_bytes is None and trace.wh_cost_usd == 0.0


def test_a_gap_on_one_window_of_several_is_disclosed_not_dropped(lane: CubeLane) -> None:
    """A data gap that is partial — one window of the ask has nothing
    loaded, another does — serves what is loaded and states the gap in the
    answer, the checks and the receipt; only a total gap refuses."""
    sep = {"dimension": "orders.ordered_at", "dateRange": ["2026-09-01", "2026-09-30"]}
    led = [Binding(word="revenue", kind="member", target="orders.revenue", source="test")]  # type: ignore[arg-type]
    candidates = [
        Candidate(query=CubeQuery.model_validate(body), ledger=led, lane="frontier")
        for body in (
            {"measures": ["orders.revenue"], "timeDimensions": [AUG]},
            {"measures": ["orders.revenue"], "timeDimensions": [sep]},
        )
    ]
    resp, _ = lane.serve(
        "revenue in August and September", candidates, ledger=led, lane="frontier", structured=True
    )
    assert resp.status == "ok", resp.answer
    assert "2026-09-01..2026-09-30 is after the last loaded day" in resp.answer
    assert resp.confidence == "partial" and resp.mode == FailureMode.data_gap
    assert any("2026-09-01" in d for d in resp.receipt.disclosures or [])  # type: ignore[union-attr]
    assert "freshness:refuse" not in (resp.receipt.checks or [])  # type: ignore[union-attr]
    total, _ = lane.serve(
        "revenue in September", candidates[1:], ledger=led, lane="frontier", structured=True
    )
    assert total.status == "refused" and total.mode == FailureMode.data_gap


def test_inline_params_on_bigquery_escapes_with_a_backslash_and_skips_backticks() -> None:
    # BigQuery reads 'it''s' as two literals (a syntax error) and " as a string
    sql = "SELECT `a?b`, \"?\", '''x ? y''', 'it\\'s ?' FROM t WHERE x = ? AND y = ?"
    assert inline_params(sql, ["o'k \\ n", 3], "bigquery") == (
        "SELECT `a?b`, \"?\", '''x ? y''', 'it\\'s ?' FROM t WHERE x = 'o\\'k \\\\ n' AND y = 3"
    )


def test_an_allocated_figure_in_a_clarification_still_carries_its_caveat(tmp_path: Path) -> None:
    """The ambiguous term serves its declared default reading and prints the
    other reading's figures beside it; a later check then asks back. The
    clarification still prints those figures, so they carry the allocation
    caveat they would carry as an answer: no figure leaves dst bare."""
    allocated = "allocated: refunds are spread over lines by their share of the order"
    term = [{**fake.OVERLAY["ambiguous"][0], "default": "refunds netted when issued (refund date)"}]
    cubes = {**fake.OVERLAY["cubes"], "orders": {"member_caveats": {"revenue_net": [allocated]}}}
    lane = _lane(tmp_path, ambiguous=term, cubes=cubes)
    resp, _ = _serve(
        lane,
        "webshop revenue net of refunds in August",
        {"measures": ["orders.revenue_net"], "timeDimensions": [AUG]},
        [("revenue", "member", "orders.revenue_net"), ("August", "period", "2026-08")],
    )
    assert resp.status == "clarification", resp.answer
    reasons = " ".join(c.reason or "" for c in resp.verification.checks)
    assert "orders.revenue_net=820" in reasons
    assert f"orders.revenue_net: {allocated}" in reasons


def _marked_view(**dst: Any) -> Any:
    """The fake lens with its level measure's meta.dst extended."""
    import copy

    from services.contracts.cube_meta import MetaDoc
    from services.contracts.cube_probe import CubeProbe
    from services.cube.lens_view import CubeLensView

    payload = copy.deepcopy(fake.meta_payload())
    staff = next(c for c in payload["cubes"] if c["name"] == "staff")
    staff["measures"][0]["meta"]["dst"].update(dst)
    return CubeLensView.build(
        MetaDoc.parse(payload),
        overlay=Overlay.model_validate(fake.OVERLAY),
        probe=CubeProbe.model_validate(fake.PROBE),
    )


def test_a_level_marked_creation_dated_reads_the_whole_table_without_a_period(
    lane: CubeLane,
) -> None:
    """One row per record, not one per record per day: with no period
    asked there is no snapshot to pin, so every row counts. A stated
    period keeps the pin."""
    body = {"measures": ["staff.headcount"]}
    ledger = [("headcount", "member", "staff.headcount")]
    resp, _ = _serve(lane, "headcount", body, ledger)
    assert resp.status == "ok" and resp.data is not None
    assert resp.data.rows[0][resp.data.columns.index("staff.headcount")] == 43  # the last snapshot
    lane.view = _marked_view(creation_dated=True)
    resp, _ = _serve(lane, "headcount", body, ledger)
    assert resp.status == "ok" and resp.data is not None
    assert resp.data.rows[0][resp.data.columns.index("staff.headcount")] == 125  # every row
    assert resp.receipt is not None and resp.receipt.checks is not None
    assert "snapshot_pin:disclose" not in resp.receipt.checks
    resp, _ = _serve(
        lane,
        "headcount at the end of Q2",
        {
            **body,
            "timeDimensions": [
                {"dimension": "staff.month", "dateRange": ["2026-04-01", "2026-06-30"]}
            ],
        },
        [*ledger, ("end of Q2", "period", "2026-04-01..2026-06-30")],
    )
    assert resp.status == "ok" and resp.data is not None
    assert resp.data.rows[0][resp.data.columns.index("staff.headcount")] == 43
