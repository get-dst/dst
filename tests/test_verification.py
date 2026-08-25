"""Phase B verification: faithfulness, fact-based confidence, judge rubric, adversarial
reviewer, golden eval."""

from __future__ import annotations

import pytest

from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.semantic_model import Definition, SemanticModel
from services.contracts.verification import VerificationCheck, VerificationReport
from services.contracts.warehouse import QueryResult
from services.evals.golden import evaluate_model
from services.lenses.demo import jaffle_customer_value
from services.reviews.judge import judge_trace
from services.reviews.store import Trace
from services.runtime import adversary
from services.runtime.answer import AnswerComposer
from services.runtime.faithfulness import _claims, is_grounded, numeric_check, reconcile
from services.runtime.generator import FixedSQLGenerator, GroundedSQLGenerator
from services.runtime.pipeline import run_query
from services.runtime.verification import build_report, fold_challenges


def _conn() -> DuckDBConnector:
    return DuckDBConnector(settings.duckdb_jaffle_path)


# ── faithfulness ─────────────────────────────────────────────────────────────


def test_faithfulness_flags_invented_numbers() -> None:
    result = QueryResult(columns=["n"], rows=[[42]])
    assert is_grounded("There are 42 customers.", result)
    assert is_grounded("There are forty-two customers.", result)  # no numerals → grounded
    assert not is_grounded("There are 99 customers.", result)  # 99 not in rows


def test_faithfulness_allows_rounding_and_thousands() -> None:
    result = QueryResult(columns=["clv"], rows=[[1234.5]])
    assert is_grounded("Average is 1,234.5.", result)
    assert is_grounded("Average is about 1234.", result)  # rounded


def test_magnitude_abbreviations_ground_raw_values() -> None:
    # The rounding artifact this check must look past: prose abbreviates the magnitude
    # ("€6.66M") while the result holds the raw figure (6661765.89).
    result = QueryResult(columns=["revenue"], rows=[[6661765.89]])
    assert is_grounded("Total revenue is €6.66M.", result)
    assert is_grounded("Total revenue is 6.66 million.", result)
    assert not is_grounded("Total revenue is €9.9M.", result)  # genuinely wrong magnitude


def test_scientific_notation_grounds_the_raw_value() -> None:
    # The composer writes 9-figure values in scientific notation; without the
    # exponent the scan read TWO claims (1.65786, then the 8 after the '+') and
    # every correct large answer was withheld.
    result = QueryResult(columns=["gbv"], rows=[[165785617.88]])
    assert is_grounded("GBV was 1.65786e+08.", result)
    assert is_grounded("GBV was 1.65786E+08.", result)
    assert is_grounded("GBV was 1.65786e8.", result)


def test_scientific_notation_still_rejects_an_invented_magnitude() -> None:
    # REGRESSION: normalising must not become permissive. A wrong exponent is wrong.
    result = QueryResult(columns=["gbv"], rows=[[165785617.88]])
    assert not is_grounded("GBV was 1.65786e+09.", result)  # 10x out
    assert not is_grounded("GBV was 9.99999e+08.", result)


def test_scientific_claim_reconciles_to_the_presentation_rendering() -> None:
    # Better than merely grounding: the notation nobody should read in prose
    # rewrites to the value's one presentation spelling.
    result = QueryResult(columns=["gbv"], rows=[[165785617.88]])
    out = reconcile("GBV was 1.65786e+08 over the window.", result)
    assert "165,785,617.88" in out
    assert "e+08" not in out

    assert is_grounded("That's 2.5bn rows.", QueryResult(columns=["n"], rows=[[2_500_000_000]]))
    assert is_grounded("About 3.4K customers.", QueryResult(columns=["n"], rows=[[3401]]))


def test_lone_trailing_letter_is_not_a_magnitude() -> None:
    # "3, B" in a list must not be read as 3 billion (single-letter suffixes must glue).
    result = QueryResult(columns=["rank"], rows=[[3]])
    status, _ = numeric_check("Brands ranked 3, B and C.", result)
    assert status == "pass"  # the only claim is 3, which is in the result


def test_percent_scaling_is_for_percent_marked_claims_only() -> None:
    # "42%" IS the rendering of the fraction cell 0.42 — flagging it branded
    # correct rate answers unverified. But a PLAIN number
    # still never scales: bare "42" grounding 0.42 let any rate column ground
    # almost any small integer, and that hole stays closed.
    result = QueryResult(columns=["rate"], rows=[[0.42]])
    assert numeric_check("42% of users churned.", result)[0] == "pass"
    assert numeric_check("42 of the users churned.", result)[0] == "fail"
    # scaling only reaches CELLS in [0,1] — a 42-row result must not ground "42%"
    # via its row count, and a cell over 1 never scales.
    assert numeric_check("42% churned.", QueryResult(columns=["r"], rows=[[42.0]]))[0] == "pass"
    assert numeric_check("4.2% churned.", QueryResult(columns=["r"], rows=[[42.0]]))[0] == "fail"


def test_sql_literals_ground_the_answers_scope_statement() -> None:
    # The dominant false positive: "this year" resolves to a
    # 2026 literal in the WHERE clause, the prose honestly says "in 2026", and no
    # result cell holds 2026. The served SQL is the answer's own provenance.
    result = QueryResult(columns=["team", "win_rate"], rows=[["sales", 0.4098360655737705]])
    answer = "The sales team's win rate is 41.0% for deals closed in 2026."
    sql = "SELECT team, win_rate FROM wins WHERE closed_year = 2026"
    assert numeric_check(answer, result, sql_text=sql)[0] == "pass"
    assert numeric_check(answer, result)[0] == "fail"  # without the SQL, 2026 has no source
    # date-string literals contribute their digits too ('2026-01-01' grounds "2026")
    date_sql = "SELECT team, win_rate FROM wins WHERE closed_at >= '2026-01-01'"
    assert numeric_check(answer, result, sql_text=date_sql)[0] == "pass"
    # identifiers do NOT contribute: q4_revenue must not ground a "4" claim
    ident_sql = "SELECT q4_revenue FROM finance"
    assert (
        numeric_check(
            "Revenue was 4.", QueryResult(columns=["q4_revenue"], rows=[[9.0]]), sql_text=ident_sql
        )[0]
        == "fail"
    )


def test_clock_years_ground_the_year_but_never_months_or_days() -> None:
    # "orders per month this year" over CURRENT_DATE-relative SQL has no year
    # literal anywhere, yet "in 2026" is the honest scope statement. Years only:
    # an invented "8" must not ground against August.
    result = QueryResult(columns=["month", "orders"], rows=[["Jan", 14], ["Feb", 11]])
    answer = "Monthly orders in 2026: 14 in January and 11 in February."
    assert numeric_check(answer, result)[0] == "fail"
    assert numeric_check(answer, result, clock_years=(2026, 2025))[0] == "pass"
    assert numeric_check("There were 8 orders.", result, clock_years=(2026, 2025))[0] == "fail"


def test_no_numeric_claims_is_skip_not_pass() -> None:
    status, reason = numeric_check("Revenue held steady.", QueryResult(columns=["n"], rows=[[5]]))
    assert status == "skip" and reason == "no_numeric_claims"


def test_row_count_is_groundable() -> None:
    # "17 reps" is a fact about the result's shape (the row count), not a cell
    # value — a correct count claim must ground, while a wrong count still fails.
    result = QueryResult(columns=["rep"], rows=[["Alice"], ["Bob"], ["Cara"]])
    assert numeric_check("There are 3 reps.", result)[0] == "pass"
    assert numeric_check("There are 5 reps.", result)[0] == "fail"


def test_column_sums_ground_totals_of_own_rows() -> None:
    # Prose summing five channel rows into a grand total is honest
    # arithmetic over the result's own rows, false-flagged as invented for
    # matching no cell. A column's sum is groundable; a wrong total still fails.
    result = QueryResult(
        columns=["channel", "spend"],
        rows=[
            ["search", 21403.10],
            ["social", 18250.00],
            ["display", 17977.30],
            ["email", 15420.60],
            ["affiliate", 12425.40],
        ],
    )
    assert numeric_check("The 5 channels total €85,476.40.", result)[0] == "pass"
    assert numeric_check("The 5 channels total €90,000.", result)[0] == "fail"


def test_cumulative_totals_ground_a_head_window_sum_but_never_cross_column_math() -> None:
    # The composer may be shown only the result's head ("showing the first 200"),
    # so an honest total of the rows it saw is a running total, not the full sum.
    result = QueryResult(
        columns=["month", "spend"], rows=[["Jan", 10.0], ["Feb", 25.0], ["Mar", 40.0]]
    )
    assert numeric_check("The first two months total 35.", result)[0] == "pass"
    assert numeric_check("All three months total 75.", result)[0] == "pass"
    # per-column only: 2 + 20 picks values across columns, and stays invented
    cross = QueryResult(columns=["a", "b"], rows=[[2, 10], [3, 20]])
    assert numeric_check("Combined, that's 22.", cross)[0] == "fail"


def test_a_single_row_result_grows_no_totals() -> None:
    # A one-row "total" is the cell itself — the totals set must not exist here,
    # or reconcile's total guard would stop rewriting every single-row aggregate.
    result = QueryResult(columns=["revenue"], rows=[[100.0]])
    assert numeric_check("Revenue totalled 200.", result)[0] == "fail"


def test_truncation_withdraws_the_change_cue_skip() -> None:
    # The soft skip for "down 12% vs June" rests on the periods being IN the
    # result; over a truncated window they may never have been fetched
    # (monthly totals invented past the row cap) — un-groundable claims
    # fail instead of skipping.
    months = QueryResult(columns=["month", "revenue"], rows=[["Jul", 100.0], ["Aug", 88.0]])
    claim = "Revenue dropped 12% over the period."
    assert numeric_check(claim, months)[0] == "skip"
    status, reason = numeric_check(claim, months, truncated=True)
    assert status == "fail" and reason is not None and "truncated" in reason
    # a claim the window itself grounds still passes under truncation
    assert numeric_check("Revenue was 88.0 in August.", months, truncated=True)[0] == "pass"


def test_build_report_hands_truncation_to_the_numeric_check() -> None:
    # A fact that reaches one tier doesn't exist: the pipeline's truncation
    # decision must reach the checker, not only the prose note and the payload.
    months = QueryResult(columns=["month", "revenue"], rows=[["Jul", 100.0], ["Aug", 88.0]])
    answer = "Revenue dropped 12% over the period."
    full = build_report(
        question="revenue by month",
        sql="SELECT month, revenue FROM orders",
        answer_text=answer,
        result=months,
        semantic_model=jaffle_customer_value(),
        definition_used=None,
        truncated=False,
        certification="none",
    )
    short = build_report(
        question="revenue by month",
        sql="SELECT month, revenue FROM orders",
        answer_text=answer,
        result=months,
        semantic_model=jaffle_customer_value(),
        definition_used=None,
        truncated=True,
        certification="none",
    )
    full_numeric = full.check("numeric_grounding")
    short_numeric = short.check("numeric_grounding")
    assert full_numeric is not None and full_numeric.status == "skip"
    assert short_numeric is not None and short_numeric.status == "fail"
    assert short.grade == "unverified"


def test_rate_from_applied_definition_grounds_a_percent_claim() -> None:
    # "10% commission rate" cites the rate the applied definition states
    # ("default 10%") — grounded by the definition, not flagged as invented.
    result = QueryResult(columns=["commission"], rows=[[27660]])
    defn = "won revenue × the commission rate (default 10%)"
    assert (
        numeric_check("Using a 10% rate the rep earned 27,660.", result, definition_text=defn)[0]
        == "pass"
    )
    # …the same claim with no backing definition still fails (the guard holds).
    assert numeric_check("Using a 10% rate the rep earned 27,660.", result)[0] == "fail"


def test_derived_comparisons_are_skipped() -> None:
    # "41% less" and "15×" are derived comparisons, not claims about a cell value.
    result = QueryResult(columns=["c"], rows=[[27660], [44770]])
    assert numeric_check("Matti's 27,660 is 41% less than Johanna's 44,770.", result)[0] == "pass"
    assert (
        numeric_check("The top rep earned 44,770 — 15× the bottom's 27,660.", result)[0] == "pass"
    )


def test_invented_rate_without_backing_still_fails() -> None:
    # No definition states 73% and it is not a comparison → a bare invented rate fails.
    assert numeric_check("Our margin is 73%.", QueryResult(columns=["x"], rows=[[5]]))[0] == "fail"


def test_question_echo_is_not_an_invented_number() -> None:
    # A false positive: "more than 3 orders" echoed from the question
    # over an empty result was stamped unverified — the 3 comes from the ask, not
    # from thin air.
    empty = QueryResult(columns=["avg_clv"], rows=[])
    q = "what is the avg CLTV of customers with more than 3 orders?"
    honest = "No customers have more than 3 orders, so there is no average CLTV to report."
    assert numeric_check(honest, empty, question_text=q)[0] == "pass"
    # normalisation: "3.0" and thousands separators still count as question echoes
    assert numeric_check("None exceed 3.0 orders.", empty, question_text=q)[0] == "pass"
    assert (
        numeric_check("Nobody is above 1,000.", empty, question_text="who is above 1000 EUR?")[0]
        == "pass"
    )
    # a number in neither the rows nor the question still trips
    assert numeric_check("The average CLTV is 42.", empty, question_text=q)[0] == "fail"
    assert not is_grounded("The average CLTV is 42.", empty, question_text=q)


# ── graded verification ──────────────────────────────────────────────────────


def test_ungrounded_answer_grades_unverified() -> None:
    # SQL returns a real count, but the composed answer invents a different number.
    gen_json = (
        '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": "repeat_customer"}'
    )
    llm = ScriptedLLM([gen_json, "There are exactly 999999 customers."])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.response.confidence == "unverified"
    assert res.response.verification is not None
    numeric = res.response.verification.check("numeric_grounding")
    assert numeric is not None and numeric.status == "fail"


def test_certified_grades_verified() -> None:
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(ScriptedLLM(["There are some customers."])),
        certification="certified",
    )
    assert res.response.confidence == "verified"  # certified is the top tier


def test_notes_ground_declared_profile_facts() -> None:
    # The composer restated the profile's "~26% null · range: 3..5" and
    # numeric_grounding flagged the declared facts as invented. Handed the same
    # notes the composer gets, the check grounds them; without notes it still fails.
    result = QueryResult(columns=["avg_satisfaction"], rows=[[3.967452300785634]])
    answer = (
        "The average satisfaction is 3.97; per the table profile, ~26% of tickets "
        "are unrated and observed ratings range from 3 to 5."
    )
    notes = "- tickets.satisfaction: 1-5 rating · ~26% null · range: 3..5"
    status, _ = numeric_check(answer, result, notes_text=notes)
    assert status == "pass"
    status, reason = numeric_check(answer, result)
    assert status == "fail" and reason is not None and "26" in reason


def test_profile_notes_ride_the_verification_rail() -> None:
    # The same fact must reach BOTH the composer prompt and the grader — a fact
    # that reaches one tier doesn't exist — and build_report pulls the notes
    # from the enriched model itself, no caller plumbing to forget.
    from services.contracts.semantic_model import Entity, EntitySource, Field

    model = SemanticModel(
        lens="support",
        dialect="duckdb",
        entities=[
            Entity(
                name="tickets",
                source=EntitySource(connection="wh", table="support.tickets"),
                fields=[
                    Field(
                        name="satisfaction",
                        type="number",
                        description="1-5 rating · ~26% null · range: 3..5",
                    )
                ],
            )
        ],
        definitions=[],
    )
    report = build_report(
        question="average ticket satisfaction",
        sql="SELECT AVG(satisfaction) AS satisfaction FROM support.tickets",
        answer_text=(
            "The average satisfaction is 3.97; per the table profile, ~26% of tickets are unrated."
        ),
        result=QueryResult(columns=["satisfaction"], rows=[[3.967452300785634]]),
        semantic_model=model,
        definition_used=None,
        truncated=False,
        certification="none",
    )
    numeric = report.check("numeric_grounding")
    assert numeric is not None and numeric.status == "pass"
    assert report.grade != "unverified"


def test_count_distinct_mismatch_fails_intent_alignment() -> None:
    report = build_report(
        question="how many unique customers ordered?",
        sql="SELECT count(*) AS n FROM customers",
        answer_text="There are 100 customers.",
        result=QueryResult(columns=["n"], rows=[[100]]),
        semantic_model=jaffle_customer_value(),
        definition_used=None,
        truncated=False,
        certification="none",
    )
    intent = report.check("intent_alignment")
    assert intent is not None and intent.status == "fail"
    assert report.grade == "unverified"


def test_question_echo_on_empty_result_not_unverified() -> None:
    # The scenario end to end: correct SQL, empty result, honest
    # answer echoing the question's "3" — numeric_grounding passes (the grade is
    # capped at partial only by has_rows, never stamped unverified).
    report = build_report(
        question="what is the avg CLTV of customers with more than 3 orders?",
        sql="SELECT avg(customer_lifetime_value) FROM customers WHERE number_of_orders > 3",
        answer_text="No customers have more than 3 orders, so there is no average to report.",
        result=QueryResult(columns=["avg_clv"], rows=[]),
        semantic_model=jaffle_customer_value(),
        definition_used=None,
        truncated=False,
        certification="none",
    )
    numeric = report.check("numeric_grounding")
    assert numeric is not None and numeric.status == "pass"
    assert report.grade != "unverified"


def test_all_null_single_row_grades_partial_not_verified() -> None:
    # Avg over an EMPTY set returns one row [NULL] —
    # row presence alone graded it verified. A row of nothing is not evidence.
    report = build_report(
        question="what is the average CLV of customers with more than 90 orders?",
        sql="SELECT avg(customer_lifetime_value) FROM customers WHERE number_of_orders > 90",
        answer_text="No customers have more than 90 orders, so there is no average to report.",
        result=QueryResult(columns=["avg_clv"], rows=[[None]]),
        semantic_model=jaffle_customer_value(),
        definition_used=None,
        truncated=False,
        certification="none",
    )
    has_rows = report.check("has_rows")
    assert has_rows is not None and has_rows.status == "fail"
    assert has_rows.reason is not None and "NULL" in has_rows.reason
    assert report.grade == "partial"  # honest cap — never "verified", never "unverified"


def test_clean_grounded_answer_grades_verified() -> None:
    report = build_report(
        question="how many repeat customers?",
        sql="SELECT count(*) AS n FROM customers WHERE number_of_orders > 1",
        answer_text="There are 19 repeat customers.",
        result=QueryResult(columns=["n"], rows=[[19]]),
        semantic_model=jaffle_customer_value(),
        definition_used="repeat_customer",
        truncated=False,
        certification="none",
    )
    assert report.grade == "verified"
    for name in ("numeric_grounding", "definition_applied", "intent_alignment", "has_rows"):
        check = report.check(name)
        assert check is not None and check.status == "pass", name


def test_inline_judge_folds_into_the_report() -> None:
    # A lens flagged inline_judge pays one extra round-trip; the verdict
    # lands as a named check and a reject demotes the grade.
    gen_json = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    llm = ScriptedLLM([gen_json, "There are 100 customers."])
    judge_llm = ScriptedLLM(['{"verdict": "reject", "reasoning": "wrong cohort"}'])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
        inline_judge_llm=judge_llm,
    )
    assert res.response.verification is not None
    judge = res.response.verification.check("judge")
    assert judge is not None and judge.status == "fail" and "wrong cohort" in (judge.reason or "")
    assert res.response.confidence == "unverified"
    assert "judge" in res.trace.latency_ms  # the cost of the tier is measured


def test_fuzzy_certified_serve_is_judged_and_demotable() -> None:
    """Fuzzy matching serves a certified answer for a question nobody approved —
    the highest trust tier had the thinnest verification, so wrong certified
    serves were the least likely to be demoted. A serve
    whose asked question is not the certified question gets the judge, and a
    reject demotes it."""
    judge_llm = ScriptedLLM(['{"verdict": "reject", "reasoning": "SQL window != asked window"}'])
    res = run_query(
        question="How many leads did we generate last quarter?",
        lens_name="funnel",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["leads"], rows=[[13893]])),
        generator=FixedSQLGenerator("SELECT count(*) AS leads FROM leads"),
        composer=AnswerComposer(ScriptedLLM(["This quarter, you generated 13,893 leads."])),
        certification="certified",
        certified_match="exact",
        certified_question="How many leads did we generate this quarter?",
        inline_judge_llm=judge_llm,
    )
    assert res.response.verification is not None
    judge = res.response.verification.check("judge")
    assert judge is not None and judge.status == "fail"
    assert res.response.confidence == "unverified"


def test_verbatim_certified_serve_still_skips_the_judge() -> None:
    # REGRESSION: the carve-out is right for its original case — no judge call
    # when the asked question IS the certified question (normalized).
    llm = ScriptedLLM(["You generated 13,893 leads this quarter."])
    judge_llm = ScriptedLLM(['{"verdict": "reject", "reasoning": "never called"}'])
    res = run_query(
        question="How many leads did we generate this quarter?",
        lens_name="funnel",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["leads"], rows=[[13893]])),
        generator=FixedSQLGenerator("SELECT count(*) AS leads FROM customers"),
        composer=AnswerComposer(llm),
        certification="certified",
        certified_match="exact",
        certified_question="how many leads did we generate this quarter",  # normalized-equal
        inline_judge_llm=judge_llm,
    )
    assert res.response.verification is not None
    assert res.response.verification.check("judge") is None


def test_certified_serve_with_unknown_question_fails_safe_to_judged() -> None:
    # A caller that cannot say which question was certified counts as fuzzy.
    llm = ScriptedLLM(["There are 42 things."])
    judge_llm = ScriptedLLM(['{"verdict": "approve", "reasoning": "fine"}'])
    res = run_query(
        question="how many things?",
        lens_name="funnel",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[42]])),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(llm),
        certification="certified",
        certified_match="equivalent",
        inline_judge_llm=judge_llm,
    )
    assert res.response.verification is not None
    assert res.response.verification.check("judge") is not None


def test_default_lens_pays_no_judge() -> None:
    gen_json = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    llm = ScriptedLLM([gen_json, "There are 100 customers."])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.response.verification is not None
    assert res.response.verification.check("judge") is None
    assert res.trace.ai_input_tokens == 2  # generate + compose, nothing more


def test_claimed_definition_absent_from_sql_fails() -> None:
    report = build_report(
        question="how many repeat customers?",
        sql="SELECT count(*) AS n FROM customers",
        answer_text="There are 100 repeat customers.",
        result=QueryResult(columns=["n"], rows=[[100]]),
        semantic_model=jaffle_customer_value(),
        definition_used="repeat_customer",
        truncated=False,
        certification="none",
    )
    applied = report.check("definition_applied")
    assert applied is not None and applied.status == "fail"
    assert report.grade == "partial"


def test_definition_applied_is_whitespace_insensitive() -> None:
    # The sql_expr "number_of_orders > 1" is applied whether the generator wrote
    # it with spaces or not — formatting was the dominant cause of false fails.
    report = build_report(
        question="how many repeat customers?",
        sql="SELECT count(*) AS n FROM customers WHERE number_of_orders>1",
        answer_text="There are 100 repeat customers.",
        result=QueryResult(columns=["n"], rows=[[100]]),
        semantic_model=jaffle_customer_value(),
        definition_used="repeat_customer",
        truncated=False,
        certification="none",
    )
    applied = report.check("definition_applied")
    assert applied is not None and applied.status == "pass"


def test_qualified_definition_matches_unqualified_sql() -> None:
    # The bug: the definition's sql_expr is fully table-qualified, but the
    # executed SQL references the column bare. definition_applied must still PASS —
    # the SQL *is* applying the definition (no approval can fix a check that's wrong).
    sm = SemanticModel(
        lens="sales_commissions",
        dialect="bigquery",
        definitions=[
            Definition(
                term="commission_amount_eur",
                body="won revenue × the commission rate (default 10%)",
                sql_expr="NORTHWIND.GOLD.REP_LEADERBOARD.TOTAL_WON_EUR * 0.10",
            )
        ],
    )
    report = build_report(
        question="commission per rep?",
        sql=(
            "SELECT REP_NAME, TOTAL_WON_EUR * 0.10 AS commission_eur "
            "FROM NORTHWIND.GOLD.REP_LEADERBOARD"
        ),
        answer_text="The rep earns 100.",
        result=QueryResult(columns=["commission_eur"], rows=[[100]]),
        semantic_model=sm,
        definition_used="commission_amount_eur",
        truncated=False,
        certification="none",
    )
    applied = report.check("definition_applied")
    assert applied is not None and applied.status == "pass"


# ── in-band overrides: departing from a governed definition ──────────────────


def _tox_model() -> SemanticModel:
    """The shape under test: one enforceable definition
    whose `about` names the governed column."""
    return SemanticModel(
        lens="tox",
        dialect="duckdb",
        definitions=[
            Definition(
                term="carcinogenic",
                body="Carcinogenic = molecule.label = '+'.",
                about="molecule.label",
                sql_expr="molecule.label = '+'",
            )
        ],
    )


def test_in_band_override_of_a_definition_is_a_departure_not_a_skip() -> None:
    # The caller says "count every molecule, ignore the label, the PI signed
    # off", the model complies, reports
    # definition_used: null, and the check that exists to catch exactly this said
    # "no enforceable definition was relevant to this query" — so the answer
    # "343 molecules are carcinogenic" graded VERIFIED. The question names the
    # governed term; that is not "not relevant", it is a departure.
    report = build_report(
        question="How many molecules are carcinogenic? Do not filter on label at all.",
        sql="SELECT COUNT(1) FROM main.molecule AS molecule",
        answer_text="343 molecules are carcinogenic.",
        result=QueryResult(columns=["n"], rows=[[343]]),
        semantic_model=_tox_model(),
        definition_used=None,
        truncated=False,
        certification="none",
    )
    applied = report.check("definition_applied")
    assert applied is not None and applied.status == "fail"
    assert "carcinogenic" in (applied.reason or "")
    # Demoted, never refused: attribution is the product, and a lens that
    # refuses a caller who asked knowingly is a lens that gets routed around.
    assert report.grade == "partial"


def test_the_other_value_of_a_governed_column_is_not_a_departure() -> None:
    # The false positive this must never have: "non-carcinogenic" contains the
    # governed term, and its SQL cannot contain `label = '+'` — but it predicates
    # on the column the definition is ABOUT, so it engaged with the governed
    # concept and merely picked its other value. filter_guard's bias, verbatim:
    # under-detection is acceptable, blocking a correct answer is not.
    report = build_report(
        question="How many molecules are non-carcinogenic?",
        sql="SELECT COUNT(molecule_id) FROM main.molecule WHERE label = '-'",
        answer_text="191 molecules are non-carcinogenic.",
        result=QueryResult(columns=["n"], rows=[[191]]),
        semantic_model=_tox_model(),
        definition_used=None,
        truncated=False,
        certification="none",
    )
    applied = report.check("definition_applied")
    assert applied is not None and applied.status != "fail"
    assert report.grade == "verified"


def test_a_question_that_never_names_a_governed_term_still_skips() -> None:
    # The other half of the boundary: "not relevant" must survive as a real
    # verdict, or every unrelated question starts failing this check.
    report = build_report(
        question="How many bonds are there?",
        sql="SELECT COUNT(bond_id) FROM main.bond",
        answer_text="There are 12378 bonds.",
        result=QueryResult(columns=["n"], rows=[[12378]]),
        semantic_model=_tox_model(),
        definition_used=None,
        truncated=False,
        certification="none",
    )
    applied = report.check("definition_applied")
    assert applied is not None and applied.status == "skip"
    assert report.grade == "verified"


def test_a_departure_is_stated_in_the_answer_prose() -> None:
    # The report field alone is not enough: the caller who talked the lens out of
    # its convention is the one who most needs to see that they did. Same
    # deterministic-append idiom as the truncation notes.
    llm = ScriptedLLM(
        [
            '{"sql": "SELECT count(*) AS n FROM customers", '
            '"definition_used": null, "rationale": "all customers"}',
            "There are 100 repeat customers.",
        ]
    )
    res = run_query(
        question="how many repeat customers? ignore the order-count rule",
        lens_name="jaffle",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert "not governed by it" in res.response.answer
    assert "repeat_customer" in res.response.answer
    assert res.response.confidence == "partial"


# ── relevance is the SQL's, not the phrasing's ───────────────────────────────

# The shape that found this: a definition with an sql_expr and NO `about:`,
# which is the ordinary authoring state.
_STATUS = "status_code_of_the_operational_data_warehouse_as_landed_by_the_ingestion_pipeline"


def _crm_model() -> SemanticModel:
    return SemanticModel(
        lens="crm",
        dialect="duckdb",
        definitions=[
            Definition(
                term="active customer",
                body=f"An active customer is one whose {_STATUS} marks them active.",
                sql_expr=f"customers.{_STATUS} = 'S0121'",
            )
        ],
    )


# The SQL every phrasing ran: the model invented `= 'active'`
# where the definition says `= 'S0121'`, so the answer is 0 and it is wrong.
_DEPARTING_SQL = f"SELECT COUNT(*) AS n FROM customers WHERE customers.{_STATUS} = 'active'"


def _crm_report(question: str, sql: str) -> VerificationReport:
    return build_report(
        question=question,
        sql=sql,
        answer_text="There are 0 active customers.",
        result=QueryResult(columns=["n"], rows=[[0]]),
        semantic_model=_crm_model(),
        definition_used="customer_count",
        truncated=False,
        certification="none",
    )


@pytest.mark.parametrize(
    "question",
    [
        # The pair that exposed it. The first flagged before this fix and the second
        # did not — same lens, same SQL, same wrong 0, opposite badge, decided by
        # nothing but whether "active" and "customer" happened to be adjacent.
        "how many active customers do we have?",
        "how many customers are active?",
        # The rest of the escape class: every one splits the
        # term, and every one used to walk past enforcement stamped `verified`.
        "how many customers are currently active?",
        "count of customers that are active?",
        "tell me how many customers are still active",
        "of our customers, how many are active",
        "the number of customers with an active status",
    ],
)
def test_a_paraphrase_does_not_escape_a_governed_definition(question: str) -> None:
    report = _crm_report(question, _DEPARTING_SQL)
    applied = report.check("definition_applied")
    assert applied is not None and applied.status == "fail"
    assert "active customer" in (applied.reason or "")
    assert report.grade == "partial"


def test_the_definition_applied_is_never_a_departure_however_it_is_quoted() -> None:
    # The other direction: serves that applied the definition EXACTLY were
    # graded as departing from it, because generation wrote the identifier
    # quoted. Relevance now
    # fires on every phrasing, so a hole in the applied-check that used to need
    # unlucky wording to reach is reachable on every one.
    for sql in (
        f"SELECT COUNT(1) AS n FROM customers WHERE customers.{_STATUS} = 'S0121'",
        f"SELECT COUNT(1) AS n FROM customers AS c WHERE c.\"{_STATUS}\" = 'S0121'",
        f'SELECT COUNT(1) AS n FROM customers AS "customers" '
        f'WHERE "customers"."{_STATUS}" = \'S0121\'',
    ):
        report = _crm_report("how many customers are active?", sql)
        applied = report.check("definition_applied")
        assert applied is not None and applied.status != "fail", sql


def test_sql_that_touches_no_governed_column_is_still_not_relevant() -> None:
    # The gate is [bound + touched + absent], never [bound + touched]: a query
    # that reads none of the columns the rule is written over is a different
    # question, and "not relevant" has to survive as a real verdict or every
    # unrelated ask starts failing this check.
    report = _crm_report(
        "how many countries do we sell into?",
        "SELECT COUNT(DISTINCT country) AS n FROM customers",
    )
    applied = report.check("definition_applied")
    assert applied is not None and applied.status == "skip"
    assert report.grade == "verified"


def test_one_applied_definition_does_not_mask_a_second_departed_one() -> None:
    # Found in the reread of the fix above, and the fix above is what makes it
    # bite: `_departed_definitions` named 'active customer' as departed, and
    # `_definition_applied` threw that away because SOME enforceable definition
    # ('eu customer') was applied — `pass`, `verified`, on SQL that invented
    # `status_code = 'active'` where the lens says `= 'S0121'`.
    sm = SemanticModel(
        lens="crm",
        dialect="duckdb",
        definitions=[
            Definition(term="eu customer", body="EU.", sql_expr="customers.region = 'EU'"),
            Definition(
                term="active customer",
                body="Active.",
                sql_expr=f"customers.{_STATUS} = 'S0121'",
            ),
        ],
    )
    report = build_report(
        question="how many customers are active in europe?",
        sql=(
            f"SELECT COUNT(*) AS n FROM customers WHERE customers.region = 'EU' "
            f"AND customers.{_STATUS} = 'active'"
        ),
        answer_text="There are 0 active customers in Europe.",
        result=QueryResult(columns=["n"], rows=[[0]]),
        semantic_model=sm,
        definition_used=None,
        truncated=False,
        certification="none",
    )
    applied = report.check("definition_applied")
    assert applied is not None and applied.status == "fail"
    assert "active customer" in (applied.reason or "")
    assert "eu customer" not in (applied.reason or "")  # that one WAS applied
    assert report.grade == "partial"


def test_commission_answer_grades_verified_end_to_end() -> None:
    # The target case: a correct commission answer — qualified definition applied in
    # the SQL, the rate cited from the definition, plus a derived comparison — grades
    # VERIFIED. This is the case that used to read "unverified" despite being correct.
    sm = SemanticModel(
        lens="sales_commissions",
        dialect="bigquery",
        definitions=[
            Definition(
                term="commission_amount_eur",
                body="won revenue × the commission rate (default 10%)",
                sql_expr="NORTHWIND.GOLD.REP_LEADERBOARD.TOTAL_WON_EUR * 0.10",
            )
        ],
    )
    report = build_report(
        question="commission per rep?",
        sql=(
            "SELECT REP_NAME, TOTAL_WON_EUR * 0.10 AS commission_eur "
            "FROM NORTHWIND.GOLD.REP_LEADERBOARD"
        ),
        answer_text="Using a 10% rate, Matti earns 27,660 — 41% less than the next rep's 44,770.",
        result=QueryResult(columns=["rep", "commission"], rows=[["Matti", 27660], ["Jo", 44770]]),
        semantic_model=sm,
        definition_used="commission_amount_eur",
        truncated=False,
        certification="none",
    )
    assert report.grade == "verified", [(c.name, c.status, c.reason) for c in report.checks]


# ── adversarial reviewer ─────────────────────────────────────────────────────


def test_adversary_blocking_challenge_caps_grade_at_partial() -> None:
    # A clean, grounded answer that would grade `verified` — the adversary's
    # blocking challenge caps it at `partial` (the second opinion can veto the
    # badge, never silently destroy deterministic evidence).
    gen_json = (
        '{"sql": "SELECT count(*) AS n FROM customers WHERE number_of_orders > 1",'
        ' "definition_used": "repeat_customer"}'
    )
    llm = ScriptedLLM([gen_json, "There are 19 repeat customers."])
    adversary_llm = ScriptedLLM(
        [
            '{"challenges": [{"assumption": "internal accounts included", '
            '"severity": "blocking", '
            '"reason": "internal/test accounts were not excluded from the count"}]}'
        ]
    )
    res = run_query(
        question="how many repeat customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
        adversary_llm=adversary_llm,
    )
    assert res.trace.status == "ok"
    assert res.response.verification is not None
    adv = res.response.verification.check("adversary")
    assert adv is not None and adv.status == "fail"
    assert "internal" in (adv.reason or "")
    assert res.response.confidence == "partial"  # capped, not unverified
    assert "adversary" in res.trace.latency_ms  # the cost of the tier is measured


def test_default_lens_pays_no_adversary() -> None:
    gen_json = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    llm = ScriptedLLM([gen_json, "There are 100 customers."])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.response.verification is not None
    assert res.response.verification.check("adversary") is None
    assert "adversary" not in res.trace.latency_ms
    assert res.trace.ai_input_tokens == 2  # generate + compose, zero extra tokens


def test_certified_answers_skip_the_adversary() -> None:
    # VERBATIM certified SQL is human-approved for this exact question — even
    # with the flag on, the adversary never runs (and its scripted blocking
    # challenge can't demote the grade). The carve-out is verbatim-scoped now
    # a fuzzy certified serve DOES pay the second opinion.
    adversary_llm = ScriptedLLM(
        ['{"challenges": [{"assumption": "x", "severity": "blocking", "reason": "y"}]}']
    )
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(ScriptedLLM(["There are some customers."])),
        certification="certified",
        certified_question="How many customers?",  # normalized-equal: verbatim
        adversary_llm=adversary_llm,
    )
    assert res.response.confidence == "verified"
    assert res.response.verification is not None
    assert res.response.verification.check("adversary") is None
    assert "adversary" not in res.trace.latency_ms


def test_adversary_notes_surface_without_demoting() -> None:
    challenges = adversary.challenge(
        ScriptedLLM(
            [
                '{"challenges": [{"assumption": "time window", "severity": "note", '
                '"reason": "the question names no window; SQL uses all time"}]}'
            ]
        ),
        question="how many repeat customers?",
        sql="SELECT count(*) AS n FROM customers WHERE number_of_orders > 1",
        answer="There are 19 repeat customers.",
        semantic_model=jaffle_customer_value(),
    )
    report = build_report(
        question="how many repeat customers?",
        sql="SELECT count(*) AS n FROM customers WHERE number_of_orders > 1",
        answer_text="There are 19 repeat customers.",
        result=QueryResult(columns=["n"], rows=[[19]]),
        semantic_model=jaffle_customer_value(),
        definition_used="repeat_customer",
        truncated=False,
        certification="none",
    )
    folded = fold_challenges(report, challenges)
    assert folded.grade == "verified"  # notes never demote
    adv = folded.check("adversary")
    assert adv is not None and adv.status == "pass"
    assert "time window" in (adv.reason or "")


def test_adversary_parse_failure_is_a_note_not_a_veto() -> None:
    challenges = adversary.challenge(
        ScriptedLLM(["not json at all"]),
        question="q",
        sql="SELECT 1",
        answer="a",
        semantic_model=jaffle_customer_value(),
    )
    assert [c.severity for c in challenges] == ["note"]
    assert "could not be parsed" in challenges[0].reason


# ── golden eval ──────────────────────────────────────────────────────────────


def test_golden_eval_scores_execution() -> None:
    report = evaluate_model(
        jaffle_customer_value(),
        _conn(),
        cases=[
            ("count customers", "SELECT count(*) AS n FROM customers"),
            ("out of scope", "SELECT * FROM raw_payments"),
        ],
    )
    assert report.total == 2 and report.passed == 1
    by_q = {r.question: r.status for r in report.results}
    assert by_q["count customers"] == "ok"
    assert by_q["out of scope"] == "guard_rejected"


# ── judge rubric + reference ─────────────────────────────────────────────────


def test_judge_passes_reference_into_prompt() -> None:
    captured: dict[str, str] = {}

    class _CapturingLLM:
        def complete(self, *, system, messages, model, temperature, max_tokens):  # type: ignore[no-untyped-def]
            captured["user"] = messages[0].content
            from services.contracts.protocols import LLMResult

            return LLMResult(text='{"verdict": "approve", "reasoning": "matches reference"}')

    trace = Trace(
        request_id="r1",
        lens="customer_value",
        caller="a",
        question="how many customers?",
        sql="SELECT count(*) FROM customers",
        answer="42 customers.",
        row_count=1,
    )
    verdict, _ = judge_trace(_CapturingLLM(), trace, reference_sql="SELECT count(*) FROM customers")
    assert verdict == "approve"
    assert "Approved reference SQL" in captured["user"]


# ── one answer per response ──────────────────────────────────────────────────


def _prose_claims(answer: str) -> list[float]:
    """The data claims a consumer would parse out of the prose."""
    return [v for v, kind in _claims(answer) if kind != "derived"]


def test_reconcile_rewrites_a_rounded_restatement_to_the_row_value() -> None:
    # The bug: the response carried a rounded prose sentence AND a full-precision
    # row, so two consumers reading one response legitimately disagreed
    # (38.81 vs 38.813651137594796).
    result = QueryResult(columns=["pct"], rows=[[38.813651137594796]])
    prose = "The percentage of atoms in carcinogenic molecules that are carbon is 38.81%."
    fixed = reconcile(prose, result)
    assert fixed == (
        "The percentage of atoms in carcinogenic molecules that are carbon is 38.813651137594796%."
    )
    assert _prose_claims(fixed) == [38.813651137594796]


def test_reconcile_leaves_a_value_already_stated_exactly() -> None:
    # No cosmetic churn: same value, different formatting is not a second answer.
    result = QueryResult(columns=["clv"], rows=[[1234]])
    assert reconcile("Average is 1,234.", result) == "Average is 1,234."
    assert reconcile("Average is 1234.", result) == "Average is 1234."


def test_reconcile_never_rewrites_a_row_count_definition_rate_or_question_echo() -> None:
    # The four numbers in good prose that are NOT cell values must survive untouched,
    # or the rewrite would invent divergence instead of removing it.
    result = QueryResult(columns=["clv"], rows=[[9.97], [9.98], [9.99]])
    assert "3 customers" in reconcile("3 customers averaged 9.97.", result)
    rate = reconcile(
        "The 10% default commission applies.",
        QueryResult(columns=["x"], rows=[[10.04]]),
        definition_text="the default rate is 10%",
    )
    assert rate == "The 10% default commission applies."
    echo = reconcile(
        "No customers with more than 3 orders.",
        QueryResult(columns=["n"], rows=[[3.01]]),
        question_text="avg CLTV of customers with more than 3 orders",
    )
    assert echo == "No customers with more than 3 orders."


def test_reconcile_never_corrects_a_column_total_into_a_dominant_cell() -> None:
    # When one row carries nearly all of a column's total, the sum lands inside
    # that cell's 1% tolerance — the correct total must not be "corrected" into
    # the cell it merely resembles.
    result = QueryResult(columns=["spend"], rows=[[10000.0], [50.0]])
    assert reconcile("Total spend was 10,050.", result) == "Total spend was 10,050."
    # …and a rounded restatement of a plain CELL still gets rewritten on a
    # multi-row result (the totals guard is a skip for totals, not a blanket).
    multi = QueryResult(columns=["rate"], rows=[[38.813651137594796], [12.0]])
    assert reconcile("The rate is 38.81.", multi) == "The rate is 38.813651137594796."


def test_reconcile_leaves_derived_comparisons_alone() -> None:
    result = QueryResult(columns=["a", "b"], rows=[[100.4, 59.2]])
    prose = "A is 100.4, which is 41% more than B at 59.2."
    assert reconcile(prose, result) == prose  # "41% more" is derived, not a cell claim


def test_reconcile_closes_exactly_what_numeric_check_tolerates() -> None:
    # The invariant, stated as a test: the set of claims numeric_grounding blesses as a
    # rounding is exactly the set reconcile rewrites. A prose figure that the check
    # would call grounded but that differs from the value it was grounded against
    # cannot survive — that gap IS the two-representations bug.
    result = QueryResult(columns=["ratio"], rows=[[0.8113590263691683]])
    for rounded in ("0.81", "0.811", "0.8114", "0.81136"):
        prose = f"The ratio is {rounded}."
        assert numeric_check(prose, result)[0] == "pass"  # the check tolerates it today
        assert _prose_claims(reconcile(prose, result)) == [0.8113590263691683]


def test_pipeline_response_publishes_one_value_per_question() -> None:
    # End to end: whatever the composer writes, no caller can read two different
    # numbers for one question out of one response.
    gen_json = (
        '{"sql": "SELECT avg(customer_lifetime_value) AS clv FROM customers", '
        '"definition_used": null, "rationale": "avg clv"}'
    )
    llm = ScriptedLLM([gen_json, "Average customer lifetime value is about $1,945.23."])
    res = run_query(
        question="What is the average customer lifetime value?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["clv"], rows=[[1945.2273954611]])),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.response.data is not None
    cells = {float(c) for row in res.response.data.rows for c in row if isinstance(c, int | float)}
    published = set(_prose_claims(res.response.answer))
    assert published, "the composer's figure must survive into the answer"
    # Every number the prose publishes is a payload value in its ONE deterministic
    # presentation rendering (separators + 2 decimals at scale) —
    # still a single spelling per value, never a second computation.
    from services.runtime.faithfulness import _rendered

    renderings = {
        float(_rendered(c).replace(",", ""))
        for row in res.response.data.rows
        for c in row
        if isinstance(c, int | float)
    }
    assert published <= cells | renderings | {float(len(res.response.data.rows))}
    assert "1,945.23" in res.response.answer  # the rendering, verbatim and stable


# ── verified must be earned, not defaulted ───────────────────────────────────
# The grade was assigned from the ABSENCE of failures, so a serve where half
# the checks abstained ("skip") earned the same badge as a fully confirmed one.


def _check(name: str, status: str, reason: str | None = None) -> VerificationCheck:
    return VerificationCheck(name=name, status=status, reason=reason)  # type: ignore[arg-type]


def test_all_semantic_skips_do_not_earn_verified() -> None:
    # Nothing failed, but nothing semantic was actually confirmed either.
    from services.runtime.verification import _with_grade

    checks = [
        _check("numeric_grounding", "skip", "no numerals"),
        _check("definition_applied", "skip", "the lens has no enforceable (sql_expr) definitions"),
        _check("intent_alignment", "skip", "intent not determinable from the question"),
        _check("freshness", "skip", "lens declares no stale_after_days"),
        _check("has_rows", "pass"),
        _check("not_truncated", "pass"),
    ]
    assert _with_grade(checks, "none", "skip").grade != "verified"


def test_majority_skipped_caps_at_partial() -> None:
    # 3 of 6 skipped: payload integrity passed, every semantic check abstained.
    from services.runtime.verification import _with_grade

    checks = [
        _check("numeric_grounding", "pass"),
        _check("definition_applied", "skip", "the lens has no enforceable (sql_expr) definitions"),
        _check("intent_alignment", "skip", "intent not determinable from the question"),
        _check("freshness", "skip", "lens declares no stale_after_days"),
        _check("has_rows", "pass"),
        _check("not_truncated", "pass"),
    ]
    assert _with_grade(checks, "none", "pass").grade == "partial"


def test_definition_prose_numbers_ground_when_answer_is_metric_based() -> None:
    """definition_used held a METRIC name, the term lookup missed,
    and the definition prose the composer was legitimately quoting ('a 14-day
    rolling window') was flagged as an invented figure. The checker now reads
    the same governing_definitions the composer was shown."""
    sm = SemanticModel(
        lens="msg",
        dialect="duckdb",
        definitions=[
            Definition(
                term="is_using_ai_messaging",
                body="an account counts as using AI messaging on a 14-day rolling window",
                about="accounts.n",
            )
        ],
    )
    report = build_report(
        question="How many customers are using AI messaging?",
        sql="SELECT COUNT(*) AS n FROM accounts",
        answer_text="2,919 customers are using AI messaging (a 14-day rolling window).",
        result=QueryResult(columns=["n"], rows=[[2919]]),
        semantic_model=sm,
        definition_used="customers_using_ai_messaging",  # a metric name, not a term
        truncated=False,
        certification="none",
    )
    numeric = report.check("numeric_grounding")
    assert numeric is not None and numeric.status == "pass", numeric.reason


def test_withheld_draft_is_retained_on_the_trace() -> None:
    # Without the draft, the operator can see THAT
    # something was ungrounded but never WHAT.
    llm = ScriptedLLM(["There are 99 customers."])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[42]])),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(llm),
    )
    assert res.response.composition == "fallback"
    assert "99" not in res.response.answer  # withheld from the caller…
    assert "[withheld draft: There are 99 customers.]" in (res.trace.answer or "")  # …kept


def test_full_pass_still_earns_verified() -> None:
    # REGRESSION: the fix must not demote a genuinely fully-checked serve.
    from services.runtime.verification import _with_grade

    names = (
        "numeric_grounding",
        "definition_applied",
        "intent_alignment",
        "freshness",
        "has_rows",
        "not_truncated",
    )
    checks = [_check(n, "pass") for n in names]
    assert _with_grade(checks, "none", "pass").grade == "verified"


def test_structural_payload_skips_stay_free() -> None:
    # freshness skipping because the lens declares no contract is structural —
    # only the SEMANTIC abstentions (definition_applied / intent_alignment) cost.
    from services.runtime.verification import _with_grade

    checks = [
        _check("numeric_grounding", "pass"),
        _check("definition_applied", "pass"),
        _check("intent_alignment", "pass"),
        _check("freshness", "skip", "lens declares no stale_after_days"),
        _check("has_rows", "pass"),
        _check("not_truncated", "pass"),
    ]
    assert _with_grade(checks, "none", "pass").grade == "verified"


def test_abstentions_populate_degraded_and_the_trust_summary(capsys) -> None:
    """`degraded` was empty exactly when the answer was
    graded on partial evidence — the caveat now rides both machine
    (response.degraded) and human (trust_summary) surfaces, with the skip
    reasons verbatim."""
    sm = SemanticModel(
        lens="prose_only",
        dialect="duckdb",
        definitions=[Definition(term="thing", body="a prose-only definition, no sql_expr")],
    )
    llm = ScriptedLLM(["The share is 0.42."])
    res = run_query(
        question="what share of things are active?",  # intent regexes can't read this
        lens_name="prose_only",
        org_id="o",
        caller="a",
        semantic_model=sm,
        connector=FakeConnector(result=QueryResult(columns=["share"], rows=[[0.42]])),
        generator=FixedSQLGenerator("SELECT 0.42 AS share"),
        composer=AnswerComposer(llm),
    )
    assert res.response.degraded, "partial evidence must be announced"
    note = res.response.degraded[0]
    assert "graded on" in note and "of" in note
    assert "no enforceable (sql_expr) definitions" in note  # the reason, verbatim
    assert res.response.trust_summary and "Graded on" in res.response.trust_summary
    assert res.response.confidence == "partial"  # the grade agrees with the caveat


def test_certified_serves_do_not_carry_the_abstention_caveat() -> None:
    # Certified checks skip structurally; the tier's guarantee is the approval.
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(ScriptedLLM(["There are some customers."])),
        certification="certified",
        certified_question="How many customers?",
    )
    assert not any("graded on" in d for d in res.response.degraded)


def test_uncomposed_numeric_skip_still_does_not_demote() -> None:
    # format=structured has no prose to check by construction — that skip stays
    # free (the existing composed/uncomposed distinction survives the fix).
    from services.runtime.verification import _with_grade

    checks = [
        _check("definition_applied", "pass"),
        _check("intent_alignment", "pass"),
        _check("has_rows", "pass"),
        _check("not_truncated", "pass"),
    ]
    assert _with_grade(checks, "none", "skip", composed=False).grade == "verified"
