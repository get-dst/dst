"""Field service lens: the maintenance-contract book and work-order margin.

The world: maintenance contracts (latest full sync batch, with a partial batch
row to exclude), their customers, the renewals mart by decision month (a
'YYYY-MM' text column), and work orders with full job cost, some completed
but not yet invoiced.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from tests.sandbox._cube import ask_cube, cell
from tests.sandbox._world import (
    World,
    ask,
    assert_reading_or_non_answer,
    describe,
    served_rows,
)

LENS = "field_service"
CONTRACTS = "raw_fieldflow.maintenance_contracts"
CUSTOMERS = "marts_customer.dim_customer_360"
RENEWALS = "marts_service.mart_contract_renewals_monthly"
WORK_ORDERS = "intermediate.int_work_order_costs"
FULL_BATCH = "B2026-08-31"

# contract, customer, name, end date, annual value, status
_CONTRACTS = [
    ("MC0019", "C101", "Asunto Oy Kotikatu 12", "2026-04-15", 30_900.0, "Renewed"),
    ("MC0021", "C102", "Asunto Oy Kotikatu 30", "2026-05-10", 31_600.0, "Renewed"),
    ("MC0023", "C103", "Haavisto Group", "2026-06-01", 40_000.0, "Churned"),
    (
        "MC0030",
        "C104",
        "Asunto Oy Kotikatu 44",
        "2026-07-15",
        45_000.0,
        "Active",
    ),  # lapsed, no outcome yet
    (
        "MC0031",
        "C105",
        "As Oy Lahden Ranta",
        "2026-08-10",
        50_000.0,
        "Active",
    ),  # lapsed, no outcome yet
    ("MC0071", "C106", "As Oy Turun Satama", "2026-09-12", 20_000.0, "Active"),
    ("MC0070", "C107", "Asunto Oy Kotikatu 55", "2026-09-28", 25_000.0, "Active"),
    ("MC0059", "C108", "Kaupungin Asunnot Oy", "2026-10-05", 9_300.0, "Active"),
    ("MC0008", "C109", "Asunto Oy Kotikatu 21", "2026-10-20", 40_000.0, "Active"),
    ("MC0054", "C110", "Asunto Oy Kotikatu 61", "2026-10-30", 39_587.7, "Active"),
    ("MC0006", "C111", "Vuokratalot Oyj", "2026-10-15", 80_000.0, "Active"),
    ("MC0048", "C112", "City of Tampere", "2026-09-20", 60_000.0, "Active"),
    ("MC0049", "C113", "Rakennus Oy", "2026-10-25", 19_033.68, "Active"),
    ("MC0090", "C120", "As Oy Vantaan Koivula", "2027-02-01", 22_000.0, "Active"),
]
# customer, name, customer_type, legacy segment label
_CUSTOMERS = [
    ("C101", "Asunto Oy Kotikatu 12", "Property management", "Housing companies"),
    ("C102", "Asunto Oy Kotikatu 30", "Property management", "Housing companies"),
    ("C103", "Haavisto Group", "Property management", "Housing companies"),
    ("C104", "Asunto Oy Kotikatu 44", "Property management", "Housing companies"),
    ("C105", "As Oy Lahden Ranta", "Property management", "Housing company"),
    ("C106", "As Oy Turun Satama", "Property management", "Housing companies"),
    ("C107", "Asunto Oy Kotikatu 55", "Property management", "Housing companies"),
    ("C108", "Kaupungin Asunnot Oy", "Property management", "Housing company"),
    ("C109", "Asunto Oy Kotikatu 21", "Property management", "Housing companies"),
    ("C110", "Asunto Oy Kotikatu 61", "Property management", "Housing companies"),
    ("C111", "Vuokratalot Oyj", "Property investor", "Commercial & public"),
    ("C112", "City of Tampere", "Public sector", "Commercial & public"),
    ("C113", "Rakennus Oy", "Construction firm", "Commercial & public"),
    ("C120", "As Oy Vantaan Koivula", "Housing company", "Housing companies"),
    ("C121", "As Oy Oulun Merituuli", "Housing company", "Housing companies"),
]
# month, expiring, renewed, churned, churned value
_RENEWALS = [
    ("2026-03", 0, 0, 0, 0.0),
    ("2026-04", 1, 1, 0, 0.0),
    ("2026-05", 2, 1, 1, 40_000.0),
    ("2026-06", 0, 0, 0, 0.0),
    ("2026-07", 1, 0, 0, 0.0),
    ("2026-08", 1, 0, 0, 0.0),
]
E, M = "Emergency callout", "Maintenance visit"
# type, status, completed, billed, labour, materials
_WORK_ORDERS = [
    *[(E, "Invoiced", f"2025-0{m}-10", 5_000.0, 2_500.0, 1_350.0) for m in range(1, 7)],
    (E, "Invoiced", "2025-07-10", 7_000.0, 3_500.0, 1_900.0),
    (E, "Invoiced", "2025-08-12", 6_500.0, 3_250.0, 1_750.0),
    *[(E, "Invoiced", f"2026-0{m}-10", 5_500.0, 2_750.0, 1_480.0) for m in range(1, 4)],
    (E, "Invoiced", "2026-04-10", 10_000.0, 5_000.0, 2_700.0),
    (E, "Invoiced", "2026-05-15", 8_000.0, 4_000.0, 2_160.0),
    (M, "Invoiced", "2026-04-20", 6_000.0, 3_000.0, 1_620.0),
    (M, "Invoiced", "2026-06-10", 6_000.0, 3_000.0, 1_620.0),
    (E, "Invoiced", "2026-07-08", 10_000.0, 4_900.0, 2_750.0),
    (E, "Invoiced", "2026-08-05", 8_000.0, 3_900.0, 2_220.0),
    (M, "Invoiced", "2026-07-20", 6_000.0, 2_900.0, 1_690.0),
    (M, "Invoiced", "2026-08-12", 6_000.0, 2_900.0, 1_690.0),
    (E, "Completed", "2026-08-25", 0.0, 3_000.0, 1_500.0),  # done, not yet billed
    (M, "Completed", "2026-08-28", 0.0, 2_500.0, 1_200.0),  # done, not yet billed
    (E, "In progress", None, 0.0, 800.0, 0.0),
]


@pytest.fixture()
def world(tmp_path: Path) -> World:
    w = World(tmp_path / "service.duckdb")
    loaded = datetime(2026, 8, 31, 23, 0)
    contracts = [
        (
            cid,
            cust,
            name,
            date.fromisoformat(end).replace(year=date.fromisoformat(end).year - 1),
            date.fromisoformat(end),
            value,
            "Annual",
            status,
            None,
            False,
            FULL_BATCH,
            loaded,
        )
        for cid, cust, name, end, value, status in _CONTRACTS
    ]
    # The partial July batch: one contract's older copy, excluded by the population.
    contracts.append(
        (
            "MC0030",
            "C104",
            "Asunto Oy Kotikatu 44",
            date(2025, 7, 15),
            date(2026, 7, 15),
            45_000.0,
            "Annual",
            "Active",
            None,
            False,
            "B2026-07-31",
            datetime(2026, 7, 31, 23, 0),
        )
    )
    w.table(
        CONTRACTS,
        {
            "contract_id": "VARCHAR",
            "customer_id": "VARCHAR",
            "customer_name": "VARCHAR",
            "start_date": "DATE",
            "end_date": "DATE",
            "annual_value_eur": "DOUBLE",
            "billing_frequency": "VARCHAR",
            "status": "VARCHAR",
            "renewed_from": "VARCHAR",
            "auto_renew": "BOOLEAN",
            "_synced_batch": "VARCHAR",
            "_loaded_at": "TIMESTAMP",
        },
        contracts,
    )
    w.table(
        CUSTOMERS,
        {
            "customer_id": "VARCHAR",
            "customer_name": "VARCHAR",
            "customer_type": "VARCHAR",
            "primary_segment": "VARCHAR",
            "region": "VARCHAR",
            "city": "VARCHAR",
            "customer_since": "DATE",
            "first_activity_date": "DATE",
            "last_activity_date": "DATE",
            "lifetime_invoiced_eur": "DOUBLE",
            "active_contracts": "BIGINT",
            "active_contract_value_eur": "DOUBLE",
            "is_partner": "BOOLEAN",
            "partner_id": "VARCHAR",
            "hubspot_company_id": "BIGINT",
            "days_since_activity": "BIGINT",
        },
        [
            (
                cid,
                name,
                ctype,
                segment,
                "Uusimaa",
                "Helsinki",
                date(2024, 1, 1),
                date(2024, 1, 1),
                date(2026, 8, 1),
                100_000.0,
                1,
                30_000.0,
                False,
                None,
                None,
                31,
            )
            for cid, name, ctype, segment in _CUSTOMERS
        ],
    )
    w.table(
        RENEWALS,
        {
            "month": "VARCHAR",
            "contracts_expiring": "BIGINT",
            "renewed": "BIGINT",
            "churned": "BIGINT",
            "churned_value_eur": "DOUBLE",
            "renewal_rate": "DOUBLE",
            "active_contracts": "BIGINT",
            "book_value_eur": "DOUBLE",
        },
        [
            (
                month,
                expiring,
                renewed,
                churned,
                value,
                (renewed / (renewed + churned)) if renewed + churned else None,
                12,
                500_000.0,
            )
            for month, expiring, renewed, churned, value in _RENEWALS
        ],
    )
    w.table(
        WORK_ORDERS,
        {
            "work_order_id": "VARCHAR",
            "customer_id": "VARCHAR",
            "deal_id": "VARCHAR",
            "contract_id": "VARCHAR",
            "wo_type": "VARCHAR",
            "status": "VARCHAR",
            "scheduled_date": "DATE",
            "completed_date": "DATE",
            "completed_month": "VARCHAR",
            "hours_total": "DOUBLE",
            "labor_cost_eur": "DOUBLE",
            "materials_cost_eur": "DOUBLE",
            "billed_amount_eur": "DOUBLE",
            "margin_eur": "DOUBLE",
            "margin_pct": "DOUBLE",
        },
        [
            (
                f"WO{i:04d}",
                f"C1{i % 10:02d}",
                None,
                None,
                wo_type,
                status,
                date.fromisoformat(completed) if completed else date(2026, 9, 3),
                date.fromisoformat(completed) if completed else None,
                completed[:7] if completed else None,
                round(labour / 58.0, 1),
                labour,
                materials,
                billed,
                billed - labour - materials,
                (billed - labour - materials) / billed if billed else None,
            )
            for i, (wo_type, status, completed, billed, labour, materials) in enumerate(
                _WORK_ORDERS, start=1
            )
        ],
    )
    return w


def test_q26_housing_company_contracts_up_for_renewal_are_not_zero(world: World) -> None:
    """Asked "how many housing company maintenance contracts are coming up for
    renewal in the next 60 days" ended, after the driver narrowed it to the
    lens's own words, as a listing of active contracts ending in the window
    whose customer's ``customer_type`` is 'Housing company' — ZERO rows,
    served as ok and answered as "0". The contracts in question belong to
    housing companies managed through a property manager, which the customer
    table types as 'Property management' and labels as housing only on its
    legacy segment column; the lens holds five of them. An empty result from a
    filter on a joined dimension went out as a genuine zero, with no word that
    the value named excludes every row. The readings count the active contracts
    ending in the window whose customer carries the housing label (5) or whose
    name is a housing company's legal form (4).

    Failure mode to carry: a filter that empties the result served as a
    genuine zero, no zero-check on the joined dimension's other spellings."""
    resp = ask(
        "List the maintenance contracts with status 'Active' and customers.customer_type "
        "'Housing company' ending in {period}",
        LENS,
        ["maintenance_contracts", "customers"],
        world,
        [
            ("shape", "listing", 1.0),
            ("entity", "maintenance_contracts", 0.99),
            ("dimension", "status", 0.51),
            ("filter", "status", 1.0),
            ("dimension", "customer_name", 0.47),
            ("filter", None, 0.63),
            ("dimension", None, 0.84),
            ("filter_value", "Active", None),
            ("filter", "customers.customer_type", 0.97),
        ],
        bindings={"end_date": "2026-09-01/2026-10-31"},
    )
    window = (
        f"m._synced_batch = '{FULL_BATCH}' AND m.status = 'Active' "
        f"AND m.end_date BETWEEN '2026-09-01' AND '2026-10-31'"
    )
    by_segment = float(
        world.scalar(
            f"SELECT count(*) FROM {CONTRACTS} m JOIN {CUSTOMERS} c USING (customer_id) "
            f"WHERE {window} AND c.primary_segment IN ('Housing company', 'Housing companies')"
        )
    )
    by_name = float(
        world.scalar(
            f"SELECT count(*) FROM {CONTRACTS} m WHERE {window} "
            "AND (m.customer_name ILIKE 'Asunto Oy%' OR m.customer_name ILIKE 'As Oy%')"
        )
    )
    served = float(len(served_rows(resp))) if resp.status == "ok" else None
    assert_reading_or_non_answer(
        resp, served, [by_segment, by_name], what="housing-company contracts renewing in 60 days"
    )


def _q2_renewal_readings(world: World) -> list[float]:
    """Contracts whose term ended in the second quarter: the share that
    renewed, by count and by annual value, in percent."""
    cohort = f"_synced_batch = '{FULL_BATCH}' AND end_date BETWEEN '2026-04-01' AND '2026-06-30'"
    by_count = world.scalar(
        f"SELECT 100.0 * count(*) FILTER (WHERE status = 'Renewed') / count(*) FROM {CONTRACTS} "
        f"WHERE {cohort}"
    )
    by_value = world.scalar(
        f"SELECT 100.0 * SUM(annual_value_eur) FILTER (WHERE status = 'Renewed') / "
        f"SUM(annual_value_eur) FROM {CONTRACTS} WHERE {cohort}"
    )
    return [round(float(by_count), 1), round(float(by_value), 1)]


def test_q27_renewal_rate_last_quarter_keeps_april(world: World) -> None:
    """Asked "what % of maintenance contracts that were up for renewal last quarter
    actually renewed" came back as 50 %: the renewals mart's ``month`` is
    'YYYY-MM' text and ``month >= '2026-04-01'`` drops '2026-04', the month the
    first renewal was recorded in, leaving one renewed and one churned. The
    readings: two of the three contracts ending in the quarter renewed (66.7 %
    by count), or 61 % by annual value.

    Failure mode to carry: a month-period text column compared against day
    literals — the first month of the window dropped."""
    resp = ask(
        "what % of maintenance contracts that were up for renewal last quarter actually renewed",
        LENS,
        ["contract_renewals_monthly", "maintenance_contracts"],
        world,
        [
            ("shape", "aggregate", 1.0),
            ("metric", "contract_renewal_rate", 1.0),
            ("metric", None, 0.94),
            ("filter", None, 0.56),
        ],
    )
    served = round(float(served_rows(resp)[0][0]) * 100, 1) if resp.status == "ok" else None
    assert_reading_or_non_answer(resp, served, _q2_renewal_readings(world), what="Q2 renewal rate")


def test_q45_renewal_rate_this_quarter_vs_last_names_the_gap(world: World) -> None:
    """Asked "how does this quarter's maintenance contract renewal rate compare to
    last quarter" came back as a comparison row with the current quarter NULL
    (no decision recorded yet for the contracts that ended in July and August)
    and last quarter at 50 % — the same dropped April — served as ok with no
    gap stated. A defensible answer either refuses the comparison, naming the
    missing outcomes, or carries last quarter's true rate beside a stated gap.

    Failure mode to carry: a comparison with one side NULL served as ok; a
    month-period text column compared against day literals."""
    resp = ask(
        "how does this quarter's maintenance contract renewal rate compare to last quarter",
        LENS,
        ["contract_renewals_monthly", "maintenance_contracts"],
        world,
        [("shape", "aggregate", 0.98), ("metric", "contract_renewal_rate", 1.0)],
    )
    served = None
    if resp.status == "ok":
        (row,) = served_rows(resp)
        assert row[0] is not None or resp.degraded or resp.declined, (
            f"a NULL current quarter served as a comparison with no gap stated — {describe(resp)}"
        )
        served = round(float(row[1]) * 100, 1) if row[1] is not None else None
    assert_reading_or_non_answer(
        resp, served, _q2_renewal_readings(world), what="last quarter's renewal rate"
    )


def test_rule_renewal_rate_in_a_named_quarter_counts_every_month_of_it(world: World) -> None:
    """The rule behind Q27 and Q45 on this book: a 'YYYY-MM' text column that
    carries the period must compile a comparison that keeps every month of
    the window, or refuse. The second quarter's renewal rate from the mart is
    renewed over decided across April, May and June."""
    resp = ask(
        "contract renewal rate in Q2 2026",
        LENS,
        ["contract_renewals_monthly"],
        world,
        [
            ("shape", "aggregate", 1.0),
            ("metric", "contract_renewal_rate", 1.0),
            ("metric", None, 0.94),
            ("filter", None, 0.9),
        ],
    )
    reading = round(
        float(
            world.scalar(
                f"SELECT 100.0 * SUM(renewed) / SUM(renewed + churned) FROM {RENEWALS} WHERE "
                f"month IN ('2026-04', '2026-05', '2026-06')"
            )
        ),
        1,
    )
    served = round(float(served_rows(resp)[0][0]) * 100, 1) if resp.status == "ok" else None
    assert_reading_or_non_answer(resp, served, [reading], what="Q2 renewal rate from the mart")


def _margin(world: World, wo_type: str, start: str, end: str) -> float:
    """Full-cost margin on INVOICED work orders completed in [start, end], percent."""
    return round(
        float(
            world.scalar(
                f"SELECT 100.0 * (SUM(billed_amount_eur) - SUM(labor_cost_eur) - "
                f"SUM(materials_cost_eur)) / SUM(billed_amount_eur) "
                f"FROM {WORK_ORDERS} WHERE wo_type = '{wo_type}' AND status = 'Invoiced' AND "
                f"completed_date BETWEEN '{start}' AND '{end}'"
            )
        ),
        1,
    )


def test_w2_service_margin_this_quarter_vs_last_is_on_invoiced_work(world: World) -> None:
    """Asked "why did <the Finnish entity>'s margin on service work drop this
    quarter" came back as negative margins by job type against ~23 % last
    quarter: the quarter's completed-but-unbilled jobs (cost booked, nothing
    billed yet) were summed in, and the comparison window was cut to 1 April
    – 1 June. On invoiced work the margin is flat. A "why" has no typed
    answer; the figures it rests on must still be defensible or refused.

    Failure mode to carry: a ratio over rows whose denominator side has not
    happened yet (billing timing) served as a real drop; a compare window that
    mirrors the elapsed days instead of the named quarter."""
    resp = ask(
        "why did Nordlund LVI's margin on service work drop this quarter",
        LENS,
        ["work_orders"],
        world,
        [
            ("shape", None, 0.95),
            ("metric", "job_margin_pct", 0.51),
            ("dimension", "wo_type", 0.61),
            ("filter", "wo_type", 0.73),
            ("dimension", None, 0.91),
            ("filter_value", "unstated", 0.83),
            ("filter", None, 0.97),
        ],
    )
    readings = {
        t: [
            [_margin(world, t, "2026-07-01", q3_end), _margin(world, t, "2026-04-01", "2026-06-30")]
            for q3_end in ("2026-08-31", "2026-09-01")
        ]
        for t in (E, M)
    }
    if resp.status == "ok":
        for row in served_rows(resp):
            served = [
                round(float(row[1]) * 100, 1) if row[1] is not None else None,
                round(float(row[2]) * 100, 1) if row[2] is not None else None,
            ]
            assert_reading_or_non_answer(
                resp, served, readings[str(row[0])], what=f"{row[0]} margin this quarter vs last"
            )
    else:
        assert_reading_or_non_answer(resp, None, [], what="margin this quarter vs last")


def test_q48_emergency_revenue_vs_this_time_last_year_compares_like_with_like(world: World) -> None:
    """Asked "how much did emergency callout revenue grow or fall compared to this
    time last year" came back as the whole of calendar 2025 against 2024
    (nothing), a single figure and no comparison: "this time last year" was
    read as the window itself. The readings compare the latest fully invoiced
    month with the same month a year earlier, or the year to that month with
    the year before.

    Failure mode to carry: a comparison phrase read as the window; a compare
    side with no data served as the answer."""
    resp = ask(
        "how much did emergency callout revenue grow or fall compared to this time last year",
        LENS,
        ["work_orders"],
        world,
        [
            ("shape", "aggregate", 0.99),
            ("metric", "billed_eur", 0.59),
            ("dimension", None, 0.84),
            ("definition", "engaged", 0.97),
            ("filter", "wo_type", 0.93),
            ("filter", None, 0.72),
            ("filter_value", "Emergency callout", None),
        ],
    )

    def billed(start: str, end: str) -> float:
        return round(
            float(
                world.scalar(
                    f"SELECT COALESCE(SUM(billed_amount_eur), 0) FROM {WORK_ORDERS} WHERE "
                    f"wo_type = '{E}' "
                    f"AND status = 'Invoiced' AND completed_date BETWEEN '{start}' AND '{end}'"
                )
            ),
            2,
        )

    readings = [
        [billed("2026-07-01", "2026-07-31"), billed("2025-07-01", "2025-07-31")],
        [billed("2026-01-01", "2026-07-31"), billed("2025-01-01", "2025-07-31")],
    ]
    served = None
    if resp.status == "ok":
        (row,) = served_rows(resp)
        served = [row[0], row[1]]
    assert_reading_or_non_answer(
        resp, served, readings, what="emergency revenue vs this time last year"
    )


# ── the same questions through the cube lane ─────────────────────────────────

RATE = "contract_renewals_monthly.contract_renewal_rate"
Q2_MONTHS = {
    "dimension": "contract_renewals_monthly.month",
    "dateRange": ["2026-04-01", "2026-06-30"],
}


def test_q26_on_the_cube_lane(world: World) -> None:
    """A filter on the joined customer type that matches no row: the lane says
    nothing was recorded instead of serving 0."""
    resp = ask_cube(
        "q26",
        "how many housing company maintenance contracts are coming up for renewal in the next "
        "60 days",
        [
            {
                "measures": ["maintenance_contracts.active_contracts"],
                "filters": [
                    {
                        "member": "customers.customer_type",
                        "operator": "equals",
                        "values": ["Housing company"],
                    }
                ],
                "timeDimensions": [
                    {
                        "dimension": "maintenance_contracts.end_date",
                        "dateRange": ["2026-09-01", "2026-10-30"],
                    }
                ],
            }
        ],
        [
            ("housing company", "value", "Housing company"),
            ("maintenance contracts", "member", "maintenance_contracts.active_contracts"),
            ("coming up for renewal", "member", "maintenance_contracts.end_date"),
            ("next 60 days", "period", "2026-09-01..2026-10-30"),
        ],
        world,
        lens=LENS,
    )
    window = (
        f"m._synced_batch = '{FULL_BATCH}' AND m.status = 'Active' "
        f"AND m.end_date BETWEEN '2026-09-01' AND '2026-10-31'"
    )
    by_segment = float(
        world.scalar(
            f"SELECT count(*) FROM {CONTRACTS} m JOIN {CUSTOMERS} c USING (customer_id) "
            f"WHERE {window} AND c.primary_segment IN ('Housing company', 'Housing companies')"
        )
    )
    by_name = float(
        world.scalar(
            f"SELECT count(*) FROM {CONTRACTS} m WHERE {window} "
            "AND (m.customer_name ILIKE 'Asunto Oy%' OR m.customer_name ILIKE 'As Oy%')"
        )
    )
    served = float(served_rows(resp)[0][0]) if resp.status == "ok" else None
    assert_reading_or_non_answer(
        resp, served, [by_segment, by_name], what="housing-company contracts renewing in 60 days"
    )


RENEWAL_LEDGER = [
    ("renewed", "member", "contract_renewals_monthly.contract_renewal_rate"),
    ("maintenance contracts", "member", "contract_renewals_monthly.contract_renewal_rate"),
    ("up for renewal", "member", "contract_renewals_monthly.contract_renewal_rate"),
]


def test_q27_on_the_cube_lane(world: World) -> None:
    """The quarter's renewal rate over every month of it."""
    resp = ask_cube(
        "q27",
        "what % of maintenance contracts that were up for renewal last quarter actually renewed",
        [
            {
                "measures": ["contract_renewals_monthly.contract_renewal_rate"],
                "timeDimensions": [Q2_MONTHS],
            }
        ],
        [
            *RENEWAL_LEDGER,
            ("last quarter", "period", "2026-04-01..2026-06-30"),
            ("actually", "none", ""),
        ],
        world,
        lens=LENS,
    )
    served = round(float(cell(resp, RATE)) * 100, 1) if resp.status == "ok" else None
    assert_reading_or_non_answer(resp, served, _q2_renewal_readings(world), what="Q2 renewal rate")


def test_q45_on_the_cube_lane(world: World) -> None:
    """This quarter against last: the current quarter has no decision recorded
    yet, which the answer must name or refuse."""
    resp = ask_cube(
        "q45",
        "how does this quarter's maintenance contract renewal rate compare to last quarter",
        [
            {
                "measures": ["contract_renewals_monthly.contract_renewal_rate"],
                "timeDimensions": [
                    {
                        "dimension": "contract_renewals_monthly.month",
                        "compareDateRange": [
                            ["2026-07-01", "2026-09-30"],
                            ["2026-04-01", "2026-06-30"],
                        ],
                    }
                ],
            }
        ],
        [
            ("renewal rate", "member", "contract_renewals_monthly.contract_renewal_rate"),
            ("maintenance contract", "member", "contract_renewals_monthly.contract_renewal_rate"),
            ("this quarter", "period", "2026-07-01..2026-09-30"),
            ("last quarter", "period", "2026-04-01..2026-06-30"),
        ],
        world,
        lens=LENS,
    )
    served = None
    if resp.status == "ok":
        assert resp.data is not None
        (row,) = resp.data.rows
        assert row[0] not in (None, 0) or resp.degraded or "data_gap" in resp.answer, (
            f"an empty current quarter served as a comparison with no gap stated — {describe(resp)}"
        )
        served = round(float(row[1]) * 100, 1) if row[1] is not None else None
    assert_reading_or_non_answer(
        resp, served, _q2_renewal_readings(world), what="last quarter's renewal rate"
    )


def test_rule_renewal_rate_in_a_named_quarter_on_the_cube_lane(world: World) -> None:
    resp = ask_cube(
        "renewal_q2",
        "contract renewal rate in Q2 2026",
        [
            {
                "measures": ["contract_renewals_monthly.contract_renewal_rate"],
                "timeDimensions": [Q2_MONTHS],
            }
        ],
        [
            ("contract renewal rate", "member", "contract_renewals_monthly.contract_renewal_rate"),
            ("Q2 2026", "period", "2026-04-01..2026-06-30"),
        ],
        world,
        lens=LENS,
    )
    reading = round(
        float(
            world.scalar(
                f"SELECT 100.0 * SUM(renewed) / SUM(renewed + churned) FROM {RENEWALS} WHERE "
                f"month IN ('2026-04', '2026-05', '2026-06')"
            )
        ),
        1,
    )
    served = round(float(cell(resp, RATE)) * 100, 1) if resp.status == "ok" else None
    assert_reading_or_non_answer(resp, served, [reading], what="Q2 renewal rate from the mart")


def test_w2_on_the_cube_lane(world: World) -> None:
    """The margin by job type, this quarter against last on the completion date."""
    resp = ask_cube(
        "w2",
        "why did Nordlund LVI's margin on service work drop this quarter",
        [
            {
                "measures": ["work_orders.job_margin_pct"],
                "dimensions": ["work_orders.wo_type"],
                "timeDimensions": [
                    {
                        "dimension": "work_orders.completed_date",
                        "compareDateRange": [
                            ["2026-07-01", "2026-09-30"],
                            ["2026-04-01", "2026-06-30"],
                        ],
                    }
                ],
            }
        ],
        [
            ("margin", "member", "work_orders.job_margin_pct"),
            ("service work", "member", "work_orders.wo_type"),
            ("this quarter", "period", "2026-07-01..2026-09-30"),
        ],
        world,
        lens=LENS,
    )
    readings = {
        t: [
            [_margin(world, t, "2026-07-01", q3_end), _margin(world, t, "2026-04-01", "2026-06-30")]
            for q3_end in ("2026-08-31", "2026-09-01")
        ]
        for t in (E, M)
    }
    if resp.status == "ok":
        for row in served_rows(resp):
            served = [
                round(float(row[1]) * 100, 1) if row[1] is not None else None,
                round(float(row[2]) * 100, 1) if row[2] is not None else None,
            ]
            assert_reading_or_non_answer(
                resp, served, readings[str(row[0])], what=f"{row[0]} margin this quarter vs last"
            )
    else:
        assert_reading_or_non_answer(resp, None, [], what="margin this quarter vs last")


def test_q48_on_the_cube_lane(world: World) -> None:
    """Emergency callouts' billing, the year so far against the same months a
    year before."""
    resp = ask_cube(
        "q48",
        "how much did emergency callout revenue grow or fall compared to this time last year",
        [
            {
                "measures": ["work_orders.billed_eur"],
                "segments": ["work_orders.emergency_callout"],
                "timeDimensions": [
                    {
                        "dimension": "work_orders.completed_date",
                        "compareDateRange": [
                            ["2026-01-01", "2026-08-31"],
                            ["2025-01-01", "2025-08-31"],
                        ],
                    }
                ],
            }
        ],
        [
            ("emergency callout", "segment", "work_orders.emergency_callout"),
            ("revenue", "member", "work_orders.billed_eur"),
            ("this time last year", "period", "2025-01-01..2025-08-31"),
        ],
        world,
        lens=LENS,
    )

    def billed(start: str, end: str) -> float:
        return round(
            float(
                world.scalar(
                    f"SELECT COALESCE(SUM(billed_amount_eur), 0) FROM {WORK_ORDERS} WHERE "
                    f"wo_type = '{E}' "
                    f"AND status = 'Invoiced' AND completed_date BETWEEN '{start}' AND '{end}'"
                )
            ),
            2,
        )

    readings = [
        [billed("2026-07-01", "2026-07-31"), billed("2025-07-01", "2025-07-31")],
        [billed("2026-01-01", "2026-07-31"), billed("2025-01-01", "2025-07-31")],
    ]
    served = None
    if resp.status == "ok":
        (row,) = served_rows(resp)
        served = [row[0], row[1]]
    assert_reading_or_non_answer(
        resp, served, readings, what="emergency revenue vs this time last year"
    )
