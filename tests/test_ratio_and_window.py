"""Check rails: ratio_population and the latest-snapshot-pin refinement of
window_applied.

A ratio whose denominator filters on the very column the numerator
aggregates unfiltered mixes populations — the served figure is an order of
magnitude off, with identical SQL across model tiers. A 'last month'
question answered from `snapshot_date = MAX(snapshot_date)` PASSED
window_applied — one snapshot day narrated as a calendar month, zero failing
checks.
"""

from __future__ import annotations

from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.runtime.verification import (
    _intent_alignment,
    _ratio_population,
    _window_applied,
    ratio_population_mismatch,
)

_BAD_RATIO = (
    "SELECT CAST(SUM(t.weekly_quota_amount) AS FLOAT64) "
    "/ NULLIF(COUNT(DISTINCT CASE WHEN t.weekly_quota_amount > 0 THEN t.rep_id END), 0) "
    "AS avg_rep_weekly_quota FROM proj.marts.t AS t WHERE t.week_start >= '2026-07-01'"
)


def test_the_measured_mixed_population_ratio_fails() -> None:
    check = _ratio_population(_BAD_RATIO, "bigquery")
    assert check.status == "fail"
    assert check.reason is not None and "weekly_quota_amount > 0" in check.reason


def test_a_consistent_ratio_passes() -> None:
    sql = (
        "SELECT SUM(CASE WHEN t.x > 0 THEN t.x END) "
        "/ NULLIF(COUNT(DISTINCT CASE WHEN t.x > 0 THEN t.id END), 0) FROM t"
    )
    assert _ratio_population(sql, "bigquery").status == "pass"


def test_share_of_total_is_a_legitimate_asymmetry() -> None:
    sql = "SELECT SUM(CASE WHEN t.paid THEN t.amt END) / SUM(t.amt) FROM t"
    assert _ratio_population(sql, "bigquery").status == "pass"


def test_a_denominator_filtered_on_a_different_column_passes() -> None:
    # 'total revenue per active rep' — the filter is on is_active, not on the
    # numerator's aggregated column; plausible intent, never flagged.
    sql = "SELECT SUM(t.revenue) / COUNT(DISTINCT CASE WHEN t.is_active THEN t.rep_id END) FROM t"
    assert _ratio_population(sql, "bigquery").status == "pass"


def test_the_mismatch_names_the_predicate_for_the_repair_prompt() -> None:
    reason = ratio_population_mismatch(_BAD_RATIO, "bigquery")
    assert reason is not None and "same predicate" in reason


# ── #53: the latest-snapshot pin is not a calendar period ─────────────────────


def _model() -> SemanticModel:
    return SemanticModel(
        lens="laundry",
        dialect="bigquery",
        entities=[
            Entity(
                name="store_kpis",
                source=EntitySource(connection="c", table="p.d.store_kpis"),
                fields=[
                    Field(name="refunds_eur", type="number"),
                    Field(name="snapshot_date", type="date"),
                ],
            )
        ],
    )


_PIN_SQL = (
    "SELECT SUM(store_kpis.refunds_eur) AS c FROM p.d.store_kpis AS store_kpis "
    "WHERE store_kpis.snapshot_date = "
    "(SELECT MAX(store_kpis.snapshot_date) FROM p.d.store_kpis AS store_kpis)"
)


def test_a_period_question_answered_from_a_snapshot_pin_fails() -> None:
    check = _window_applied("How much was refunded last month, chain-wide?", _PIN_SQL, _model())
    assert check.status == "fail"
    assert check.reason is not None and "snapshot" in check.reason


def test_a_real_window_still_passes() -> None:
    sql = (
        "SELECT SUM(t.refunds_eur) FROM p.d.store_kpis AS t "
        "WHERE t.status_date >= DATE_TRUNC(DATE_SUB(CURRENT_DATE, INTERVAL 1 MONTH), MONTH)"
    )
    assert _window_applied("How much was refunded last month?", sql, _model()).status == "pass"


def test_a_current_question_keeps_the_pin_legal() -> None:
    # 'current refunds' has no period term — the latest snapshot IS the right read.
    assert _window_applied("What are our current refunds?", _PIN_SQL, _model()).status == "skip"


def test_intent_alignment_owns_its_own_narrowness() -> None:
    # the audit: the skip reason blamed an explicit question for the check's
    # narrow vocabulary ("intent not determinable from the question").
    check = _intent_alignment("How much was refunded last month, chain-wide?", _PIN_SQL, "bigquery")
    assert check.status == "skip"
    assert check.reason is not None and "vocabulary" in check.reason
    assert "not determinable" not in check.reason
