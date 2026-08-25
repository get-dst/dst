"""Stage attribution: a wrong answer names the stage that broke.

Offline, stub lanes only: these tests pin the ATTRIBUTION machinery (ordering,
fail-loud unattributed, the grounding flip, determinism decomposition), not any
lane's ability to answer.
"""

from dataclasses import replace

from services.benchmark.grading import (
    first_failed_stage,
    prose_signature,
    stage_statuses,
)
from services.benchmark.lanes import LaneAnswer
from services.benchmark.questions import Question
from services.benchmark.runner import (
    QuestionResult,
    prose_agreement,
    render_markdown,
    run_benchmark,
)

Q_COUNT = Question(
    id="q1", category="counts", question="How many?", oracle_path=["n"], kind="count"
)
ORACLE = {"n": 2}


class StubLane:
    def __init__(self, answer: LaneAnswer, name: str = "stub") -> None:
        self.name = name
        self._answer = answer

    def answer(self, question: Question) -> LaneAnswer:
        return self._answer


def _result(**kw) -> QuestionResult:
    base = dict(
        question_id="q1",
        category="counts",
        lane="stub",
        correct=False,
        reason="",
        sql=None,
        error=None,
        outcome="wrong",
    )
    return QuestionResult(**{**base, **kw})


# --- the classifier ---------------------------------------------------------


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


# --- the grounding flip -----------------------------------------------------


def test_rows_correct_but_ungrounded_prose_is_wrong_at_grounding():
    """Cap-hit-rows-narrated-as-fiction: the oracle rows matched, production's
    numeric_grounding failed the delivered prose — graded `correct` before
    stage attribution, must grade wrong_at=grounding now."""
    lane = StubLane(
        LaneAnswer(
            columns=["n"],
            rows=[[2]],
            sql="SELECT count(*) FROM t",
            answer="There are 3 customers.",
            grounding="fail",
            grounding_reason="claim 3 matches no result value",
        )
    )
    (r,) = run_benchmark([lane], [Q_COUNT], ORACLE)
    assert r.outcome == "wrong" and not r.correct
    assert r.wrong_at == "grounding"
    assert r.stages == {"routing": "skipped", "rows": "passed", "grounding": "failed"}
    assert "claim 3" in r.stage_evidence
    # and the report says which stage broke, with the fail-loud column present
    report = render_markdown([r])
    assert "## Wrong, by stage" in report and "unattributed" in report
    assert "[grounding]" in report  # the miss line carries the tag


def test_grounding_untested_stays_correct_and_skipped():
    lane = StubLane(LaneAnswer(columns=["n"], rows=[[2]], sql="SELECT 2"))
    (r,) = run_benchmark([lane], [Q_COUNT], ORACLE)
    assert r.correct and r.outcome == "correct"
    assert r.wrong_at is None
    assert r.stages["grounding"] == "skipped"


def test_wrong_rows_attributes_rows_not_grounding():
    lane = StubLane(
        LaneAnswer(columns=["n"], rows=[[5]], sql="SELECT 5", answer="Five.", grounding="fail")
    )
    (r,) = run_benchmark([lane], [Q_COUNT], ORACLE)
    assert r.outcome == "wrong" and r.wrong_at == "rows"  # first failed stage wins


def test_misroute_beats_rows_in_attribution():
    lane = StubLane(LaneAnswer(columns=["n"], rows=[[5]], sql="SELECT 5", lens="sales"))
    q = replace(Q_COUNT, expected_lens="finance")
    (r,) = run_benchmark([lane], [q], ORACLE)
    assert r.wrong_at == "routing"
    assert "routed to sales, expected finance" in r.stage_evidence


# --- determinism decomposition ----------------------------------------------


def test_prose_signature_is_claims_not_wording():
    a = prose_signature("Revenue was 1,500 EUR across 3 orders.")
    b = prose_signature("Across 3 orders the revenue reached 1500 euros.")
    assert a == b  # same claims, free wording — thousands separator normalized
    assert prose_signature("Revenue was 1,600 EUR across 3 orders.") != a
    assert prose_signature(None) is None
    assert prose_signature("No numbers here.") == "∅"


def test_prose_agreement_catches_same_rows_different_responses():
    same_rows = dict(answer_signature="1r::2", outcome="correct", correct=True)
    runs = [
        _result(**same_rows, prose_signature="2 ;; 3"),
        _result(**same_rows, prose_signature="2 ;; 4"),
    ]
    assert prose_agreement(runs) == {"stub": 0.5}
    agreed = [
        _result(**same_rows, prose_signature="2 ;; 3"),
        _result(**same_rows, prose_signature="2 ;; 3"),
    ]
    assert prose_agreement(agreed) == {"stub": 1.0}
    # rows disagreed → the flip is generation's, not the composer's: excluded
    split_rows = [
        _result(answer_signature="1r::2", prose_signature="2"),
        _result(answer_signature="1r::5", prose_signature="5"),
    ]
    assert prose_agreement(split_rows) == {"stub": None}


# --- fail-loud rendering ----------------------------------------------------


def test_unattributed_wrong_renders_in_summary():
    r = _result(wrong_at="unattributed", stage_evidence="wrong, but no graded stage failed")
    report = render_markdown([r])
    assert "unattributed" in report and "triage by hand" in report
