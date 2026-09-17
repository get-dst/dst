"""The judge as a composition of checks — the documented false positives replayed.

Every pattern from the 2026-09 field run is a fixture with its expected
verdict: numbers that were the literal query output called "invented"; a
structured serve graded "no data"; a correction auto-approved by grading the
original; the data_as_of footer read as a claim; MAX-vs-AVG and population
objections that are judgment, not fact. Grounding is deterministic; scope is
one typed decision; unsure is said, never invented.
"""

from __future__ import annotations

from services.contracts.fakes import ScriptedDecider
from services.contracts.protocols import Decision
from services.reviews.judge import judge_typed
from services.reviews.store import Trace


def _trace(**kw: object) -> Trace:
    base: dict[str, object] = dict(
        request_id="r1",
        lens="finance",
        caller="noora",
        question="What was total revenue in August 2026?",
        sql="SELECT SUM(amount) AS revenue FROM invoices WHERE month = '2026-08'",
        answer="Total revenue in August 2026 was 2,268,094 EUR.",
        definition_used="revenue",
        confidence="verified",
        row_count=1,
        columns=["revenue"],
        rows=[[2268094.0]],
    )
    base.update(kw)
    return Trace(**base)  # type: ignore[arg-type]


def _within(p: float = 1.0) -> Decision:
    return Decision(
        chosen="within_scope", probs={"within_scope": p, "overreach": 1 - p}, provider="v"
    )


def _overreach() -> Decision:
    return Decision(chosen="overreach", probs={"within_scope": 0.0, "overreach": 1.0}, provider="v")


def test_a_number_that_is_the_query_output_is_grounded_and_approved() -> None:
    """FP pattern 1: 'invented' numbers that were the literal rows."""
    verdict, reasoning, decisions = judge_typed(_trace(), ScriptedDecider([_within()]))
    assert verdict == "approve" and "grounded" in reasoning
    assert [d.slot for d in decisions] == ["judge_scope"]


def test_an_ungrounded_number_is_a_deterministic_reject() -> None:
    t = _trace(answer="Total revenue in August 2026 was 9,999,999 EUR.")
    verdict, reasoning, decisions = judge_typed(t, ScriptedDecider([_within()]))
    assert verdict == "reject" and "ungrounded" in reasoning
    assert decisions == []  # no judgment was needed


def test_a_structured_serve_is_never_no_data() -> None:
    """FP pattern 2: format=structured leaves the prose empty on purpose."""
    t = _trace(answer="", structured=True)
    verdict, reasoning, _ = judge_typed(t, ScriptedDecider([_within()]))
    assert verdict == "approve" and "structured serve" in reasoning


def test_a_correction_is_what_gets_graded() -> None:
    """FP pattern 3: corrections auto-approved by grading the ORIGINAL."""
    fake = ScriptedDecider([_within()])
    t = _trace(
        correction_sql="SELECT SUM(net_amount) AS revenue FROM invoices WHERE month = '2026-08'"
    )
    verdict, reasoning, _ = judge_typed(t, fake)
    assert "grading the correction" in reasoning
    assert "net_amount" in str(fake.calls[0]["question"])
    assert "not executed" in reasoning and verdict == "approve"


def test_the_data_as_of_footer_is_not_a_claim() -> None:
    """FP pattern 4: the frozen freshness footer read as a real number."""
    t = _trace(answer="Total revenue in August 2026 was 2,268,094 EUR. (data as of 2026-02-23)")
    verdict, _, _ = judge_typed(t, ScriptedDecider([_within()]))
    assert verdict == "approve"


def test_rows_unavailable_skips_grounding_and_says_so() -> None:
    t = _trace(rows=None, columns=None)
    verdict, reasoning, _ = judge_typed(t, ScriptedDecider([_within()]))
    assert verdict == "approve" and "rows not available" in reasoning


def test_overreach_is_changes_and_unsure_is_said() -> None:
    verdict, reasoning, _ = judge_typed(_trace(), ScriptedDecider([_overreach()]))
    assert verdict == "changes" and "overreach" in reasoning
    verdict, reasoning, decisions = judge_typed(_trace(), ScriptedDecider([_within(0.55)]))
    assert verdict == "changes" and "unsure" in reasoning
    assert decisions[0].verdict == "clarify"


def test_no_sql_is_changes_not_a_verdict() -> None:
    verdict, reasoning, _ = judge_typed(_trace(sql=None), ScriptedDecider([_within()]))
    assert verdict == "changes" and "no SQL" in reasoning


def test_the_field_run_overreach_cases_reach_the_scope_decision_whole() -> None:
    """Two true positives from the field run: an answer computed over ONE
    account line when the question asked for revenue, and a MAX() over a
    one-row-per-month grain served as "the rate". The scope decision is the
    provider's; what the judge owes it is the whole picture — question, the
    SQL with its restriction or its aggregate, the prose — and `changes` when
    it says overreach."""
    seen: list[str] = []

    class _Sees(ScriptedDecider):
        def decide(self, **kw):  # type: ignore[no-untyped-def]
            seen.append(kw["question"])
            return super().decide(**kw)

    partial = _trace(
        question="What was group revenue by month in 2026?",
        sql=(
            "SELECT month, SUM(amount_eur) AS amount FROM consolidation "
            "WHERE group_account_name = 'Revenue (SE)' GROUP BY month"
        ),
        answer="Revenue was 1,200,000 EUR in January.",
        rows=[["2026-01", 1200000.0]],
        columns=["month", "amount"],
    )
    verdict, reasoning, decisions = judge_typed(partial, _Sees([_overreach()]))
    assert verdict == "changes" and "overreach" in reasoning
    assert "Revenue (SE)" in seen[-1] and "group revenue by month" in seen[-1]

    wrong_grain = _trace(
        question="What was the SEK/EUR average rate in August 2026?",
        sql="SELECT MAX(fx_rates.avg_rate) AS avg_rate FROM fx_rates WHERE month = '2026-08'",
        answer="The average rate was 11.32.",
        rows=[[11.32]],
        columns=["avg_rate"],
    )
    verdict, reasoning, _ = judge_typed(wrong_grain, _Sees([_overreach()]))
    assert verdict == "changes" and "MAX(fx_rates.avg_rate)" in seen[-1]
