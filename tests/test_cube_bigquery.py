"""The cube lane on BigQuery, live: Cube's BigQuery driver compiles, dst
inlines the parameters, guards the statement and runs it through its own
BigQuery connector. Each shape's served rows are compared with the same
figure read straight from BigQuery.

Opt-in. The data goes in once (load jobs, which BigQuery does not bill) and
the project for that warehouse, its Cube model included, is written with::

    python tests/fixtures/cube_shop/make_data.py --bigquery PROJECT KEY.json
    python tests/fixtures/cube_shop/make_data.py --bigquery-project /tmp/cube_bq PROJECT
    docker run -d --name dst-test-cube-bq -p 4101:4000 \\
      -e CUBEJS_DEV_MODE=false -e CUBEJS_API_SECRET=<secret> \\
      -e CUBEJS_CACHE_AND_QUEUE_DRIVER=memory -e CUBEJS_DB_TYPE=bigquery \\
      -e CUBEJS_DB_BQ_PROJECT_ID=PROJECT -e CUBEJS_DB_BQ_LOCATION=EU \\
      -e CUBEJS_DB_BQ_KEY_FILE=/creds/key.json -v KEY.json:/creds/key.json:ro \\
      -v /tmp/cube_bq/cube/model:/cube/conf/model:ro \\
      -v $PWD/deploy/cube/cube.js:/cube/conf/cube.js:ro cubejs/cube:$(cat deploy/cube.version)
    DST_TEST_CUBE_BQ_URL=http://localhost:4101 DST_TEST_CUBE_SECRET=<secret> \\
      DST_TEST_CUBE_BQ_KEY=KEY.json DST_TEST_CUBE_BQ_PROJECT=PROJECT \\
      pytest tests/test_cube_bigquery.py

Every statement is capped at 1 GB billed; the tables are under 1 MB.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from services.connectors.bigquery import BigQueryConnector
from services.contracts.cube_query import Binding, Candidate, CubeQuery
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_config import LensConfig
from services.cube.client import CubeClient
from services.cube.context import CubeContext
from services.observability import usage as ai_usage
from services.serve import door
from services.serve.cube_lane import CubeLane

URL = os.environ.get("DST_TEST_CUBE_BQ_URL")
KEY = os.environ.get("DST_TEST_CUBE_BQ_KEY")
PROJECT = os.environ.get("DST_TEST_CUBE_BQ_PROJECT")
SECRET = os.environ.get("DST_TEST_CUBE_SECRET") or os.environ.get("CUBEJS_API_SECRET")

pytestmark = pytest.mark.skipif(
    not (URL and KEY and PROJECT and SECRET),
    reason="DST_TEST_CUBE_BQ_URL/_KEY/_PROJECT + DST_TEST_CUBE_SECRET name no BigQuery Cube",
)

CLOCK = date(2026, 9, 14)
TODAY = date(2026, 9, 15)
AUG = ["2026-08-01", "2026-08-31"]
# The model's orders: the latest sync of each order, test orders excluded.
ORDERS = (
    "(SELECT * FROM (SELECT * FROM `{p}.bakery.orders_raw` QUALIFY ROW_NUMBER() "
    "OVER (PARTITION BY order_id ORDER BY _synced DESC) = 1) WHERE NOT is_test)"
)
IN_AUG = "o.ordered_at >= TIMESTAMP '2026-08-01' AND o.ordered_at < TIMESTAMP '2026-09-01'"


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The fixture project on BigQuery, with the probe and the monitor's
    facts read through the live Cube by `dst probe --cube` and `dst monitor`."""
    from services.cli.main import main
    from tests.fixtures.cube_shop.make_data import bigquery_project

    root = bigquery_project(tmp_path_factory.mktemp("cube_bq") / "project", str(PROJECT))
    dst_yaml = root / "dst.yaml"
    dst_yaml.write_text(
        dst_yaml.read_text(encoding="utf-8").replace("http://localhost:4100", str(URL)),
        encoding="utf-8",
    )
    mp = pytest.MonkeyPatch()
    mp.setenv("CUBEJS_API_SECRET", str(SECRET))
    try:
        for verb in (["probe", "--cube"], ["monitor"]):
            mp.setattr(sys, "argv", ["dst", *verb, "--dir", str(root), "--as-of", str(CLOCK)])
            code = main()
            # monitor exits 2 on findings (September is loading short)
            assert code in (0, 2), f"dst {' '.join(verb)} exited {code}"
    finally:
        mp.undo()
    return root


@pytest.fixture(scope="module")
def bq() -> BigQueryConnector:
    return BigQueryConnector(str(KEY), project=PROJECT, max_bytes_billed=1_000_000_000)


def _lane(project: Path, bq: BigQueryConnector, groups: list[str] | None = None) -> CubeLane:
    ctx = CubeContext(org="", caller="analyst", groups=list(groups or []), lens="sales")
    config = LensConfig(name="sales", timezone="Europe/Helsinki", serving="cube")
    return door.open_lane(
        "sales",
        config,
        connector=bq,
        dialect="bigquery",
        ctx=ctx,
        today=TODAY,
        org_id="org-test",
        caller="analyst",
        groups=list(groups or []),
        request_id="req_bq",
        client=CubeClient(str(URL), SECRET),
        root=project,
    )


def _direct(bq: BigQueryConnector, sql: str) -> list[list[Any]]:
    """The figure read straight from BigQuery, dry-run first."""
    estimate = bq.dry_run(sql.format(p=PROJECT))
    assert estimate.valid, estimate.error
    assert (estimate.bytes_estimated or 0) < 10_000_000
    return [list(r) for r in bq.execute(sql.format(p=PROJECT)).rows]


def _serve(lane: CubeLane, body: dict[str, Any]) -> Any:
    query = CubeQuery.model_validate(body)
    return lane.serve(
        "structured",
        [Candidate(query=query, lane="structured")],
        ledger=[],
        lane="structured",
        structured=True,
    )


def _rows(resp: Any, *members: str) -> list[list[Any]]:
    assert resp.data is not None, resp.answer
    idx = [resp.data.columns.index(m) for m in members]
    return [[row[i] for i in idx] for row in resp.data.rows]


def _close(a: list[list[Any]], b: list[list[Any]]) -> bool:
    def norm(v: Any) -> Any:
        if isinstance(v, float):
            return round(v, 2)
        if isinstance(v, str) and len(v) >= 10 and v[4] == "-" and v[7] == "-":
            return v[:10]
        return v

    return [[norm(v) for v in r] for r in a] == [[norm(v) for v in r] for r in b]


# ── the shapes ────────────────────────────────────────────────────────────


def test_a_sum_in_a_window(project: Path, bq: BigQueryConnector) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": ["sales_orders.orders_revenue"],
            "timeDimensions": [{"dimension": "sales_orders.orders_ordered_at", "dateRange": AUG}],
            "filters": [
                {"member": "sales_orders.stores_region", "operator": "equals", "values": ["South"]}
            ],
        },
    )
    assert resp.status == "ok", resp.answer
    direct = _direct(
        bq,
        f"SELECT SUM(IF(o.status = 'paid', o.amount_eur, NULL)) FROM {ORDERS} o "
        f"JOIN `{{p}}.bakery.stores` s USING (store_id) WHERE {IN_AUG} AND s.region = 'South'",
    )
    assert _close(_rows(resp, "sales_orders.orders_revenue"), direct)
    assert resp.receipt is not None and resp.receipt.sql_sha256


@pytest.mark.parametrize(
    ("member", "measure", "day", "figure"),
    [
        # a TIMESTAMP clock
        ("orders_ordered_at", "orders_order_count", "DATE(o.ordered_at)", "COUNT(*)"),
        # a DATE clock: the web revenue counted on the day it shipped
        (
            "orders_shipped_on",
            "orders_shipped_revenue",
            "o.shipped_on",
            "SUM(IF(o.channel = 'web', o.amount_eur, NULL))",
        ),
    ],
)
def test_by_month_on_each_time_type(
    project: Path, bq: BigQueryConnector, member: str, measure: str, day: str, figure: str
) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": [f"sales_orders.{measure}"],
            "timeDimensions": [
                {
                    "dimension": f"sales_orders.{member}",
                    "granularity": "month",
                    "dateRange": ["2026-05-01", "2026-07-31"],
                }
            ],
        },
    )
    assert resp.status == "ok", resp.answer
    direct = _direct(
        bq,
        f"SELECT FORMAT_DATE('%Y-%m-%d', DATE_TRUNC({day}, MONTH)), {figure} FROM {ORDERS} o "
        f"WHERE {day} BETWEEN '2026-05-01' AND '2026-07-31' GROUP BY 1 ORDER BY 1",
    )
    got = _rows(resp, f"sales_orders.{member}.month", f"sales_orders.{measure}")
    assert _close(got, direct), (got, direct)


def test_by_year_on_a_datetime(project: Path, bq: BigQueryConnector) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": ["sales_lens.stores_store_count"],
            "timeDimensions": [
                {
                    "dimension": "sales_lens.stores_opened_on",
                    "granularity": "year",
                    "dateRange": ["2019-01-01", "2022-12-31"],
                }
            ],
        },
    )
    assert resp.status == "ok", resp.answer
    direct = _direct(
        bq,
        "SELECT FORMAT_DATE('%Y-%m-%d', DATE_TRUNC(DATE(opened_on), YEAR)), COUNT(*) "
        "FROM `{p}.bakery.stores` GROUP BY 1 ORDER BY 1",
    )
    got = _rows(resp, "sales_lens.stores_opened_on.year", "sales_lens.stores_store_count")
    assert _close(got, direct), (got, direct)


def test_a_dimension_two_joins_away(project: Path, bq: BigQueryConnector) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": ["sales_order_lines.order_lines_units_sold"],
            "dimensions": ["sales_order_lines.stores_region"],
            "timeDimensions": [
                {"dimension": "sales_order_lines.orders_ordered_at", "dateRange": AUG}
            ],
            "order": {"sales_order_lines.stores_region": "asc"},
        },
    )
    assert resp.status == "ok", resp.answer
    direct = _direct(
        bq,
        f"SELECT s.region, SUM(l.qty) FROM `{{p}}.bakery.order_lines` l "
        f"JOIN {ORDERS} o USING (order_id) JOIN `{{p}}.bakery.stores` s ON o.store_id = s.store_id "
        f"WHERE {IN_AUG} GROUP BY 1 ORDER BY 1",
    )
    got = _rows(resp, "sales_order_lines.stores_region", "sales_order_lines.order_lines_units_sold")
    assert _close(got, direct), (got, direct)


def test_top_n_extends_to_the_rows_tied_at_the_limit(project: Path, bq: BigQueryConnector) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": ["sales_orders.orders_order_count"],
            "dimensions": ["sales_orders.stores_store_name"],
            "timeDimensions": [
                {
                    "dimension": "sales_orders.orders_ordered_at",
                    "dateRange": ["2026-08-02", "2026-08-02"],
                }
            ],
            "order": {"sales_orders.orders_order_count": "desc"},
            "limit": 1,
        },
    )
    assert resp.status == "ok", resp.answer
    direct = _direct(
        bq,
        f"SELECT s.store_name, COUNT(*) n FROM {ORDERS} o JOIN `{{p}}.bakery.stores` s "
        "USING (store_id) WHERE DATE(o.ordered_at) = '2026-08-02' GROUP BY 1 "
        "QUALIFY RANK() OVER (ORDER BY n DESC) = 1 ORDER BY 1",
    )
    assert len(direct) == 2  # the data has a tie for first that day
    got = sorted(_rows(resp, "sales_orders.stores_store_name", "sales_orders.orders_order_count"))
    assert _close(got, direct), (got, direct)
    assert "tie at the limit" in resp.answer


def test_a_level_over_a_quarter_is_its_last_snapshot(project: Path, bq: BigQueryConnector) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": ["sales_stock_monthly.stock_monthly_stock_on_hand"],
            "dimensions": ["sales_stock_monthly.stores_region"],
            "timeDimensions": [
                {
                    "dimension": "sales_stock_monthly.stock_monthly_month",
                    "dateRange": ["2026-04-01", "2026-06-30"],
                }
            ],
            "order": {"sales_stock_monthly.stores_region": "asc"},
        },
    )
    assert resp.status == "ok", resp.answer
    direct = _direct(
        bq,
        "SELECT s.region, SUM(m.units_on_hand) FROM `{p}.bakery.stock_monthly` m "
        "JOIN `{p}.bakery.stores` s USING (store_id) WHERE m.month = '2026-06' "
        "GROUP BY 1 ORDER BY 1",
    )
    got = _rows(
        resp,
        "sales_stock_monthly.stores_region",
        "sales_stock_monthly.stock_monthly_stock_on_hand",
    )
    assert _close(got, direct), (got, direct)


def test_a_text_month_reads_by_month(project: Path, bq: BigQueryConnector) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": ["sales_stock_monthly.stock_monthly_stock_on_hand"],
            "timeDimensions": [
                {
                    "dimension": "sales_stock_monthly.stock_monthly_month",
                    "granularity": "month",
                    "dateRange": ["2026-01-01", "2026-03-31"],
                }
            ],
        },
    )
    assert resp.status == "ok", resp.answer
    direct = _direct(
        bq,
        "SELECT CONCAT(month, '-01'), SUM(units_on_hand) FROM `{p}.bakery.stock_monthly` "
        "WHERE month BETWEEN '2026-01' AND '2026-03' GROUP BY 1 ORDER BY 1",
    )
    got = _rows(
        resp,
        "sales_stock_monthly.stock_monthly_month.month",
        "sales_stock_monthly.stock_monthly_stock_on_hand",
    )
    assert _close(got, direct), (got, direct)


def test_a_comparison_sets_each_window_side_by_side(project: Path, bq: BigQueryConnector) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": ["sales_orders.orders_order_count"],
            "dimensions": ["sales_orders.orders_status"],
            "timeDimensions": [
                {
                    "dimension": "sales_orders.orders_ordered_at",
                    "compareDateRange": [AUG, ["2026-07-01", "2026-07-31"]],
                }
            ],
        },
    )
    assert resp.status == "ok", resp.answer
    direct = _direct(
        bq,
        f"SELECT o.status, COUNTIF({IN_AUG}), COUNTIF(o.ordered_at >= TIMESTAMP '2026-07-01' "
        f"AND o.ordered_at < TIMESTAMP '2026-08-01') FROM {ORDERS} o GROUP BY 1 ORDER BY 1",
    )
    assert resp.data is not None
    got = sorted([list(r) for r in resp.data.rows])
    assert _close(got, direct), (resp.data.columns, got, direct)


def test_a_segment(project: Path, bq: BigQueryConnector) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": ["sales_orders.orders_order_count"],
            "segments": ["sales_orders.orders_web_order"],
            "timeDimensions": [
                {
                    "dimension": "sales_orders.orders_ordered_at",
                    "dateRange": ["2026-07-01", "2026-07-31"],
                }
            ],
        },
    )
    assert resp.status == "ok", resp.answer
    direct = _direct(
        bq,
        f"SELECT COUNT(*) FROM {ORDERS} o WHERE o.channel = 'web' AND DATE(o.ordered_at) "
        "BETWEEN '2026-07-01' AND '2026-07-31'",
    )
    assert _close(_rows(resp, "sales_orders.orders_order_count"), direct)


def test_the_distinct_grain_companion_sums_one_value_per_order(
    project: Path, bq: BigQueryConnector
) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": ["sales_order_lines_by_order_id.order_lines_by_order_id_order_discount"],
            "timeDimensions": [
                {"dimension": "sales_order_lines_by_order_id.orders_ordered_at", "dateRange": AUG}
            ],
        },
    )
    assert resp.status == "ok", resp.answer
    direct, naive = (
        _direct(
            bq,
            f"SELECT SUM(d) FROM (SELECT order_id, {agg}(order_discount_eur) d "
            f"FROM `{{p}}.bakery.order_lines` GROUP BY order_id{grain}) l "
            f"JOIN {ORDERS} o USING (order_id) WHERE {IN_AUG}",
        )
        for agg, grain in (("MAX", ""), ("SUM", ""))
    )
    got = _rows(resp, "sales_order_lines_by_order_id.order_lines_by_order_id_order_discount")
    assert _close(got, direct), (got, direct)
    assert not _close(got, naive)  # the line-grain sum repeats each order's discount


def test_a_value_with_a_quote_is_one_literal(project: Path, bq: BigQueryConnector) -> None:
    lane = _lane(project, bq)
    resp, _ = _serve(
        lane,
        {
            "measures": ["sales_orders.orders_order_count"],
            "filters": [
                {
                    "member": "sales_orders.stores_store_name",
                    "operator": "contains",
                    "values": ["Baker's \\ Row"],
                }
            ],
        },
    )
    # BigQuery ran the statement (no store's name holds it): one literal,
    # never a syntax error.
    assert "did not run Cube's SQL" not in resp.answer, resp.answer
    assert lane.executed and "LOWER('Baker\\'s " in lane.executed[-1]


def test_a_hidden_member_is_refused_and_a_granted_group_reads_it(
    project: Path, bq: BigQueryConnector
) -> None:
    body = {
        "measures": ["sales_orders.orders_order_count"],
        "dimensions": ["sales_orders.stores_manager_email"],
        "order": {"sales_orders.stores_manager_email": "asc"},
    }
    public = _lane(project, bq)
    resp, _ = _serve(public, body)
    assert resp.status == "refused" and resp.mode == FailureMode.hidden_column, resp.answer
    assert public.executed == []
    ops = _lane(project, bq, groups=["ops"])
    resp, _ = _serve(ops, body)
    assert resp.status == "ok", resp.answer
    direct = _direct(
        bq,
        f"SELECT s.manager_email, COUNT(*) FROM {ORDERS} o JOIN `{{p}}.bakery.stores` s "
        "USING (store_id) GROUP BY 1 ORDER BY 1",
    )
    got = _rows(resp, "sales_orders.stores_manager_email", "sales_orders.orders_order_count")
    assert _close(got, direct), (got, direct)


def test_query_cube_in_driver_mode(project: Path, bq: BigQueryConnector) -> None:
    lane = _lane(project, bq)
    query = CubeQuery.model_validate(
        {
            "measures": ["sales_orders.orders_revenue"],
            "timeDimensions": [{"dimension": "sales_orders.orders_ordered_at", "dateRange": AUG}],
            "filters": [
                {"member": "sales_orders.stores_region", "operator": "equals", "values": ["South"]}
            ],
        }
    )
    ledger = [
        Binding(word="south", kind="value", target="South", source="driver"),
        Binding(
            word="revenue", kind="member", target="sales_orders.orders_revenue", source="driver"
        ),
        Binding(word="August", kind="period", target="2026-08", source="driver"),
    ]
    with ai_usage.no_models():
        resp, trace = door.answer_driver(lane, query, ledger, question="south revenue in August")
    assert resp.status == "ok", resp.answer
    assert trace.serving == "cube:driver"
    direct = _direct(
        bq,
        f"SELECT SUM(IF(o.status = 'paid', o.amount_eur, NULL)) FROM {ORDERS} o "
        f"JOIN `{{p}}.bakery.stores` s USING (store_id) WHERE {IN_AUG} AND s.region = 'South'",
    )
    assert _close(_rows(resp, "sales_orders.orders_revenue"), direct)
    assert resp.receipt is not None and "words:pass" in (resp.receipt.checks or [])


def test_monitor_on_bigquery_finds_what_the_recorded_cube_does(project: Path) -> None:
    """`dst monitor` through the BigQuery Cube (the module fixture ran it)
    names the same findings as the DuckDB cassette, feed axes as declared."""
    from services.contracts.lens_model import LensModel
    from services.monitor import cube as monitor
    from tests.test_monitor_cube import _kinds, _overlay, _run

    client = CubeClient(str(URL), SECRET)
    ctx = CubeContext.operator()
    try:
        meta = client.meta(ctx, extended=True)
        model = LensModel.build(meta, _overlay(["channel"]), "sales")
        probe, _f, live = monitor.monitor(
            [model], lambda q: client.load(q, ctx).data, clock_end=CLOCK
        )
    finally:
        client.close()
    recorded_probe, _f, recorded = _run(feed_axes=["channel"])
    assert _kinds(live) == _kinds(recorded)
    assert probe["dictionaries"] == recorded_probe["dictionaries"]
    assert probe["data_ends"] == recorded_probe["data_ends"]
    assert (project / "profiles" / "cube.freshness.json").is_file()
