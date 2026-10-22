"""The doors a data app is built on: the metric catalog, the batch door, and
period comparison.

An app rendering a metric tree reads the tree's shape from the catalog (one
call), renders every node from one batch (one snapshot), and shows each node's
delta with `compare`. Everything runs on a small DuckDB file with a level
metric (monthly membership fees, read at the latest snapshot) and a flow metric (bookings, summed).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import duckdb
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text

from services.api import query as query_api
from services.api.query import _batch_reader
from services.auth.tokens import hash_token, new_admin_token, new_caller_key
from services.config import settings
from services.connectors.postgres import PostgresConnector
from services.contracts.fakes import FakeConnector
from services.contracts.lens_config import AccessRule, LensConfig
from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.query_intent import IntentCompare, IntentFilter, QueryIntent
from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    Join,
    Metric,
    SemanticModel,
)
from services.db.session import org_session
from services.governance import ratelimit
from services.governance.credentials import CallerIdentity
from services.lenses import connection_store, store
from services.lenses.describe import catalog_of, metric_tree
from services.lenses.store import LensBundle
from services.mcp import server as srv
from services.runtime.compiler import CompileError, compile_intent

LENS = "memberships"
_admin = create_engine(settings.database_admin_url)


def _model() -> SemanticModel:
    return SemanticModel(
        lens=LENS,
        dialect="duckdb",
        entities=[
            Entity(
                name="monthly_fees",
                source=EntitySource(connection="wh", table="fin.monthly_fees"),
                default_time_field="month",
                fields=[
                    Field(name="month", type="date"),
                    Field(name="member_id", type="string"),
                    Field(name="monthly_fees", type="number"),
                    Field(name="movement", type="string"),
                ],
                metrics=[
                    Metric(
                        name="monthly_fees",
                        agg="sum",
                        expr="monthly_fees.monthly_fees",
                        additivity="level",
                        label="monthly membership fees",
                        aliases=["membership fees"],
                        currency="EUR",
                        description="Monthly membership fees on the books at month end.",
                    ),
                    Metric(name="new_fees", agg="sum", expr="monthly_fees.delta_new"),
                    Metric(name="upgrade_fees", agg="sum", expr="monthly_fees.delta_upgrade"),
                    Metric(name="cancelled_fees", agg="sum", expr="monthly_fees.delta_cancel"),
                    Metric(
                        name="net_new_fees",
                        type="derived",
                        expr="{new_fees} + {upgrade_fees} - {cancelled_fees}",
                        label="net new fees",
                    ),
                    Metric(name="members", agg="count_distinct", expr="monthly_fees.member_id"),
                    Metric(
                        name="fees_per_member",
                        type="ratio",
                        numerator="monthly_fees",
                        denominator="members",
                    ),
                ],
            ),
            Entity(
                name="members",
                source=EntitySource(connection="wh", table="fin.members"),
                primary_key=["member_id"],
                fields=[
                    Field(name="member_id", type="string"),
                    Field(name="location", type="string"),
                    Field(name="desks", type="number"),
                ],
            ),
            Entity(
                name="bookings",
                source=EntitySource(connection="wh", table="fin.bookings"),
                default_time_field="day",
                fields=[
                    Field(name="day", type="date"),
                    Field(name="member_id", type="string"),
                    Field(name="amount", type="number"),
                ],
                metrics=[Metric(name="booked", agg="sum", expr="bookings.amount")],
            ),
        ],
        joins=[
            Join(
                left="monthly_fees",
                right="members",
                on="monthly_fees.member_id = members.member_id",
                relationship="many_to_one",
            )
        ],
    )


def _warehouse(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA fin")
    con.execute(
        "CREATE TABLE fin.monthly_fees (month DATE, member_id VARCHAR, monthly_fees DOUBLE, "
        "movement VARCHAR, delta_new DOUBLE, delta_upgrade DOUBLE, delta_cancel DOUBLE)"
    )
    con.execute(
        """INSERT INTO fin.monthly_fees VALUES
        ('2026-08-01', 'a', 100, 'flat', 0, 0, 0),
        ('2026-08-01', 'b', 200, 'flat', 0, 0, 0),
        ('2026-09-01', 'a', 120, 'upgrade', 0, 20, 0),
        ('2026-09-01', 'b', 260, 'upgrade', 0, 60, 0),
        ('2026-09-01', 'c', 50, 'new', 50, 0, 0),
        ('2025-09-01', 'a', 80, 'flat', 0, 0, 0)"""
    )
    con.execute("CREATE TABLE fin.members (member_id VARCHAR, location VARCHAR, desks INT)")
    con.execute(
        "INSERT INTO fin.members VALUES ('a','north',1), ('b','harbour',12), ('c','north',2)"
    )
    con.execute("CREATE TABLE fin.bookings (day DATE, member_id VARCHAR, amount DOUBLE)")
    con.execute(
        """INSERT INTO fin.bookings VALUES
        ('2026-08-05', 'a', 10), ('2026-08-20', 'b', 5),
        ('2026-09-03', 'a', 7), ('2026-09-10', 'c', 50), ('2026-09-30', 'b', 60)"""
    )
    con.close()


SEPT = [
    IntentFilter(field="month", op=">=", value="2026-09-01"),
    IntentFilter(field="month", op="<", value="2026-10-01"),
]


def _rows(path: Path, sql: str) -> tuple[list[str], list[tuple[object, ...]]]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        cur = con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()
    finally:
        con.close()


# ── the catalog, pure ────────────────────────────────────────────────────────


def test_a_derived_metric_lists_what_it_is_computed_from() -> None:
    metrics, _dims = catalog_of(_model())
    by = {m.name: m for m in metrics}
    assert by["net_new_fees"].depends_on == ["new_fees", "upgrade_fees", "cancelled_fees"]
    assert by["net_new_fees"].type == "derived" and by["net_new_fees"].label == "net new fees"
    assert by["new_fees"].depends_on == []


def test_a_ratio_names_its_numerator_and_denominator() -> None:
    metrics, _dims = catalog_of(_model())
    ratio = next(m for m in metrics if m.name == "fees_per_member")
    assert ratio.depends_on == ["monthly_fees", "members"]


def test_the_catalog_carries_what_an_app_renders() -> None:
    metrics, dims = catalog_of(
        _model(),
        domains={"members": {"location": ["harbour", "north"]}},
        certified=[("c1", ["monthly_fees", "month"]), ("c2", ["booked"])],
    )
    fees = next(m for m in metrics if m.name == "monthly_fees")
    assert fees.label == "monthly membership fees" and fees.aliases == ["membership fees"]
    assert fees.unit == "EUR" and fees.additivity == "level"
    assert fees.time_field == "month"
    assert fees.grains == ["day", "week", "month", "quarter", "year"]
    assert fees.certified_answer_ids == ["c1"]
    # its own members by bare name, a joined entity's qualified; a number is no dimension
    assert "movement" in fees.dimensions and "members.location" in fees.dimensions
    assert "monthly_fees" not in fees.dimensions and "members.desks" not in fees.dimensions
    # bookings has no declared join to members: nothing of members is callable with it
    booked = next(m for m in metrics if m.name == "booked")
    assert booked.dimensions == ["day", "member_id"]
    location = next(d for d in dims if d.name == "location")
    assert location.entity == "members" and location.values == ["harbour", "north"]
    assert next(d for d in dims if d.name == "movement").values is None


def test_the_tree_roots_are_what_nothing_depends_on() -> None:
    metrics, _dims = catalog_of(_model())
    roots = {n.metric: n for n in metric_tree(metrics)}
    assert set(roots) == {"net_new_fees", "fees_per_member", "booked"}
    assert [c.metric for c in roots["net_new_fees"].children] == [
        "new_fees",
        "upgrade_fees",
        "cancelled_fees",
    ]
    assert [c.metric for c in roots["fees_per_member"].children] == ["monthly_fees", "members"]


def test_an_exposed_table_stays_out_of_the_catalog() -> None:
    model = _model()
    model.entities.append(
        Entity(
            name="raw_events",
            source=EntitySource(connection="wh", table="raw.events"),
            exposed=True,
            fields=[Field(name="kind", type="string")],
        )
    )
    metrics, dims = catalog_of(model)
    assert all(d.entity != "raw_events" for d in dims)
    assert all(m.entity != "raw_events" for m in metrics)


# ── compare, compiled and run on DuckDB ──────────────────────────────────────


def test_a_level_metric_compares_latest_snapshot_to_latest_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    _warehouse(db)
    intent = QueryIntent(
        metrics=["monthly_fees"], filters=SEPT, compare=IntentCompare(to="previous_period")
    )
    cols, rows = _rows(db, compile_intent(intent, _model()))
    assert cols == [
        "monthly_fees",
        "monthly_fees_compare",
        "monthly_fees_delta",
        "monthly_fees_delta_pct",
    ]
    # September's snapshot (430) against August's (300), never a sum of snapshots
    assert rows == [(430.0, 300.0, 130.0, pytest.approx(130 / 300))]


def test_a_flow_metric_compares_sum_to_sum(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    _warehouse(db)
    intent = QueryIntent(
        metrics=["booked"],
        filters=[
            IntentFilter(field="day", op=">=", value="2026-09-01"),
            IntentFilter(field="day", op="<=", value="2026-09-30"),
        ],
        compare=IntentCompare(to="previous_period"),
    )
    _cols, rows = _rows(db, compile_intent(intent, _model()))
    assert rows == [(117.0, 15.0, 102.0, pytest.approx(102 / 15))]


def test_previous_year_by_a_dimension_keeps_groups_only_one_window_has(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    _warehouse(db)
    intent = QueryIntent(
        metrics=["monthly_fees"],
        dimensions=["member_id"],
        filters=SEPT,
        compare=IntentCompare(to="previous_year"),
        order_by=[{"field": "member_id", "dir": "asc"}],  # type: ignore[list-item]
    )
    _cols, rows = _rows(db, compile_intent(intent, _model()))
    assert rows == [
        ("a", 120.0, 80.0, 40.0, pytest.approx(0.5)),
        ("b", 260.0, None, None, None),
        ("c", 50.0, None, None, None),
    ]


def test_a_month_grain_matches_each_month_to_its_counterpart(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    _warehouse(db)
    intent = QueryIntent(
        metrics=["monthly_fees"],
        grain="month",
        filters=[
            IntentFilter(field="month", op=">=", value="2026-09-01"),
            IntentFilter(field="month", op="<", value="2026-10-01"),
        ],
        compare=IntentCompare(to="2026-08"),
    )
    _cols, rows = _rows(db, compile_intent(intent, _model()))
    assert [r[1:] for r in rows] == [(430.0, 300.0, 130.0, pytest.approx(130 / 300))]


def test_compare_without_a_date_window_is_refused_naming_why() -> None:
    with pytest.raises(CompileError, match="no period to compare against"):
        compile_intent(
            QueryIntent(metrics=["monthly_fees"], compare=IntentCompare(to="previous_period")),
            _model(),
        )


def test_compare_on_an_open_window_is_refused() -> None:
    with pytest.raises(CompileError, match="bounded on both ends"):
        compile_intent(
            QueryIntent(
                metrics=["monthly_fees"],
                filters=[IntentFilter(field="month", op=">=", value="2026-01-01")],
                compare=IntentCompare(to="previous_year"),
            ),
            _model(),
        )


def test_buckets_that_cannot_line_up_are_refused_not_approximated() -> None:
    with pytest.raises(CompileError, match="month boundary"):
        compile_intent(
            QueryIntent(
                metrics=["booked"],
                grain="month",
                filters=[
                    IntentFilter(field="day", op=">=", value="2026-09-05"),
                    IntentFilter(field="day", op="<", value="2026-10-05"),
                ],
                compare=IntentCompare(to="previous_period"),
            ),
            _model(),
        )


def test_an_unreadable_compare_target_names_the_grammar() -> None:
    with pytest.raises(CompileError, match="previous_period, previous_year or a range"):
        compile_intent(
            QueryIntent(
                metrics=["monthly_fees"], filters=SEPT, compare=IntentCompare(to="last month")
            ),
            _model(),
        )


# ── compare on a moment column, per dialect ──────────────────────────────────


def _events_model(dialect: str, declared: str = "timestamp") -> SemanticModel:
    return SemanticModel(
        lens="events",
        dialect=dialect,
        entities=[
            Entity(
                name="events",
                source=EntitySource(connection="wh", table="app.events"),
                default_time_field="seen",
                fields=[Field(name="seen", type=declared), Field(name="id", type="integer")],
                metrics=[Metric(name="events", agg="count", expr="events.id")],
            )
        ],
    )


# Events on the last day of each month, late in the day: a bucket or a bound
# that loses the time of day, or the month's last day, loses them.
EVENTS = [
    ("2025-07-31 23:30:00", 1),
    ("2026-03-31 23:30:00", 2),
    ("2026-04-30 23:30:00", 3),
    ("2026-04-30 23:30:00", 4),
    ("2026-06-30 23:30:00", 5),
    ("2026-07-31 23:30:00", 6),
    ("2026-07-31 23:30:00", 7),
    ("2026-07-31 23:30:00", 8),
]
SUMMER = [
    IntentFilter(field="seen", op=">=", value="2026-06-01"),
    IntentFilter(field="seen", op="<", value="2026-09-01"),
]
# month -> (events, events_compare): June, July and August 2026 against March
# to May (previous_period) and against the same months of 2025 (previous_year).
COMPARED = {
    "previous_period": {"2026-06": (1, 1), "2026-07": (3, 2)},
    "previous_year": {"2026-06": (1, None), "2026-07": (3, 1)},
}


def _by_month(cols: list[str], rows: list[tuple[object, ...]]) -> dict[str, tuple[object, ...]]:
    i, n, c = cols.index("seen_month"), cols.index("events"), cols.index("events_compare")
    return {str(r[i])[:7]: (r[n], r[c]) for r in rows}


@pytest.mark.parametrize("to", ["previous_period", "previous_year"])
@pytest.mark.parametrize("declared", ["timestamp", "date"])
def test_a_month_grain_compare_runs_on_a_timestamp_column_in_duckdb(
    tmp_path: Path, to: str, declared: str
) -> None:
    db = tmp_path / "events.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA app")
    con.execute("CREATE TABLE app.events (seen TIMESTAMP, id INT)")
    con.executemany("INSERT INTO app.events VALUES (?, ?)", EVENTS)
    con.close()
    intent = QueryIntent(
        metrics=["events"], grain="month", filters=SUMMER, compare=IntentCompare(to=to)
    )
    sql = compile_intent(
        intent, _events_model("duckdb", declared), column_types={"events": {"seen": "TIMESTAMP"}}
    )
    cols, rows = _rows(db, sql)
    assert _by_month(cols, rows) == COMPARED[to]


@pytest.mark.parametrize(
    ("physical", "declared", "grain", "written", "never"),
    [
        # BigQuery's DATE_SUB takes a TIMESTAMP only in parts up to DAY.
        ("TIMESTAMP", "timestamp", "month", "DATETIME_SUB(CAST(", "DATE_SUB("),
        ("TIMESTAMP", "date", "quarter", "DATETIME_SUB(CAST(", "DATE_SUB("),
        ("TIMESTAMP", "timestamp", "week", "TIMESTAMP_SUB(", "DATE_SUB("),
        ("DATETIME", "timestamp", "month", "DATETIME_SUB(", "CAST(dst_current"),
        ("DATE", "date", "month", "DATE_SUB(", "DATETIME"),
        # No profile: the declared type decides.
        (None, "timestamp", "year", "DATETIME_SUB(CAST(", "DATE_SUB("),
        (None, "date", "month", "DATE_SUB(", "DATETIME"),
    ],
)
def test_a_bigquery_bucket_moves_in_its_own_type(
    physical: str | None, declared: str, grain: str, written: str, never: str
) -> None:
    window = (
        [
            IntentFilter(field="seen", op=">=", value="2026-06-01"),
            IntentFilter(field="seen", op="<", value="2026-06-29"),
        ]
        if grain == "week"
        else [
            IntentFilter(field="seen", op=">=", value="2026-01-01"),
            IntentFilter(field="seen", op="<", value="2027-01-01"),
        ]
    )
    intent = QueryIntent(
        metrics=["events"], grain=grain, filters=window, compare=IntentCompare(to="previous_year")
    )
    types = {"events": {"seen": physical}} if physical else None
    sql = compile_intent(intent, _events_model("bigquery", declared), column_types=types)
    join = sql[sql.index(" ON ") : sql.index(" UNION ALL ")]
    label = sql[sql.index(" UNION ALL SELECT ") : sql.index(", NULL AS events")]
    assert written in join and never not in join
    assert written.replace("_SUB", "_ADD") in label
    # BigQuery refuses a NOT EXISTS whose condition is not a plain equality.
    assert "NOT EXISTS" not in sql and "dst_current.dst_matched IS NULL" in sql
    if written == "DATETIME_SUB(CAST(":
        # Moved through DATETIME and back, so both sides compare as TIMESTAMP.
        assert join.count("AS TIMESTAMP)") == label.count("AS TIMESTAMP)") == 1
    if physical == "TIMESTAMP" and declared == "date":
        # The profile's TIMESTAMP, not the declared date, types the bounds.
        assert "CAST('2026-01-01' AS TIMESTAMP)" in sql and "AS DATE)" not in sql


def test_a_snowflake_month_compare_steps_in_calendar_months() -> None:
    intent = QueryIntent(
        metrics=["events"],
        grain="month",
        filters=SUMMER,
        compare=IntentCompare(to="previous_period"),
    )
    sql = compile_intent(
        intent, _events_model("snowflake"), column_types={"events": {"seen": "TIMESTAMP_NTZ"}}
    )
    assert "DATEADD(MONTH, 3 * -1, dst_current.seen_month)" in sql
    assert "DATEADD(MONTH, 3, dst_compare.seen_month)" in sql


# ── the doors, end to end ────────────────────────────────────────────────────


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


@needs_db
@pytest.mark.parametrize("to", ["previous_period", "previous_year"])
@pytest.mark.parametrize("physical", ["timestamp", "timestamptz", "date"])
def test_a_month_grain_compare_runs_on_postgres(to: str, physical: str) -> None:
    declared = "date" if physical == "date" else "timestamp"
    intent = QueryIntent(
        metrics=["events"], grain="month", filters=SUMMER, compare=IntentCompare(to=to)
    )
    sql = compile_intent(
        intent,
        _events_model("postgres", declared),
        column_types={"events": {"seen": physical.upper()}},
    )
    schema = f"app_cmp_{uuid.uuid4().hex[:8]}"
    with _admin.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
        c.execute(text(f"CREATE TABLE {schema}.events (seen {physical}, id int)"))
        for seen, n in EVENTS:
            c.execute(text(f"INSERT INTO {schema}.events VALUES (:s, :n)"), {"s": seen, "n": n})
    try:
        with _admin.begin() as c:
            c.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            res = c.execute(text(sql.replace("app.events", f"{schema}.events")))
            cols, rows = list(res.keys()), [tuple(r) for r in res]
    finally:
        with _admin.begin() as c:
            c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    assert _by_month(cols, rows) == COMPARED[to]


@pytest.fixture
def lens(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """An org with the memberships lens over a DuckDB file, granted to group `apps`.
    Yields (admin token, caller key of a member of `apps`)."""
    ratelimit.reset()
    db = tmp_path / "wh.duckdb"
    _warehouse(db)
    admin_raw, caller_raw = new_admin_token(), new_caller_key()
    with _admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Apps') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(admin_raw)},
        )
        cid = c.execute(
            text(
                "INSERT INTO caller (org_id, name, type, groups) "
                "VALUES (:o, 'app@example.com', 'service', ARRAY['apps']) RETURNING id"
            ),
            {"o": org},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO api_key (org_id, caller_id, key_hash, prefix) VALUES (:o, :c, :h, :p)"
            ),
            {"o": org, "c": cid, "h": hash_token(caller_raw), "p": caller_raw[:12]},
        )
    config = LensConfig(name=LENS, connections=["wh"], access={"allow": [AccessRule(group="apps")]})
    with org_session(org) as s:
        connection_store.create_connection(s, "wh", "duckdb", {"path": str(db)}, None)
        store.create_lens(s, LensBundle(config=config, semantic_model=_model()))
        store.publish(s, LENS)
        s.commit()
    try:
        yield admin_raw, caller_raw
    finally:
        with _admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})
        ratelimit.reset()


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


@needs_db
def test_the_catalog_door_serves_the_tree_and_respects_grants(
    lens: tuple[str, str], live_client: TestClient
) -> None:
    admin_raw, caller_raw = lens
    r = live_client.get(f"/v1/lenses/{LENS}/catalog", headers=_auth(caller_raw))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {n["metric"] for n in body["tree"]} == {"net_new_fees", "fees_per_member", "booked"}
    assert any(d["name"] == "location" for d in body["dimensions"])
    described = live_client.get(f"/v1/lenses/{LENS}", headers=_auth(caller_raw)).json()
    assert [m["name"] for m in described["metrics"]] == [m["name"] for m in body["metrics"]]
    assert described["entities"]  # additive: everything describe served before is still there

    # A caller outside the allow-list gets the one not-available answer, catalog or not.
    with _admin.begin() as c:
        c.execute(text("UPDATE caller SET groups = ARRAY['other'] WHERE name = 'app@example.com'"))
    for path in (f"/v1/lenses/{LENS}/catalog", f"/v1/lenses/{LENS}"):
        denied = live_client.get(path, headers=_auth(caller_raw))
        assert denied.status_code == 404, path


@needs_db
def test_the_metrics_door_takes_compare(lens: tuple[str, str], live_client: TestClient) -> None:
    admin_raw, _caller = lens
    r = live_client.post(
        f"/v1/lenses/{LENS}/metrics",
        json={
            "metrics": ["monthly_fees"],
            "filters": [f.model_dump() for f in SEPT],
            "compare": {"to": "previous_period"},
        },
        headers=_auth(admin_raw),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["columns"] == [
        "monthly_fees",
        "monthly_fees_compare",
        "monthly_fees_delta",
        "monthly_fees_delta_pct",
    ]
    assert data["rows"][0][:3] == [430.0, 300.0, 130.0]
    # the second window is on the ledger, so its column never reads as the asked one
    slots = r.json()["resolution"]["slots"]
    assert {
        "kind": "window",
        "name": "month",
        "source": "declared",
        "value": "compared with 2026-08-01 to 2026-08-31 (previous_period)",
    } in slots
    refused = live_client.post(
        f"/v1/lenses/{LENS}/metrics",
        json={"metrics": ["monthly_fees"], "compare": {"to": "previous_period"}},
        headers=_auth(admin_raw),
    )
    assert refused.status_code == 400 and "no period to compare" in refused.json()["detail"]


@needs_db
def test_a_batch_answers_each_intent_alone_at_one_data_as_of(
    lens: tuple[str, str], live_client: TestClient
) -> None:
    _admin_raw, caller_raw = lens
    window = [f.model_dump() for f in SEPT]
    r = live_client.post(
        f"/v1/lenses/{LENS}/metrics/batch",
        json={
            "intents": [
                {"id": "root", "metrics": ["net_new_fees"], "filters": window},
                {
                    "id": "fees",
                    "metrics": ["monthly_fees"],
                    "filters": window,
                    "compare": {"to": "previous_period"},
                },
                {"id": "broken", "metrics": ["not_a_metric"]},
            ]
        },
        headers=_auth(caller_raw),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    results = body["results"]
    assert list(results) == ["root", "fees", "broken"]
    assert results["root"]["ok"] and results["root"]["answer"]["data"]["rows"] == [[130.0]]
    assert results["fees"]["answer"]["data"]["rows"][0][:3] == [430.0, 300.0, 130.0]
    # the failure is the broken intent's alone, with the status the single door gives it
    assert results["broken"] == {
        "ok": False,
        "request_id": None,
        "status": 400,
        "error": results["broken"]["error"],
        "answer": None,
    }
    assert "not_a_metric" in results["broken"]["error"]
    # each served intent is its own request, with its own receipt
    ids = {results[k]["request_id"] for k in ("root", "fees")}
    assert len(ids) == 2 and all(i and i.startswith("req_") for i in ids)
    assert {results[k]["answer"]["request_id"] for k in ("root", "fees")} == ids
    # DuckDB cannot hold one snapshot across connections, and the batch says so
    assert body["snapshot"] == "per_query" and "own moment" in body["snapshot_note"]
    assert {results[k]["answer"]["data_as_of"] for k in ("root", "fees")} == {body["data_as_of"]}


@needs_db
def test_a_batch_intent_whose_answer_errored_is_not_ok(
    lens: tuple[str, str], live_client: TestClient, tmp_path: Path
) -> None:
    """An app keys its error rendering on `ok`: an intent the door answered
    with an error is `ok: false`, beside the answer that says why."""
    _admin_raw, caller_raw = lens
    con = duckdb.connect(str(tmp_path / "wh.duckdb"))
    con.execute("ALTER TABLE fin.bookings DROP COLUMN amount")
    con.close()
    r = live_client.post(
        f"/v1/lenses/{LENS}/metrics/batch",
        json={
            "intents": [
                {
                    "id": "fees",
                    "metrics": ["monthly_fees"],
                    "filters": [f.model_dump() for f in SEPT],
                },
                {"id": "booked", "metrics": ["booked"]},
            ]
        },
        headers=_auth(caller_raw),
    )
    assert r.status_code == 200, r.text
    fees, booked = r.json()["results"]["fees"], r.json()["results"]["booked"]
    assert fees["ok"] is True and fees["answer"]["status"] == "ok"
    assert booked["ok"] is False and booked["status"] == 200
    assert booked["answer"]["status"] == "error"
    assert booked["error"] == booked["answer"]["answer"]
    assert booked["request_id"] == booked["answer"]["request_id"]


def test_the_metrics_door_compiles_with_the_profiles_column_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A column declared `date` that the warehouse stores as a TIMESTAMP: the
    door casts its bounds to the TIMESTAMP, as the prose door does, alone and
    in a comparison."""
    profile = TableProfile(
        connection="wh",
        table="app.events",
        columns=[
            ColumnProfile(name="seen", type="TIMESTAMP"),
            ColumnProfile(name="id", type="INT64"),
        ],
    )
    monkeypatch.setattr(
        query_api.assembly, "profile_facts", lambda b, o: ([profile], None, None, {})
    )
    bundle = LensBundle(
        config=LensConfig(name="events", connections=["wh"]),
        semantic_model=_events_model("bigquery", "date"),
    )
    caller = CallerIdentity(org_id=uuid.uuid4(), name="app", is_admin=False)
    serve = query_api._MetricsServe("events", bundle, caller, FakeConnector(), None, "none")
    window = [
        IntentFilter(field="seen", op=">=", value="2026-03-20"),
        IntentFilter(field="seen", op="<=", value="2026-03-28"),
    ]
    sql = serve.compiled(QueryIntent(metrics=["events"], filters=window))
    assert "events.seen >= CAST('2026-03-20' AS TIMESTAMP)" in sql
    # the last day is closed at the next midnight, so its events count
    assert "events.seen < CAST('2026-03-29' AS TIMESTAMP)" in sql
    compared = serve.compiled(
        QueryIntent(
            metrics=["events"],
            grain="month",
            filters=SUMMER,
            compare=IntentCompare(to="previous_year"),
        )
    )
    assert "AS DATE)" not in compared and "DATETIME_SUB(CAST(" in compared


@needs_db
def test_a_batch_over_the_cap_is_a_422(lens: tuple[str, str], live_client: TestClient) -> None:
    admin_raw, _caller = lens
    over = [{"id": f"n{i}", "metrics": ["monthly_fees"]} for i in range(51)]
    r = live_client.post(
        f"/v1/lenses/{LENS}/metrics/batch", json={"intents": over}, headers=_auth(admin_raw)
    )
    assert r.status_code == 422
    dupes = [{"id": "x", "metrics": ["monthly_fees"]}, {"id": "x", "metrics": ["booked"]}]
    r = live_client.post(
        f"/v1/lenses/{LENS}/metrics/batch", json={"intents": dupes}, headers=_auth(admin_raw)
    )
    assert r.status_code == 422 and "unique" in r.text


@needs_db
def test_a_batch_beyond_the_rate_limit_refuses_the_rest_one_by_one(
    lens: tuple[str, str], live_client: TestClient
) -> None:
    """Each intent costs what it would cost alone: a batch is not a way around
    the per-minute limit."""
    _admin_raw, caller_raw = lens
    with _admin.begin() as c:
        org = c.execute(text("SELECT org_id FROM caller WHERE name = 'app@example.com'")).scalar()
    ratelimit.reset()
    key = f"{org}:{LENS}:app@example.com"
    for _ in range(58):  # the default limit is 60 a minute: two left
        assert ratelimit.check(key, 60)
    r = live_client.post(
        f"/v1/lenses/{LENS}/metrics/batch",
        json={"intents": [{"id": f"n{i}", "metrics": ["booked"]} for i in range(4)]},
        headers=_auth(caller_raw),
    )
    assert r.status_code == 200, r.text
    statuses = [(v["ok"], v["status"]) for v in r.json()["results"].values()]
    assert statuses == [(True, None), (True, None), (False, 429), (False, 429)]


@needs_db
def test_postgres_holds_one_snapshot_across_queries() -> None:
    """Queries through the pinned connector each open their own connection and
    still read the state of the moment the snapshot was taken."""
    url = make_url(settings.database_admin_url)
    pg = PostgresConnector(
        host=url.host or "localhost",
        port=url.port or 5432,
        database=url.database or "",
        user=url.username or "",
        password=url.password or "",
    )
    count = "SELECT count(*) FROM app_door_snapshot"
    with _admin.begin() as c:
        c.execute(text("CREATE TABLE app_door_snapshot (n int)"))
        c.execute(text("INSERT INTO app_door_snapshot VALUES (1)"))
    try:
        with _batch_reader(pg) as (pinned, kind):
            assert kind == "shared"
            before = pinned.execute(count).rows
            with _admin.begin() as c:
                c.execute(text("INSERT INTO app_door_snapshot VALUES (2)"))
            assert pinned.execute(count).rows == before == [[1]]
            assert pg.execute(count).rows == [[2]]
    finally:
        with _admin.begin() as c:
            c.execute(text("DROP TABLE app_door_snapshot"))


# ── the MCP tools ────────────────────────────────────────────────────────────


def _mcp(monkeypatch: pytest.MonkeyPatch, sent: list[tuple[str, object]]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        sent.append((f"{request.method} {request.url.path}", body))
        return httpx.Response(200, json={"lens": LENS, "results": {}, "tree": []})

    monkeypatch.setattr(srv, "DST_API_KEY", "dst_test")
    monkeypatch.setattr(
        srv,
        "_client",
        lambda key, agent="mcp": httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://dst.test"
        ),
    )


def test_the_mcp_tools_reach_the_new_doors(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, object]] = []
    _mcp(monkeypatch, sent)
    assert asyncio.run(srv.metric_catalog(LENS, ctx=None))["ok"] is True
    intents = [{"id": "fees", "metrics": ["monthly_fees"]}]
    assert asyncio.run(srv.query_metrics_batch(LENS, intents, ctx=None))["ok"] is True
    asyncio.run(
        srv.query_metrics(LENS, ctx=None, metrics=["monthly_fees"], compare={"to": "previous_year"})
    )
    assert sent == [
        (f"GET /v1/lenses/{LENS}/catalog", None),
        (f"POST /v1/lenses/{LENS}/metrics/batch", {"intents": intents, "format": "structured"}),
        (
            f"POST /v1/lenses/{LENS}/metrics",
            {
                "format": "structured",
                "metrics": ["monthly_fees"],
                "compare": {"to": "previous_year"},
            },
        ),
    ]
