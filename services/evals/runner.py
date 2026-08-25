"""The eval runner — behavioral mode and health mode.

  - **behavioral** (the shape pins): ``expect: clarify | refuse | answer`` cases run
    through the real pipeline and assert the RESPONSE SHAPE — the things certification
    cannot express (must-clarify, must-refuse, and must-answer: refusal
    measured in both directions), and exactly what pinned the clarify regression.
  - **health** (the monitor): run each case against *live* data and judge-score it (reusing
    the review judge). Catches drift / null-explosion / freshness.

The value-case regression lane (push-down diff against a frozen ``__dst``
snapshot) was removed: certified answers are the sole value-level eval
mechanism — the certified suite is the gate's regression run.

Generation inputs come per question from the assembly seam:
lanes take ``assemble_for(question)`` + ``generators_for(assembled)`` instead of a
fixed model/generator, so they grade the pipeline production runs. Production
callers build the pair with ``assembly.question_harness(bundle, org_id, llm)``.

All are pure orchestration over injected fakes (connector / harness / composer / llm),
so the whole thing is testable without a cloud warehouse or a live model.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from services.contracts.eval import EvalCase, EvalResult, EvalRun
from services.contracts.protocols import Connector, LLMProvider
from services.reviews.judge import judge_trace
from services.reviews.store import Trace as ReviewTrace
from services.runtime.answer import AnswerComposer
from services.runtime.assembly import AssembledInputs, GeneratorsFor
from services.runtime.pipeline import run_query

# Relative tolerance for the certified suite's single-scalar comparison: float
# noise (aggregation order alone can wiggle a DOUBLE's last bits), not a
# rounding amnesty.
_SCALAR_RTOL = 1e-9

# Retry-before-fail: a gate case fails only after failing every
# attempt. 3 attempts total; retries are paid only on failures, so a green
# suite costs exactly one generation per case.
GATE_ATTEMPTS = 3
# A pass-on-retry is a different fact from a clean pass — the reason carries
# this prefix so the gate can count wobble as wobble (and the operator knows
# which questions to certify).
FLAKY_PREFIX = "flaky: "


def _pooled(
    fn: Callable[[EvalCase], EvalResult], items: list[EvalCase], concurrency: int | None
) -> list[EvalResult]:
    """Run *fn* over *items* through a bounded thread pool, results in item
    order. Provider-bound HTTP with no shared mutable state; 1 (the whole test
    suite's pin — scripted fakes are ordered state) is a plain serial loop."""
    from services.config import settings

    workers = max(1, concurrency if concurrency is not None else settings.gate_concurrency)
    if workers == 1 or len(items) <= 1:
        return [fn(item) for item in items]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(fn, items))


@dataclass
class RunOutcome:
    run: EvalRun
    results: list[EvalResult]


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _finish(lens: str, mode: str, results: list[EvalResult]) -> RunOutcome:
    passed = sum(1 for r in results if r.passed)
    errored = sum(1 for r in results if r.grade == "errored")
    failed = len(results) - passed - errored
    total = len(results)
    run = EvalRun(
        id="er_" + uuid.uuid4().hex[:12],
        lens=lens,
        mode=mode,  # type: ignore[arg-type]
        started_at=_now(),
        score=(passed / total if total else None),
        passed=passed,
        failed=failed,
        errored=errored,
    )
    for r in results:
        r.run_id = run.id
    return RunOutcome(run=run, results=results)


def run_behavioral(
    *,
    connector: Connector,
    lens: str,
    cases: list[EvalCase],
    assemble_for: Callable[[str], AssembledInputs],
    generators_for: GeneratorsFor,
    composer: AnswerComposer,
    model_name: str = "claude-sonnet-4-6",
    concurrency: int | None = None,
) -> RunOutcome:
    """Score behavioral expectations by RESPONSE SHAPE: each ``expect`` case runs
    through the real pipeline (deterministic clarify/exclusion pre-checks
    included — that's the behavior being pinned) and passes iff the response
    came back in the expected shape. ``clarify`` → a clarification, on the
    pinned term when given; ``refuse`` → a refused/rejected response with no
    data served; ``answer`` → a data answer (refusal is measured in
    BOTH directions — a lens that regresses into refusing an answerable
    question fails this pin). Shape-match = pass; no result diffing, no judge.

    A failing case is RETRIED before it counts as failed: one
    generation per case and block-on-any-failure made a green apply
    statistically unreachable at suite scale — (1-p)^N with generation's own
    wobble as p, the one-rep verdict dst-flywheel warns against. Retries are
    paid only on failures; a pass-on-retry is marked flaky in its reason so
    wobble reads as wobble, never as a phantom failing case.

    Cases score through a BOUNDED POOL (``concurrency``, default
    settings.gate_concurrency): each case is an isolated run_query with no
    shared mutable state, and strictly-serial execution made a full apply a
    measured 15-30 minutes of back-to-back provider round-trips. Results keep
    case order regardless of completion order; ``1`` restores serial exactly."""
    shape_cases = [c for c in cases if c.expect is not None]

    def _score(c: EvalCase) -> EvalResult:
        ok, reason, grade = False, None, "fail"
        sql: str | None = None
        for attempt in range(GATE_ATTEMPTS):
            assembled = assemble_for(c.question)
            generator, escalate = generators_for(assembled)
            pr = run_query(
                question=c.question,
                lens_name=lens,
                org_id="eval",
                caller="eval",
                semantic_model=assembled.model,
                value_domains=assembled.value_domains,
                connector=connector,
                generator=generator,
                escalate_generator=escalate,
                composer=composer,
                prose_context=assembled.prose,
                model_name=model_name,
                data_as_of=assembled.data_as_of,
            )
            t = pr.trace
            sql = t.sql
            if t.status == "error":
                ok, reason, grade = False, t.error, "errored"
                continue
            grade = "fail"
            if c.expect == "clarify":
                clar = pr.response.clarification
                if clar is None:
                    ok = False
                    reason = (
                        f"expected a clarification, got status={t.status} (the response answered)"
                    )
                elif c.term and clar.term != c.term:
                    ok = False
                    reason = f"clarified on '{clar.term}', expected term '{c.term}'"
                else:
                    ok, reason = True, None
            elif c.expect == "refuse":
                ok = t.status in ("refused", "rejected") and pr.response.data is None
                reason = (
                    None
                    if ok
                    else f"expected a refusal, got status={t.status} with an answer served"
                )
            else:  # answer — the must-answer pin
                ok = t.status == "ok" and pr.response.data is not None
                reason = (
                    None
                    if ok
                    else f"expected a data answer, got status={t.status}: "
                    f"{t.error or 'no data served'}"
                )
            if ok:
                grade = "pass"
                if attempt > 0:
                    reason = f"{FLAKY_PREFIX}passed on attempt {attempt + 1} of {GATE_ATTEMPTS}"
                break
        return EvalResult(
            run_id="",
            case_id=c.id,
            question=c.question,
            passed=ok,
            grade=grade,
            actual_sql=sql,
            reason=reason,
        )

    results = list(_pooled(_score, shape_cases, concurrency))
    return _finish(lens, "behavioral", results)


def run_health(
    *,
    connector: Connector,
    lens: str,
    cases: list[EvalCase],
    assemble_for: Callable[[str], AssembledInputs],
    generators_for: GeneratorsFor,
    composer: AnswerComposer,
    judge_llm: LLMProvider,
    judge_model: str = "claude-sonnet-4-6",
    model_name: str = "claude-sonnet-4-6",
) -> RunOutcome:
    """Run each case live and judge-score it (with the case's expected_sql as the oracle)."""
    results: list[EvalResult] = []
    for c in cases:
        assembled = assemble_for(c.question)
        generator, escalate = generators_for(assembled)
        pr = run_query(
            question=c.question,
            lens_name=lens,
            org_id="eval",
            caller="eval",
            semantic_model=assembled.model,
            value_domains=assembled.value_domains,
            connector=connector,
            generator=generator,
            escalate_generator=escalate,
            composer=composer,
            prose_context=assembled.prose,
            model_name=model_name,
            data_as_of=assembled.data_as_of,
        )
        t = pr.trace
        if t.status != "ok":
            results.append(
                EvalResult(
                    run_id="",
                    case_id=c.id,
                    question=c.question,
                    passed=False,
                    grade="errored",
                    reason=t.error,
                    actual_sql=t.sql,
                )
            )
            continue
        rt = ReviewTrace(
            request_id=t.request_id,
            lens=t.lens,
            caller=t.caller,
            question=t.question,
            sql=t.sql,
            answer=t.answer,
            definition_used=t.definition_used,
            confidence=t.confidence,
            row_count=t.row_count,
        )
        verdict, reasoning = judge_trace(judge_llm, rt, judge_model, reference_sql=c.expected_sql)
        results.append(
            EvalResult(
                run_id="",
                case_id=c.id,
                question=c.question,
                passed=verdict == "approve",
                # NO_VERDICT ("") means the judge returned nothing: the case is
                # UNSCORED, like a broken oracle — never a lens failure the health
                # lane reports as the lens getting worse.
                grade=verdict or "errored",
                reason=reasoning,
                actual_sql=t.sql,
            )
        )
    return _finish(lens, "health", results)
