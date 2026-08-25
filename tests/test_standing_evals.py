"""The standing certified-eval cadence.

`run_standing_certified` re-runs each published lens's parity suite when its
latest standing run predates the cutoff, persists the outcome as a normal
eval_run (mode='certified'), and skips fresh lenses — the audit scheduler's
staleness semantics, applied to accuracy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.config import settings
from services.contracts.fakes import ScriptedLLM
from services.db.session import org_session
from services.evals import service as evals_service
from services.evals import store as eval_store
from services.lenses import connection_store
from services.lenses import store as lens_store
from services.lenses.demo import jaffle_customer_value_bundle
from services.llm.registry import ResolvedModel
from services.runtime.generator import GroundedSQLGenerator

_Q = "How many repeat customers are there?"
_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 1"


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


@pytest.fixture()
def org():
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('StandingEvalT') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    with org_session(oid) as s:
        connection_store.create_connection(
            s, "jaffle", "duckdb", {"path": settings.duckdb_jaffle_path}, None
        )
        lens_store.create_lens(s, jaffle_customer_value_bundle())
        lens_store.publish(s, "customer_value")
        certify_store.create(s, "customer_value", _Q, _SQL, None, "t")
        s.commit()
    yield oid
    with admin.begin() as c:
        for table in (
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


def _pin_llm(monkeypatch: pytest.MonkeyPatch, responses: list[str]) -> None:
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM(list(responses)), "fake", "m"),
    )
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )


@needs_db
def test_standing_run_records_and_staleness_gate_skips(org, monkeypatch) -> None:
    oid = org
    cutoff = datetime.now(UTC)  # everything is stale relative to "now"
    _pin_llm(monkeypatch, [_SQL, "There are 19."])
    assert evals_service.run_standing_certified(oid, cutoff) == 1
    with org_session(oid) as s:
        runs = eval_store.list_runs(s, "customer_value")
    assert len(runs) == 1
    assert runs[0].mode == evals_service.STANDING_MODE
    assert runs[0].score == 1.0 and runs[0].passed == 1 and runs[0].failed == 0

    # Within the interval the lens is fresh — nothing runs, no second row.
    _pin_llm(monkeypatch, ["GARBAGE ;;;"])  # would fail if consumed
    assert evals_service.run_standing_certified(oid, datetime.now(UTC) - timedelta(hours=1)) == 0
    with org_session(oid) as s:
        assert len(eval_store.list_runs(s, "customer_value")) == 1

    # Past the interval it runs again — a diverging generation records honestly.
    _pin_llm(monkeypatch, ["SELECT count(*) AS n FROM customers", "compose"] * 2)
    assert evals_service.run_standing_certified(oid, datetime.now(UTC) + timedelta(hours=1)) == 1
    with org_session(oid) as s:
        runs = eval_store.list_runs(s, "customer_value")
    assert len(runs) == 2
    latest = runs[0]
    assert latest.score == 0.0 and latest.failed == 1


@needs_db
def test_lens_without_certified_answers_is_skipped(org, monkeypatch) -> None:
    oid = org
    with org_session(oid) as s:
        for a in certify_store.list_for_lens(s, "customer_value"):
            certify_store.update(s, a.id, sql=a.sql, verified_value=None, status="retired")
        s.commit()
    _pin_llm(monkeypatch, ["GARBAGE ;;;"])  # would fail if anything ran
    assert evals_service.run_standing_certified(oid, datetime.now(UTC)) == 0
    with org_session(oid) as s:
        assert eval_store.list_runs(s, "customer_value") == []
