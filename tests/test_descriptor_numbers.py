"""Descriptor numbers in prose are not claimed figures.

A number that DESCRIBES the window, grain, or ranking — "the last 30 days",
"a 7-day rolling average", "the top 5 regions" — is normal, desirable prose.
Treating them as claims means numeric_grounding fails to find them in the
result and withholds correct answers whole: an account count suppressed for
mentioning its own 30-day window. The guarantee must not weaken: invented
figures still fail, and near-miss figures are reconciled to the row's value.
"""

from __future__ import annotations

from services.contracts.warehouse import QueryResult
from services.runtime import faithfulness as f


def _r(columns: list[str], *cells: object) -> QueryResult:
    return QueryResult(columns=columns, rows=[list(cells)])


def test_a_window_length_in_prose_is_not_a_claimed_figure() -> None:
    r = _r(["marketplace_partner_accounts_30d"], 12770)
    status, reason = f.numeric_check("12,770 accounts were active in the last 30 days.", r)
    assert status == "pass", reason


def test_a_glued_short_unit_is_a_descriptor_too() -> None:
    # "(30d)" — the column-name echo idiom. NOTE: identifier digits themselves
    # never ground (q4_revenue must not bless a claim of 4 — pinned in
    # test_grounding_adversarial); it is the unit suffix that marks this a
    # descriptor, not the column name.
    r = _r(["accounts_30d"], 12770)
    assert f.numeric_check("Active accounts (30d): 12,770.", r)[0] == "pass"


def test_a_date_in_prose_is_not_a_claimed_figure() -> None:
    r = _r(["arr"], 88137195.61)
    status, reason = f.numeric_check(
        "As of 2026-08-20, ARR was 88,137,195.61.", r, clock_years=(2025, 2026)
    )
    assert status == "pass", reason


def test_a_rank_in_prose_is_not_a_claimed_figure() -> None:
    r = _r(["orders"], 1234)
    assert f.numeric_check("The top 5 regions account for 1,234 orders.", r)[0] == "pass"


def test_a_rolling_window_is_not_a_claimed_figure() -> None:
    r = _r(["avg7"], 42.5)
    assert f.numeric_check("The 7-day rolling average was 42.5.", r)[0] == "pass"


def test_a_genuinely_invented_figure_still_fails() -> None:
    r = _r(["rev"], 20620095.35)
    status, _ = f.numeric_check("Revenue was 99,000,000 last month.", r)
    assert status == "fail"


def test_a_transposed_digit_is_reconciled_to_the_row_value() -> None:
    """12,707 for the cell 12770 sits inside the rounding tolerance, so the
    deterministic reconcile pass rewrites it to the cell's own rendering —
    stronger than failing: the caller reads the RIGHT figure."""
    r = _r(["accounts_30d"], 12770)
    out = f.reconcile("12,707 accounts were active in the last 30 days.", r)
    assert "12,770" in out and "12,707" not in out


def test_descriptor_numbers_are_never_rewritten() -> None:
    # The 30 must not be "reconciled" into some nearby cell value.
    r = _r(["n"], 29)
    out = f.reconcile("Active accounts in the last 30 days: 29.", r)
    assert "last 30 days" in out


def test_a_separator_figure_before_a_duration_noun_still_claims() -> None:
    # "1,234 days of history" is a real figure, not a window descriptor.
    r = _r(["n"], 99)
    assert f.numeric_check("We hold 1,234 days of history.", r)[0] == "fail"
