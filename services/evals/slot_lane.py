"""The slot lane: grade a certified question's typed resolution before any SQL runs.

A certified answer that carries a stored resolution (its typed gold, copied
from the trace at certify time) can be graded in milliseconds: run the typed
resolver on the question, compare the governed names it resolved to against
the stored slots (the same ``resolution.grade`` the rows lane uses), and grade
every slot decision the resolver made against the gold the stored slots imply.
No warehouse, no composer — the cost is the decisions. An answer certified
before the ledger carries no stored gold; its human-approved SQL is attributed
into slots instead (``method: attributed``) and graded the same way, provided
the attribution found a governed measure — an empty gold would pass anything,
so it is skipped and counted instead.

``repeat`` resolves every question N times: the verdict holds only when every
run passes AND every run produced the same typed intent — determinism is the
claim typed serving makes, and here it is measured, not assumed.

The compiled SQL is a deterministic function of the intent, so the lane also
compares it to the certified SQL text (both sqlglot-canonicalised): identical
means the query is proven without a warehouse; different means only that the
certified text was written another way (a raw-SQL CTE, a CASE) — the rows lane
on apply is the oracle for those until they earn typed gold.

Pure over an injected resolver; `dst test --slots` builds it from the install's
decider and the assembly seam.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import sqlglot

from services.certify.store import CertifiedAnswer, is_active
from services.contracts.resolution import MEASURE_KINDS, DecisionRecord, Resolution
from services.contracts.semantic_model import SemanticModel
from services.evals.calibration import GradedDecision
from services.runtime import resolution as ledger
from services.runtime.compiler import CompileError, compile_intent
from services.runtime.typed_resolver import TypedResolution

GOVERNED = {"declared", "certified"}


@dataclass
class SlotCase:
    answer_id: str
    question: str
    verdict: str  # passed | failed | skipped
    evidence: str
    typed: bool
    decisions: list[DecisionRecord] = field(default_factory=list)
    graded: list[GradedDecision] = field(default_factory=list)
    gold: str = "stored"  # stored | attributed
    runs: int = 1
    agreement: int = 1  # runs whose typed intent equals the first run's
    # Compiled SQL vs the certified SQL text, canonicalised: True = proven
    # identical (no warehouse needed), False = written another way, None = no
    # compiled SQL (the question did not type).
    sql_match: bool | None = None
    # Per resolution (mean over runs): wall seconds, provider calls, tokens,
    # and USD from the provider's price (None when unpriced).
    elapsed_s: float = 0.0
    calls: float = 0.0
    input_tokens: float = 0.0
    output_tokens: float = 0.0
    cost_usd: float | None = None


@dataclass
class SlotLaneOutcome:
    cases: list[SlotCase]
    skipped_no_gold: int
    attributed_gold: int = 0
    elapsed_s: float = 0.0
    workers: int = 1
    repeat: int = 1
    # Over every resolution the lane ran: USD (None when any was unpriced)
    # and the per-resolution latencies, for a median and a p90 per arm.
    cost_usd: float | None = None
    latencies_s: list[float] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.verdict == "passed")

    @property
    def failed(self) -> int:
        return sum(1 for c in self.cases if c.verdict == "failed")


def _gold_names(expected: Resolution, kind: str) -> set[str]:
    return {s.name for s in expected.slots if s.kind == kind and s.source in GOVERNED}


def grade_decisions(records: list[DecisionRecord], expected: Resolution) -> list[GradedDecision]:
    """Each slot decision against the gold the stored slots imply.

    ``shape``: a figure (aggregate or ranking) iff the gold carries a measure slot. ``metric`` /
    ``dimension`` / ``grain``: a pick is right when the gold carries that name;
    a ``none`` is right when every gold name of that kind was already picked
    (tracked in order). Filters and definitions are not graded here — their
    gold is a value, which the stored slots carry only as text.
    """
    out: list[GradedDecision] = []
    has_measure = any(s.kind in MEASURE_KINDS and s.source in GOVERNED for s in expected.slots)
    remaining = {
        "metric": _gold_names(expected, "metric"),
        "dimension": _gold_names(expected, "dimension"),
    }
    grain_gold = next(
        (s.value for s in expected.slots if s.kind == "grain" and s.source in GOVERNED), None
    )
    for r in records:
        gold: str | None
        if r.slot == "shape":
            # The stored slots carry no order, so a ranking and a plain aggregate
            # grade as one family: a figure was asked for, or a listing was.
            figure = "ranking" if r.chosen == "ranking" else "aggregate"
            gold = figure if has_measure else "listing"
        elif r.slot in ("metric", "dimension"):
            pool = remaining[r.slot]
            if r.chosen is None:
                gold = None if not pool else next(iter(sorted(pool)))
            else:
                first = next(iter(sorted(pool))) if pool else None
                gold = r.chosen if r.chosen in pool else first
                pool.discard(r.chosen)
        elif r.slot == "grain":
            gold = grain_gold
        else:
            continue
        out.append(
            GradedDecision(
                decider=r.provider,
                slot=r.slot,
                chosen=r.chosen,
                gold=gold,
                p=r.p,
                margin=r.margin,
            )
        )
    return out


def canonical_sql(sql: str, dialect: str) -> str:
    try:
        return sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect).lower()
    except Exception:  # noqa: BLE001 — unparseable SQL canonicalises to itself
        return " ".join(sql.split()).lower()


class EmissionResolver:
    """The lane's view of the one-shot intent emission (typed serving off):
    the generator's QueryIntent graded like a typed one, one ``intent`` record
    carrying the call's tokens and latency. Not a decider — there is nothing
    per slot to grade — but the same accuracy, latency and cost columns, so
    the emission is an arm beside the typed ones."""

    def __init__(
        self,
        generator: Any,
        prose: list[Any],
        *,
        domains: dict[str, list[str]] | None = None,
    ) -> None:
        self._generator = generator
        self._prose = prose
        self._domains = domains or {}
        self.provider = f"json:{getattr(generator, 'model', 'unknown')}"

    def resolve(self, question: str, model: SemanticModel) -> TypedResolution:
        started = time.perf_counter()
        out = self._generator.generate(
            question=question,
            semantic_model=model,
            prose_context=self._prose,
            dialect=model.dialect,
        )
        usage = {
            "calls": 1.0,
            "input_tokens": float(out.input_tokens or 0),
            "output_tokens": float(out.output_tokens or 0),
            "latency_s": round(time.perf_counter() - started, 4),
        }
        record = DecisionRecord(
            slot="intent",
            chosen="emitted" if out.intent is not None else None,
            verdict="act" if out.intent is not None else "clarify",
            provider=self.provider,
            usage=usage,
        )
        if out.intent is None:
            return TypedResolution(
                intent=None,
                decisions=[record],
                clarification=out.clarification,
                decline=None if out.clarification else (out.no_answer_reason or "no intent"),
            )
        return TypedResolution(intent=out.intent, decisions=[record])


def gold_for(a: CertifiedAnswer, model: SemanticModel, resolver: Any) -> Resolution | None:
    """The gold a certified answer grades against: its stored typed resolution,
    else its approved SQL attributed into slots — None when neither carries a
    governed measure (nothing to grade; never a vacuous pass)."""
    if a.resolution:
        stored = Resolution.model_validate(a.resolution)
        # A stored CONSTRUCTION is the approved typed reading; a stored
        # attribution is a cache of the attributor, re-read with today's.
        if stored.method == "construction":
            return stored
    attributed = ledger.attribute(a.sql, model, resolver._domains)
    if any(s.kind in MEASURE_KINDS and s.source in GOVERNED for s in attributed.slots):
        return attributed
    return None


@dataclass
class _Run:
    verdict: str
    evidence: str
    typed: bool
    decisions: list[DecisionRecord]
    graded: list[GradedDecision]
    intent_key: str | None
    sql_match: bool | None = None
    elapsed_s: float = 0.0
    usage: dict[str, tuple[float, float, float]] = field(
        default_factory=dict
    )  # provider -> (calls, in, out)


def _run_once(
    a: CertifiedAnswer,
    model: SemanticModel,
    resolver_for: Callable[[CertifiedAnswer], Any],
    expected: Resolution,
) -> _Run:
    resolver = resolver_for(a)
    started = time.perf_counter()
    try:
        res = resolver.resolve(a.question, model)
    except Exception as exc:  # noqa: BLE001 — a starved decider fails the case, never the lane
        return _Run(
            "failed",
            f"decider unavailable: {type(exc).__name__}: {exc}",
            False,
            [],
            [],
            None,
            elapsed_s=time.perf_counter() - started,
        )
    elapsed = time.perf_counter() - started
    usage: dict[str, tuple[float, float, float]] = {}
    for r in res.decisions:
        if r.usage:
            c, i, o = usage.get(r.provider, (0.0, 0.0, 0.0))
            usage[r.provider] = (
                c + r.usage.get("calls", 0.0),
                i + r.usage.get("input_tokens", 0.0),
                o + r.usage.get("output_tokens", 0.0),
            )
    graded = grade_decisions(res.decisions, expected)
    if res.intent is None:
        why = res.clarification.question if res.clarification else (res.decline or "did not type")
        return _Run(
            "failed",
            f"did not type: {why}",
            False,
            res.decisions,
            graded,
            None,
            elapsed_s=elapsed,
            usage=usage,
        )
    got = ledger.from_intent(res.intent, model, resolver._domains, supplied=res.supplied)
    verdict, evidence = ledger.grade(expected, got)
    sql_match: bool | None = None
    try:
        compiled = compile_intent(res.intent, model)
    except CompileError:
        compiled = None
    if compiled is not None and a.sql.strip():
        sql_match = canonical_sql(compiled, model.dialect) == canonical_sql(a.sql, model.dialect)
    return _Run(
        verdict,
        evidence,
        True,
        res.decisions,
        graded,
        res.intent.model_dump_json(),
        sql_match,
        elapsed_s=elapsed,
        usage=usage,
    )


def _fold(a: CertifiedAnswer, runs: list[_Run], gold: str) -> SlotCase:
    first = runs[0]
    agreement = sum(1 for r in runs if r.intent_key == first.intent_key)
    failed = [r for r in runs if r.verdict != "passed"]
    if failed:
        verdict, evidence = "failed", failed[0].evidence
        if len(runs) > 1:
            evidence = f"{len(failed)}/{len(runs)} runs failed: {evidence}"
    elif agreement < len(runs):
        verdict = "failed"
        evidence = (
            f"not deterministic: {agreement}/{len(runs)} runs produced the first run's intent"
        )
    else:
        verdict, evidence = "passed", first.evidence
    return SlotCase(
        answer_id=a.id,
        question=a.question,
        verdict=verdict,
        evidence=evidence,
        typed=all(r.typed for r in runs),
        decisions=first.decisions,
        graded=[g for r in runs for g in r.graded],
        gold=gold,
        runs=len(runs),
        agreement=agreement,
        sql_match=first.sql_match if all(r.sql_match == first.sql_match for r in runs) else False,
        elapsed_s=round(sum(r.elapsed_s for r in runs) / len(runs), 3),
        calls=round(sum(c for r in runs for c, _i, _o in r.usage.values()) / len(runs), 3),
        input_tokens=round(sum(i for r in runs for _c, i, _o in r.usage.values()) / len(runs), 1),
        output_tokens=round(sum(o for r in runs for _c, _i, o in r.usage.values()) / len(runs), 1),
        cost_usd=_mean_cost(runs),
    )


def _mean_cost(runs: list[_Run]) -> float | None:
    """Mean USD per resolution over the runs; None when any provider is unpriced."""
    from services.observability import cost as pricing

    total: float | None = 0.0
    for r in runs:
        by_provider = {p: (i, o) for p, (_c, i, o) in r.usage.items()}
        total = pricing.add_cost(total, pricing.decisions_cost_usd(by_provider))
    return None if total is None else round(total / len(runs), 6)


def run_slot_lane(
    answers: list[CertifiedAnswer],
    model: SemanticModel,
    resolver_for: Callable[[CertifiedAnswer], Any],
    *,
    concurrency: int = 1,
    repeat: int = 1,
) -> SlotLaneOutcome:
    """Every eligible answer graded ``repeat`` times; ``concurrency`` > 1 runs
    the resolutions on a thread pool (each run gets its own resolver from
    ``resolver_for``, so no per-resolution state is shared). Cases land in
    corpus order either way — the report must not depend on which decision
    came back first."""
    started = time.perf_counter()
    eligible: list[tuple[CertifiedAnswer, Resolution, str]] = []
    skipped = attributed = 0
    for a in answers:
        if not is_active(a.status) or a.slots:
            continue
        expected = gold_for(a, model, resolver_for(a))
        if expected is None:
            skipped += 1
            continue
        if not a.resolution:
            attributed += 1
        eligible.append((a, expected, "stored" if a.resolution else "attributed"))
    repeat = max(1, repeat)
    jobs = [(i, a, expected) for i, (a, expected, _g) in enumerate(eligible) for _ in range(repeat)]
    workers = max(1, min(concurrency, len(jobs)))

    def run(job: tuple[int, CertifiedAnswer, Resolution]) -> _Run:
        _i, a, expected = job
        return _run_once(a, model, resolver_for, expected)

    if workers == 1:
        results = [run(j) for j in jobs]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run, jobs))
    by_case: list[list[_Run]] = [[] for _ in eligible]
    for (i, _a, _e), r in zip(jobs, results, strict=True):
        by_case[i].append(r)
    cases = [_fold(a, by_case[i], g) for i, (a, _e, g) in enumerate(eligible)]
    from services.observability import cost as pricing

    lane_cost: float | None = 0.0
    for r in results:
        by_provider = {p: (i, o) for p, (_c, i, o) in r.usage.items()}
        lane_cost = pricing.add_cost(lane_cost, pricing.decisions_cost_usd(by_provider))
    return SlotLaneOutcome(
        cases=cases,
        skipped_no_gold=skipped,
        attributed_gold=attributed,
        elapsed_s=round(time.perf_counter() - started, 2),
        workers=workers,
        repeat=repeat,
        cost_usd=None if lane_cost is None else round(lane_cost, 6),
        latencies_s=[round(r.elapsed_s, 3) for r in results],
    )
