"""Additive expansion of fixtures/jaffle_shop.duckdb — complexity without
touching the teaching core.

customers/orders (and the raw/stg layers) stay byte-identical: every pinned
baseline (19 repeat customers, CLV numbers) survives. The expansion adds a
coherent later chapter of the same business — the jaffle shop went online and
sells subscriptions — with the messiness deep probes need:

- web_sessions: high-volume fan-out (customer 1:many), anonymous rows
  (customer_id NULL), TIMESTAMP granularity, booleans, enums.
- subscriptions: lifecycle NULLs (canceled_on), plan tiers, MRR.
- invoices: MULTI-CURRENCY amounts (summing across currencies is the trap a
  'revenue' definition must resolve), status enum, nullable paid_at.
- refunds: POSITIVE amounts with negative semantics (net-revenue judgment).
- support_tickets: nullable closed_at/csat, severity enum.

Deterministic (seeded); rerunning replaces exactly these five tables.
Run: uv run python scripts/gen_jaffle_expansion.py
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import duckdb

DB = Path(__file__).resolve().parents[1] / "fixtures" / "jaffle_shop.duckdb"
R = random.Random(4242)

EPOCH = dt.date(2023, 1, 1)
DAYS = 1150  # ~2023-01 .. 2026-02

PLANS = [("starter", 29.0), ("growth", 99.0), ("scale", 299.0), ("enterprise", 899.0)]
CHANNELS = ["organic", "paid_search", "social", "email", "referral", "direct"]
DEVICES = ["desktop", "mobile", "tablet"]
COUNTRIES = ["FI", "SE", "DE", "US", "GB", "NL", "FR", "EE"]
CURRENCIES = ["EUR", "EUR", "EUR", "USD", "USD", "GBP"]  # weighted
SEVERITIES = ["low", "medium", "high", "urgent"]
REFUND_REASONS = ["billing_error", "churn_goodwill", "outage_credit", "duplicate_charge"]


def _day(offset: int) -> dt.date:
    return EPOCH + dt.timedelta(days=offset)


def _ts(offset: int) -> dt.datetime:
    return dt.datetime.combine(_day(offset), dt.time(R.randrange(6, 23), R.randrange(60)))


def build() -> None:
    con = duckdb.connect(str(DB))
    core = {
        t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("customers", "orders")
    }

    # ── subscriptions (~800: existing 100 customers + 700 online-era ids) ────
    subs = []
    for sid in range(1, 801):
        customer = R.randrange(1, 801)  # ids 101-800 exist only here — join honesty
        plan, base = PLANS[min(int(R.expovariate(1.6)), 3)]
        seats = 1 + min(int(R.expovariate(0.55)), 24)
        start = R.randrange(0, DAYS - 30)
        canceled = None
        if R.random() < 0.38:
            canceled = start + R.randrange(30, max(31, DAYS - start))
            canceled = min(canceled, DAYS - 1)
        subs.append(
            (
                sid,
                customer,
                plan,
                round(base * seats * R.uniform(0.85, 1.0), 2),
                seats,
                _day(start),
                _day(canceled) if canceled else None,
            )
        )
    con.execute("DROP TABLE IF EXISTS subscriptions")
    con.execute(
        "CREATE TABLE subscriptions (sub_id INTEGER, customer_id INTEGER, plan VARCHAR, "
        "mrr DOUBLE, seats INTEGER, started_on DATE, canceled_on DATE)"
    )
    con.executemany("INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?)", subs)

    # ── invoices (~monthly per active sub, multi-currency, some unpaid) ──────
    invoices = []
    iid = 1
    for sid, _cust, _plan, mrr, _seats, started_on, canceled_on in subs:
        cur = R.choice(CURRENCIES)
        fx = {"EUR": 1.0, "USD": 1.08, "GBP": 0.86}[cur]
        start_off = (started_on - EPOCH).days
        end_off = (canceled_on - EPOCH).days if canceled_on else DAYS
        for month_start in range(start_off, end_off, 30):
            status = R.choices(["paid", "paid", "paid", "paid", "overdue", "void"], k=1)[0]
            paid_at = (
                _ts(min(month_start + R.randrange(0, 21), DAYS - 1)) if status == "paid" else None
            )
            invoices.append(
                (
                    iid,
                    sid,
                    _day(month_start),
                    _day(min(month_start + 30, DAYS - 1)),
                    round(mrr * fx * R.uniform(0.98, 1.02), 2),
                    cur,
                    status,
                    paid_at,
                )
            )
            iid += 1
    con.execute("DROP TABLE IF EXISTS invoices")
    con.execute(
        "CREATE TABLE invoices (invoice_id INTEGER, sub_id INTEGER, period_start DATE, "
        "period_end DATE, amount DOUBLE, currency VARCHAR, status VARCHAR, paid_at TIMESTAMP)"
    )
    con.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?)", invoices)

    # ── refunds (positive amounts, negative meaning) ─────────────────────────
    paid = [inv for inv in invoices if inv[6] == "paid"]
    refunds = []
    for rid, inv in enumerate(R.sample(paid, min(320, len(paid))), start=1):
        refunds.append(
            (
                rid,
                inv[0],
                round(inv[4] * R.uniform(0.2, 1.0), 2),
                R.choice(REFUND_REASONS),
                _ts(min((inv[2] - EPOCH).days + R.randrange(3, 45), DAYS - 1)),
            )
        )
    con.execute("DROP TABLE IF EXISTS refunds")
    con.execute(
        "CREATE TABLE refunds (refund_id INTEGER, invoice_id INTEGER, amount DOUBLE, "
        "reason VARCHAR, refunded_at TIMESTAMP)"
    )
    con.executemany("INSERT INTO refunds VALUES (?,?,?,?,?)", refunds)

    # ── web_sessions (~50k, fan-out + anonymous) ─────────────────────────────
    sessions = []
    for sid in range(1, 50_001):
        known = R.random() < 0.55
        sessions.append(
            (
                sid,
                R.randrange(1, 801) if known else None,
                _ts(R.randrange(0, DAYS)),
                R.choice(CHANNELS),
                R.choice(DEVICES),
                R.choice(COUNTRIES),
                max(5, int(R.expovariate(1 / 240))),
                known and R.random() < 0.11,
            )
        )
    con.execute("DROP TABLE IF EXISTS web_sessions")
    con.execute(
        "CREATE TABLE web_sessions (session_id INTEGER, customer_id INTEGER, "
        "started_at TIMESTAMP, channel VARCHAR, device VARCHAR, country VARCHAR, "
        "duration_s INTEGER, converted BOOLEAN)"
    )
    con.executemany("INSERT INTO web_sessions VALUES (?,?,?,?,?,?,?,?)", sessions)

    # ── support_tickets (~2k, lifecycle NULLs) ───────────────────────────────
    tickets = []
    for tid in range(1, 2_001):
        opened = R.randrange(0, DAYS)
        closed = None if R.random() < 0.18 else min(opened + R.randrange(0, 21), DAYS - 1)
        tickets.append(
            (
                tid,
                R.randrange(1, 801),
                _ts(opened),
                _ts(closed) if closed is not None else None,
                R.choice(SEVERITIES),
                R.randrange(1, 6) if closed is not None and R.random() < 0.6 else None,
            )
        )
    con.execute("DROP TABLE IF EXISTS support_tickets")
    con.execute(
        "CREATE TABLE support_tickets (ticket_id INTEGER, customer_id INTEGER, "
        "opened_at TIMESTAMP, closed_at TIMESTAMP, severity VARCHAR, csat INTEGER)"
    )
    con.executemany("INSERT INTO support_tickets VALUES (?,?,?,?,?,?)", tickets)

    after = {
        t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("customers", "orders")
    }
    assert after == core, f"core tables changed: {core} -> {after}"
    for t in ("subscriptions", "invoices", "refunds", "web_sessions", "support_tickets"):
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"{t}: {n} rows")
    con.close()
    print(f"expanded {DB} (core untouched: {core})")


if __name__ == "__main__":
    build()
