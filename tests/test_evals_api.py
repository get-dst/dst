"""Eval management API round-trip — generate candidates, approve/retire,
list the per-lens runs. Mirrors tests/test_lens_api.py setup."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.lenses.demo import jaffle_customer_value_bundle

client = TestClient(app)


def _create_lens(headers: dict[str, str]) -> int:
    body = jaffle_customer_value_bundle().model_dump(mode="json")
    return client.post("/mgmt/lenses", json=body, headers=headers).status_code


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


def _org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('EvalApi') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM eval_run WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM eval_case WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_observe_eval_trend() -> None:
    """The per-lens accuracy trend the Observe dashboard charts."""
    from services.db.session import org_session
    from services.evals import store as eval_store

    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        with org_session(org) as s:
            eval_store.create_run(s, "customer_value", "regression", score=0.6, passed=3, failed=2)
            eval_store.create_run(s, "customer_value", "regression", score=0.8, passed=4, failed=1)
            eval_store.create_run(s, "customer_value", "health", score=1.0, passed=5)
        r = client.get("/mgmt/observe/evals", headers=h)
        assert r.status_code == 200, r.text
        trend = next(t for t in r.json() if t["lens"] == "customer_value")
        assert trend["latest_score"] == 1.0
        scores = [x["score"] for x in trend["runs"]]
        assert scores == [0.6, 0.8, 1.0]  # oldest → newest: the chart shows improvement
        assert trend["runs"][-1]["mode"] == "health"
        assert trend["runs"][0]["started_at"]
    finally:
        _cleanup(org)


@needs_db
def test_run_results_drilldown() -> None:
    """Per-case outcomes behind a run row — failing first, 404 off-lens."""
    from services.contracts.eval import EvalResult
    from services.db.session import org_session
    from services.evals import store as eval_store

    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        with org_session(org) as s:
            run_id = eval_store.create_run(
                s, "customer_value", "regression", score=0.5, passed=1, failed=1
            )
            eval_store.record_results(
                s,
                run_id,
                [
                    EvalResult(
                        run_id=run_id,
                        case_id="c-fail",
                        question="What is the churn rate?",
                        passed=False,
                        grade="fail",
                        reason="3 missing / 1 extra row(s) vs expected",
                        actual_sql="SELECT churn FROM kpis",
                    ),
                    EvalResult(
                        run_id=run_id,
                        case_id="c-pass",
                        question="How many active subscribers?",
                        passed=True,
                        grade="pass",
                    ),
                ],
            )
        r = client.get(f"/mgmt/lenses/customer_value/evals/runs/{run_id}/results", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert [x["case_id"] for x in body] == ["c-fail", "c-pass"]  # failing first
        assert body[0]["question"] == "What is the churn rate?"
        assert body[0]["reason"] == "3 missing / 1 extra row(s) vs expected"
        assert body[0]["actual_sql"] == "SELECT churn FROM kpis"

        # The run belongs to customer_value — another lens's URL 404s
        r = client.get(f"/mgmt/lenses/other_lens/evals/runs/{run_id}/results", headers=h)
        assert r.status_code == 404
        # Unknown and malformed run ids 404 too (never 500)
        for bogus in (str(uuid.uuid4()), "not-a-uuid"):
            r = client.get(f"/mgmt/lenses/customer_value/evals/runs/{bogus}/results", headers=h)
            assert r.status_code == 404
    finally:
        _cleanup(org)


@needs_db
def test_run_regression_mode_is_a_400_naming_the_removal() -> None:
    """The value-case regression mode is gone — asking for it
    must fail loudly with the pointer at certified answers, never run silently."""
    org, raw = _org_token()
    h = {"Authorization": f"Bearer {raw}"}
    try:
        r = client.post("/mgmt/lenses/customer_value/evals/run?mode=regression", headers=h)
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "removed" in detail and "certified answers" in detail and "dst test" in detail
        # Unknown modes 400 too, naming the one that remains.
        r = client.post("/mgmt/lenses/customer_value/evals/run?mode=banana", headers=h)
        assert r.status_code == 400 and "health" in r.json()["detail"]
    finally:
        _cleanup(org)
