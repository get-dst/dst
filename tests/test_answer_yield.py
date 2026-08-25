"""Stratified answer yield, offline."""

from __future__ import annotations

from services.contracts.fakes import ScriptedLLM
from services.contracts.warehouse import (
    ColumnSchema,
    QueryResult,
    SchemaSnapshot,
    TableSchema,
)
from services.probe.answer_yield import (
    GroundTruthQuestion,
    RawdogCaller,
    run_answer_yield,
    schema_summary,
)


class _FakeWarehouse:
    """Maps SQL → canned results; truth queries return the verified numbers."""

    def __init__(self, table: dict[str, QueryResult]) -> None:
        self._table = table

    def execute(self, sql, *, read_only=True, row_limit=None) -> QueryResult:
        if sql not in self._table:
            raise RuntimeError(f"no such query: {sql[:40]}")
        return self._table[sql]

    def introspect(self) -> SchemaSnapshot:
        return SchemaSnapshot(
            connection="c",
            dialect="snowflake",
            tables=[
                TableSchema(
                    name="silver.fct_invoices", columns=[ColumnSchema(name="id", type="INT")]
                )
            ],
        )


def _r(n: float) -> QueryResult:
    return QueryResult(columns=["v"], rows=[[n]])


def test_schema_summary_lists_tables_and_columns():
    snap = SchemaSnapshot(
        connection="c",
        dialect="snowflake",
        tables=[TableSchema(name="gold.rev", columns=[ColumnSchema(name="eur", type="NUMBER")])],
    )
    assert "gold.rev(eur)" in schema_summary(snap)


def test_three_strata_classified_against_customer_ground_truth():
    # caller produces: a right answer, a wrong answer, then broken SQL.
    llm = ScriptedLLM(["SELECT 771", "SELECT 999", "SELECT oops FROM nowhere"])
    wh = _FakeWarehouse(
        {
            "SELECT 771": _r(771),  # caller right
            "SELECT 999": _r(999),  # caller wrong
            "TRUTH_count": _r(771),
            "TRUTH_total": _r(35020740.0),  # caller said 999 → wrong
            # broken SQL raises → unanswered
        }
    )
    caller = RawdogCaller(llm, schema_summary(wh.introspect()), model="scripted")
    questions = [
        GroundTruthQuestion("How many invoices?", "TRUTH_count"),
        GroundTruthQuestion("Total net invoiced?", "TRUTH_total"),
        GroundTruthQuestion("Overdue?", "TRUTH_overdue"),
    ]
    report = run_answer_yield(wh, caller, questions)
    assert (report.verified, report.wrong, report.unanswered) == (1, 1, 1)
    assert report.total == 3
    assert abs(report.yield_pct - 33.33) < 0.1
    # the wrong row carries both numbers for the report
    wrong = next(r for r in report.rows if r.stratum == "wrong")
    assert wrong.caller_value == "999.0" and wrong.truth_value == "35020740.0"


def test_count_tolerance_matches_integers_exactly_but_floats_relatively():
    llm = ScriptedLLM(["SELECT 35020740.01"])  # within rel tol of the truth
    wh = _FakeWarehouse({"SELECT 35020740.01": _r(35020740.01), "T": _r(35020740.0)})
    caller = RawdogCaller(llm, "", model="scripted")
    report = run_answer_yield(wh, caller, [GroundTruthQuestion("Revenue?", "T")])
    assert report.verified == 1


def test_yield_is_zero_with_wide_ci_when_all_wrong():
    llm = ScriptedLLM(["SELECT 1", "SELECT 2"])
    wh = _FakeWarehouse({"SELECT 1": _r(1), "SELECT 2": _r(2), "Ta": _r(100), "Tb": _r(200)})
    caller = RawdogCaller(llm, "", model="scripted")
    report = run_answer_yield(
        wh, caller, [GroundTruthQuestion("a", "Ta"), GroundTruthQuestion("b", "Tb")]
    )
    assert report.yield_pct == 0.0
    lo, hi = report.ci
    assert lo == 0.0 and hi > 0.0  # Wilson upper bound is honest about small n


def test_certdef_governs_the_caller_prompt():
    plain = RawdogCaller(ScriptedLLM(["SELECT 1"]), "T(a)", model="m")
    governed = RawdogCaller(
        ScriptedLLM(["SELECT 1"]), "T(a)", model="m", certified="exclude test rows"
    )
    assert "DEFINITIONS" not in plain._system
    assert "DEFINITIONS" in governed._system and "exclude test rows" in governed._system


def test_grades_against_a_direct_truth_value_for_held_out_questions():
    # held-out: no truth_sql, the verified answer is a known oracle fact
    llm = ScriptedLLM(["SELECT 799520", "SELECT 123"])
    wh = _FakeWarehouse({"SELECT 799520": _r(799520), "SELECT 123": _r(123)})
    caller = RawdogCaller(llm, "", model="scripted")
    report = run_answer_yield(
        wh,
        caller,
        [
            GroundTruthQuestion("Revenue in Jan 2025?", truth_value=799520.0),  # caller right
            GroundTruthQuestion("Revenue in Feb 2025?", truth_value=1843144.0),  # caller wrong
        ],
    )
    assert (report.verified, report.wrong) == (1, 1)
