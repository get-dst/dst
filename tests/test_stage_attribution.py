"""Stage attribution: a wrong answer names the stage that broke.

Offline: these tests pin the ATTRIBUTION machinery (ordering, fail-loud
unattributed, the routing verdict, the prose signature), not any lane's
ability to answer.
"""

from services.evals.grading import (
    first_failed_stage,
    prose_signature,
    stage_statuses,
)

# --- the classifier ---------------------------------------------------------


def test_resolution_sits_between_routing_and_rows():
    """The ledger lane grades MEANING before rows grade values: a case that
    resolved to the wrong metric and produced wrong rows is a resolution
    miss first — the rows are wrong because the meaning was."""
    from services.evals.grading import STAGE_OWNERS, STAGES

    assert STAGES == ("routing", "resolution", "rows", "grounding")
    assert "resolution" in STAGE_OWNERS
    both = {"resolution": "failed", "rows": "failed"}
    assert first_failed_stage(both, "wrong") == "resolution"
    assert (
        stage_statuses(rows_correct=True, delivered=True, grounding=None)["resolution"] == "skipped"
    )
    assert (
        stage_statuses(rows_correct=True, delivered=True, grounding=None, resolution="failed")[
            "resolution"
        ]
        == "failed"
    )


def test_first_failed_stage_picks_pipeline_order_and_fails_loud():
    both = {"routing": "failed", "rows": "failed", "grounding": "skipped"}
    assert first_failed_stage(both, "wrong") == "routing"
    assert first_failed_stage({"rows": "failed"}, "wrong") == "rows"
    # not wrong → no tag: a decline is an outcome, not an error
    assert first_failed_stage(both, "correct") is None
    assert first_failed_stage(both, "declined") is None
    # wrong with no failed stage: counted loudly, never dropped
    all_clean = {"routing": "skipped", "rows": "passed", "grounding": "passed"}
    assert first_failed_stage(all_clean, "wrong") == "unattributed"


def test_pinned_lens_reports_routing_skipped_never_passed():
    pinned = stage_statuses(
        rows_correct=True, delivered=True, grounding=None, expected_lens="finance", served_lens=None
    )
    assert pinned["routing"] == "skipped"
    routed = stage_statuses(
        rows_correct=True,
        delivered=True,
        grounding=None,
        expected_lens="finance",
        served_lens="finance",
    )
    assert routed["routing"] == "passed"
    mis = stage_statuses(
        rows_correct=True,
        delivered=True,
        grounding=None,
        expected_lens="finance",
        served_lens="sales",
    )
    assert mis["routing"] == "failed"
    # no verification ran → grounding is untested, not clean
    assert pinned["grounding"] == "skipped"


def test_prose_signature_is_claims_not_wording():
    a = prose_signature("Revenue was 1,500 EUR across 3 orders.")
    b = prose_signature("Across 3 orders the revenue reached 1500 euros.")
    assert a == b  # same claims, free wording — thousands separator normalized
    assert prose_signature("Revenue was 1,600 EUR across 3 orders.") != a
    assert prose_signature(None) is None
    assert prose_signature("No numbers here.") == "∅"
    # one scanner, two callers: the fi-FI spelling signs like the en-US one
    assert prose_signature("Saldo on 195 419,94 EUR.") == prose_signature(
        "Balance is 195,419.94 EUR."
    )
