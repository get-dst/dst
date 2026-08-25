"""Convention-aware correctness: the audit crowns the RIGHT reading, not the
busiest one. The deterministic medallion-tier prior runs offline; the LLM
tiebreak is scripted; the fixture covers gold winning over majority-bronze."""

from __future__ import annotations

import json
from pathlib import Path

from services.contracts.fakes import ScriptedLLM
from services.probe.correctness import (
    annotate_correctness,
    choose_canon,
    classify_table,
    variant_tier,
)
from services.probe.drift import DriftFinding, DriftVariant

FIXTURE = Path(__file__).parent / "fixtures" / "live_findings.json"


def _variant(tables: list[str], value: float | None, runs: int = 1) -> DriftVariant:
    rows = [[value]] if value is not None else None
    return DriftVariant(
        statement="SELECT SUM(x) FROM " + (tables[0] if tables else "t"),
        source_tables=tables,
        run_count=runs,
        distinguishing="SUM(x); over " + ", ".join(tables),
        observed_rows=rows,
    )


def _conflict(*variants: DriftVariant) -> DriftFinding:
    return DriftFinding(
        metric_intent="sum of revenue",
        variants=list(variants),
        blast_radius=sum(v.run_count for v in variants),
        severity="conflict",
    )


# ── tier classification ───────────────────────────────────────────────────────


def test_classify_table_reads_the_layer_from_the_name() -> None:
    assert classify_table("gold.fct_revenue_monthly") == "gold"
    assert classify_table("silver.dim_customers") == "silver"
    assert classify_table("bronze.netvisor__invoices") == "bronze"
    assert classify_table("crm.dim_customers") == "gold"  # dimensional cue, no layer schema
    assert classify_table("fct_orders") == "gold"  # bare fact table
    assert classify_table("raw_landing.events") == "bronze"
    assert classify_table("app.users") == "unknown"  # no medallion signal


def test_variant_tier_is_the_lowest_of_its_tables() -> None:
    # a gold mart joined to a raw bronze landing is only as trustworthy as the bronze
    assert variant_tier(_variant(["gold.fct_revenue", "bronze.raw_invoices"], 1.0)) == "bronze"
    assert variant_tier(_variant(["gold.fct_revenue", "silver.dim_customers"], 1.0)) == "silver"
    assert variant_tier(_variant(["gold.fct_revenue"], 1.0)) == "gold"
    assert variant_tier(_variant(["app.weird", "other.thing"], 1.0)) == "unknown"


# ── the canon choice: gold beats the majority bronze ──────────────────────────


def test_gold_reading_wins_over_a_bronze_majority() -> None:
    # three analysts sum revenue from raw bronze (agreeing on 35,020,740); one from the
    # governed gold mart (a different, correct number). The vote would crown bronze.
    finding = annotate_correctness(
        [
            _conflict(
                _variant(["bronze.raw_invoices"], 35_020_740.0, runs=12),
                _variant(["bronze.raw_invoices_legacy"], 35_020_740.0, runs=8),
                _variant(["bronze.raw_dump"], 35_020_740.0, runs=5),
                _variant(["gold.fct_revenue_monthly"], 41_818_677.59, runs=2),
            )
        ]
    )[0]
    assert [v.tier for v in finding.variants] == ["bronze", "bronze", "bronze", "gold"]
    assert finding.canon_index == 3  # the gold mart, not the busy bronze crowd
    assert "gold layer" in (finding.canon_rationale or "")
    assert "bronze" in (finding.canon_rationale or "")


def test_no_gold_falls_to_silver_then_bronze() -> None:
    silver = annotate_correctness(
        [_conflict(_variant(["bronze.raw"], 100.0, runs=9), _variant(["silver.clean"], 90.0))]
    )[0]
    assert silver.canon_index == 1  # silver over bronze, despite bronze's run count
    assert "silver" in (silver.canon_rationale or "")

    all_bronze = annotate_correctness(
        [_conflict(_variant(["bronze.a"], 100.0, runs=9), _variant(["bronze.b"], 90.0, runs=2))]
    )[0]
    assert all_bronze.canon_index == 0  # tie at bronze → most-run, and a governed-gold nudge
    assert "governed gold" in (all_bronze.canon_rationale or "")


def test_duplications_get_tiers_but_no_canon() -> None:
    dup = DriftFinding(
        metric_intent="sum of revenue",
        variants=[_variant(["gold.fct_revenue"], 10.0), _variant(["gold.fct_revenue"], 10.0)],
        blast_radius=2,
        severity="duplication",
    )
    (annotated,) = annotate_correctness([dup])
    assert [v.tier for v in annotated.variants] == ["gold", "gold"]
    assert annotated.canon_index is None  # equivalent readings — nothing to choose


# ── the LLM tiebreak (only on a genuine tie) ──────────────────────────────────


def test_llm_breaks_a_genuine_tie_between_same_tier_readings() -> None:
    # two gold readings, different numbers, equal runs, neither agreed-upon → a real tie.
    a = _variant(["gold.fct_revenue_monthly"], 100.0, runs=3)
    b = _variant(["gold.mart_revenue"], 200.0, runs=3)
    llm = ScriptedLLM(['{"canon": 1, "reason": "mart_revenue is the certified mart."}'])
    idx, why = choose_canon(
        annotate_correctness([_conflict(a, b)])[0],
        llm=llm,
        model="deepseek-fake",
    )
    assert idx == 1
    assert "mart" in why


def test_tie_without_an_llm_falls_back_deterministically() -> None:
    a = _variant(["gold.fct_revenue_monthly"], 100.0, runs=3)
    b = _variant(["gold.mart_revenue"], 200.0, runs=3)
    idx, why = choose_canon(annotate_correctness([_conflict(a, b)])[0])
    assert idx == 0  # first by order — deterministic, no LLM consulted
    assert why  # still carries a rationale


def test_garbage_llm_answer_keeps_the_deterministic_pick() -> None:
    a = _variant(["gold.a"], 100.0, runs=3)
    b = _variant(["gold.b"], 200.0, runs=3)
    idx, _ = choose_canon(
        annotate_correctness([_conflict(a, b)])[0],
        llm=ScriptedLLM(["not json"]),
        model="deepseek-fake",
    )
    assert idx == 0  # malformed JSON → fall back, never crash


# ── the headline fix on the real live fixture ─────────────────────────────────


def test_live_fixture_revenue_conflict_crowns_the_gold_mart() -> None:
    findings = [DriftFinding.model_validate(f) for f in json.loads(FIXTURE.read_text())]
    revenue = next(f for f in findings if f.metric_intent == "sum of net")
    (annotated,) = annotate_correctness([revenue])
    canon = annotated.variants[annotated.canon_index]  # type: ignore[index]
    # the majority reading is the BRONZE netvisor union (35,020,740 across 3 readings);
    # convention-aware canon takes the gold mart at the same value instead.
    assert canon.source_tables == ["gold.fct_revenue_monthly"]
    assert canon.tier == "gold"
    assert "gold layer" in (annotated.canon_rationale or "")
