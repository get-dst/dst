"""Support lens: a ranking over a nullable key.

The world: support tickets, most of them from webshop shoppers who are no CRM
customer (``customer_id`` NULL), the rest from a handful of CRM customers.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tests.sandbox._cube import ask_cube
from tests.sandbox._world import World, ask, assert_reading_or_non_answer, describe, served_rows

LENS = "support"
TICKETS = "staging.stg_zendesk__tickets"

# customer (None = a webshop shopper), count of 2026 tickets
_TICKETS_2026 = [
    (None, 8),
    ("C008", 4),
    ("C344", 3),
    ("C365", 3),
    ("C359", 2),
    ("C025", 2),
    ("C317", 2),
]


@pytest.fixture()
def world(tmp_path: Path) -> World:
    w = World(tmp_path / "support.duckdb")
    rows = []
    n = 0
    for customer, count in _TICKETS_2026:
        for i in range(count):
            n += 1
            rows.append(
                (
                    n,
                    datetime(2026, 1 + (n % 8), 3 + i, 9, 0),
                    "webshop customer" if customer is None else "b2b contact",
                    customer,
                    1000 + n if customer is None else None,
                    None,
                    "Webshop order" if customer is None else "Scheduling",
                    "normal",
                    "s",
                    "d",
                    "closed",
                    "agent1",
                    None,
                    30,
                    datetime(2026, 1 + (n % 8), 4 + i, 9, 0),
                    None,
                )
            )
    for i in range(3):  # last year's tickets of the top customer: outside the window
        n += 1
        rows.append(
            (
                n,
                datetime(2025, 5, 1 + i, 9, 0),
                "b2b contact",
                "C008",
                None,
                None,
                "Scheduling",
                "normal",
                "s",
                "d",
                "closed",
                "agent1",
                None,
                30,
                datetime(2025, 5, 2 + i, 9, 0),
                None,
            )
        )
    w.table(
        TICKETS,
        {
            "ticket_id": "BIGINT",
            "created_at": "TIMESTAMP",
            "requester": "VARCHAR",
            "customer_id": "VARCHAR",
            "order_id": "BIGINT",
            "work_order_id": "VARCHAR",
            "category": "VARCHAR",
            "priority": "VARCHAR",
            "subject": "VARCHAR",
            "description": "VARCHAR",
            "status": "VARCHAR",
            "assignee": "VARCHAR",
            "resolution": "VARCHAR",
            "first_response_minutes": "BIGINT",
            "resolved_at": "TIMESTAMP",
            "satisfaction": "VARCHAR",
        },
        rows,
    )
    return w


def test_q25_top_customers_by_tickets_are_customers(world: World) -> None:
    """Asked "top 5 customers by number of support tickets opened this year" came
    back with NULL in first place: every webshop ticket with no CRM customer
    counted as one customer with more tickets than any real one, pushing the
    fifth customer off the list. The reading ranks CRM customers only, the
    ties at the fifth place named.

    Failure mode to carry: a ranking over a nullable dimension serving the
    NULL bucket as a member."""
    resp = ask(
        "top 5 customers by number of support tickets opened this year",
        LENS,
        ["tickets"],
        world,
        [
            ("shape", "ranking", 1.0),
            ("metric", "ticket_count", 0.92),
            ("dimension", "customer_id", None),
            ("filter", None, 0.83),
        ],
    )
    ranked = world.sql(
        f"SELECT customer_id, count(*) AS n FROM {TICKETS} WHERE customer_id IS NOT NULL "
        "AND created_at >= '2026-01-01' GROUP BY 1 ORDER BY n DESC, customer_id"
    )
    top4 = [[str(c), float(n)] for c, n in ranked[:4]]
    fifth = [[str(c), float(n)] for c, n in ranked if n == ranked[4][1]]
    readings = [[*top4, candidate] for candidate in fifth]
    served = None
    if resp.status == "ok":
        rows = served_rows(resp)
        assert all(row[0] is not None for row in rows), (
            f"NULL served as a customer — {describe(resp)}"
        )
        served = [[row[0], row[1]] for row in rows[:5]]
    assert_reading_or_non_answer(resp, served, readings, what="top 5 customers by tickets")


# ── the same question through the cube lane ──────────────────────────────────


def test_q25_on_the_cube_lane(world: World) -> None:
    """The frontier lane ranks by the customer key; the empty bucket is reported
    apart, never ranked, and the tie at the fifth place is extended, not cut."""
    resp = ask_cube(
        "q25",
        "top 5 customers by number of support tickets opened this year",
        [
            {
                "measures": ["tickets.ticket_count"],
                "dimensions": ["tickets.customer_id"],
                "timeDimensions": [
                    {"dimension": "tickets.created_at", "dateRange": ["2026-01-01", "2026-08-31"]}
                ],
                "order": {"tickets.ticket_count": "desc", "tickets.customer_id": "asc"},
                "limit": 5,
            }
        ],
        [
            ("top 5", "value", "limit: 5"),
            ("customers", "member", "tickets.customer_id"),
            ("number of support tickets", "member", "tickets.ticket_count"),
            ("opened", "member", "tickets.created_at"),
            ("this year", "period", "2026-01-01 to 2026-08-31"),
        ],
        world,
        lens=LENS,
    )
    ranked = world.sql(
        f"SELECT customer_id, count(*) AS n FROM {TICKETS} WHERE customer_id IS NOT NULL "
        "AND created_at >= '2026-01-01' GROUP BY 1 ORDER BY n DESC, customer_id"
    )
    top4 = [[str(c), float(n)] for c, n in ranked[:4]]
    fifth = [[str(c), float(n)] for c, n in ranked if n == ranked[4][1]]
    readings = [[*top4, candidate] for candidate in fifth]
    served = None
    if resp.status == "ok":
        rows = served_rows(resp)
        assert all(row[0] is not None for row in rows), f"NULL served — {describe(resp)}"
        served = [[row[0], row[1]] for row in rows[:5]]
    assert_reading_or_non_answer(resp, served, readings, what="top 5 customers by tickets")
