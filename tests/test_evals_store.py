"""Eval store tests: ``needs_db``-gated round-trips of ``eval_case`` /
``eval_run`` / ``eval_result`` through ``org_session``, with the same
admin-helper pattern as ``test_certify.py``. Behavioral shapes only — there is
no value-case/snapshot plane and no warehouse DDL helpers."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.contracts.eval import EvalResult
from services.db.session import org_session
from services.evals import store

# ---------------------------------------------------------------------------
# DB reachability guard (mirrors test_certify.py)
# ---------------------------------------------------------------------------


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------


def _org() -> object:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        return c.execute(
            text("INSERT INTO org (name) VALUES ('EvalStoreTest') RETURNING id")
        ).scalar_one()


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM eval_run WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM eval_case WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


# ---------------------------------------------------------------------------
# DB-gated round-trip tests
# ---------------------------------------------------------------------------


@needs_db
def test_eval_case_lifecycle() -> None:
    """Create candidate → approve → list approved — with org isolation."""
    org = _org()
    try:
        # Create two candidate behavioral cases
        with org_session(org) as s:
            case_id_a = store.create_case(
                s,
                lens="churn",
                question="What do you mean by active?",
                source="authored",
                expect="clarify",
                term="active",
            )
            _case_id_b = store.create_case(
                s,
                lens="churn",
                question="What is the churn rate?",
                source="harvested",
            )

        # List all cases for the lens
        with org_session(org) as s:
            all_cases = store.list_cases(s, "churn")
            assert len(all_cases) == 2
            assert all(c.status == "candidate" for c in all_cases)

        # Approve case A
        with org_session(org) as s:
            affected = store.approve_case(s, case_id_a)
            assert affected == 1

        # list_cases with status filter
        with org_session(org) as s:
            approved = store.list_cases(s, "churn", status="approved")
            assert len(approved) == 1
            assert approved[0].id == case_id_a
            assert approved[0].expect == "clarify"
            assert approved[0].term == "active"

            candidates = store.list_cases(s, "churn", status="candidate")
            assert len(candidates) == 1

        # Retire case A
        with org_session(org) as s:
            affected = store.retire_case(s, case_id_a)
            assert affected == 1

        with org_session(org) as s:
            retired = store.list_cases(s, "churn", status="retired")
            assert len(retired) == 1

        # Delete case A
        with org_session(org) as s:
            affected = store.delete_case(s, case_id_a)
            assert affected == 1
            remaining = store.list_cases(s, "churn")
            assert len(remaining) == 1

    finally:
        _cleanup(org)


@needs_db
def test_eval_run_lifecycle() -> None:
    """Create run → list runs for lens."""
    org = _org()
    try:
        with org_session(org) as s:
            run_id = store.create_run(
                s,
                lens="churn",
                mode="behavioral",
                lens_version="v2",
                score=0.92,
                passed=23,
                failed=2,
                errored=0,
            )

        with org_session(org) as s:
            runs = store.list_runs(s, "churn")
            assert len(runs) == 1
            r = runs[0]
            assert r.id == run_id
            assert r.mode == "behavioral"
            assert r.lens_version == "v2"
            assert r.score is not None and abs(r.score - 0.92) < 1e-9
            assert r.passed == 23
            assert r.failed == 2
            assert r.errored == 0

        # No cross-lens leakage
        with org_session(org) as s:
            other_runs = store.list_runs(s, "revenue")
            assert len(other_runs) == 0

    finally:
        _cleanup(org)


@needs_db
def test_eval_result_roundtrip() -> None:
    """Persist a run's per-case outcomes and read them back, failing first."""
    org = _org()
    try:
        with org_session(org) as s:
            run_id = store.create_run(
                s, lens="churn", mode="behavioral", score=0.5, passed=1, failed=1
            )
            store.record_results(
                s,
                run_id,
                [
                    EvalResult(
                        run_id=run_id,
                        case_id="c-pass",
                        question="What do you mean by active?",
                        passed=True,
                        grade="pass",
                        actual_sql=None,
                    ),
                    EvalResult(
                        run_id=run_id,
                        case_id="c-fail",
                        question="What is the churn rate?",
                        passed=False,
                        grade="fail",
                        actual_sql="SELECT churn FROM kpis",
                        reason="expected a clarification, got status=ok (the response answered)",
                    ),
                ],
            )

        with org_session(org) as s:
            rows = store.list_results(s, run_id)
            assert len(rows) == 2
            failing = rows[0]  # failing first
            assert failing.case_id == "c-fail"
            assert failing.passed is False
            assert failing.question == "What is the churn rate?"
            assert failing.reason == (
                "expected a clarification, got status=ok (the response answered)"
            )
            assert rows[1].passed is True
            assert rows[1].case_id == "c-pass"

        # Unknown run → empty, and get_run resolves the run's lens
        with org_session(org) as s:
            assert store.list_results(s, str(uuid.uuid4())) == []
            run = store.get_run(s, run_id)
            assert run is not None and run.lens == "churn"
            assert store.get_run(s, str(uuid.uuid4())) is None
    finally:
        _cleanup(org)
