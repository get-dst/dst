"""Decimal cells are first-class numerics in the faithfulness layer.

Warehouse drivers return decimal.Decimal for exact-numeric columns — BigQuery
NUMERIC, i.e. the money columns. An `int | float` isinstance check silently
excludes them, so every prose answer over a NUMERIC money column has NOTHING
to ground against: exact claims fail, reconcile cannot rewrite, the prose is
withheld, and the failure message's `{value:g}` rendering reports the claim as
scientific notation the composer never wrote.
"""

from __future__ import annotations

from decimal import Decimal

from services.contracts.warehouse import QueryResult
from services.runtime import faithfulness as f
from services.runtime import verification


def _r(*cells: object) -> QueryResult:
    return QueryResult(columns=[f"c{i}" for i in range(len(cells))], rows=[list(cells)])


def test_an_exact_claim_grounds_against_a_decimal_cell() -> None:
    status, _ = f.numeric_check("GBV was 20,620,095.35 USD.", _r(Decimal("20620095.35")))
    assert status == "pass"


def test_a_rounded_sci_notation_claim_reconciles_to_the_exact_rendering() -> None:
    r = _r(Decimal("20620095.35"))
    assert f.numeric_check("GBV was 2.06201e+07 USD.", r)[0] == "pass"
    assert f.reconcile("GBV was 2.06201e+07 USD.", r) == "GBV was 20,620,095.35 USD."


def test_prose_grounds_for_all_three_reported_figures() -> None:
    for figure in ("20620095.35", "522656131.0", "165626982.22"):
        status, reason = f.numeric_check(
            f"The total was {float(Decimal(figure)):,.2f}.", _r(Decimal(figure))
        )
        assert status == "pass", (figure, reason)


def test_a_wrong_claim_over_a_decimal_cell_still_fails() -> None:
    status, reason = f.numeric_check("GBV was 99,999,999 USD.", _r(Decimal("20620095.35")))
    assert status == "fail"
    # The message quotes the claim AS WRITTEN — never a {value:g} rendering
    # that puts scientific notation in the composer's mouth.
    assert reason is not None and "99,999,999" in reason and "e+0" not in reason


def test_the_rendering_of_a_decimal_is_never_exponential() -> None:
    assert f._rendered(Decimal("20620095.35")) == "20,620,095.35"
    assert f._rendered(Decimal("522656131")) == "522,656,131"
    assert f._rendered(Decimal("0.000012")) == "0.000012"


def test_decimal_column_totals_ground() -> None:
    r = QueryResult(columns=["v"], rows=[[Decimal("100.50")], [Decimal("200.25")]])
    assert f.numeric_check("The two channels total 300.75.", r)[0] == "pass"


def test_zero_ratio_check_sees_decimal_zeros() -> None:
    r = QueryResult(columns=["ratio"], rows=[[Decimal("0")]])
    check = verification._zero_ratio_plausibility("SELECT SAFE_DIVIDE(a, b) FROM t", r)
    assert check.status == "fail"  # an all-zero ratio row is the implausible shape
