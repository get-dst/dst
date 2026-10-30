"""Sales pipeline lens: win rates, open pipeline and lead counts served wrong.

The world: quotes with a sent date and a decision date (Won, Lost or Expired —
every quote carries a final status, and quotes still open today carry a
decision dated AFTER today), and CRM leads with a segment and a source.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.sandbox._cube import ask_cube, column
from tests.sandbox._world import (
    World,
    ask,
    assert_reading_or_non_answer,
    describe,
    served_rows,
)

LENS = "sales_pipeline"
QUOTES = "staging.stg_pipedrive__quotes"
LEADS = "staging.stg_pipedrive__leads"
HH, HC, CP = "Households", "Housing companies", "Commercial & public"

# segment, sent, status, decided, quote value
_QUOTES = [
    # Q1 2026
    (HH, "2026-01-10", "Won", "2026-01-25", 12000),
    (HH, "2026-02-05", "Won", "2026-02-20", 9000),
    (HH, "2026-03-02", "Won", "2026-03-15", 8000),
    (HH, "2026-03-05", "Lost", "2026-03-20", 7000),
    (HC, "2026-01-12", "Won", "2026-02-01", 40000),
    (HC, "2026-02-10", "Won", "2026-02-28", 35000),
    (HC, "2026-03-03", "Lost", "2026-03-25", 30000),
    (CP, "2026-01-20", "Won", "2026-02-10", 60000),
    (CP, "2026-02-14", "Lost", "2026-03-01", 55000),
    (CP, "2026-03-10", "Expired", "2026-03-31", 50000),
    # Q2 2026
    (HH, "2026-04-03", "Won", "2026-04-18", 11000),
    (HH, "2026-05-06", "Won", "2026-05-20", 9500),
    (HH, "2026-06-01", "Won", "2026-06-15", 8500),
    (HH, "2026-06-04", "Lost", "2026-06-20", 7500),
    (HC, "2026-04-11", "Won", "2026-05-02", 42000),
    (HC, "2026-05-12", "Won", "2026-05-30", 36000),
    (HC, "2026-06-02", "Lost", "2026-06-25", 31000),
    (CP, "2026-04-21", "Won", "2026-05-10", 62000),
    (CP, "2026-05-15", "Lost", "2026-06-01", 56000),
    (CP, "2026-06-10", "Expired", "2026-06-30", 51000),
    # Q3 2026, decided by 31 August
    (HH, "2026-07-02", "Won", "2026-07-16", 10000),
    (HH, "2026-07-09", "Won", "2026-07-30", 9800),
    (HH, "2026-07-12", "Lost", "2026-07-28", 7200),
    (HC, "2026-07-05", "Won", "2026-07-25", 41000),
    (HC, "2026-08-03", "Won", "2026-08-21", 37000),
    (HC, "2026-07-15", "Expired", "2026-08-14", 30500),
    (CP, "2026-08-05", "Won", "2026-08-20", 61000),
    (CP, "2026-08-10", "Won", "2026-08-25", 57000),
    (CP, "2026-08-12", "Lost", "2026-08-28", 52000),
    # decided today
    (HH, "2026-08-15", "Lost", "2026-09-01", 7100),
    # still open today: sent in August, decision dated after today
    (HH, "2026-08-20", "Lost", "2026-09-20", 7300),
    (HH, "2026-08-21", "Lost", "2026-09-22", 7400),
    (HH, "2026-08-25", "Expired", "2026-10-05", 7600),
    (HC, "2026-08-18", "Expired", "2026-09-30", 33000),
    (HC, "2026-08-19", "Expired", "2026-10-01", 34000),
    (CP, "2026-08-22", "Lost", "2026-09-15", 53000),
    (CP, "2026-08-24", "Lost", "2026-09-18", 54000),
    # sent today, open
    (CP, "2026-09-01", "Lost", "2026-09-25", 20000),
]
# segment, source, created
_LEADS = [
    (HC, "Web form", "2026-01-15"),
    (HC, "Web form", "2026-03-02"),
    (HC, "Web form", "2026-05-10"),
    (HC, "Property manager", "2026-02-20"),
    (HC, "Property manager", "2026-07-07"),
    (HC, "Referral", "2026-08-21"),
    (HC, "Property manager", "2025-06-01"),
    (HC, "Property manager", "2025-09-10"),
    (HH, "Phone", "2026-02-02"),
    (HH, "Phone", "2026-06-06"),
    (HH, "Web form", "2026-07-01"),
    (CP, "Campaign", "2026-04-04"),
]


@pytest.fixture()
def world(tmp_path: Path) -> World:
    w = World(tmp_path / "sales.duckdb")
    w.table(
        QUOTES,
        {
            "quote_id": "VARCHAR",
            "lead_id": "VARCHAR",
            "survey_id": "VARCHAR",
            "customer_id": "VARCHAR",
            "rep_id": "VARCHAR",
            "segment": "VARCHAR",
            "service": "VARCHAR",
            "sent_date": "DATE",
            "status": "VARCHAR",
            "quote_value_eur": "DOUBLE",
            "term_months": "BIGINT",
            "deal_id": "VARCHAR",
            "decided_date": "DATE",
        },
        [
            (
                f"Q{i:03d}",
                f"L{i:03d}",
                None,
                f"C{i:03d}",
                f"R{i % 4}",
                segment,
                "Heating system",
                date.fromisoformat(sent),
                status,
                float(value),
                12,
                f"D{i:03d}" if status == "Won" else None,
                date.fromisoformat(decided),
            )
            for i, (segment, sent, status, decided, value) in enumerate(_QUOTES, start=1)
        ],
    )
    w.table(
        LEADS,
        {
            "lead_id": "VARCHAR",
            "customer_id": "VARCHAR",
            "segment": "VARCHAR",
            "source": "VARCHAR",
            "owner_rep_id": "VARCHAR",
            "status": "VARCHAR",
            "created_date": "DATE",
            "lost_reason": "VARCHAR",
        },
        [
            (
                f"L{i:03d}",
                f"C{i:03d}",
                segment,
                source,
                f"R{i % 4}",
                "Won",
                date.fromisoformat(created),
                None,
            )
            for i, (segment, source, created) in enumerate(_LEADS, start=1)
        ],
    )
    return w


def _win_rates(world: World, start: str, end: str, *, expired_counts: bool) -> dict[str, float]:
    """Win rate by segment over quotes DECIDED in [start, end], in percent:
    won over won + lost, or won over every decided quote."""
    denominator = (
        "count(*)" if expired_counts else "count(*) FILTER (WHERE status IN ('Won', 'Lost'))"
    )
    return {
        str(segment): round(float(rate), 1)
        for segment, rate in world.sql(
            f"SELECT segment, 100.0 * count(*) FILTER (WHERE status = 'Won') / {denominator} "
            f"FROM {QUOTES} WHERE decided_date BETWEEN '{start}' AND '{end}' GROUP BY 1"
        )
    }


def _served_rates(resp) -> dict[str, float] | None:  # type: ignore[no-untyped-def]
    if resp.status != "ok":
        return None
    return {str(row[0]): round(float(row[1]) * 100, 1) for row in served_rows(resp)}


Q03_READINGS = [
    ("2026-01-01", "2026-08-31", False),
    ("2026-01-01", "2026-08-31", True),
    ("2026-01-01", "2026-09-01", False),
    ("2026-01-01", "2026-09-01", True),
]


def test_q03_win_rate_by_segment_year_to_date_is_year_to_date_by_decision_date(
    world: World,
) -> None:
    """Asked "what's our win rate by segment year to date" came back as the win rate of
    quotes SENT in August: "year to date" left no window, so the resolver fell to
    the last complete month and said so in a note, and the ratio's window went
    on the entity's sent date although both of its components aggregate by
    decision date. The readings are year to date by decision date, won over
    won + lost or over every decided quote, through 31 August or today.

    Failure mode to carry: a stated period the parser does not read ("year to
    date") silently replaced by a default window; the ratio windowed on the
    entity's time field instead of its components' ``agg_time_field``."""
    resp = ask(
        "what's our win rate by segment year to date",
        LENS,
        ["quotes"],
        world,
        [
            ("shape", "aggregate", 1.0),
            ("metric", "quote_win_rate", 0.98),
            ("dimension", "segment", None),
            ("dimension", None, 0.95),
            ("filter", None, 0.63),
        ],
    )
    readings = [_win_rates(world, s, e, expired_counts=x) for s, e, x in Q03_READINGS]
    assert_reading_or_non_answer(
        resp, _served_rates(resp), readings, what="win rate by segment YTD"
    )


def test_q03_win_rate_through_a_named_day_is_not_that_day_alone(world: World) -> None:
    """The same question with the day spelled out — "year to date (through
    2026-09-01)" — came back as the quotes sent ON 1 September: one row,
    Households 0 %. "through <date>" closed the window at both ends.

    Failure mode to carry: "through <date>" read as a one-day window."""
    resp = ask(
        "What is our win rate by segment year to date (through 2026-09-01)?",
        LENS,
        ["quotes"],
        world,
        [
            ("shape", "aggregate", 1.0),
            ("metric", "quote_win_rate", 0.95),
            ("dimension", "segment", None),
            ("dimension", None, 0.92),
            ("filter", None, 0.54),
        ],
    )
    readings = [_win_rates(world, s, e, expired_counts=x) for s, e, x in Q03_READINGS]
    assert_reading_or_non_answer(resp, _served_rates(resp), readings, what="win rate through a day")


def test_w1_win_rate_this_quarter_vs_last_is_not_the_sent_cohort_with_future_decisions(
    world: World,
) -> None:
    """Asked "why did our win rate drop this quarter compared to last quarter" came
    back as 46 % vs 64 %: quotes SENT in each quarter, with every quote still
    open today counted as lost or expired on a decision dated after today. By
    decision date the rate did not drop. A "why" has no typed answer; the facts
    it rests on must still be defensible or refused.

    Failure mode to carry: a ratio windowed on the sent date counting decisions
    dated after today — a false premise confirmed."""
    resp = ask(
        "why did our win rate drop this quarter compared to last quarter",
        LENS,
        ["quotes"],
        world,
        [
            ("shape", None, 0.89),
            ("metric", "quote_win_rate", 0.96),
            ("dimension", None, 0.86),
            ("filter", None, 0.64),
        ],
    )

    def rate(start: str, end: str, expired_counts: bool) -> float:
        denominator = (
            "count(*)" if expired_counts else "count(*) FILTER (WHERE status IN ('Won', 'Lost'))"
        )
        return round(
            float(
                world.scalar(
                    f"SELECT 100.0 * count(*) FILTER (WHERE status = 'Won') / {denominator} "
                    f"FROM {QUOTES} WHERE decided_date BETWEEN '{start}' AND '{end}'"
                )
            ),
            1,
        )

    readings = [
        [rate("2026-07-01", q3_end, x), rate("2026-04-01", "2026-06-30", x)]
        for q3_end in ("2026-08-31", "2026-09-01")
        for x in (False, True)
    ]
    served = None
    if resp.status == "ok":
        (row,) = served_rows(resp)
        served = [round(float(row[0]) * 100, 1), round(float(row[1]) * 100, 1)]
    assert_reading_or_non_answer(resp, served, readings, what="win rate this quarter vs last")


def test_q04_open_pipeline_is_not_the_value_quoted_in_the_month(world: World) -> None:
    """Asked "total open pipeline value this month compared to last month" came back
    as the value of quotes SENT on 1 September against quotes sent in August,
    a -99.5 % drop served as ok. The lens declares open quotes not computable
    from this table (every quote carries a final status); the typed lane read
    "pipeline value" as the quoted-value metric and dropped "open". The
    readings rebuild the open book by date: quotes sent on or before a day and
    decided after it, today vs end of August, or end of August vs end of July.

    Failure mode to carry: a declared not-computable measure served anyway
    through a metric alias; a month-to-date compared with a complete month,
    no gap stated."""
    resp = ask(
        "total open pipeline value this month compared to last month",
        LENS,
        ["quotes"],
        world,
        [
            ("shape", "aggregate", 0.98),
            ("metric", "quoted_value_eur", 0.31),
            ("dimension", None, 0.99),
            ("filter", "status", 0.67),
            ("filter_value", "unstated", 0.65),
            ("filter", None, 1.0),
        ],
    )

    def open_at(day: str) -> float:
        return round(
            float(
                world.scalar(
                    f"SELECT COALESCE(SUM(quote_value_eur), 0) FROM {QUOTES} "
                    f"WHERE sent_date <= '{day}' AND decided_date > '{day}'"
                )
            ),
            2,
        )

    readings = [
        [open_at("2026-09-01"), open_at("2026-08-31")],
        [open_at("2026-08-31"), open_at("2026-07-31")],
    ]
    served = None
    if resp.status == "ok":
        (row,) = served_rows(resp)
        served = [row[0], row[1]]
    assert_reading_or_non_answer(resp, served, readings, what="open pipeline this month vs last")


Q08_HTTP = [
    ("shape", "aggregate", 1.0),
    ("metric", "crm_lead_count", 0.78),
    ("dimension", "source", 0.77),
    ("filter", "source", 0.82),
    ("dimension", None, 0.96),
    ("filter_value", "Property manager", 0.55),
    ("filter", "segment", 0.68),
    ("filter_value", "Housing companies", None),
    ("filter", None, 0.97),
]
Q08 = "how many leads came from housing companies year to date"


def _housing_leads_ytd(world: World) -> float:
    return float(
        world.scalar(
            f"SELECT count(*) FROM {LEADS} WHERE segment = '{HC}' "
            "AND created_date BETWEEN '2026-01-01' AND '2026-09-01'"
        )
    )


def test_q08_leads_from_housing_companies_are_not_also_filtered_by_a_source(world: World) -> None:
    """Asked "how many leads came from housing companies year to date" came back as
    the housing-company leads whose SOURCE is 'Property manager' — a value the
    question never names, picked at p 0.55 and acted on — with no window at all
    on the HTTP door. The reading is every lead in the housing-companies
    segment created this year.

    Failure mode to carry: an unasked filter (a stored value the question does
    not name, applied as a restriction); a stated period dropped."""
    resp = ask(Q08, LENS, ["crm_leads"], world, Q08_HTTP)
    served = served_rows(resp)[0][0] if resp.status == "ok" else None
    assert_reading_or_non_answer(
        resp, served, [_housing_leads_ytd(world)], what="housing-company leads YTD"
    )


def test_rule_no_filter_value_absent_from_the_question_is_applied(world: World) -> None:
    """The rule behind Q08 and Q41: a filter value the question does not name,
    and that neither the lens nor the entity's population pins, may not be
    applied. Every filter on the answer's resolution ledger must be a value
    spelled in the question (a named value) — otherwise the answer is a
    clarification, never a served figure."""
    resp = ask(Q08, LENS, ["crm_leads"], world, Q08_HTTP)
    if resp.status != "ok":
        return
    assert resp.resolution is not None
    asked = Q08.lower()
    unasked = [
        slot.value
        for slot in resp.resolution.slots
        if slot.kind == "filter" and slot.value and _literal(slot.value).lower() not in asked
    ]
    assert not unasked, f"filters the question never asked for: {unasked} — {describe(resp)}"


def _literal(predicate: str) -> str:
    """The quoted value of a ``column = 'value'`` slot, else the slot itself."""
    _, _, tail = predicate.partition("=")
    return tail.strip().strip("'") if tail else predicate


# ── the same questions through the cube lane ─────────────────────────────────

DECIDED_YTD = {"dimension": "quotes.decided_date", "dateRange": ["2026-01-01", "2026-08-31"]}
RATE_LEDGER = [
    ("win rate", "member", "quotes.quote_win_rate"),
    ("segment", "member", "quotes.segment"),
    ("year to date", "period", "2026-01-01..2026-08-31"),
]


def _cube_rates(resp) -> dict[str, float] | None:  # type: ignore[no-untyped-def]
    if resp.status != "ok":
        return None
    segments, rates = column(resp, "quotes.segment"), column(resp, "quotes.quote_win_rate")
    return {str(s): round(float(r) * 100, 1) for s, r in zip(segments, rates, strict=True)}


def test_q03_on_the_cube_lane(world: World) -> None:
    """Year to date read as a window, on the decision date the ratio counts by."""
    resp = ask_cube(
        "q03",
        "what's our win rate by segment year to date",
        [
            {
                "measures": ["quotes.quote_win_rate"],
                "dimensions": ["quotes.segment"],
                "timeDimensions": [DECIDED_YTD],
            }
        ],
        RATE_LEDGER,
        world,
        lens=LENS,
    )
    readings = [_win_rates(world, s, e, expired_counts=x) for s, e, x in Q03_READINGS]
    assert_reading_or_non_answer(resp, _cube_rates(resp), readings, what="win rate by segment YTD")


def test_q03_through_a_named_day_on_the_cube_lane(world: World) -> None:
    """ "Through 2026-09-01" closes the window at the end, never at both ends."""
    through = {"dimension": "quotes.decided_date", "dateRange": ["2026-01-01", "2026-09-01"]}
    resp = ask_cube(
        "q03_through",
        "What is our win rate by segment year to date (through 2026-09-01)?",
        [
            {
                "measures": ["quotes.quote_win_rate"],
                "dimensions": ["quotes.segment"],
                "timeDimensions": [through],
            }
        ],
        [
            *RATE_LEDGER[:2],
            ("year to date (through 2026-09-01)", "period", "2026-01-01..2026-09-01"),
        ],
        world,
        lens=LENS,
    )
    readings = [_win_rates(world, s, e, expired_counts=x) for s, e, x in Q03_READINGS]
    assert_reading_or_non_answer(resp, _cube_rates(resp), readings, what="win rate through a day")


def test_w1_on_the_cube_lane(world: World) -> None:
    """Both quarters by decision date, the current one clipped to the clock so
    no decision dated after today is counted."""
    resp = ask_cube(
        "w1",
        "why did our win rate drop this quarter compared to last quarter",
        [
            {
                "measures": ["quotes.quote_win_rate"],
                "timeDimensions": [
                    {
                        "dimension": "quotes.decided_date",
                        "compareDateRange": [
                            ["2026-07-01", "2026-09-30"],
                            ["2026-04-01", "2026-06-30"],
                        ],
                    }
                ],
            }
        ],
        [
            ("win rate", "member", "quotes.quote_win_rate"),
            ("this quarter", "period", "2026-07-01..2026-09-30"),
            ("last quarter", "period", "2026-04-01..2026-06-30"),
        ],
        world,
        lens=LENS,
    )

    def rate(start: str, end: str, expired_counts: bool) -> float:
        denominator = (
            "count(*)" if expired_counts else "count(*) FILTER (WHERE status IN ('Won', 'Lost'))"
        )
        return round(
            float(
                world.scalar(
                    f"SELECT 100.0 * count(*) FILTER (WHERE status = 'Won') / {denominator} "
                    f"FROM {QUOTES} WHERE decided_date BETWEEN '{start}' AND '{end}'"
                )
            ),
            1,
        )

    readings = [
        [rate("2026-07-01", q3_end, x), rate("2026-04-01", "2026-06-30", x)]
        for q3_end in ("2026-08-31", "2026-09-01")
        for x in (False, True)
    ]
    served = None
    if resp.status == "ok":
        assert resp.data is not None
        (row,) = resp.data.rows
        served = [round(float(row[0]) * 100, 1), round(float(row[1]) * 100, 1)]
    assert_reading_or_non_answer(resp, served, readings, what="win rate this quarter vs last")


def test_q04_on_the_cube_lane(world: World) -> None:
    """The quoted value read for "open pipeline": nothing in the query carries
    "open", so the word check asks instead of serving the value quoted."""
    resp = ask_cube(
        "q04",
        "total open pipeline value this month compared to last month",
        [
            {
                "measures": ["quotes.quoted_value_eur"],
                "timeDimensions": [
                    {
                        "dimension": "quotes.sent_date",
                        "compareDateRange": [
                            ["2026-09-01", "2026-09-30"],
                            ["2026-08-01", "2026-08-31"],
                        ],
                    }
                ],
            }
        ],
        [
            ("pipeline value", "member", "quotes.quoted_value_eur"),
            ("open", "none", ""),
            ("this month", "period", "2026-09"),
            ("last month", "period", "2026-08"),
        ],
        world,
        lens=LENS,
    )

    def open_at(day: str) -> float:
        return round(
            float(
                world.scalar(
                    f"SELECT COALESCE(SUM(quote_value_eur), 0) FROM {QUOTES} "
                    f"WHERE sent_date <= '{day}' AND decided_date > '{day}'"
                )
            ),
            2,
        )

    readings = [
        [open_at("2026-09-01"), open_at("2026-08-31")],
        [open_at("2026-08-31"), open_at("2026-07-31")],
    ]
    served = None
    if resp.status == "ok":
        (row,) = served_rows(resp)
        served = [row[0], row[1]]
    assert_reading_or_non_answer(resp, served, readings, what="open pipeline this month vs last")


def test_q08_on_the_cube_lane(world: World) -> None:
    """The segment the question names, no source it does not, this year."""
    resp = ask_cube(
        "q08",
        Q08,
        [
            {
                "measures": ["crm_leads.crm_lead_count"],
                "filters": [{"member": "crm_leads.segment", "operator": "equals", "values": [HC]}],
                "timeDimensions": [
                    {
                        "dimension": "crm_leads.created_date",
                        "dateRange": ["2026-01-01", "2026-08-31"],
                    }
                ],
            }
        ],
        [
            ("leads", "member", "crm_leads.crm_lead_count"),
            ("housing companies", "value", HC),
            ("year to date", "period", "2026-01-01..2026-08-31"),
        ],
        world,
        lens=LENS,
    )
    served = served_rows(resp)[0][0] if resp.status == "ok" else None
    assert_reading_or_non_answer(
        resp, served, [_housing_leads_ytd(world)], what="housing-company leads YTD"
    )
