"""Group finance lens: a month stored as 'YYYY-MM' text, and what the window
compiled against it did to every question that touched it.

The world: revenue per entity, stream and month (the group's revenue carrier)
with the Finnish contracting feed stopping in June 2026; the board budget vs
actuals by entity and month; the Finnish invoices and the Swedish subsidiary's
invoices in SEK that an invoiced-revenue reading is built from.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.sandbox._cube import ask_cube
from tests.sandbox._world import (
    World,
    ask,
    assert_reading_or_non_answer,
    describe,
    served_rows,
)

LENS = "group_finance"
REVENUE = "intermediate.int_revenue_by_entity_month"
BUDGET = "marts_finance.mart_budget_vs_actual_monthly"
INVOICES = "marts.fct_invoices"
FAKTUROR = "raw_fortnox_bergstrom.fakturor"
LVI, SHOP, SE = "Nordlund LVI Oy", "Nordlund Verkkokauppa", "Bergström Rör AB"
SEK_PER_EUR = 10.8

# month, entity, stream, revenue
_REVENUE = [
    *[
        ("2025-0" + m, SHOP, "Webshop", v)
        for m, v in (("7", 900_000.0), ("8", 1_000_000.0), ("9", 1_100_000.0))
    ],
    *[
        ("2026-0" + m, SHOP, "Webshop", v)
        for m, v in (("6", 2_400_000.0), ("7", 2_500_000.0), ("8", 2_600_000.0))
    ],
    *[
        ("2025-0" + m, LVI, "Service contracts", v)
        for m, v in (("7", 130_000.0), ("8", 140_000.0), ("9", 150_000.0))
    ],
    *[
        ("2026-0" + m, LVI, "Service contracts", v)
        for m, v in (("6", 260_000.0), ("7", 270_000.0), ("8", 280_000.0))
    ],
    *[
        ("2025-0" + m, LVI, "Contracting", v)
        for m, v in (("7", 2_000_000.0), ("8", 2_100_000.0), ("9", 2_200_000.0))
    ],
    ("2026-06", LVI, "Contracting", 300_000.0),  # the feed stops on 5 June; no July or August rows
    ("2026-07", LVI, "Intercompany", 30_000.0),
    ("2026-08", LVI, "Intercompany", 30_000.0),
    *[
        ("2026-0" + m, SE, "Contracting", v)
        for m, v in (("6", 150_000.0), ("7", 160_000.0), ("8", 46_877.0))
    ],
]
_BUDGET_ACTUAL = {LVI: 2_500_000.0, SHOP: 2_000_000.0, SE: 150_000.0}
_BUDGET = {LVI: 2_900_000.0, SHOP: 500_000.0, SE: 700_000.0}


@pytest.fixture()
def world(tmp_path: Path) -> World:
    w = World(tmp_path / "finance.duckdb")
    w.table(
        REVENUE,
        {
            "month": "VARCHAR",
            "entity": "VARCHAR",
            "revenue_stream": "VARCHAR",
            "revenue_eur": "DOUBLE",
            "fx_method": "VARCHAR",
        },
        [(m, e, s, v, "average") for m, e, s, v in _REVENUE],
    )
    budget_rows = []
    for month in range(1, 9):
        for entity in (LVI, SHOP, SE):
            actual = _BUDGET_ACTUAL[entity]
            if entity == LVI and month >= 6:
                actual = {6: 300_000.0, 7: 270_000.0, 8: 280_000.0}[
                    month
                ]  # service contracts only once the feed stops
            if entity == SE and month == 8:
                actual = 46_877.0
            budget = _BUDGET[entity]
            budget_rows.append(
                (
                    f"2026-{month:02d}",
                    entity,
                    "Revenue",
                    budget,
                    actual,
                    actual - budget,
                    actual / budget,
                )
            )
    for month in range(9, 13):
        for entity in (LVI, SHOP, SE):
            budget_rows.append(
                (f"2026-{month:02d}", entity, "Revenue", _BUDGET[entity], None, None, None)
            )
    w.table(
        BUDGET,
        {
            "month": "VARCHAR",
            "entity": "VARCHAR",
            "line_item": "VARCHAR",
            "budget_eur": "DOUBLE",
            "actual_eur": "DOUBLE",
            "variance_eur": "DOUBLE",
            "attainment_pct": "DOUBLE",
        },
        budget_rows,
    )
    invoices = []
    n = 0
    for month in range(1, 9):
        if month <= 5:
            n += 1
            invoices.append(
                (
                    f"INV{n:04d}",
                    "C001",
                    date(2026, month, 15),
                    date(2026, month, 28),
                    2_200_000.0,
                    2_761_000.0,
                    0.0,
                    "Contracting",
                    "Heating system",
                )
            )
        if month == 6:
            n += 1
            invoices.append(
                (
                    f"INV{n:04d}",
                    "C001",
                    date(2026, 6, 5),
                    date(2026, 6, 19),
                    300_000.0,
                    376_500.0,
                    0.0,
                    "Contracting",
                    "Heating system",
                )
            )
        n += 1
        invoices.append(
            (
                f"INV{n:04d}",
                "C002",
                date(2026, month, 1),
                date(2026, month, 15),
                250_000.0,
                313_750.0,
                0.0,
                "Service contract",
                "Maintenance contract",
            )
        )
    w.table(
        INVOICES,
        {
            "invoice_id": "VARCHAR",
            "customer_id": "VARCHAR",
            "issue_date": "DATE",
            "due_date": "DATE",
            "net_amount_eur": "DOUBLE",
            "gross_amount_eur": "DOUBLE",
            "outstanding_eur": "DOUBLE",
            "revenue_stream": "VARCHAR",
            "service": "VARCHAR",
        },
        invoices,
    )
    w.table(
        FAKTUROR,
        {
            "fakturanummer": "BIGINT",
            "kundnummer": "BIGINT",
            "fakturadatum": "DATE",
            "forfallodatum": "DATE",
            "belopp_inkl_moms": "DOUBLE",
            "belopp_exkl_moms": "DOUBLE",
            "moms": "DOUBLE",
            "valuta": "VARCHAR",
            "status": "VARCHAR",
            "projekt": "VARCHAR",
            "rakenskapsar": "VARCHAR",
        },
        [
            (
                1000 + month,
                7,
                date(2026, month, 10),
                date(2026, month, 24),
                2_025_000.0,
                1_620_000.0,
                405_000.0,
                "SEK",
                "Betald",
                None,
                "2026",
            )
            for month in range(1, 9)
        ],
    )
    return w


def _revenue(world: World, entity: str, months: list[str], stream: str | None = None) -> float:
    streams = (
        f"AND revenue_stream = '{stream}'" if stream else "AND revenue_stream <> 'Intercompany'"
    )
    quoted = ", ".join(f"'{m}'" for m in months)
    return round(
        float(
            world.scalar(
                f"SELECT COALESCE(SUM(revenue_eur), 0) FROM {REVENUE} WHERE entity = '{entity}' "
                f"AND month IN ({quoted}) {streams}"
            )
        ),
        2,
    )


def test_q20_revenue_this_quarter_vs_last_year_by_business_covers_the_whole_quarter(
    world: World,
) -> None:
    """Asked "revenue this quarter vs same quarter last year by business" came back
    as August alone against August AND September 2025: ``month`` holds
    'YYYY-MM' text and the window compiled ``month >= '2026-07-01' AND month <=
    '2026-09-01'``, which '2026-07' fails and '2025-09' passes. The Finnish
    entity's figure was then served as a 94 % collapse, when its contracting
    feed simply stops in June. The readings: July–August 2026 against
    July–August 2025 (or the full third quarter of 2025) per entity, the
    Finnish entity by its service-contract stream with the contracting gap
    named, never summed as if the missing months were zero.

    Failure mode to carry: a month-period text column compared against day
    literals — months silently dropped and added; a feed gap served as a drop."""
    resp = ask(
        "revenue this quarter vs same quarter last year by business",
        LENS,
        ["group_revenue_monthly"],
        world,
        [
            ("shape", "aggregate", 0.83),
            ("metric", "revenue_eur", 0.81),
            ("dimension", "entity", None),
            ("filter", None, 0.84),
        ],
    )
    q3_2026 = ["2026-07", "2026-08"]
    readings_shop = [
        [_revenue(world, SHOP, q3_2026), _revenue(world, SHOP, ["2025-07", "2025-08"])],
        [_revenue(world, SHOP, q3_2026), _revenue(world, SHOP, ["2025-07", "2025-08", "2025-09"])],
    ]
    readings_lvi = [
        [
            _revenue(world, LVI, q3_2026, "Service contracts"),
            _revenue(world, LVI, ["2025-07", "2025-08"], "Service contracts"),
        ],
        [
            _revenue(world, LVI, q3_2026, "Service contracts"),
            _revenue(world, LVI, ["2025-07", "2025-08", "2025-09"], "Service contracts"),
        ],
    ]
    served = (
        {str(row[0]): [row[1], row[2]] for row in served_rows(resp)} if resp.status == "ok" else {}
    )
    assert_reading_or_non_answer(
        resp, served.get(SHOP), readings_shop, what="webshop entity, Q3 vs last year"
    )
    assert_reading_or_non_answer(
        resp, served.get(LVI), readings_lvi, what="Finnish entity, Q3 vs last year"
    )


def test_q11_revenue_vs_budget_for_august_is_not_an_empty_answer(world: World) -> None:
    """Asked "revenue by business for August 2026 vs budget" came back as ZERO rows,
    served as ok: ``month >= '2026-08-01' AND month <= '2026-08-31'`` matches no
    'YYYY-MM' value at all. An empty answer to a question whose rows exist is
    a vacuous pass. The reading is one row per entity for August with its
    attainment of the board budget.

    Failure mode to carry: a month-period text column compared against day
    literals — an empty result served as the answer."""
    resp = ask(
        "revenue by business for August 2026 vs budget",
        LENS,
        ["budget_vs_actual"],
        world,
        [
            ("shape", "aggregate", 0.75),
            ("metric", "attainment_vs_budget", 0.44),
            ("dimension", "entity", None),
            ("metric", None, 0.87),
            ("filter", None, 0.82),
            ("filter", "line_item", 0.94),
        ],
    )
    reading = {
        str(entity): round(float(attainment), 4)
        for entity, attainment in world.sql(
            f"SELECT entity, SUM(actual_eur) / SUM(budget_eur) FROM {BUDGET} WHERE month = "
            f"'2026-08' GROUP BY 1"
        )
    }
    served = None
    if resp.status == "ok":
        rows = served_rows(resp)
        assert rows, f"no rows served for a month that has them — {describe(resp)}"
        served = {str(row[0]): round(float(row[1]), 4) for row in rows}
    assert_reading_or_non_answer(
        resp, served, [reading], what="attainment vs budget by entity, August"
    )


def test_q43_invoiced_revenue_jan_to_aug_is_invoiced_and_includes_january(world: World) -> None:
    """Asked "what was total invoiced revenue between jan 1 and aug 31 2026" came
    back as the budget-basis actuals of February to August: the metric went to
    the budget table (p 0.79, after the invoiced metrics at 0.39), and the
    window ``month >= '2026-01-01'`` dropped '2026-01'. The reading is
    invoice-recognised revenue net of VAT — the Finnish invoices plus the
    Swedish invoices at the planning rate — with the contracting gap named.

    Failure mode to carry: a month-period text column compared against day
    literals — the first month dropped; a stated basis ("invoiced") served
    from another."""
    resp = ask(
        "what was total invoiced revenue between jan 1 and aug 31 2026",
        LENS,
        ["budget_vs_actual", "fi_invoices", "bergstrom_invoices"],
        world,
        [
            ("shape", "aggregate", 1.0),
            ("metric", "invoiced_gross_eur", 0.39),
            ("metric", "actual_vs_budget_eur", 0.79),
            ("dimension", None, 0.97),
            ("filter", None, 0.97),
            ("filter", "line_item", 0.79),
        ],
    )
    finnish = float(
        world.scalar(
            f"SELECT SUM(net_amount_eur) FROM {INVOICES} WHERE issue_date BETWEEN '2026-01-01' "
            f"AND '2026-08-31'"
        )
    )
    swedish = (
        float(
            world.scalar(
                f"SELECT SUM(belopp_exkl_moms) FROM {FAKTUROR} WHERE fakturadatum BETWEEN "
                f"'2026-01-01' AND '2026-08-31'"
            )
        )
        / SEK_PER_EUR
    )
    readings = [round(finnish + swedish, 2), round(finnish, 2)]
    served = served_rows(resp)[0][0] if resp.status == "ok" else None
    assert_reading_or_non_answer(resp, served, readings, what="invoiced revenue Jan-Aug 2026")


def test_rule_a_month_period_text_column_never_drops_a_month(world: World) -> None:
    """The rule behind Q11, Q20, Q43 (and Q27/Q45 on the service book): a text
    column holding 'YYYY-MM' that carries the entity's period must either
    compile a comparison that keeps every month of the window, or refuse —
    never serve a window with its first month missing. Asked for the second
    quarter, the budget variance per entity must be the sum of April, May and
    June."""
    resp = ask(
        "budget variance by business in Q2 2026",
        LENS,
        ["budget_vs_actual"],
        world,
        [
            ("shape", "aggregate", 0.9),
            ("metric", "budget_variance_eur", 0.9),
            ("dimension", "entity", None),
            ("filter", None, 0.9),
        ],
    )
    reading = {
        str(entity): round(float(variance), 2)
        for entity, variance in world.sql(
            f"SELECT entity, SUM(variance_eur) FROM {BUDGET} WHERE month IN ('2026-04', "
            f"'2026-05', '2026-06') GROUP BY 1"
        )
    }
    served = {str(row[0]): row[1] for row in served_rows(resp)} if resp.status == "ok" else None
    assert_reading_or_non_answer(resp, served, [reading], what="budget variance by entity, Q2")


# ── the same questions through the cube lane ─────────────────────────────────


def test_q20_on_the_cube_lane(world: World) -> None:
    """The quarter against the same quarter a year before, by entity, on the
    month cube (a time dimension, not text): every month of each window kept."""
    resp = ask_cube(
        "q20",
        "revenue this quarter vs same quarter last year by business",
        [
            {
                "measures": ["group_revenue_monthly.revenue_eur"],
                "dimensions": ["group_revenue_monthly.entity"],
                "timeDimensions": [
                    {
                        "dimension": "group_revenue_monthly.month",
                        "compareDateRange": [
                            ["2026-07-01", "2026-09-30"],
                            ["2025-07-01", "2025-09-30"],
                        ],
                    }
                ],
            }
        ],
        [
            ("revenue", "member", "group_revenue_monthly.revenue_eur"),
            ("business", "member", "group_revenue_monthly.entity"),
            ("this quarter", "period", "2026-07-01..2026-09-30"),
            ("same quarter last year", "period", "2025-07-01..2025-09-30"),
        ],
        world,
        lens=LENS,
    )
    q3_2026 = ["2026-07", "2026-08"]
    readings_shop = [
        [_revenue(world, SHOP, q3_2026), _revenue(world, SHOP, ["2025-07", "2025-08"])],
        [_revenue(world, SHOP, q3_2026), _revenue(world, SHOP, ["2025-07", "2025-08", "2025-09"])],
    ]
    readings_lvi = [
        [
            _revenue(world, LVI, q3_2026, "Service contracts"),
            _revenue(world, LVI, ["2025-07", "2025-08"], "Service contracts"),
        ],
        [
            _revenue(world, LVI, q3_2026, "Service contracts"),
            _revenue(world, LVI, ["2025-07", "2025-08", "2025-09"], "Service contracts"),
        ],
    ]
    served = (
        {str(row[0]): [row[1], row[2]] for row in served_rows(resp)} if resp.status == "ok" else {}
    )
    assert_reading_or_non_answer(
        resp, served.get(SHOP), readings_shop, what="webshop entity, Q3 vs last year"
    )
    assert_reading_or_non_answer(
        resp, served.get(LVI), readings_lvi, what="Finnish entity, Q3 vs last year"
    )


def test_q43_on_the_cube_lane(world: World) -> None:
    """The Finnish invoices alone for an ask that names no company, where the
    group-level figure exists: the lane asks which before serving one."""
    resp = ask_cube(
        "q43",
        "what was total invoiced revenue between jan 1 and aug 31 2026",
        [
            {
                "measures": ["fi_invoices.invoiced_net_eur"],
                "timeDimensions": [
                    {
                        "dimension": "fi_invoices.issue_date",
                        "dateRange": ["2026-01-01", "2026-08-31"],
                    }
                ],
            }
        ],
        [
            ("invoiced revenue", "member", "fi_invoices.invoiced_net_eur"),
            ("between jan 1 and aug 31 2026", "period", "2026-01-01..2026-08-31"),
        ],
        world,
        lens=LENS,
    )
    finnish = float(
        world.scalar(
            f"SELECT SUM(net_amount_eur) FROM {INVOICES} WHERE issue_date BETWEEN '2026-01-01' "
            f"AND '2026-08-31'"
        )
    )
    swedish = (
        float(
            world.scalar(
                f"SELECT SUM(belopp_exkl_moms) FROM {FAKTUROR} WHERE fakturadatum BETWEEN "
                f"'2026-01-01' AND '2026-08-31'"
            )
        )
        / SEK_PER_EUR
    )
    served = served_rows(resp)[0][0] if resp.status == "ok" else None
    assert_reading_or_non_answer(
        resp, served, [round(finnish + swedish, 2), round(finnish, 2)], what="invoiced revenue"
    )
