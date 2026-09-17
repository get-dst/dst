"""Grading the deciders over the corpus dst test already owns — offline."""

from __future__ import annotations

from services.certify.store import CertifiedAnswer
from services.contracts.fakes import HashEmbedder, ScriptedDecider, ScriptedLLM
from services.contracts.protocols import Decision
from services.evals import calibrate
from services.router import CoverageProfile, Router
from services.router.decider import LlmDecider

PROFILES = [
    CoverageProfile(lens="finance", anchors=["total net invoiced revenue"], description="invoices"),
    CoverageProfile(lens="sales", anchors=["quote win rate"], description="deals"),
]
BY_LENS = {p.lens: p for p in PROFILES}


def test_lens_decisions_grade_only_what_the_decider_decided() -> None:
    measured = Decision(
        chosen="finance", probs={"finance": 0.8, "sales": 0.2, "none": 0.0}, provider="v"
    )
    decider = LlmDecider(ScriptedLLM(["unused"]), "m", decider=ScriptedDecider([measured]))
    labeled = [
        ("how much have we billed", "finance"),  # paraphrase → decider consulted
        ("total net invoiced revenue", "finance"),  # verbatim anchor → cosine fast path, skipped
        ("what is our win rate lately", "sales"),  # decider says finance: a graded miss
    ]
    recs = calibrate.lens_decisions(Router(PROFILES, HashEmbedder()), BY_LENS, decider, labeled)
    assert [(r.chosen, r.gold, r.p) for r in recs] == [
        ("finance", "finance", 0.8),
        ("finance", "sales", 0.8),
    ]
    assert all(r.decider == "v" and r.slot == "lens" for r in recs)


def _answer(i: int, sql: str, status: str = "active") -> CertifiedAnswer:
    return CertifiedAnswer(f"a{i}", "l", f"question {i}?", sql, "t", "2026-01-01", status=status)


def test_equivalence_pairs_label_by_normalised_sql() -> None:
    answers = [
        _answer(1, "SELECT count(*) FROM customers"),
        _answer(2, "select COUNT(*) from customers"),  # same query, other spelling
        _answer(3, "SELECT sum(amount) FROM orders"),
        _answer(4, "SELECT 1", status="retired"),  # never paired
    ]
    pairs = calibrate.equivalence_pairs(answers, "duckdb")
    labels = {(a.id, b.id): gold for a, b, gold in pairs}
    assert labels[("a1", "a2")] == "equivalent"
    assert labels[("a1", "a3")] == "different" and labels[("a2", "a3")] == "different"
    assert not any("a4" in key for key in labels)
    assert len(calibrate.equivalence_pairs(answers, "duckdb", cap=2)) == 2


def test_equivalence_decisions_carry_the_gold_and_the_measurement() -> None:
    answers = [
        _answer(1, "SELECT count(*) FROM customers"),
        _answer(2, "select COUNT(*) from customers"),
        _answer(3, "SELECT sum(amount) FROM orders"),
    ]
    # Legacy k=1 gate: yes / no / yes — the third is a wrong 'equivalent'.
    recs = calibrate.equivalence_decisions(
        ScriptedLLM(["yes", "no", "yes"]), "m", answers, "duckdb", k=1
    )
    assert [(r.chosen, r.gold, r.p) for r in recs] == [
        ("equivalent", "equivalent", None),
        ("different", "different", None),
        ("equivalent", "different", None),
    ]
    assert all(r.decider == "legacy:m" for r in recs)


def test_the_lanes_run_wide_and_keep_corpus_order() -> None:
    """Records land in pair order however the pool schedules them, and a
    decision that raises is a missing record, not a crash of the lane."""
    import time

    from services.contracts.protocols import Decision, Option

    class _Slow:
        def decide(self, *, question: str, context: str, options: list[Option], allow_none: bool):
            time.sleep(0.05)
            if "boom" in question:
                raise RuntimeError("starved")
            return Decision(
                chosen="equivalent", probs={"equivalent": 0.9, "different": 0.1}, provider="t"
            )

    answers = [_answer(i, f"SELECT {i} FROM t") for i in range(9)] + [
        CertifiedAnswer("boom", "sales", "boom?", "SELECT 99 FROM t", "t", "2026-01-01")
    ]
    started = time.perf_counter()
    recs = calibrate.equivalence_decisions(
        ScriptedLLM([]), "m", answers, "duckdb", k=1, cap=30, decider=_Slow(), concurrency=8
    )
    elapsed = time.perf_counter() - started
    pairs = calibrate.equivalence_pairs(answers, "duckdb", cap=30)
    # The pairs whose ASKED question is the raising one are missing records;
    # every other pair is graded, in pair order.
    assert 20 <= len(recs) < len(pairs) and all(r.gold == "different" for r in recs)
    assert elapsed < 30 * 0.05, elapsed  # sequential would be >= 1.5 s


def test_all_lenses_routing_skips_cosine_and_offers_every_lens() -> None:
    """The no-cosine arm: a verbatim anchor is not a fast path, and the decider
    sees every published lens as an option."""
    from services.router.decider import route_with_llm

    measured = Decision(
        chosen="sales", probs={"sales": 0.7, "finance": 0.3, "none": 0.0}, provider="v"
    )
    scripted = ScriptedDecider([measured])
    decider = LlmDecider(ScriptedLLM(["unused"]), "m", decider=scripted)
    router = Router(PROFILES, HashEmbedder())
    d = route_with_llm(router, BY_LENS, decider, "total net invoiced revenue", all_lenses=True)
    assert d.decision is not None and d.decision.chosen == "sales"
    assert scripted.calls and set(scripted.calls[0]["options"]) >= {"finance", "sales"}
    fast = route_with_llm(router, BY_LENS, decider, "total net invoiced revenue")
    assert fast.decided_by == "cosine-verbatim" and fast.decision is None


def test_a_typed_decision_provider_routes_over_every_lens_by_default() -> None:
    """No cosine in front of a typed decider: the verbatim anchor is decided
    like any other question, over the whole catalogue."""
    from services.router.decider import route_with_llm

    class _Typed(ScriptedDecider):
        typed_decisions = True

    measured = Decision(
        chosen="finance", probs={"finance": 0.9, "sales": 0.1, "none": 0.0}, provider="typesafe:j"
    )
    scripted = _Typed([measured])
    decider = LlmDecider(ScriptedLLM(["unused"]), "m", decider=scripted)
    assert decider.typed_decisions
    d = route_with_llm(
        Router(PROFILES, HashEmbedder()), BY_LENS, decider, "total net invoiced revenue"
    )
    assert d.decision is not None and d.decided_by != "cosine-verbatim"
    assert set(scripted.calls[0]["options"]) >= {"finance", "sales"}
