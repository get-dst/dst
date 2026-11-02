"""A fake Cube for the cube lane's tests: a small model served as ``/v1/meta``,
and ``/v1/sql`` compiled the way Cube compiles a single-cube query (aliases
``cube__member``, ``?`` parameters, ``aliasNameToMember``), so the lane runs
end to end on a DuckDB file with no Cube and no model in the loop.

The world is invented: two companies of one group selling online, a stock of
staff counted monthly, quotes decided on their own date.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import httpx

from services.contracts.cube_meta import MetaDoc
from services.contracts.cube_probe import CubeProbe
from services.contracts.lens_model import Overlay
from services.cube.client import CubeClient
from services.cube.lens_view import CubeLensView

TODAY = date(2026, 9, 1)
SECRET = "fake-cube-secret-fake-cube-secret-00"


def _m(name: str, agg: str, sql: str, **dst: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": "number",
        "aggType": agg,
        "title": name.split(".")[-1].replace("_", " "),
        "_sql": sql,
        "meta": {"dst": dst} if dst else {},
    }


def _d(name: str, sql: str, kind: str = "string", **dst: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": kind,
        "title": name.split(".")[-1].replace("_", " "),
        "_sql": sql,
        "meta": {"dst": dst} if dst else {},
    }


CUBES: list[dict[str, Any]] = [
    {
        "name": "orders",
        "type": "cube",
        "title": "Webshop orders",
        "_table": "shop.orders",
        "meta": {"dst": {"grain": "one row per order", "key": ["order_id"]}},
        "measures": [
            _m("orders.count", "count", "order_id", aliases=["orders"]),
            _m("orders.revenue", "sum", "amount", aliases=["sales", "revenue"], clock="ordered_at"),
            _m("orders.revenue_net", "sum", "amount - refunded", clock="ordered_at"),
        ],
        "dimensions": [
            _d("orders.order_id", "order_id"),
            _d("orders.status", "status", dictionary=True, value_aliases={"Paid": ["settled"]}),
            _d("orders.company", "company", dictionary=True),
            _d("orders.region", "region", dictionary=True),
            _d("orders.customer_id", "customer_id"),
            _d("orders.customer_name", "customer_name", label_of="customer_id"),
            _d("orders.ordered_at", "ordered_at", "time"),
        ],
        "segments": [],
    },
    {
        "name": "refund_events",
        "type": "cube",
        "title": "Webshop revenue by event date",
        "_table": "shop.refund_events",
        "measures": [
            _m("refund_events.revenue_refund_date", "sum", "amount", clock="event_at"),
        ],
        "dimensions": [
            _d("refund_events.event_at", "event_at", "time"),
            _d("refund_events.company", "company", dictionary=True),
        ],
        "segments": [],
    },
    {
        "name": "staff",
        "type": "cube",
        "title": "Staff",
        "_table": "hr.staff_monthly",
        "measures": [
            _m("staff.headcount", "sum", "headcount", additivity="level", snapshot_grain="month"),
        ],
        "dimensions": [
            _d("staff.month", "month", "time"),
            _d("staff.company", "company", dictionary=True),
            _d("staff.salary", "salary", hide={"except_groups": ["hr"]}),
        ],
        "segments": [],
    },
    {
        # A daily snapshot: which accounts were active on each day, and the
        # staff on roster that day. No snapshot_grain declared (the default,
        # month, is what a migrated model carries), so a month bucket would
        # union the month's accounts and add its 29 days of headcount.
        "name": "staff_daily",
        "type": "cube",
        "title": "Daily roster",
        "_table": "hr.staff_daily",
        "measures": [
            _m(
                "staff_daily.active_accounts",
                "countDistinct",
                "account_id",
                additivity="level",
                clock="day",
            ),
            _m("staff_daily.headcount", "sum", "headcount", additivity="level", clock="day"),
        ],
        "dimensions": [
            _d("staff_daily.day", "day", "time"),
            _d("staff_daily.company", "company", dictionary=True),
        ],
        "segments": [],
    },
    {
        "name": "north_invoices",
        "type": "cube",
        "title": "North Oy invoices",
        "_table": "fin.north_invoices",
        "measures": [_m("north_invoices.invoiced", "sum", "amount", clock="issued_at")],
        "dimensions": [_d("north_invoices.issued_at", "issued_at", "time")],
        "segments": [],
    },
    {
        "name": "quotes",
        "type": "cube",
        "title": "Quotes",
        "_table": "sales.quotes",
        "measures": [_m("quotes.won", "count", "quote_id", clock="decided_at")],
        "dimensions": [
            _d("quotes.sent_at", "sent_at", "time"),
            _d("quotes.decided_at", "decided_at", "time"),
        ],
        "segments": [],
    },
    {
        "name": "contracts",
        "type": "cube",
        "title": "Service contracts",
        "_table": "sales.contracts",
        "measures": [_m("contracts.count", "count", "contract_id", clock="end_date")],
        "dimensions": [_d("contracts.end_date", "end_date", "time")],
        "segments": [],
    },
]
VIEWS: list[dict[str, Any]] = [
    {
        "name": "shop",
        "type": "view",
        "measures": [
            {"name": "shop.count", "type": "number", "aliasMember": "orders.count"},
            {"name": "shop.revenue", "type": "number", "aliasMember": "orders.revenue"},
            {"name": "shop.revenue_net", "type": "number", "aliasMember": "orders.revenue_net"},
        ],
        "dimensions": [
            {"name": "shop.status", "type": "string", "aliasMember": "orders.status"},
            {"name": "shop.ordered_at", "type": "time", "aliasMember": "orders.ordered_at"},
        ],
        "segments": [],
    }
]


def meta_payload() -> dict[str, Any]:
    """The model as ``/v1/meta`` serves it (the fake's compile hints removed)."""

    def strip(x: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in x.items() if not k.startswith("_")}

    cubes = []
    for c in CUBES:
        cubes.append(
            {
                **strip(c),
                "measures": [strip(m) for m in c["measures"]],
                "dimensions": [strip(d) for d in c["dimensions"]],
            }
        )
    return {"cubes": cubes + VIEWS}


def doc() -> MetaDoc:
    return MetaDoc.parse(meta_payload())


OVERLAY: dict[str, Any] = {
    "ambiguous": [
        {
            "metric": "refund_basis",
            "summary": "Ambiguous - revenue net of refunds by order date or by refund date.",
            "readings": [
                {
                    "says": "refunds netted against their orders (order date)",
                    "members": ["orders.revenue_net"],
                    "select_words": ["order date"],
                },
                {
                    "says": "refunds netted when issued (refund date)",
                    "default": True,
                    "members": ["refund_events.revenue_refund_date"],
                    "select_words": ["refund date"],
                },
            ],
        }
    ],
    "scope": {
        "names": {
            "North Oy": ["north", "finland", "dictionary:orders.region"],
            "South AB": ["south", "sweden"],
        }
    },
    "cubes": {
        "quotes": {"clock_kind": {"decided_at": "event"}},
        "north_invoices": {
            "entity_scope": {
                "entity": "North Oy",
                "group_level": {"north_invoices.invoiced": "orders.revenue"},
            }
        },
    },
    "hide": [{"cube": "staff", "dimension": "salary", "except_groups": ["hr"]}],
    "lenses": {
        "shop": {
            "views": ["shop"],
            "not_computable": [{"measure": "lifetime value", "aliases": ["ltv"]}],
            "not_for": [{"ask": "contract churn", "sibling": "service"}],
        }
    },
}

PROBE: dict[str, Any] = {
    "clock_end": "2026-08-31",
    "stale_after_days": 3,
    "dictionaries": {
        "orders.status": ["Open", "Paid", "Refunded"],
        "orders.company": ["North Oy", "South AB"],
        "orders.region": ["Pirkanmaa", "Uusimaa"],
        "refund_events.company": ["North Oy", "South AB"],
        "staff.company": ["North Oy", "South AB"],
    },
    "cubes": {
        "orders": {
            "clock": "orders.ordered_at",
            "feed_axes": ["company"],
            "streams": [
                {"values": {"company": "North Oy"}, "end": "2026-06-05", "stopped": True},
                {"values": {"company": "South AB"}, "end": "2026-08-31"},
            ],
            "time_dimensions": {"ordered_at": {"kind": "event", "data_end": "2026-08-31"}},
        },
        "quotes": {
            "clock": "quotes.decided_at",
            "time_dimensions": {
                "decided_at": {
                    "kind": "event",
                    "data_end": "2026-08-31",
                    "max": "2026-10-07",
                    "future_rows": 4,
                },
                "sent_at": {"kind": "event", "data_end": "2026-08-31"},
            },
        },
        "contracts": {
            "clock": "contracts.end_date",
            "time_dimensions": {
                "end_date": {"kind": "schedule", "data_end": "2026-07-31", "future_rows": 3},
            },
        },
        "staff": {
            "clock": "staff.month",
            # The last snapshot in hr.staff_monthly, as the monitor records it.
            "time_dimensions": {"month": {"kind": "event", "data_end": "2026-06-01"}},
        },
        "staff_daily": {
            "clock": "staff_daily.day",
            "time_dimensions": {"day": {"kind": "event", "data_end": "2026-08-29"}},
        },
    },
    "measures": {
        "orders.revenue": {"cube": "orders", "clock": "orders.ordered_at", "type": "sum"},
        "orders.count": {
            "cube": "orders",
            "clock": "orders.ordered_at",
            "type": "count",
            "partial": {
                "period": "2026-08",
                "partial_from": "2026-08-01",
                "partial_through": "2026-08-31",
                "partial_ratio": 0.4,
                "trailing_median": 100,
                "trailing_months": 6,
                "value": 40,
            },
        },
        "quotes.won": {"cube": "quotes", "clock": "quotes.decided_at", "type": "count"},
        "contracts.count": {"cube": "contracts", "clock": "contracts.end_date", "type": "count"},
        "staff.headcount": {"cube": "staff", "clock": "staff.month", "type": "sum"},
        "staff_daily.active_accounts": {
            "cube": "staff_daily",
            "clock": "staff_daily.day",
            "type": "count_distinct",
        },
        "staff_daily.headcount": {"cube": "staff_daily", "clock": "staff_daily.day", "type": "sum"},
    },
}


def view(
    views: list[str] | None = None, probe: dict[str, Any] | None = None, **overlay: Any
) -> CubeLensView:
    """The fake lens: scoped to ``views`` through its LensModel when given,
    else unscoped (every member the fake Cube serves). ``probe`` overrides
    top-level keys of ``PROBE`` (``clock_end``, ``cubes``, ``measures``)."""
    return CubeLensView.build(
        doc(),
        overlay=Overlay.model_validate({**OVERLAY, **overlay}),
        probe=CubeProbe.model_validate({**PROBE, **(probe or {})}),
        lens="shop" if views else None,
        views=views,
    )


# ── /v1/sql: Cube's compile, for one cube at a time ─────────────────────────

_MEMBERS = {x["name"]: (c, x) for c in CUBES for x in [*c["measures"], *c["dimensions"]]}
_ALIASES = {x["name"]: x["aliasMember"] for v in VIEWS for x in [*v["measures"], *v["dimensions"]]}
_AGG = {"count": "count({})", "sum": "sum({})", "countDistinct": "count(DISTINCT {})"}


def _alias(member: str) -> str:
    return member.replace(".", "__")


def compile_query(query: dict[str, Any]) -> dict[str, Any]:
    """The ``/v1/sql`` payload for a query over one cube (views resolved)."""
    params: list[Any] = []
    select: list[str] = []
    group: list[int] = []
    alias_map: dict[str, str] = {}
    tables: set[str] = set()

    def col(member: str) -> str:
        cube, x = _MEMBERS[_ALIASES.get(member, member)]
        tables.add(cube["_table"])
        return f'"{cube["name"]}".{x["_sql"]}' if re.fullmatch(r"\w+", x["_sql"]) else x["_sql"]

    for d in query.get("dimensions", []):
        select.append(f'{col(d)} "{_alias(d)}"')
        alias_map[_alias(d)] = d
        group.append(len(select))
    where: list[str] = []
    for td in query.get("timeDimensions", []):
        c = col(td["dimension"])
        if td.get("granularity"):
            name = f"{td['dimension']}.{td['granularity']}"
            select.append(f"date_trunc('{td['granularity']}', {c}) \"{_alias(name)}\"")
            alias_map[_alias(name)] = name
            group.append(len(select))
        rng = td.get("dateRange")
        if isinstance(rng, list):
            where.append(f"({c} >= ?::timestamp AND {c} <= ?::timestamp)")
            params += [f"{rng[0]}T00:00:00.000", f"{rng[-1]}T23:59:59.999"]
    for m in query.get("measures", []):
        cube, x = _MEMBERS[_ALIASES.get(m, m)]
        tables.add(cube["_table"])
        select.append(f'{_AGG[x["aggType"]].format(x["_sql"])} "{_alias(m)}"')
        alias_map[_alias(m)] = m
    ops = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

    def clause(f: dict[str, Any]) -> str:
        if "and" in f or "or" in f:
            key = "and" if "and" in f else "or"
            return "(" + f" {key.upper()} ".join(clause(x) for x in f[key]) + ")"
        c = col(f["member"])
        op, vals = f["operator"], f.get("values") or []
        if op == "set":
            return f"({c} IS NOT NULL)"
        if op in ("equals", "notEquals"):
            params.extend(vals)
            marks = ", ".join("?" for _ in vals)
            return f"({c} {'NOT ' if op == 'notEquals' else ''}IN ({marks}))"
        if op in ("beforeOrOnDate", "afterOrOnDate"):
            params.append(vals[0])
            return f"({c} {'<=' if op == 'beforeOrOnDate' else '>='} ?::date)"
        params.append(vals[0])
        return f"({c} {ops[op]} ?)"

    where += [clause(f) for f in query.get("filters", [])]
    (table,) = tables
    cube_name = next(c["name"] for c in CUBES if c["_table"] == table)
    sql = f'SELECT {", ".join(select)} FROM {table} AS "{cube_name}"'
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group:
        sql += " GROUP BY " + ", ".join(map(str, group))
    order = query.get("order") or []
    if isinstance(order, dict):
        order = list(order.items())
    if order:
        sql += " ORDER BY " + ", ".join(f'"{_alias(m)}" {d.upper()}' for m, d in order)
    sql += f" LIMIT {query.get('limit') or 10000}"
    return {"sql": {"sql": [sql, params], "aliasNameToMember": alias_map}}


class FakeCube:
    """The MockTransport handler, recording every compiled query."""

    def __init__(self) -> None:
        self.compiled: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/meta"):
            return httpx.Response(200, json=meta_payload())
        if path.endswith("/sql"):
            query = json.loads(request.content)["query"]
            self.compiled.append(query)
            try:
                return httpx.Response(200, json=compile_query(query))
            except KeyError as exc:
                return httpx.Response(
                    400, json={"error": f"'{exc.args[0]}' not found for path '{exc.args[0]}'"}
                )
        return httpx.Response(404, json={"error": f"no route {path}"})


def client(fake: FakeCube | None = None) -> CubeClient:
    return CubeClient("http://cube.test", SECRET, transport=httpx.MockTransport(fake or FakeCube()))


# ── the warehouse ───────────────────────────────────────────────────────────


def world(path: Path) -> Path:
    con = duckdb.connect(str(path))
    for schema in ("shop", "hr", "fin", "sales"):
        con.execute(f"CREATE SCHEMA {schema}")
    con.execute(
        "CREATE TABLE shop.orders (order_id INT, status VARCHAR, company VARCHAR, region VARCHAR,"
        " customer_id VARCHAR, customer_name VARCHAR, ordered_at TIMESTAMP, amount DOUBLE,"
        " refunded DOUBLE)"
    )
    rows = [
        (1, "Paid", "South AB", None, "C1", "Acme", "2026-08-03 10:00", 100.0, 0.0),
        (2, "Paid", "South AB", None, "C1", "Acme", "2026-08-04 10:00", 50.0, 0.0),
        (3, "Paid", "South AB", None, "C2", "Birch", "2026-08-05 10:00", 150.0, 0.0),
        (4, "Paid", "South AB", None, "C3", "Acme", "2026-08-06 10:00", 120.0, 0.0),
        (5, "Paid", "North Oy", "Uusimaa", None, None, "2026-08-07 10:00", 400.0, 0.0),
        (6, "Refunded", "South AB", None, "C4", "Cedar", "2026-08-08 10:00", 90.0, 90.0),
        (7, "Paid", "South AB", None, "C5", "Dune", "2026-07-08 10:00", 60.0, 0.0),
    ]
    con.executemany("INSERT INTO shop.orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute(
        "CREATE TABLE shop.refund_events (event_at TIMESTAMP, company VARCHAR, amount DOUBLE)"
    )
    con.execute(
        "CREATE TABLE hr.staff_monthly (month DATE, company VARCHAR, headcount INT, salary INT)"
    )
    con.executemany(
        "INSERT INTO hr.staff_monthly VALUES (?, ?, ?, ?)",
        [
            ("2026-04-01", "North Oy", 40, 0),
            ("2026-05-01", "North Oy", 42, 0),
            ("2026-06-01", "North Oy", 43, 0),
        ],
    )
    # One row per account per day it was active, 2026-08-01..29: on day d the
    # accounts are A1..A(1 + (d + 1) % 3), so the last day (29) holds one
    # account with 10 heads per company, while a month bucket unions three
    # accounts and adds 29 days of headcount.
    con.execute(
        "CREATE TABLE hr.staff_daily (day DATE, company VARCHAR, account_id VARCHAR, headcount INT)"
    )
    daily = [
        (f"2026-08-{d:02d}", company, f"A{i}", 10)
        for d in range(1, 30)
        for company in ("North Oy", "South AB")
        for i in range(1, 2 + (d + 1) % 3)
    ]
    con.executemany("INSERT INTO hr.staff_daily VALUES (?, ?, ?, ?)", daily)
    con.execute("CREATE TABLE fin.north_invoices (issued_at TIMESTAMP, amount DOUBLE)")
    con.execute("CREATE TABLE sales.quotes (quote_id INT, sent_at TIMESTAMP, decided_at TIMESTAMP)")
    # Contracts end on a schedule: three run out in October, after the clock.
    con.execute("CREATE TABLE sales.contracts (contract_id INT, end_date DATE)")
    con.executemany(
        "INSERT INTO sales.contracts VALUES (?, ?)",
        [(1, "2026-07-31"), (2, "2026-10-05"), (3, "2026-10-20"), (4, "2026-10-31")],
    )
    con.close()
    return path


def monitor_files() -> tuple[dict[str, Any], dict[str, Any]]:
    """``PROBE`` as the probe and the monitor write it to a project:
    ``profiles/cube.probe.json`` and ``profiles/cube.freshness.json``."""
    data_ends = {
        f"{cube}.{dim}": {"data_end": t.get("data_end"), "future_rows": bool(t.get("future_rows"))}
        for cube, rec in PROBE["cubes"].items()
        for dim, t in rec["time_dimensions"].items()
    }
    probe = {
        "clock_end": PROBE["clock_end"],
        "dictionaries": PROBE["dictionaries"],
        "data_ends": data_ends,
    }
    freshness = {
        "clock_end": PROBE["clock_end"],
        "stale_after_days": PROBE["stale_after_days"],
        "cubes": {
            cube: {"clock": rec["clock"], "streams": rec.get("streams", [])}
            for cube, rec in PROBE["cubes"].items()
        },
        "measures": PROBE["measures"],
    }
    return probe, freshness
