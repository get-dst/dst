"""A share, a delta or a ratio the question asked for grounds when it is exactly
one division or subtraction of two result values; without the ask, or when the
arithmetic is off, nothing changes."""

from __future__ import annotations

from decimal import Decimal

from services.contracts.warehouse import QueryResult
from services.runtime import faithfulness as f

_TWO = QueryResult(columns=["month", "invoiced"], rows=[["Aug", Decimal("800")], ["Sep", 1000]])
_SHARE = QueryResult(
    columns=["region", "revenue"], rows=[["north", 250], ["south", 150], ["east", 100]]
)


def test_a_share_the_question_asked_for_grounds_and_names_the_division() -> None:
    status, reason = f.numeric_check(
        "North is 50.0% of revenue.", _SHARE, question_text="what is each region's share?"
    )
    assert status == "pass"
    assert reason == "50.0% = 250 / 500"


def test_a_delta_the_question_asked_for_grounds_as_a_subtraction() -> None:
    status, reason = f.numeric_check(
        "September was 200 higher than August.",
        _TWO,
        question_text="what is the difference between August and September?",
    )
    assert status == "pass"
    assert reason == "200 = 1000 - 800"


def test_a_growth_rate_grounds_in_percent_space() -> None:
    status, reason = f.numeric_check(
        "Invoicing grew by 25% in September.",
        _TWO,
        question_text="what was the change from August to September?",
    )
    assert status == "pass"
    assert reason == "25% = (1000 - 800) / 800"


def test_a_ratio_grounds_as_a_plain_division() -> None:
    status, reason = f.numeric_check(
        "The ratio is 1.25.", _TWO, question_text="what is the ratio of September to August?"
    )
    assert status == "pass"
    assert reason == "1.25 = 1000 / 800"


def test_arithmetic_off_by_the_printed_precision_still_fails() -> None:
    status, reason = f.numeric_check(
        "North is 50.3% of revenue.", _SHARE, question_text="what is each region's share?"
    )
    assert status == "fail"
    assert reason is not None and "50.3" in reason


def test_without_the_ask_a_derived_share_stays_ungrounded() -> None:
    status, _ = f.numeric_check("North is 50.0% of revenue.", _SHARE, question_text="revenue?")
    assert status == "fail"


def test_a_truncated_window_never_grounds_a_derivation() -> None:
    status, _ = f.numeric_check(
        "North is 50.0% of revenue.",
        _SHARE,
        question_text="what is each region's share?",
        truncated=True,
    )
    assert status == "fail"
