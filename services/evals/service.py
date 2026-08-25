"""Eval orchestration: resolve the connector, run the suites, persist the run.

Shared by the publish gate and the run endpoint. The pure
gate-decision logic (``gate_decision``) is split out so it's unit-testable without a DB,
connector, or model.

The certified corpus IS the gate's regression suite (the value-case/snapshot
plane it replaced has been removed). On apply, the ACTIVE
certified answers whose bindings the push's changed asset hashes invalidate
(the staleness system as test selection) run through the certified suite
against the CANDIDATE bundle; their results — plus the behavioral shape pins —
fold into one persisted regression run. A diverging answer becomes a verbatim
divergence error; a passing one re-stamps its bindings to current hashes
(re-verify through evidence). Everything stages on the caller's session —
blue/green rolls the run AND the re-stamps back with an aborted apply.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from services.certify import store as certify_store
from services.certify.bindings import restamp_bindings
from services.contracts.errors import ProviderError
from services.contracts.eval import EvalCase, EvalResult
from services.contracts.protocols import Connector
from services.evals import store
from services.evals.certified_suite import (
    CertifiedCaseResult,
    _render_template_case,
    format_result,
    run_certified_suite,
)
from services.evals.runner import RunOutcome, _finish, run_behavioral, run_health
from services.lenses.connections import ConnectionUnavailable, resolve_connector
from services.lenses.store import LensBundle
from services.llm.registry import ResolvedModel
from services.project.plan import stale_certified
from services.runtime import assembly
from services.runtime.answer import AnswerComposer
from services.semantic import store as semantic_store

log = logging.getLogger("dst")

# A score drop beyond this (absolute, on a 0–1 score) counts as a regression.
REGRESSION_EPS = 1e-9


def _connector_for(session: Session, org_id: uuid.UUID | str, connection: str) -> Connector:
    """The eval path's connector, resolved on the CALLER's session.

    Apply stages the push's connections on that session and commits nothing
    until the whole push succeeds (blue/green), so resolving on a session of our
    own was blind to them: the FIRST apply of a project — dst.yaml, the lens
    and evals/cases.yaml in one push — died on `unknown connection 'x'`, and the
    only workaround was applying twice. Whatever else stops a connector building
    becomes a ConnectionUnavailable the publish gate degrades on; an
    unresolvable connection must never reach the caller as an opaque 500."""
    try:
        return resolve_connector(connection, org_id, session=session)
    except ConnectionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — a dead credential skips the gate, never 500s it
        raise ConnectionUnavailable(f"connection '{connection}' is unavailable: {exc}") from exc


def _row_to_case(r: store.EvalCaseRow) -> EvalCase:
    return EvalCase(
        id=r.id,
        lens=r.lens,
        question=r.question,
        expected_sql=r.expected_sql,
        expected_answer=r.expected_answer,
        expect=r.expect,  # type: ignore[arg-type]
        term=r.term,
        snapshot_ref=r.snapshot_ref,
        source=r.source,  # type: ignore[arg-type]
        status=r.status,  # type: ignore[arg-type]
        created_by=r.created_by,
        tags=r.tags,
    )


def build_and_run(
    *,
    session: Session,
    org_id: uuid.UUID | str,
    lens: str,
    bundle: LensBundle,
    mode: str,
    resolved: ResolvedModel,
    extra_results: list[EvalResult] | None = None,
) -> RunOutcome | None:
    """Run what remains runnable and persist the run.

    The value-case regression plane is gone: certified answers are the
    regression suite, so the gate path arrives here with its score
    already computed — ``extra_results`` (certified suite + behavioral pins)
    persist as one run. ``mode="health"`` still runs live: each approved case
    with an expected_sql (legacy value cases pending ``dst evals migrate``) is
    judge-scored with that SQL as the reference oracle. Returns ``None`` when
    there is nothing at all to score.
    """
    approved = (
        [
            c
            for c in (_row_to_case(r) for r in store.list_cases(session, lens, status="approved"))
            if c.expected_sql
        ]
        if mode == "health"
        else []
    )
    extra = extra_results or []
    if not approved and not extra:
        return None
    config = bundle.config
    if approved:
        if not config.connections:
            raise ValueError("lens has no connection to evaluate against")
        connector = _connector_for(session, org_id, config.connections[0])
        assemble_q, generators_for = assembly.question_harness(bundle, org_id, resolved)
        outcome = run_health(
            connector=connector,
            lens=lens,
            cases=approved,
            assemble_for=assemble_q,
            generators_for=generators_for,
            composer=AnswerComposer(resolved.llm, model=resolved.name),
            judge_llm=resolved.llm,
            judge_model=resolved.name,
            # Omitting this left runner.py's claude-named parameter default
            # live — the one member of that family a caller actually reached.
            model_name=resolved.name,
        )
        if extra:
            outcome = _finish(lens, mode, outcome.results + extra)
    else:
        outcome = _finish(lens, mode, extra)
    run = outcome.run
    run_id = store.create_run(
        session,
        lens,
        run.mode,
        score=run.score,
        passed=run.passed,
        failed=run.failed,
        errored=run.errored,
    )
    # The persisted row's uuid replaces the runner's in-memory id, so the id a
    # caller gets back is the one /runs and /runs/{id}/results answer to.
    run.id = run_id
    for r in outcome.results:
        r.run_id = run_id
    store.record_results(session, run_id, outcome.results)
    return outcome


@dataclass
class GateDecision:
    gated: bool
    blocked: bool = False
    regressed: bool = False
    score: float | None = None
    prev_score: float | None = None
    failing: list[str] = field(default_factory=list)
    detail: str = ""
    # Why a configured gate did NOT score (gated=False): "empty suite" is the
    # benign case (nothing to score); "provider error" / "warehouse
    # unavailable" / "model unservable" mean the SCORER broke — the moments a
    # gated pipeline most wants to fail closed on. One message shape used to
    # cover both; --require-gates and the apply footer read this field to tell
    # them apart.
    skip_reason: str | None = None
    # Verbatim divergence messages, one per certified answer whose
    # generated result no longer reproduces its certified SQL. Under
    # eval_gate: block these are errors in their own right (the apply aborts
    # until one side is fixed in the same push); under warn they surface loudly.
    certified_failures: list[str] = field(default_factory=list)
    # Case ids that passed only on retry — wobble, not failure:
    # never blocks, always surfaced (the fix for wobble is certification).
    flaky: list[str] = field(default_factory=list)


def gate_label(decision: GateDecision | None) -> str | None:
    """The one-word verdict the apply footer and `dst evals gate` both print —
    one implementation so the two surfaces can never disagree — a footer that
    contradicts the gate is exactly this logic drifting."""
    if decision is None:
        return None
    if not decision.gated:
        return f"skipped ({decision.skip_reason or 'unscored'})"
    if decision.blocked:
        return "blocked"
    if decision.regressed:
        return "regressed"
    if decision.failing:
        # warn-mode with red cases: "passed" would be a lie — a 0.5 run with a
        # failing case must not print '1 passed'.
        return f"failing ({len(decision.failing)} case(s), score {decision.score})"
    return "passed"


def gate_decision_dict(decision: GateDecision | None) -> dict[str, object] | None:
    """The decision as a serializable fragment — ONE shape shared by apply's
    --json rows and the mgmt gate endpoint. A row carrying only a prose label
    makes machine output a strict subset of what the gate knew, so nothing can
    be diffed between applies or asserted in CI."""
    if decision is None:
        return None
    return {
        "gated": decision.gated,
        "blocked": decision.blocked,
        "regressed": decision.regressed,
        "score": decision.score,
        "prev_score": decision.prev_score,
        "failing": list(decision.failing),
        "skip_reason": decision.skip_reason,
        "certified_failures": list(decision.certified_failures),
        "flaky": list(decision.flaky),
        "detail": decision.detail,
    }


def gate_decision(
    *, gate: str, score: float | None, prev_score: float | None, failing: list[str]
) -> GateDecision:
    """Pure: given the gate mode and the new/previous scores, decide warn vs block.

    ``block`` blocks on FAILING CASES, not only on a score decrease: a
    regression-only rule means a lens's first gated apply publishes
    ANY failure (no prev_score = no possible regression — exactly when the
    gate is most needed), and a case promoted red bakes its failure into the
    baseline, silently lowering every later bar. The same break now blocks the
    first time and the second time alike; regression detection stays as the
    additional signal for score drops the failing list can't see (e.g. a
    certified-suite case that stopped being selected)."""
    regressed = prev_score is not None and score is not None and score < prev_score - REGRESSION_EPS
    blocked = gate == "block" and (regressed or bool(failing))
    return GateDecision(
        gated=True,
        blocked=blocked,
        regressed=regressed,
        score=score,
        prev_score=prev_score,
        failing=failing,
    )


def publish_gate(
    *,
    session: Session,
    org_id: uuid.UUID | str,
    lens: str,
    bundle: LensBundle,
) -> GateDecision | None:
    """The publish-time gate check, shared by the interactive publish endpoint and
    apply's publish/recompile paths (review wave 2 — apply must not bypass it).

    ``None`` means the lens opted out (``eval_gate: off``) and publish proceeds
    ungated. A decision with ``gated=False`` means the gate is CONFIGURED but
    could not score — no smart-tier model resolves (keyless environments), or
    the lens has no approved eval cases — and ``detail`` says why; callers must
    surface it loudly, because a seatbelt that silently stands down is worse
    than none. ``blocked`` refuses the publish, otherwise
    surface the scores."""
    gate = bundle.config.eval_gate
    if gate == "off":
        return None
    from services.llm import registry

    # The lens's OWN model ref, and NOTHING else. There used to be an
    # `or registry.resolve(registry.tier("smart"))` fallback here, which sounds
    # harmless and is not: it fires exactly when the lens PINS a model this
    # install cannot serve, and then scores the lens on a different vendor's
    # model. A lens with no `model:` block needs no fallback — the empty ref
    # already resolves through `default_ref()` (smart tier, else fast).
    ref = bundle.config.model.model_ref()
    pair = registry.resolve(ref)
    if pair is not None:
        # The gate rides a LONGER backoff than serving: a sustained rate-limit
        # window used to outlast the serving policy's ~3s and turn into
        # "skipped (provider error)" — an apply publishing UNGATED with the
        # warning in a footer. A bounded pool raises the pressure, so the gate
        # absorbs limit windows as slowness (worst ~75s of sleep per call)
        # instead of standing down; the skip path remains only for failures
        # backoff cannot cure.
        from services.llm.retry import RetryingLLM

        pair = registry.ResolvedModel(
            llm=RetryingLLM(pair.llm, attempts=5, base_delay=5.0),
            provider=pair.provider,
            name=pair.name,
        )
    if pair is None:
        return GateDecision(
            gated=False,
            skip_reason="model unservable",
            detail=(
                f"eval_gate: {gate} configured but this lens's model cannot be served "
                f"here: {registry.unservable_detail(ref)} — gate SKIPPED"
            ),
        )
    try:
        decision = evaluate_publish(
            session=session, org_id=org_id, lens=lens, bundle=bundle, resolved=pair
        )
    except ProviderError as exc:
        # The model provider failed mid-suite. Degrade loudly, never sink the
        # apply — same doctrine as ConnectionUnavailable below.
        return GateDecision(
            gated=False,
            skip_reason="provider error",
            detail=f"eval_gate: {gate} configured but the model provider failed "
            f"({exc}) — gate SKIPPED",
        )
    except ConnectionUnavailable as exc:
        # The lens's warehouse won't build a connector, so nothing CAN score.
        # That is a skip to say out loud, never an opaque 500 (which is what an
        # apply's first push used to get, back when the gate resolved on a
        # session that could not see the connection the push itself declared).
        return GateDecision(
            gated=False,
            skip_reason="warehouse unavailable",
            detail=f"eval_gate: {gate} configured but {exc} — gate SKIPPED",
        )
    if not decision.gated:
        return GateDecision(
            gated=False,
            skip_reason=decision.skip_reason or "empty suite",
            detail=f"eval_gate: {gate} configured but {decision.detail} — gate SKIPPED",
        )
    return decision


def _divergence_error(question: str, result: CertifiedCaseResult, changed: list[str]) -> str:
    """Decision 3, verbatim shape: both executed values, the asset that moved,
    and the forced human choice — fix the definition, or re-certify/retire."""
    what = ", ".join(k.replace("/", " ", 1) for k in changed)
    return (
        f"certified '{question}' diverged: certified SQL → "
        f"{format_result(result.oracle_result)}, generated → "
        f"{format_result(result.generated_result)} ({what} changed this push) — "
        "fix the definition, or re-certify/retire the answer"
    )


def _certified_gate(
    *,
    session: Session,
    org_id: uuid.UUID | str,
    lens: str,
    bundle: LensBundle,
    resolved: ResolvedModel,
) -> tuple[list[EvalResult], list[str], bool]:
    """The certified corpus as gate cases, binding-scoped.

    Selection is the staleness system: only ACTIVE answers whose stored bindings
    disagree with the current asset hashes (the push's changes, post-upsert) run
    — cheap by construction; `dst test` is the full-corpus sweep. Each
    selected answer runs through the certified suite against the CANDIDATE
    bundle. A GREEN answer is re-verified by evidence: its bindings re-stamp to
    current hashes on this session (rolled back with an aborted apply); a
    diverging one becomes a verbatim divergence message.

    Returns (suite results as EvalResults, divergence messages, corpus_alive) —
    ``corpus_alive`` says the lens HAS active certified answers, so an empty
    selection means "nothing this push touches", not an inert seatbelt.
    """
    active = [
        a for a in certify_store.list_for_lens(session, lens) if certify_store.is_active(a.status)
    ]
    if not active:
        return [], [], False
    hashes = semantic_store.asset_hashes(session)
    affected: list[tuple[certify_store.CertifiedAnswer, list[str]]] = []
    for a in active:
        _questions, changed = stale_certified([(a.question, a.bindings)], hashes)
        if changed:
            affected.append((a, changed))
    if not affected:
        return [], [], True
    config = bundle.config
    assemble_for, generators_for = assembly.eval_harness(bundle, org_id, resolved)
    outcome = run_certified_suite(
        connector=_connector_for(session, org_id, config.connections[0]),
        lens=lens,
        answers=[a for a, _changed in affected],
        assemble_for=assemble_for,
        generators_for=generators_for,
        composer=AnswerComposer(resolved.llm, model=resolved.name),
        model_name=resolved.name,
    )
    by_id = {r.answer_id: r for r in outcome.results}
    results: list[EvalResult] = []
    failures: list[str] = []
    for a, changed in affected:
        r = by_id[a.id]
        if r.passed:
            # Templates re-stamp from the RENDERED sample-bound SQL — raw
            # {placeholder} SQL never parses.
            rendered = _render_template_case(a, bundle.semantic_model.dialect) if a.slots else None
            certify_store.update(
                session,
                a.id,
                sql=a.sql,
                verified_value=a.verified_value,
                bindings=restamp_bindings(
                    rendered[0] if rendered else a.sql, a.bindings, bundle.semantic_model
                ),
            )
        else:
            failures.append(_divergence_error(a.question, r, changed))
        results.append(
            EvalResult(
                run_id="",
                case_id=f"certified:{a.id}",
                question=a.question,
                passed=r.passed,
                grade="pass" if r.passed else ("errored" if "error" in r.oracle_result else "fail"),
                checks=(
                    {"wrong_at": "rows"} if not r.passed and "error" not in r.oracle_result else {}
                ),
                reason=r.reason,
                actual_sql=r.generated_sql,
            )
        )
    return results, failures, True


def _behavioral_gate(
    *,
    session: Session,
    org_id: uuid.UUID | str,
    lens: str,
    bundle: LensBundle,
    resolved: ResolvedModel,
) -> list[EvalResult]:
    """Approved behavioral expectations (expect: clarify|refuse) scored against
    the CANDIDATE bundle, folded into the same gate run as the certified suite.
    Not binding-scoped — the pins are the slim rest of cases.yaml and assert
    response SHAPE, which any part of the push can regress (the demo's
    must-clarify dies the moment 'value' becomes guessable)."""
    cases = [
        c
        for c in (_row_to_case(r) for r in store.list_cases(session, lens, status="approved"))
        if c.expect is not None
    ]
    if not cases or not bundle.config.connections:
        return []
    assemble_q, generators_for = assembly.question_harness(bundle, org_id, resolved)
    outcome = run_behavioral(
        connector=_connector_for(session, org_id, bundle.config.connections[0]),
        lens=lens,
        cases=cases,
        assemble_for=assemble_q,
        generators_for=generators_for,
        composer=AnswerComposer(resolved.llm, model=resolved.name),
        model_name=resolved.name,
    )
    return outcome.results


def evaluate_publish(
    *,
    session: Session,
    org_id: uuid.UUID | str,
    lens: str,
    bundle: LensBundle,
    resolved: ResolvedModel,
) -> GateDecision:
    """Run the regression gate for a draft about to publish."""
    gate = bundle.config.eval_gate
    if gate == "off":
        return GateDecision(gated=False, detail="gate off")
    # Baseline = the last PERSISTED regression run, read before this run is
    # recorded. Apply is blue/green: a blocked publish aborts the whole apply
    # and its create_run rolls back with it, so persisted runs only ever come
    # from applies that fully published (or an operator's explicit `evals run`)
    # — a failing attempt can never lower the bar for the next one. No
    # flag/join needed; atomicity IS the filter.
    prev = store.last_run_score(session, lens, "regression")
    certified_results, certified_failures, corpus_alive = _certified_gate(
        session=session, org_id=org_id, lens=lens, bundle=bundle, resolved=resolved
    )
    behavioral_results = _behavioral_gate(
        session=session, org_id=org_id, lens=lens, bundle=bundle, resolved=resolved
    )
    outcome = build_and_run(
        session=session,
        org_id=org_id,
        lens=lens,
        bundle=bundle,
        mode="regression",
        resolved=resolved,
        extra_results=certified_results + behavioral_results,
    )
    if outcome is None:
        if corpus_alive:
            # The corpus exists; this push touched no answer's bindings — test
            # selection selected zero, which is a quiet pass, not an inert gate.
            return GateDecision(
                gated=True,
                score=None,
                prev_score=prev,
                detail="no binding-affected certified answers this push — nothing to re-test",
            )
        # "empty suite" while hundreds of cases exist sends an operator hunting
        # a file-ingest bug: candidates are not scored, and the detail must say
        # how many are parked rather than implying absence.
        candidates = sum(1 for c in store.list_cases(session, lens) if c.status == "candidate")
        parked = (
            f" — {candidates} candidate case(s) exist but are not scored; promote "
            "them with status: approved in cases.yaml"
            if candidates
            else " (certify answers — the regression suite — or pin expect: cases "
            "with status: approved)"
        )
        return GateDecision(
            gated=False,
            skip_reason="empty suite" if not candidates else "no approved cases",
            detail=f"no active certified answers and no approved behavioral cases{parked}",
        )
    failing = [r.case_id for r in outcome.results if not r.passed]
    decision = gate_decision(gate=gate, score=outcome.run.score, prev_score=prev, failing=failing)
    # Pass-on-retry cases: a different fact from a clean pass —
    # surfaced so wobble is visible and certifiable, never a phantom failure.
    from services.evals.runner import FLAKY_PREFIX

    decision.flaky = [
        r.case_id for r in outcome.results if r.passed and (r.reason or "").startswith(FLAKY_PREFIX)
    ]
    decision.certified_failures = certified_failures
    if certified_failures and gate == "block":
        # A certified divergence blocks in its own right (decision 3): the apply
        # aborts until the definition is fixed or the answer re-certified/retired
        # in the same push — baseline machinery untouched for everything else.
        decision.blocked = True
    return decision
