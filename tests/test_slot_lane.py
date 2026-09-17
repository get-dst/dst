"""The slot lane: typed gold on a certified answer is graded before any SQL."""

from __future__ import annotations

from services.certify.store import CertifiedAnswer
from services.contracts.fakes import ScriptedDecider
from services.contracts.protocols import Decision
from services.contracts.semantic_model import (
    Dimension,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.evals import slot_lane
from services.runtime.typed_resolver import TypedResolver

MODEL = SemanticModel(
    lens="sales",
    dialect="duckdb",
    entities=[
        Entity(
            name="orders",
            source=EntitySource(connection="wh", table="orders"),
            fields=[Field(name="amount", type="number"), Field(name="country", type="string")],
            dimensions=[Dimension(name="country")],
            metrics=[
                Metric(name="revenue", agg="sum", expr="orders.amount"),
                Metric(name="order_count", agg="count"),
            ],
        )
    ],
)
GOLD = {
    "method": "construction",
    "tag": "declared",
    "slots": [
        {"kind": "metric", "name": "revenue", "source": "declared"},
        {"kind": "dimension", "name": "country", "source": "declared"},
    ],
}


def _answer(i: int, resolution: dict | None) -> CertifiedAnswer:
    return CertifiedAnswer(
        f"a{i}",
        "sales",
        "revenue by country?",
        "SELECT 1",
        "t",
        "2026-01-01",
        resolution=resolution,
    )


def _sure(name: str | None) -> Decision:
    return Decision(chosen=name, probs={name or "none": 1.0}, provider="v")


def _resolver(*picks: str | None) -> TypedResolver:
    return TypedResolver(ScriptedDecider([_sure(p) for p in picks]), domains={})


def test_typed_resolution_grades_against_stored_gold_without_sql() -> None:
    # shape, metric, another(none), dimension, another(none), filter(none)
    right = _resolver("aggregate", "revenue", None, "country", None, None)
    wrong = _resolver("aggregate", "order_count", None, "country", None, None)
    out = slot_lane.run_slot_lane(
        [_answer(1, GOLD), _answer(2, GOLD), _answer(3, None)],
        MODEL,
        lambda a: right if a.id == "a1" else wrong,
    )
    assert out.skipped_no_gold == 1 and out.passed == 1 and out.failed == 1
    ok, miss = out.cases
    assert ok.typed and ok.verdict == "passed"
    assert miss.verdict == "failed" and "revenue" in miss.evidence
    # Per-decision grading: the wrong metric pick is a graded miss, the rest right.
    graded = {(g.slot, g.chosen): g.correct for g in miss.graded}
    assert graded[("metric", "order_count")] is False
    assert graded[("shape", "aggregate")] is True
    assert graded[("dimension", "country")] is True


def test_a_question_that_does_not_type_fails_the_lane_with_the_reason() -> None:
    unsure = Decision(chosen="revenue", probs={"revenue": 0.5, "order_count": 0.45}, provider="v")
    resolver = TypedResolver(ScriptedDecider([_sure("aggregate"), unsure]), domains={})
    out = slot_lane.run_slot_lane([_answer(1, GOLD)], MODEL, lambda a: resolver)
    (case,) = out.cases
    assert case.verdict == "failed" and not case.typed and "did not type" in case.evidence


def test_the_lane_runs_cases_in_parallel_and_reports_them_in_corpus_order() -> None:
    """Wall time is the provider's round trips over the workers, and the
    report is corpus-ordered whatever order the decisions came back in."""
    import threading
    import time

    class _Slow(ScriptedDecider):
        # Every decision takes a beat, so 8 cases sequential would be ≥ 8 beats.
        def decide(self, **kw):  # type: ignore[no-untyped-def]
            time.sleep(0.05)
            return super().decide(**kw)

    seen: list[str] = []
    lock = threading.Lock()

    def resolver_for(a: CertifiedAnswer) -> TypedResolver:
        with lock:
            seen.append(a.id)
        return TypedResolver(
            _Slow(
                [_sure("aggregate"), _sure("revenue"), _sure(None), _sure("country"), _sure(None)]
            ),
            domains={},
        )

    answers = [_answer(i, GOLD) for i in range(8)]
    started = time.perf_counter()
    out = slot_lane.run_slot_lane(answers, MODEL, resolver_for, concurrency=8)
    elapsed = time.perf_counter() - started
    assert [c.answer_id for c in out.cases] == [f"a{i}" for i in range(8)]
    assert set(seen) == {f"a{i}" for i in range(8)}
    assert all(c.typed and c.verdict == "passed" for c in out.cases), [
        c.evidence for c in out.cases
    ]
    # ~6 decisions × 0.05 s per case: sequential ≥ 2.4 s, eight-wide well under 1 s.
    assert elapsed < 1.5, elapsed


def test_an_answer_certified_before_the_ledger_grades_against_attributed_gold() -> None:
    """No stored resolution: the approved SQL is attributed into slots and the
    case grades against those; SQL that attributes to no governed measure is
    skipped and counted — never a vacuous pass."""
    old = CertifiedAnswer(
        "old",
        "sales",
        "revenue by country?",
        "SELECT country, SUM(orders.amount) AS revenue FROM orders GROUP BY country",
        "t",
        "2026-01-01",
    )
    bare = CertifiedAnswer("bare", "sales", "ids?", "SELECT 1 AS x", "t", "2026-01-01")
    out = slot_lane.run_slot_lane(
        [old, bare],
        MODEL,
        lambda a: _resolver("aggregate", "revenue", None, "country", None, None, None),
    )
    assert out.skipped_no_gold == 1 and out.attributed_gold == 1
    (case,) = out.cases
    assert case.answer_id == "old" and case.gold == "attributed" and case.verdict == "passed"


def test_repeat_measures_determinism_and_fails_a_wobbling_resolution() -> None:
    class _Wobble:
        """Acts every time, but the second resolution picks a different metric."""

        def __init__(self) -> None:
            self.calls = 0

        def decide(self, *, question, context, options, allow_none):  # type: ignore[no-untyped-def]
            names = [o.name for o in options]
            self.calls += 1
            if "aggregate" in names:
                return _sure("aggregate")
            if "revenue" in names and "order_count" in names and "none" not in names:
                return _sure("order_count" if self.calls > 6 else "revenue")
            return _sure(None)

    wobble = _Wobble()
    out = slot_lane.run_slot_lane(
        [_answer(1, GOLD)],
        MODEL,
        lambda a: TypedResolver(wobble, domains={}),  # type: ignore[arg-type]
        repeat=3,
    )
    (case,) = out.cases
    assert case.runs == 3 and case.agreement < 3 and case.verdict == "failed"
    assert "not deterministic" in case.evidence or "runs failed" in case.evidence
    assert out.repeat == 3 and len(case.graded) >= 3
    ok = slot_lane.run_slot_lane(
        [_answer(2, GOLD)],
        MODEL,
        # a fresh scripted decider per run: the pool must never share one
        lambda a: _resolver("aggregate", "revenue", None, "country", None, None),
        repeat=3,
        concurrency=3,
    )
    (c2,) = ok.cases
    assert c2.runs == 3 and c2.agreement == 3 and c2.verdict == "passed" and ok.workers == 3


def test_compiled_sql_is_compared_to_the_certified_text_without_a_warehouse() -> None:
    """Same query in another spelling canonicalises equal — proven, no
    execution; a certified text written another way reads as differs (the
    rows lane's job on apply), never as a failed slot grade."""
    from services.contracts.query_intent import QueryIntent
    from services.runtime.compiler import compile_intent

    compiled = compile_intent(
        QueryIntent(entity="orders", metrics=["revenue"], dimensions=["country"]), MODEL
    )
    same = CertifiedAnswer(
        "s", "sales", "revenue by country?", compiled.upper(), "t", "2026-01-01", resolution=GOLD
    )
    other = CertifiedAnswer(
        "o",
        "sales",
        "revenue by country?",
        "WITH d AS (SELECT * FROM orders) SELECT country, SUM(amount) AS revenue FROM d GROUP BY 1",
        "t",
        "2026-01-01",
        resolution=GOLD,
    )
    out = slot_lane.run_slot_lane(
        [same, other],
        MODEL,
        lambda a: _resolver("aggregate", "revenue", None, "country", None, None),
    )
    a, b = out.cases
    assert a.verdict == "passed" and a.sql_match is True
    assert b.verdict == "passed" and b.sql_match is False


def test_a_decider_that_raises_fails_the_case_with_the_reason_never_the_lane() -> None:
    class _Down:
        def decide(self, **kw):  # type: ignore[no-untyped-def]
            raise RuntimeError("starved")

    out = slot_lane.run_slot_lane(
        [_answer(1, GOLD)],
        MODEL,
        lambda a: TypedResolver(_Down(), domains={}),  # type: ignore[arg-type]
    )
    (case,) = out.cases
    assert case.verdict == "failed" and "decider unavailable" in case.evidence
    assert not case.typed and case.decisions == []


def test_the_lane_grades_latency_and_cost_per_case() -> None:
    from services.contracts.protocols import DecisionUsage

    def priced(chosen: str | None) -> Decision:
        return Decision(
            chosen=chosen,
            probs={chosen or "none": 1.0},
            provider="vote:deepseek-v4-flash:k=5",
            usage=DecisionUsage(calls=5, input_tokens=100_000, output_tokens=0, latency_s=0.01),
        )

    def resolver_for(a: CertifiedAnswer) -> TypedResolver:
        return TypedResolver(
            ScriptedDecider(
                [
                    priced("aggregate"),
                    priced("revenue"),
                    priced(None),
                    priced("country"),
                    priced(None),
                    priced(None),
                ]
            ),
            domains={},
        )  # type: ignore[arg-type]

    out = slot_lane.run_slot_lane([_answer(1, GOLD)], MODEL, resolver_for, repeat=2)
    (case,) = out.cases
    n = len(case.decisions)
    assert case.verdict == "passed" and case.calls == 5.0 * n and case.elapsed_s >= 0.0
    # n decisions × 100k tokens at $0.14/M per resolution, two resolutions in the lane
    assert case.cost_usd == round(n * 0.014, 6) and out.cost_usd == round(2 * n * 0.014, 6)
    assert len(out.latencies_s) == 2


def test_the_emission_arm_grades_the_json_intent_like_a_typed_one() -> None:
    from services.contracts.protocols import GeneratedQuery
    from services.contracts.query_intent import QueryIntent

    class _Emits:
        model = "deepseek-v4-flash"

        def generate(self, **kw):  # type: ignore[no-untyped-def]
            return GeneratedQuery(
                sql="SELECT 1",
                intent=QueryIntent(entity="orders", metrics=["revenue"], dimensions=["country"]),
                input_tokens=1_000_000,
                output_tokens=0,
            )

    out = slot_lane.run_slot_lane(
        [_answer(1, GOLD)], MODEL, lambda a: slot_lane.EmissionResolver(_Emits(), [])
    )
    (case,) = out.cases
    assert case.verdict == "passed" and case.typed
    (rec,) = case.decisions
    assert rec.slot == "intent" and rec.provider == "json:deepseek-v4-flash"
    assert case.calls == 1.0 and case.cost_usd == 0.14  # 1M input tokens at $0.14/M
