"""Eval orchestration: resolve the connector, run the suites, persist the run.

Shared by the publish gate and the run endpoint. The pure
gate-decision logic (``gate_decision``) is split out so it's unit-testable without a DB,
connector, or model.

The certified corpus IS the gate's regression suite (the value-case/snapshot
plane it replaced has been removed). Two lanes on apply:

* The VALUE lane grades every ACTIVE certified answer by its own evidence: its
  approved SQL (sample-bound for a template) runs once more and must return
  the stored ``verified_value``. A restated value fails — the signed figure
  moved under the answer. An answer with no stored value gets it recorded
  now. The case set is the whole active corpus, so the score's denominator is
  stable across pushes, and a passing answer re-stamps its bindings to the
  current asset hashes (re-verify through evidence).
* The GENERATION lane runs only the answers whose bindings the push's changed
  asset hashes invalidate (staleness as test selection) through the certified
  suite against the CANDIDATE bundle: does generation still reproduce the
  approved SQL? A divergence is REPORTED, verbatim, naming both results. It
  blocks only under ``eval_gate: block_generation``; under ``block`` the
  certified answer is the override and the push lands. The file-side
  allow-list ``evals/known_divergences.yaml`` (id + reason) covers the
  divergences a team has judged, so the override is not retyped per push.

Both lanes plus the behavioral shape pins fold into one persisted regression
run. Everything stages on the caller's session — blue/green rolls the run AND
the re-stamps back with an aborted apply.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from services.certify import binding as certify_binding
from services.certify import store as certify_store
from services.certify.bindings import restamp_bindings
from services.certify.generate import _value_summary
from services.connectors.tagging import query_context
from services.contracts.errors import ProviderError
from services.contracts.eval import EvalCase, EvalResult
from services.contracts.protocols import Connector
from services.evals import store
from services.evals.certified_suite import (
    _ROW_CAP,
    CertifiedCaseResult,
    _compare,
    _render_template_case,
    format_result,
    run_certified_suite,
)
from services.evals.runner import (
    REPORTED_PREFIX,
    RunOutcome,
    _finish,
    run_behavioral,
    run_health,
)
from services.lenses.connections import ConnectionUnavailable, resolve_connector
from services.lenses.store import LensBundle, bundle_fingerprint
from services.llm.registry import ResolvedModel
from services.project.plan import stale_certified
from services.runtime import assembly
from services.runtime.answer import AnswerComposer
from services.runtime.bounded import ApplyStepTimeout
from services.runtime.canon import canon_cell
from services.runtime.pipeline import _jsonable
from services.semantic import store as semantic_store

log = logging.getLogger("dst")

# A score drop beyond this (absolute, on a 0–1 score) counts as a regression.
REGRESSION_EPS = 1e-9

#: The gate modes that refuse a publish. ``block`` blocks on a restated
#: certified value, a failing behavioral pin or a score regression;
#: ``block_generation`` adds a generation divergence to that list.
BLOCKING_GATES = ("block", "block_generation")


def blocks(gate: str) -> bool:
    return gate in BLOCKING_GATES


def known_divergence_ids(entries: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """``evals/known_divergences.yaml`` as a lookup: the key an entry names
    (``id: certified:<answer id>``, ``generation:<answer id>`` or the bare id,
    or ``question: <the certified question>``), lowercased -> its reason. An
    entry with no reason is not an allow: nobody downstream can tell a judged
    divergence from a silenced one."""
    out: dict[str, str] = {}
    for entry in entries:
        reason = str(entry.get("reason") or "").strip()
        if not reason:
            continue
        for key in ("id", "question"):
            raw = str(entry.get(key) or "").strip().lower()
            if raw:
                out[raw.removeprefix("certified:").removeprefix("generation:")] = reason
    return out


def _known_reason(known: Mapping[str, str], answer: certify_store.CertifiedAnswer) -> str | None:
    return known.get(answer.id.lower()) or known.get(answer.question.strip().lower())


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
        # The published bundle's fingerprint: the identity a later run compares
        # its verdicts against (same fingerprint, same lens).
        lens_version=bundle_fingerprint(bundle),
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
    # Verbatim generation divergences that do NOT block under this gate
    # (eval_gate: block): reported on the row, the certified answer stands.
    generation_divergences: list[str] = field(default_factory=list)
    # ``generation:<id> — reason`` for each divergence evals/known_divergences.yaml
    # covered on this run — what the apply report lists.
    known_covered: list[str] = field(default_factory=list)
    # Why the previous score is, or is not, the bar: "no previous run" / "no
    # previous run with this case set" / "previous run <id>".
    baseline: str = ""
    recorded: int = 0


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
        "generation_divergences": list(decision.generation_divergences),
        "known_covered": list(decision.known_covered),
        "baseline": decision.baseline,
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
    blocked = blocks(gate) and (regressed or bool(failing))
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
    known_divergences: Mapping[str, str] | None = None,
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
            session=session,
            org_id=org_id,
            lens=lens,
            bundle=bundle,
            resolved=pair,
            known_divergences=known_divergences,
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


def _divergence_error(
    question: str,
    result: CertifiedCaseResult,
    changed: list[str],
    sample_bindings: list[dict[str, Any]] | None = None,
) -> str:
    """Decision 3, verbatim shape: both executed values, the asset that moved,
    and the forced human choice — fix the definition, or re-certify/retire.
    A template whose sample binding is an explicit ``start/end`` range names
    the likeliest cause first: explicit ranges are inclusive since 0.10.0, so
    the certified side now covers the end day too while generation reads the
    same period end-exclusive — the sample result changed with the release,
    not with the asset. The line says how to re-record it."""
    what = ", ".join(k.replace("/", " ", 1) for k in changed)
    inclusive = [
        f"{slot} = {value}"
        for binding in sample_bindings or []
        for slot, value in binding.items()
        if isinstance(value, str) and certify_binding.explicit_range_note(value) is not None
    ]
    cause = (
        (
            f" — the sample result changed because explicit ranges are inclusive since "
            f"0.10.0 ({'; '.join(inclusive)} covers its end day too); re-record the sample "
            "with `dst apply --refresh-samples`, or bind the range to the last day meant"
        )
        if inclusive
        else ""
    )
    return (
        f"certified '{question}' diverged: certified SQL → "
        f"{format_result(result.oracle_result)}, generated → "
        f"{format_result(result.generated_result)} ({what} changed this push){cause} — "
        "fix the definition, or re-certify/retire the answer"
    )


@dataclass
class CertifiedLane:
    results: list[EvalResult] = field(default_factory=list)
    # Verbatim messages: a certified value that moved (or SQL that no longer
    # runs) — errors under any blocking gate.
    restated: list[str] = field(default_factory=list)
    # Verbatim generation divergences the allow-list does NOT cover.
    divergences: list[str] = field(default_factory=list)
    # ``case id — reason`` for every divergence the allow-list covered.
    known_covered: list[str] = field(default_factory=list)
    corpus_alive: bool = False
    recorded: int = 0  # answers whose verified value was recorded on this run


def _oracle_sql(a: certify_store.CertifiedAnswer, dialect: str) -> tuple[str | None, str | None]:
    """The executable witness of an answer — its SQL, or a template's first
    sample binding rendered — or (None, why not)."""
    if a.slots:
        rendered = _render_template_case(a, dialect)
        if rendered is None:
            return None, "template slots/sample_bindings failed validation — re-certify"
        return rendered[0], None
    if certify_binding.placeholders_in(a.sql):
        return None, "the stored SQL carries {placeholders} but the row has no slots — re-certify"
    return a.sql, None


def _as_table(summary: Mapping[str, Any]) -> tuple[list[str], list[list[Any]]]:
    if set(summary) == {"value"}:
        return ["value"], [[summary["value"]]]
    return list(summary.get("columns") or []), [list(r) for r in summary.get("rows") or []]


def _restated(question: str, stored: dict[str, Any], now: dict[str, Any]) -> str:
    return (
        f"certified '{question}' restated: stored verified value {format_result(stored)}, "
        f"its approved SQL now returns {format_result(now)} — accept it by writing the new "
        "verified_value in certified_answers.yaml (a template: `dst apply "
        "--refresh-samples`), re-author the sql to re-probe it, or fix the data"
    )


def _modernised(stored: dict[str, Any], now: dict[str, Any]) -> dict[str, Any] | None:
    """The stored value re-read JSON-native when a release before 0.11.1 stored
    it as text — ``str(Decimal)`` for a number, ``str(datetime)`` for a day —
    and the live read says what type the cell is: a numeric string against a
    live number becomes that number (at the precision it was stored with), a
    date-shaped string against live text becomes ISO. None when nothing is
    text that should not be. Labels keep their case: only cells whose live
    counterpart is a number or a date move."""
    want_cols, want_rows = _as_table(stored)
    now_cols, now_rows = _as_table(now)
    witness: dict[str, Any] = {}
    for i, col in enumerate(now_cols):
        cells = (r[i] for r in now_rows if i < len(r) and r[i] is not None)
        witness[col.lower()] = next(cells, None)

    def _cell(col: str, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        live = witness.get(col.lower())
        c = canon_cell(v)
        if isinstance(c, int | float) and not isinstance(c, bool):
            return c if isinstance(live, int | float) and not isinstance(live, bool) else v
        if isinstance(live, str) and isinstance(c, str) and c != v.strip().casefold():
            return c  # a date/timestamp read as its ISO day
        return v

    rows = [[_cell(c, v) for c, v in zip(want_cols, r, strict=False)] for r in want_rows]
    if rows == want_rows:
        return None
    if set(stored) == {"value"}:
        return {"value": rows[0][0]}
    # Everything else the store recorded (the probe's row_count) rides along.
    return {**stored, "columns": want_cols, "rows": rows}


def _value_lane(
    *,
    session: Session,
    connector: Connector,
    lens: str,
    bundle: LensBundle,
    active: list[certify_store.CertifiedAnswer],
    hashes: dict[str, str],
) -> CertifiedLane:
    """Every active answer against its own stored value. One warehouse query
    per answer, no model: the lane is the gate's stable denominator."""
    lane = CertifiedLane(corpus_alive=True)
    dialect = bundle.semantic_model.dialect
    for a in active:
        case_id = f"certified:{a.id}"
        oracle_sql, why = _oracle_sql(a, dialect)
        if oracle_sql is None:
            lane.restated.append(f"certified '{a.question}' cannot run: {why}")
            lane.results.append(
                EvalResult(
                    run_id="",
                    case_id=case_id,
                    question=a.question,
                    passed=False,
                    grade="errored",
                    reason=why,
                )
            )
            continue
        try:
            with query_context(purpose="eval", lens=lens):
                got = connector.execute(oracle_sql, read_only=True, row_limit=_ROW_CAP)
        except ApplyStepTimeout:
            raise
        except Exception as exc:  # noqa: BLE001 — a broken oracle fails its case, not the run
            lane.restated.append(
                f"certified '{a.question}' no longer runs: {exc} — fix the sql or retire the answer"
            )
            lane.results.append(
                EvalResult(
                    run_id="",
                    case_id=case_id,
                    question=a.question,
                    passed=False,
                    grade="errored",
                    reason=f"certified SQL failed to execute: {exc}",
                    actual_sql=oracle_sql,
                )
            )
            continue
        rows = [[_jsonable(v) for v in row] for row in got.rows]
        now = _value_summary(got.columns, rows)
        stored = a.verified_value
        if stored is None:
            certify_store.update(session, a.id, sql=a.sql, verified_value=now)
            lane.recorded += 1
            passed: bool = True
            reason: str | None = "verified value recorded on this run (none was stored)"
        else:
            want_cols, want_rows = _as_table(stored)
            passed, reason = _compare(want_cols, want_rows, *_as_table(now))
            if not passed:
                lane.restated.append(_restated(a.question, stored, now))
            elif (modern := _modernised(stored, now)) is not None:
                # Stored as text by an older release; the value stands, so it
                # is re-recorded in today's shape on this first comparison.
                stored = modern
                certify_store.update(session, a.id, sql=a.sql, verified_value=modern)
                lane.recorded += 1
                reason = "verified value stored as text by an earlier release — re-recorded"
        if passed:
            _questions, changed = stale_certified([(a.question, a.bindings)], hashes)
            if changed:
                # Re-verified by evidence: the approved SQL still returns the
                # signed value on the candidate layer, so the bindings move to
                # the current hashes (rolled back with an aborted apply).
                certify_store.update(
                    session,
                    a.id,
                    sql=a.sql,
                    verified_value=stored if stored else now,
                    bindings=restamp_bindings(oracle_sql, a.bindings, bundle.semantic_model),
                )
        lane.results.append(
            EvalResult(
                run_id="",
                case_id=case_id,
                question=a.question,
                passed=passed,
                grade="pass" if passed else "fail",
                checks={} if passed else {"wrong_at": "value"},
                reason=reason,
                actual_sql=oracle_sql,
                actual_value=format_result(now),
            )
        )
    return lane


def _certified_gate(
    *,
    session: Session,
    org_id: uuid.UUID | str,
    lens: str,
    bundle: LensBundle,
    resolved: ResolvedModel,
    known: Mapping[str, str] | None = None,
) -> CertifiedLane:
    """The certified corpus as gate cases: the value lane over every active
    answer, then the generation lane over the answers whose stored bindings
    disagree with the current asset hashes (the push's changes, post-upsert —
    cheap by construction; `dst test` is the full sweep). A generation result
    rides the run under ``generation:<id>`` and never moves the score; a
    divergence the allow-list covers is listed as covered, with its reason."""
    active = [
        a for a in certify_store.list_for_lens(session, lens) if certify_store.is_active(a.status)
    ]
    if not active:
        return CertifiedLane()
    config = bundle.config
    if not config.connections:
        # Nothing can execute the approved SQL: the gate stands down loudly
        # (publish_gate turns this into the warehouse-unavailable skip).
        raise ConnectionUnavailable("the lens declares no connection to run certified SQL on")
    connector = _connector_for(session, org_id, config.connections[0])
    hashes = semantic_store.asset_hashes(session)
    affected: list[tuple[certify_store.CertifiedAnswer, list[str]]] = []
    for a in active:
        _questions, changed = stale_certified([(a.question, a.bindings)], hashes)
        if changed:
            affected.append((a, changed))
    lane = _value_lane(
        session=session,
        connector=connector,
        lens=lens,
        bundle=bundle,
        active=active,
        hashes=hashes,
    )
    if not affected:
        return lane
    assemble_for, generators_for = assembly.eval_harness(bundle, org_id, resolved)
    outcome = run_certified_suite(
        connector=connector,
        lens=lens,
        answers=[a for a, _changed in affected],
        assemble_for=assemble_for,
        generators_for=generators_for,
        composer=AnswerComposer(resolved.llm, model=resolved.name),
        model_name=resolved.name,
    )
    by_id = {r.answer_id: r for r in outcome.results}
    for a, changed in affected:
        r = by_id[a.id]
        case_id = f"{REPORTED_PREFIX}{a.id}"
        if not r.passed:
            message = _divergence_error(a.question, r, changed, a.sample_bindings)
            reason = _known_reason(known or {}, a)
            if reason is not None:
                lane.known_covered.append(f"{case_id} — {reason}")
            else:
                lane.divergences.append(message)
        lane.results.append(
            EvalResult(
                run_id="",
                case_id=case_id,
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
    return lane


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


def previous_baseline(
    session: Session, lens: str, case_ids: Iterable[str]
) -> tuple[float | None, str]:
    """The previous regression run's score as the bar, and why: only a run
    over the SAME case set compares — a score over a different selection is
    a different fraction, and "regressed" against it was noise ("0.4 < prev
    0.75" with the same four red answers). Generation lane cases are not part
    of the identity: they are reported, never scored."""
    prev = store.last_run(session, lens, "regression")
    if prev is None:
        return None, "no previous run"
    wanted = {c for c in case_ids if not c.startswith(REPORTED_PREFIX)}
    had = {
        r.case_id
        for r in store.list_results(session, prev.id)
        if not r.case_id.startswith(REPORTED_PREFIX)
    }
    if had != wanted:
        return None, "no previous run with this case set"
    return prev.score, f"previous run {prev.id}"


def evaluate_publish(
    *,
    session: Session,
    org_id: uuid.UUID | str,
    lens: str,
    bundle: LensBundle,
    resolved: ResolvedModel,
    known_divergences: Mapping[str, str] | None = None,
) -> GateDecision:
    """Run the regression gate for a draft about to publish."""
    gate = bundle.config.eval_gate
    if gate == "off":
        return GateDecision(gated=False, detail="gate off")
    lane = _certified_gate(
        session=session,
        org_id=org_id,
        lens=lens,
        bundle=bundle,
        resolved=resolved,
        known=known_divergences,
    )
    behavioral_results = _behavioral_gate(
        session=session, org_id=org_id, lens=lens, bundle=bundle, resolved=resolved
    )
    scored = lane.results + behavioral_results
    # Baseline = the last PERSISTED regression run over the same case set, read
    # before this run is recorded. Apply is blue/green: a blocked publish aborts
    # the whole apply and its create_run rolls back with it, so persisted runs
    # only ever come from applies that fully published (or an operator's
    # explicit `evals run`) — a failing attempt can never lower the bar.
    prev, baseline = previous_baseline(session, lens, (r.case_id for r in scored))
    outcome = build_and_run(
        session=session,
        org_id=org_id,
        lens=lens,
        bundle=bundle,
        mode="regression",
        resolved=resolved,
        extra_results=scored,
    )
    if outcome is None:
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
    failing = [
        r.case_id
        for r in outcome.results
        if not r.passed and not r.case_id.startswith(REPORTED_PREFIX)
    ]
    if gate == "block_generation":
        covered = {c.split(" — ", 1)[0] for c in lane.known_covered}
        failing += [
            r.case_id
            for r in outcome.results
            if not r.passed and r.case_id.startswith(REPORTED_PREFIX) and r.case_id not in covered
        ]
    decision = gate_decision(gate=gate, score=outcome.run.score, prev_score=prev, failing=failing)
    decision.baseline = baseline
    decision.recorded = lane.recorded
    decision.known_covered = lane.known_covered
    # Pass-on-retry cases: a different fact from a clean pass —
    # surfaced so wobble is visible and certifiable, never a phantom failure.
    from services.evals.runner import FLAKY_PREFIX

    decision.flaky = [
        r.case_id for r in outcome.results if r.passed and (r.reason or "").startswith(FLAKY_PREFIX)
    ]
    # A restated certified value is an error in its own right under any
    # blocking gate: the signed figure moved, and the apply aborts until the
    # value is re-accepted or the data fixed. A generation divergence is the
    # same kind of error only under block_generation; under block (and warn)
    # it is reported, verbatim, and the certified answer stands.
    decision.certified_failures = list(lane.restated)
    if gate == "block_generation":
        decision.certified_failures += lane.divergences
    else:
        decision.generation_divergences = lane.divergences
    if decision.certified_failures and blocks(gate):
        decision.blocked = True
    return decision
