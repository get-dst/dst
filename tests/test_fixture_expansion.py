"""The jaffle fixture's expansion chapter: complexity is ADDITIVE.

The teaching core (customers/orders — every pinned baseline, the 19) must
never change; the online-era tables carry the governance traps deep probes
exercise (multi-currency, anonymous fan-out, lifecycle NULLs, positive-sign
refunds). Regenerate with scripts/gen_jaffle_expansion.py.
"""

from __future__ import annotations

import duckdb

from services.config import settings


def test_core_baselines_and_expansion_traps() -> None:
    con = duckdb.connect(settings.duckdb_jaffle_path, read_only=True)
    assert con.execute("SELECT count(*) FROM customers").fetchone()[0] == 100
    assert con.execute("SELECT count(*) FROM orders").fetchone()[0] == 99
    assert (
        con.execute("SELECT count(*) FROM customers WHERE number_of_orders > 1").fetchone()[0] == 19
    )
    for table, at_least in (
        ("subscriptions", 500),
        ("invoices", 5_000),
        ("refunds", 200),
        ("web_sessions", 20_000),
        ("support_tickets", 1_000),
    ):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= at_least, table
    # the traps that make definitions worth arguing about
    currencies = {r[0] for r in con.execute("SELECT DISTINCT currency FROM invoices").fetchall()}
    assert len(currencies) >= 3  # summing amount across these is the wrong answer
    assert (
        con.execute("SELECT count(*) FROM web_sessions WHERE customer_id IS NULL").fetchone()[0] > 0
    )  # anonymous rows
    assert con.execute("SELECT min(amount) FROM refunds").fetchone()[0] > 0  # positive sign
    assert (
        con.execute("SELECT count(*) FROM support_tickets WHERE closed_at IS NULL").fetchone()[0]
        > 0
    )  # lifecycle NULLs
