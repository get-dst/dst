"""Webshop lens: four wrong-served answers over a correct layer.

The world: paid webshop orders with a UTC order time and a Finnish-time twin,
two channels (consumers by card, installers by invoice), refunds as their own
rows, order lines and a product catalogue. Orders at the edges of a day and a
month sit on both sides of the UTC/Helsinki boundary on purpose.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.sandbox._cube import ask_cube, column
from tests.sandbox._world import (
    World,
    ask,
    assert_reading_or_non_answer,
    describe,
    norm,
    served_rows,
)

LENS = "webshop"
ENTITIES = ["webshop_orders", "webshop_order_lines", "webshop_products", "webshop_refunds"]
VAT = 1.255
HELSINKI = timedelta(hours=3)  # every order here falls in summer time

ORDERS = "intermediate.int_orders_enriched"
REFUNDS = "staging.stg_shopify__refunds"
LINES = "raw_shopify.order_line"
PRODUCTS = "raw_shopify.product"

# order_id, UTC order time, channel, gross incl. VAT, refunds later issued, product
_ORDERS = [
    (101, "2026-01-12 10:00", "b2c_card", 1255.0, 0.0, 1),
    (102, "2026-06-02 21:30", "b2c_card", 2510.0, 0.0, 1),  # 3 June in Finland, 2 June in UTC
    (103, "2026-06-15 10:00", "b2b_invoice", 3765.0, 500.0, 2),
    (104, "2026-07-31 21:30", "b2c_card", 1255.0, 0.0, 3),  # 1 August in Finland, July in UTC
    (105, "2026-08-10 09:00", "b2c_card", 2510.0, 251.0, 1),
    (106, "2026-08-12 09:00", "b2b_invoice", 6275.0, 0.0, 2),
    (107, "2026-08-20 09:00", "b2c_card", 1255.0, 0.0, 3),
    (108, "2026-08-31 21:30", "b2b_invoice", 1255.0, 0.0, 1),  # 1 September in Finland
    (109, "2026-07-05 09:00", "b2b_invoice", 2510.0, 502.0, 3),
    (110, "2026-05-20 09:00", "b2c_card", 1255.0, 0.0, 2),
    (111, "2026-04-10 09:00", "b2c_card", 1255.0, 0.0, 1),
    (112, "2026-03-10 09:00", "b2c_card", 1255.0, 0.0, 3),
]
# refund_id, order_id, UTC refund time, amount incl. VAT, reason
_REFUNDS = [
    (1, 105, "2026-08-20 10:00", 251.0, "returned"),
    (2, 109, "2026-08-03 10:00", 502.0, "damaged"),
    (3, 103, "2026-06-20 10:00", 500.0, "wrong item"),
]
_PRODUCTS = [
    (1, "VV-4030", "Pro mixing valve", "Valves", 400.0, 1255.0),
    (2, "VV-4070", "Eco panel radiator 600x1000", "Radiators", 1200.0, 3765.0),
    (3, "VV-4042", "Compact booster pump", "Circulation pumps", 500.0, 1255.0),
]


@pytest.fixture()
def world(tmp_path: Path) -> World:
    w = World(tmp_path / "webshop.duckdb")
    rows = []
    for oid, utc, channel, gross, refunded, _product in _ORDERS:
        at = datetime.fromisoformat(utc)
        local = at + HELSINKI
        rows.append(
            (
                oid,
                at,
                local,
                at.strftime("%Y-%m"),
                local.strftime("%Y-%m"),
                channel,
                f"h{oid}",
                "paid",
                gross,
                refunded,
                round(gross * 0.014, 2) if channel == "b2c_card" else 0.0,
                gross - refunded,
                round(gross / VAT, 2),
            )
        )
    w.table(
        ORDERS,
        {
            "order_id": "BIGINT",
            "created_at_utc": "TIMESTAMP",
            "created_at_local": "TIMESTAMP",
            "order_month_utc": "VARCHAR",
            "order_month_local": "VARCHAR",
            "channel": "VARCHAR",
            "customer_email_hash": "VARCHAR",
            "financial_status": "VARCHAR",
            "gross_eur": "DOUBLE",
            "refunds_eur": "DOUBLE",
            "fees_eur": "DOUBLE",
            "net_of_refunds_eur": "DOUBLE",
            "net_of_vat_eur": "DOUBLE",
        },
        rows,
    )
    w.table(
        REFUNDS,
        {
            "refund_id": "BIGINT",
            "order_id": "BIGINT",
            "created_at": "TIMESTAMP",
            "amount_eur": "DOUBLE",
            "reason": "VARCHAR",
        },
        [
            (rid, oid, datetime.fromisoformat(at), amount, why)
            for rid, oid, at, amount, why in _REFUNDS
        ],
    )
    synced = datetime(2026, 8, 31, 22, 0)
    lines = [
        (
            oid,
            1,
            product,
            _PRODUCTS[product - 1][1],
            1,
            gross,
            gross,
            _PRODUCTS[product - 1][4],
            synced,
            False,
        )
        for oid, _utc, _channel, gross, _refunded, product in _ORDERS
    ]
    # One line re-synced: a superseded copy the money metrics must not double.
    lines.append(
        (106, 1, 2, "VV-4070", 1, 6275.0, 6275.0, 1200.0, synced - timedelta(days=3), False)
    )
    w.table(
        LINES,
        {
            "order_id": "BIGINT",
            "line_number": "BIGINT",
            "product_id": "BIGINT",
            "sku": "VARCHAR",
            "quantity": "BIGINT",
            "unit_price_eur": "DOUBLE",
            "line_total_eur": "DOUBLE",
            "unit_cost_eur": "DOUBLE",
            "_fivetran_synced": "TIMESTAMP",
            "_fivetran_deleted": "BOOLEAN",
        },
        lines,
    )
    w.table(
        PRODUCTS,
        {
            "product_id": "BIGINT",
            "sku": "VARCHAR",
            "title": "VARCHAR",
            "category": "VARCHAR",
            "unit_cost_eur": "DOUBLE",
            "price_eur": "DOUBLE",
            "active": "BOOLEAN",
            "_fivetran_synced": "TIMESTAMP",
            "_fivetran_deleted": "BOOLEAN",
        },
        [
            (pid, sku, title, cat, cost, price, True, synced, False)
            for pid, sku, title, cat, cost, price in _PRODUCTS
        ],
    )
    return w


LOCAL_DAY = "CAST(created_at_local AS DATE)"


def _revenue(world: World, start: str, end: str, *, cohort: bool = False) -> float:
    """Webshop revenue over Finnish-time days [start, end]: paid orders' gross
    minus refunds dated in the window (or, cohort basis, the refunds on those
    orders), net of 25.5 % VAT."""
    gross = world.scalar(
        f"SELECT COALESCE(SUM(gross_eur), 0) FROM {ORDERS} WHERE {LOCAL_DAY} BETWEEN '{start}' "
        f"AND '{end}'"
    )
    if cohort:
        refunds = world.scalar(
            f"SELECT COALESCE(SUM(refunds_eur), 0) FROM {ORDERS} WHERE {LOCAL_DAY} BETWEEN "
            f"'{start}' AND '{end}'"
        )
    else:
        refunds = world.scalar(
            f"SELECT COALESCE(SUM(amount_eur), 0) FROM {REFUNDS} "
            f"WHERE CAST(created_at + INTERVAL 3 HOUR AS DATE) BETWEEN '{start}' AND '{end}'"
        )
    return round((gross - refunds) / VAT, 2)


def test_q41_webshop_revenue_last_month_is_not_the_card_channel_alone(world: World) -> None:
    """Asked "what was webshop revenue last month" came back as August revenue for
    consumers paying by card only: the resolver added ``channel = 'b2c_card'``
    (p 0.79 on the column, 0.61 on the value) to a question that names no
    channel, and served it as ok. The readings are August in Finnish time, net
    of VAT, refunds netted by refund date or on the orders themselves.

    Failure mode to carry: an unasked filter — a filter value absent from the
    question and pinned by neither the lens nor the entity's population."""
    resp = ask(
        "what was webshop revenue last month",
        LENS,
        ENTITIES,
        world,
        [
            ("shape", "aggregate", 1.0),
            ("metric", "webshop_revenue_eur", 1.0),
            ("dimension", None, 0.78),
            ("filter", "channel", 0.79),
            ("filter_value", "b2c_card", 0.61),
        ],
    )
    readings = [
        _revenue(world, "2026-08-01", "2026-08-31"),
        _revenue(world, "2026-08-01", "2026-08-31", cohort=True),
    ]
    served = served_rows(resp)[0][0] if resp.status == "ok" else None
    assert_reading_or_non_answer(resp, served, readings, what="August webshop revenue")


def test_q15_sales_and_refunds_for_the_last_90_days_serves_both_or_neither(world: World) -> None:
    """Asked "webshop sales and refunds for the last 90 days" came back as one
    figure, ``webshop_revenue_eur`` picked at p 0.38 with the refunds half of
    the question dropped and nothing said about it. A defensible answer carries
    sales net of VAT and refunds net of VAT over the last 90 Finnish-time days
    (complete days to 31 August, or the rolling window to today).

    Failure mode to carry: a multi-measure ask served as one measure, the other
    silently dropped."""
    resp = ask(
        "webshop sales and refunds for the last 90 days",
        LENS,
        ENTITIES,
        world,
        [
            ("shape", "aggregate", 0.87),
            ("metric", "webshop_revenue_eur", 0.38),
            ("dimension", None, 0.77),
            ("filter", None, 0.95),
        ],
    )

    def refunds(start: str, end: str) -> float:
        return round(
            world.scalar(
                f"SELECT COALESCE(SUM(amount_eur), 0) FROM {REFUNDS} "
                f"WHERE CAST(created_at + INTERVAL 3 HOUR AS DATE) BETWEEN '{start}' AND '{end}'"
            )
            / VAT,
            2,
        )

    def sales(start: str, end: str) -> float:
        return round(
            world.scalar(
                f"SELECT COALESCE(SUM(gross_eur), 0) FROM {ORDERS} WHERE {LOCAL_DAY} BETWEEN "
                f"'{start}' AND '{end}'"
            )
            / VAT,
            2,
        )

    readings = [
        {sales("2026-06-03", "2026-08-31"), refunds("2026-06-03", "2026-08-31")},
        {sales("2026-06-04", "2026-09-01"), refunds("2026-06-04", "2026-09-01")},
    ]
    if resp.status == "ok":
        (row,) = served_rows(resp)
        served = set(row)
        assert len(row) >= 2, f"sales AND refunds asked, one figure served — {describe(resp)}"
    else:
        served = None
    assert_reading_or_non_answer(resp, served, readings, what="sales and refunds, last 90 days")


def _product_revenue(world: World, *, net_of_refunds: bool) -> list[tuple[str, float]]:
    """Year to date (Finnish time) by product title, the latest copy of each
    line, net of VAT; optionally with each order's refunds allocated to its
    lines pro rata."""
    allocation = "- l.line_total_eur * o.refunds_eur / o.gross_eur" if net_of_refunds else ""
    return [
        (str(title), round(float(net), 2))
        for title, net in world.sql(
            f"""
            WITH l AS (
              SELECT * FROM {LINES}
              QUALIFY row_number() OVER (PARTITION BY order_id, line_number ORDER BY
                  _fivetran_synced DESC) = 1
            )
            SELECT p.title, SUM((l.line_total_eur {allocation}) / {VAT}) AS net
            FROM l JOIN {ORDERS} o USING (order_id) JOIN {PRODUCTS} p USING (product_id)
            WHERE NOT l._fivetran_deleted AND CAST(o.created_at_local AS DATE) BETWEEN
                '2026-01-01' AND '2026-08-31'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10
            """
        )
    ]


def test_q46_top_products_by_revenue_are_products_not_order_ids(world: World) -> None:
    """Asked "top 10 webshop products by revenue year to date" came back as the ten
    largest ORDERS (order_id, order revenue), all time: the ranking's subject
    fell to the order table's key, and "year to date" left no window. The
    readings rank product titles by line revenue net of VAT, year to date in
    Finnish time, before or after refunds.

    Failure mode to carry: a ranking subject the question names (products) that
    the chosen entity cannot group by, served by its key column instead."""
    resp = ask(
        "top 10 webshop products by revenue year to date",
        LENS,
        ENTITIES,
        world,
        [
            ("shape", "ranking", 1.0),
            ("metric", "webshop_revenue_eur", 0.95),
            ("dimension", "order_id", None),
            ("filter", None, 0.39),
        ],
    )
    readings = [
        [[t, v] for t, v in _product_revenue(world, net_of_refunds=False)],
        [[t, v] for t, v in _product_revenue(world, net_of_refunds=True)],
    ]
    served = None
    if resp.status == "ok":
        assert resp.data is not None
        subject = resp.data.columns[0]
        assert subject in ("title", "sku", "product_id"), (
            f"ranked by {subject!r}, not by a product — {describe(resp)}"
        )
        served = [[row[0], row[1]] for row in served_rows(resp)]
    assert_reading_or_non_answer(resp, served, readings, what="top products year to date")


def test_q47_highest_refund_rate_category_is_a_category(world: World) -> None:
    """Asked "which webshop category has the highest refund rate this year" came
    back as thousands of per-order rows with refund_rate 1.0 — a ratio ranked
    over the order key, where every refunded order is its own 100 %. The
    readings name a category: refunded orders over orders containing it, or
    refund value over line value, year to date in Finnish time.

    Failure mode to carry: a ratio ranked over a key-grained dimension, the
    asked category never reached."""
    resp = ask(
        "which webshop category has the highest refund rate this year",
        LENS,
        ENTITIES,
        world,
        [
            ("shape", "ranking", 0.98),
            ("metric", "refund_rate", 1.0),
            ("dimension", "order_id", None),
            ("filter", None, 0.88),
        ],
    )
    by_orders = world.sql(
        f"""
        WITH l AS (
          SELECT * FROM {LINES}
          QUALIFY row_number() OVER (PARTITION BY order_id, line_number ORDER BY
              _fivetran_synced DESC) = 1
        ), r AS (SELECT DISTINCT order_id FROM {REFUNDS})
        SELECT p.category,
               100.0 * COUNT(DISTINCT r.order_id) / COUNT(DISTINCT o.order_id) AS rate
        FROM {ORDERS} o JOIN l USING (order_id) JOIN {PRODUCTS} p USING (product_id)
        LEFT JOIN r USING (order_id)
        WHERE CAST(o.created_at_local AS DATE) BETWEEN '2026-01-01' AND '2026-08-31'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 1
        """
    )
    by_value = world.sql(
        f"""
        WITH l AS (
          SELECT * FROM {LINES}
          QUALIFY row_number() OVER (PARTITION BY order_id, line_number ORDER BY
              _fivetran_synced DESC) = 1
        )
        SELECT p.category,
               100.0 * SUM(l.line_total_eur * o.refunds_eur / o.gross_eur) /
                   SUM(l.line_total_eur) AS rate
        FROM {ORDERS} o JOIN l USING (order_id) JOIN {PRODUCTS} p USING (product_id)
        WHERE CAST(o.created_at_local AS DATE) BETWEEN '2026-01-01' AND '2026-08-31'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 1
        """
    )
    readings = [[str(c), round(float(r), 2)] for c, r in [*by_orders, *by_value]]
    served = None
    if resp.status == "ok":
        assert resp.data is not None
        assert resp.data.columns[0] == "category", (
            f"ranked by {resp.data.columns[0]!r}, not by category — {describe(resp)}"
        )
        top = served_rows(resp)[0]
        served = [top[0], norm(float(top[1]) * 100)]
    assert_reading_or_non_answer(resp, served, readings, what="highest refund-rate category")


# ── the same questions through the cube lane ─────────────────────────────────

AUG_LOCAL = ["2026-08-01", "2026-08-31"]


def test_q41_on_the_cube_lane(world: World) -> None:
    """The cube lane reads webshop revenue on the declared default refund
    basis, checks the other reading is within 1 %, and binds no channel the
    question did not name."""
    resp = ask_cube(
        "q41",
        "what was webshop revenue last month",
        [
            {
                "measures": ["webshop_revenue_events.webshop_revenue_refund_date_eur"],
                "timeDimensions": [
                    {"dimension": "webshop_revenue_events.event_at", "dateRange": AUG_LOCAL}
                ],
            }
        ],
        [
            ("webshop revenue", "member", "webshop_revenue_events.webshop_revenue_refund_date_eur"),
            ("last month", "period", "2026-08-01..2026-08-31"),
        ],
        world,
        lens=LENS,
    )
    readings = [
        _revenue(world, "2026-08-01", "2026-08-31"),
        _revenue(world, "2026-08-01", "2026-08-31", cohort=True),
    ]
    served = norm(served_rows(resp)[0][0]) if resp.status == "ok" else None
    assert_reading_or_non_answer(resp, served, readings, what="August webshop revenue")


def test_q15_on_the_cube_lane(world: World) -> None:
    """The frontier lane serves both measures; the refunds half is the
    refund amount on the window's orders, gross of VAT, a member the refund
    basis term does not cover."""
    window = {
        "dimension": "webshop_orders.created_at_local",
        "dateRange": ["2026-06-03", "2026-08-31"],
    }
    resp = ask_cube(
        "q15",
        "webshop sales and refunds for the last 90 days",
        [
            {
                "measures": [
                    "webshop_orders.webshop_revenue_eur",
                    "webshop_orders.webshop_refunds_eur",
                ],
                "timeDimensions": [window],
            }
        ],
        [
            ("webshop sales", "member", "webshop_orders.webshop_revenue_eur"),
            ("refunds", "member", "webshop_orders.webshop_refunds_eur"),
            ("last 90 days", "period", "2026-06-03..2026-08-31"),
        ],
        world,
        lens=LENS,
    )

    def refunds(start: str, end: str) -> float:
        return round(
            world.scalar(
                f"SELECT COALESCE(SUM(amount_eur), 0) FROM {REFUNDS} "
                f"WHERE CAST(created_at + INTERVAL 3 HOUR AS DATE) BETWEEN '{start}' AND '{end}'"
            )
            / VAT,
            2,
        )

    def sales(start: str, end: str) -> float:
        return round(
            world.scalar(
                f"SELECT COALESCE(SUM(gross_eur), 0) FROM {ORDERS} WHERE {LOCAL_DAY} BETWEEN "
                f"'{start}' AND '{end}'"
            )
            / VAT,
            2,
        )

    readings = [
        {sales("2026-06-03", "2026-08-31"), refunds("2026-06-03", "2026-08-31")},
        {sales("2026-06-04", "2026-09-01"), refunds("2026-06-04", "2026-09-01")},
    ]
    served = set(served_rows(resp)[0]) if resp.status == "ok" else None
    assert_reading_or_non_answer(resp, served, readings, what="sales and refunds, last 90 days")


YTD_LOCAL = {
    "dimension": "webshop_orders.created_at_local",
    "dateRange": ["2026-01-01", "2026-08-31"],
}


def test_q46_on_the_cube_lane(world: World) -> None:
    """Products ranked by line revenue through the order-line cube, grouped by
    the product key under the title, year to date in Finnish time."""
    resp = ask_cube(
        "q46",
        "top 10 webshop products by revenue year to date",
        [
            {
                "measures": ["webshop_order_lines.line_revenue_ex_vat_eur"],
                "dimensions": ["webshop_products.title"],
                "timeDimensions": [YTD_LOCAL],
                "order": {"webshop_order_lines.line_revenue_ex_vat_eur": "desc"},
                "limit": 10,
            }
        ],
        [
            ("products", "member", "webshop_products.title"),
            ("revenue", "member", "webshop_order_lines.line_revenue_ex_vat_eur"),
            ("year to date", "period", "2026-01-01..2026-08-31"),
        ],
        world,
        lens=LENS,
    )
    readings = [
        [[t, v] for t, v in _product_revenue(world, net_of_refunds=False)],
        [[t, v] for t, v in _product_revenue(world, net_of_refunds=True)],
    ]
    served = None
    if resp.status == "ok":
        titles = column(resp, "webshop_products.title")
        values = column(resp, "webshop_order_lines.line_revenue_ex_vat_eur")
        served = [[t, norm(v)] for t, v in zip(titles, values, strict=True)]
    assert_reading_or_non_answer(resp, served, readings, what="top products year to date")


def test_q47_on_the_cube_lane(world: World) -> None:
    """The refund rate ranked by category through the order-line cube."""
    resp = ask_cube(
        "q47",
        "which webshop category has the highest refund rate this year",
        [
            {
                "measures": ["webshop_order_lines.line_refund_rate"],
                "dimensions": ["webshop_products.category"],
                "timeDimensions": [YTD_LOCAL],
                "order": {"webshop_order_lines.line_refund_rate": "desc"},
                "limit": 1,
            }
        ],
        [
            ("category", "member", "webshop_products.category"),
            ("refund rate", "member", "webshop_order_lines.line_refund_rate"),
            ("this year", "period", "2026-01-01..2026-08-31"),
        ],
        world,
        lens=LENS,
    )
    by_value = world.sql(
        f"""
        WITH l AS (
          SELECT * FROM {LINES}
          QUALIFY row_number() OVER (PARTITION BY order_id, line_number ORDER BY
              _fivetran_synced DESC) = 1
        )
        SELECT p.category,
               100.0 * SUM(l.line_total_eur * o.refunds_eur / o.gross_eur) /
                   SUM(l.line_total_eur) AS rate
        FROM {ORDERS} o JOIN l USING (order_id) JOIN {PRODUCTS} p USING (product_id)
        WHERE CAST(o.created_at_local AS DATE) BETWEEN '2026-01-01' AND '2026-08-31'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 1
        """
    )
    by_orders = world.sql(
        f"""
        WITH l AS (
          SELECT * FROM {LINES}
          QUALIFY row_number() OVER (PARTITION BY order_id, line_number ORDER BY
              _fivetran_synced DESC) = 1
        ), r AS (SELECT DISTINCT order_id FROM {REFUNDS})
        SELECT p.category,
               100.0 * COUNT(DISTINCT r.order_id) / COUNT(DISTINCT o.order_id) AS rate
        FROM {ORDERS} o JOIN l USING (order_id) JOIN {PRODUCTS} p USING (product_id)
        LEFT JOIN r USING (order_id)
        WHERE CAST(o.created_at_local AS DATE) BETWEEN '2026-01-01' AND '2026-08-31'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 1
        """
    )
    readings = [[str(c), round(float(r), 2)] for c, r in [*by_orders, *by_value]]
    served = None
    if resp.status == "ok":
        top = [
            column(resp, "webshop_products.category")[0],
            column(resp, "webshop_order_lines.line_refund_rate")[0],
        ]
        served = [top[0], norm(float(top[1]) * 100)]
    assert_reading_or_non_answer(resp, served, readings, what="highest refund-rate category")
