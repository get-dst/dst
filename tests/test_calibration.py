"""Calibration report: the regime is measured, and what was not measured says so."""

from __future__ import annotations

import pytest

from services.evals import calibration as cal


def _rec(p: float | None, correct: bool, decider: str = "v") -> cal.GradedDecision:
    return cal.GradedDecision(
        decider=decider, slot="lens", chosen="a", gold="a" if correct else "b", p=p
    )


def test_brier_and_ece_on_a_hand_computed_set() -> None:
    # Four decisions at p=0.8, three correct: Brier = (3·0.04 + 0.64)/4 = 0.19;
    # one bin, conf 0.8, acc 0.75 → ECE = 0.05.
    rs = [_rec(0.8, True), _rec(0.8, True), _rec(0.8, True), _rec(0.8, False)]
    assert cal.brier(rs) == pytest.approx(0.19)
    assert cal.ece(rs) == pytest.approx(0.05)
    (b,) = cal.bins(rs)
    assert (b["n"], b["conf"], b["acc"]) == (4, pytest.approx(0.8), 0.75)


def test_coverage_curve_reports_what_a_threshold_buys_and_costs() -> None:
    rs = [_rec(0.95, True), _rec(0.7, False), _rec(0.65, True), _rec(0.3, False)]
    curve = {c["threshold"]: c for c in cal.coverage_curve(rs, thresholds=(0.6, 0.9))}
    assert curve[0.6]["coverage"] == 0.75 and curve[0.6]["error_among_acted"] == pytest.approx(
        1 / 3
    )
    assert curve[0.9]["coverage"] == 0.25 and curve[0.9]["error_among_acted"] == 0.0


def test_unmeasured_and_thin_cells_never_print_a_number() -> None:
    rep = cal.report([_rec(None, True, "k1"), _rec(None, False, "k1")])
    assert rep["k1 [lens]"]["status"] == "UNTESTED" and "brier" not in rep["k1 [lens]"]
    assert rep["k1 [lens]"]["accuracy"] == 0.5  # accuracy needs no probability
    thin = cal.report([_rec(0.9, True, "k5")] * 5)
    assert thin["k5 [lens]"]["status"] == "n too small" and "ece" not in thin["k5 [lens]"]
    full = cal.report([_rec(0.9, True, "k5")] * cal.MIN_N)
    assert full["k5 [lens]"]["status"] == "measured"
    assert full["k5 [lens]"]["ece"] == pytest.approx(0.1)


def test_report_keeps_slots_apart() -> None:
    """The same voting model routing lenses and gating paraphrases are two
    calibrations — merged, one hides the other."""
    lens = cal.GradedDecision(decider="v", slot="lens", chosen="a", gold="a", p=1.0)
    equiv = cal.GradedDecision(
        decider="v", slot="certified_equivalent", chosen="a", gold="b", p=1.0
    )
    rep = cal.report([lens] * 20 + [equiv] * 20)
    assert rep["v [lens]"]["accuracy"] == 1.0
    assert rep["v [certified_equivalent]"]["accuracy"] == 0.0


def test_format_report_says_untested_for_nothing() -> None:
    assert cal.format_report({}) == "calibration: UNTESTED — no decisions recorded"
    text = cal.format_report(cal.report([_rec(None, True, "k1")]))
    assert "k1" in text and "UNTESTED" in text
    text = cal.format_report(cal.report([_rec(0.9, True, "k5")] * cal.MIN_N))
    assert "brier=" in text and "act at p>=0.90" in text
