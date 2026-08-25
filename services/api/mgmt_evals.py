"""Control-plane eval management (/mgmt/lenses/{lens}/evals). Admin-authed, org-scoped.

The platform engineer's "approve, don't author" surface: review auto-generated candidate
cases, approve/retire them, trigger a run, and read the per-lens accuracy trend. Generation
and scoring happen behind the scenes.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.api.llm import require_llm
from services.auth.deps import get_admin_org, get_app_session
from services.contracts.eval import EvalCase
from services.evals import service, store
from services.evals.runner import run_health
from services.lenses import store as lens_store
from services.lenses.connections import resolve_connector
from services.runtime import assembly
from services.runtime.answer import AnswerComposer

router = APIRouter(prefix="/mgmt/lenses/{lens}/evals", tags=["evals"])


def _case_dict(c: store.EvalCaseRow) -> dict[str, object]:
    return {
        "id": c.id,
        "question": c.question,
        "expected_sql": c.expected_sql,
        # A BEHAVIORAL pin carries no expected_sql — without these it rendered
        # as an empty value case, which reads as a broken row rather than a
        # response-shape expectation (migration 0031 made them real columns).
        "expect": c.expect,
        "term": c.term,
        "expected_answer": c.expected_answer,
        "source": c.source,
        "status": c.status,
        "created_by": c.created_by,
        "tags": c.tags,
    }


def _run_dict(r: store.EvalRunRow) -> dict[str, object]:
    return {
        "id": r.id,
        "mode": r.mode,
        "score": r.score,
        "passed": r.passed,
        "failed": r.failed,
        "errored": r.errored,
        "started_at": r.started_at,
    }


def _load_bundle(session: Session, lens: str) -> lens_store.LensBundle:
    """The lens's draft bundle (where eval cases are authored), else its published one."""
    row = lens_store.get_lens(session, lens)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{lens}' not found")
    raw = row.get("draft") or row.get("published")
    if raw is None:
        raise HTTPException(status_code=400, detail=f"lens '{lens}' has no bundle")
    return lens_store.LensBundle.model_validate(raw)


@router.get("/cases")
def list_cases(
    lens: str,
    status: str | None = None,
    tag: str | None = None,
    session: Session = Depends(get_app_session),
) -> list[dict[str, object]]:
    """The lens's eval cases, optionally filtered by status (candidate/approved/
    retired) and/or ``?tag=`` (exact match on any of the case's tags, so a
    battery can be scored per persona/intent slice, not only per lens)."""
    cases = store.list_cases(session, lens, status)
    if tag is not None:
        cases = [c for c in cases if tag in c.tags]
    return [_case_dict(c) for c in cases]


@router.post("/cases/{case_id}/approve")
def approve(lens: str, case_id: str, session: Session = Depends(get_app_session)) -> dict[str, str]:
    """Promote a candidate case into the approved suite. 404 when it's missing
    or already decided."""
    if store.approve_case(session, case_id) == 0:
        raise HTTPException(status_code=404, detail="case not found or not a candidate")
    return {"id": case_id, "status": "approved"}


@router.post("/cases/{case_id}/retire")
def retire(lens: str, case_id: str, session: Session = Depends(get_app_session)) -> dict[str, str]:
    """Retire a case — out of the running suite, kept for history. 404 unknown."""
    if store.retire_case(session, case_id) == 0:
        raise HTTPException(status_code=404, detail="case not found")
    return {"id": case_id, "status": "retired"}


@router.delete("/cases/{case_id}", status_code=204)
def delete(lens: str, case_id: str, session: Session = Depends(get_app_session)) -> None:
    """Hard-delete a case (retire keeps history; this doesn't). 404 unknown."""
    if store.delete_case(session, case_id) == 0:
        raise HTTPException(status_code=404, detail="case not found")


class RunResult(BaseModel):
    run_id: str | None = None
    mode: str
    score: float | None = None
    passed: int = 0
    failed: int = 0
    errored: int = 0
    failing: list[str] = []
    detail: str = ""


@router.post("/run")
def run(
    lens: str,
    mode: str = "health",
    session: Session = Depends(get_app_session),
    org_id: uuid.UUID = Depends(get_admin_org),
) -> RunResult:
    """Run the lens's health suite on the lens's OWN configured model — never a
    tier it doesn't serve on: each approved case with an expected_sql is run
    live and judge-scored against that oracle. The value-case regression mode
    was removed — certified answers are the regression suite;
    run `dst test` (or apply, which gates on the certified corpus)."""
    if mode == "regression":
        raise HTTPException(
            status_code=400,
            detail="mode=regression was removed: certified answers are the "
            "regression suite — run `dst test` (full corpus) or apply "
            "(binding-scoped gate); mode=health remains for judge-scored "
            "live checks",
        )
    if mode != "health":
        raise HTTPException(status_code=400, detail=f"unknown mode '{mode}' (use 'health')")
    bundle = _load_bundle(session, lens)
    outcome = service.build_and_run(
        session=session,
        org_id=org_id,
        lens=lens,
        bundle=bundle,
        mode="health",
        # The LENS's model, not the install's smart tier: it used to pass a tier
        # CLIENT while the runner named the calls from the lens config, which is
        # a 404 (or worse, another vendor) the moment the two disagree. An
        # unpinned lens resolves to the tier anyway; a pinned one 503s here
        # instead of being graded somewhere it does not run.
        resolved=require_llm(bundle.config.model.model_ref()),
    )
    if outcome is None:
        return RunResult(mode=mode, detail="no approved eval cases")
    r = outcome.run
    return RunResult(
        run_id=r.id,
        mode=r.mode,
        score=r.score,
        passed=r.passed,
        failed=r.failed,
        errored=r.errored,
        failing=[res.case_id for res in outcome.results if not res.passed],
    )


class GateDryRunBody(BaseModel):
    """The CANDIDATE tree, exactly as `dst apply` would push it. Optional for
    compatibility — but without it the dry run can only gate the stored
    bundle, which is blind to unpublished shared-asset edits and so can report
    a clean pass where the apply that follows scores red."""

    files: dict[str, str] = {}


@router.post("/gate")
def gate_dry_run(
    lens: str,
    body: GateDryRunBody | None = None,
    session: Session = Depends(get_app_session),
    org_id: uuid.UUID = Depends(get_admin_org),
) -> dict[str, object]:
    """Run this ONE lens's publish gate — the exact code path apply gates on —
    and report the decision without publishing anything — otherwise the only way
    to learn a verdict is a full apply over every armed gate, and `dst test` is
    not the gate's code path.

    With ``files`` (what the CLI now always sends): a REAL apply of the
    candidate — shared semantic assets landed, the lens compiled against
    them, cases synced, the gate scored — all inside a savepoint that rolls
    back. Gating the stored bundle instead false-greens every shared-asset
    change, certifying exactly the pushes the apply then rejects; one code
    path means the dry run cannot drift from the apply.
    Without files, the stored bundle is gated and the response SAYS
    the verdict is blind to unpublished file changes — a loud unknown, never
    a silent false pass.

    True dry run either way: everything staged (semantic upserts, the lens
    publish, the eval run) rolls back, so a red dry run can never become the
    baseline the next real apply regresses against."""
    from services.project import apply as apply_engine
    from services.project.loader import load_lens_source, split_by_lens, split_semantic

    files = body.files if body is not None else {}
    scope_note: str | None = None
    nested = session.begin_nested()
    try:
        if files:
            semantic_files = split_semantic(files)
            if semantic_files:
                try:
                    apply_engine.apply_semantic_assets(session, semantic_files)
                except ValueError as exc:
                    return {
                        "lens": lens,
                        "gate": "blocked",
                        "gate_detail": None,
                        "errors": [str(exc)],
                    }
            tree = split_by_lens(files).get(lens)
            if tree is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"the pushed tree carries no lenses/{lens}/ — check --dir",
                )
            try:
                source = load_lens_source(tree)
            except (ValueError, KeyError) as exc:
                return {
                    "lens": lens,
                    "gate": "blocked",
                    "gate_detail": None,
                    "errors": [str(exc)],
                }
            row = apply_engine.apply_lens(
                session, lens, source, org_id=org_id, created_by="evals-gate-dry-run"
            )
            gate = row.gate if row.gate is not None else ("blocked" if row.errors else "off")
            return {
                "lens": lens,
                "gate": gate,
                "gate_detail": row.gate_detail,
                "errors": row.errors,
            }
        bundle = _load_bundle(session, lens)
        decision = service.publish_gate(session=session, org_id=org_id, lens=lens, bundle=bundle)
        scope_note = (
            "verdict reflects the STORED bundle — unpublished file changes (shared "
            "semantic assets above all) are NOT in it; run from the project dir so "
            "the candidate tree is gated"
        )
        return {
            "lens": lens,
            "gate": service.gate_label(decision) if decision is not None else "off",
            "gate_detail": service.gate_decision_dict(decision),
            "scope_note": scope_note,
        }
    finally:
        nested.rollback()


class QuickcheckBody(BaseModel):
    limit: int = 6


class QuickcheckCase(BaseModel):
    question: str
    passed: bool
    reason: str


class QuickcheckResult(BaseModel):
    score: float
    results: list[QuickcheckCase] = []


@router.post("/quickcheck")
def quickcheck(
    lens: str,
    body: QuickcheckBody | None = None,
    session: Session = Depends(get_app_session),
    org_id: uuid.UUID = Depends(get_admin_org),
) -> QuickcheckResult:
    """Lightweight HEALTH check over the lens's sample_queries, for the wizard's Validate step.

    Unlike ``/run``, this needs no approved cases: it builds ephemeral eval cases
    straight from the (draft/published) bundle's ``sample_queries``, runs them live,
    and judges each with the sample's own SQL as the reference oracle. Nothing is
    persisted (no rows in ``eval_case``/``eval_run``).

    The 'health' runner already errors-out a case (rather than crashing) when generation
    or answering fails; we additionally guard each case so a single failure can't abort the
    batch (e.g. a connector hiccup). ``score`` is fraction passed (0.0 when there's nothing
    to check).
    """
    limit = max(0, (body.limit if body is not None else 6))
    bundle = _load_bundle(session, lens)
    model = bundle.semantic_model
    samples = list(model.sample_queries)[:limit]
    if not samples:
        return QuickcheckResult(score=0.0, results=[])

    cases = [
        EvalCase(
            id=f"qc_{i}",
            lens=lens,
            question=sq.question,
            expected_sql=sq.sql or None,
            source="sample_query",
            status="candidate",
        )
        for i, sq in enumerate(samples)
    ]
    if not bundle.config.connections:
        raise HTTPException(status_code=400, detail="lens has no connection to evaluate against")
    resolved = require_llm(bundle.config.model.model_ref())
    llm, judge_model = resolved.llm, resolved.name
    connector = resolve_connector(bundle.config.connections[0], org_id)
    assemble_q, generators_for = assembly.question_harness(bundle, org_id, resolved)
    composer = AnswerComposer(llm, model=judge_model)

    by_id = {c.id: c for c in cases}
    out: list[QuickcheckCase] = []
    passed = 0
    for c in cases:
        try:
            outcome = run_health(
                connector=connector,
                lens=lens,
                cases=[c],
                assemble_for=assemble_q,
                generators_for=generators_for,
                composer=composer,
                judge_llm=llm,
                judge_model=judge_model,
                model_name=judge_model,
            )
            res = outcome.results[0]
            ok = bool(res.passed)
            reason = res.reason or ("ok" if ok else "judged not approved")
        except Exception as exc:  # noqa: BLE001 — best-effort: one bad case shouldn't abort
            ok = False
            reason = f"error: {exc}"
        if ok:
            passed += 1
        out.append(QuickcheckCase(question=by_id[c.id].question, passed=ok, reason=reason))
    return QuickcheckResult(score=passed / len(out), results=out)


@router.get("/runs")
def list_runs(lens: str, session: Session = Depends(get_app_session)) -> list[dict[str, object]]:
    """The per-lens accuracy trend (newest first) — the chart Observe renders."""
    return [_run_dict(r) for r in store.list_runs(session, lens)]


def _result_dict(r: store.EvalResultRow) -> dict[str, object]:
    return {
        "case_id": r.case_id,
        "question": r.question,
        "passed": r.passed,
        "grade": r.grade,
        "checks": r.checks,
        "actual_sql": r.actual_sql,
        "actual_value": r.actual_value,
        "reason": r.reason,
    }


@router.get("/runs/{run_id}/results")
def run_results(
    lens: str, run_id: str, session: Session = Depends(get_app_session)
) -> list[dict[str, object]]:
    """One run's per-case outcomes, failing first — the drill-down behind a trend
    point. Empty for runs recorded before per-case persistence (migration 0046)."""
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="run not found") from None
    run = store.get_run(session, run_id)
    if run is None or run.lens != lens:
        raise HTTPException(status_code=404, detail="run not found")
    return [_result_dict(r) for r in store.list_results(session, run_id)]
