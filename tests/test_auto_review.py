"""Auto-review: a lens flags its own low-confidence answers into the review
queue (origin='ai'), reusing the caller path's judge — humans and callers keep
raising tickets that land origin='human'. Certified answers, clarifications,
and refusals never auto-flag."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.api import query as query_api
from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.response import ClarificationRequest, QueryResponse
from services.contracts.trace import TraceLog
from services.db.session import org_session
from services.lenses import store as lens_store
from services.lenses.demo import jaffle_customer_value_bundle
from services.runtime.pipeline import PipelineResult

client = TestClient(app)


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


def _make_org_token() -> tuple[object, str]:
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('AutoRev') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _seed_lens(org: object, auto_review: str) -> None:
    bundle = jaffle_customer_value_bundle()
    bundle.config.auto_review = auto_review  # type: ignore[assignment]
    with org_session(org) as session:
        lens_store.create_lens(session, bundle)
        lens_store.publish(session, "customer_value")


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM review WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM request_log WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM lens WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _fake_pipeline(monkeypatch: pytest.MonkeyPatch, confidence: str | None) -> None:
    """Pin the pipeline outcome so the auto-review trigger is deterministic —
    the wiring under test starts where run_query returns."""

    def fake_run_query(**kw: object) -> PipelineResult:
        rid = f"req-{uuid.uuid4()}"
        return PipelineResult(
            response=QueryResponse(
                lens=str(kw["lens_name"]),
                answer="There are 19 repeat customers.",
                sql="SELECT 42",
                confidence=confidence,  # type: ignore[arg-type]
                request_id=rid,
            ),
            trace=TraceLog(
                request_id=rid,
                org_id=str(kw["org_id"]),
                lens=str(kw["lens_name"]),
                caller=str(kw["caller"]),
                question=str(kw["question"]),
                sql="SELECT 42",
                valid=True,
                row_count=1,
                answer="There are 19 repeat customers.",
                confidence=confidence,
                status="ok",
            ),
        )

    monkeypatch.setattr(query_api, "run_query", fake_run_query)


def _query(raw: str) -> dict[str, object]:
    r = client.post(
        "/v1/lenses/customer_value/query",
        json={"q": "how many repeat customers?"},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _tickets(org: object) -> list[tuple[str, str, str | None, str]]:
    admin = create_engine(settings.database_admin_url)
    with admin.connect() as c:
        rows = c.execute(
            text("SELECT request_id, origin, ai_verdict, state FROM review WHERE org_id = :o"),
            {"o": org},
        ).all()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


# ─── the policy predicate, exhaustively (no DB) ──────────────────────────────


def _resp(**kw: object) -> QueryResponse:
    return QueryResponse.model_validate({"lens": "l", "answer": "a", "request_id": "r", **kw})


def test_auto_review_policy_matrix() -> None:
    flags = query_api._auto_review_flags
    assert flags("partial", _resp(confidence="partial"))
    assert flags("partial", _resp(confidence="unverified"))
    assert flags("unverified", _resp(confidence="unverified"))
    assert not flags("unverified", _resp(confidence="partial"))
    assert not flags("off", _resp(confidence="unverified"))
    assert not flags("partial", _resp(confidence="verified"))
    # never on non-answers or certified answers
    clar = ClarificationRequest(term="active", question="which meaning?")
    assert not flags("partial", _resp(clarification=clar))
    assert not flags("partial", _resp(confidence="partial", certification="certified"))
    assert not flags("partial", _resp())  # refusal/error: no confidence grade


# ─── the wiring, end to end ──────────────────────────────────────────────────


@needs_db
def test_auto_review_partial_flags_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto_review=partial + a partial answer → ONE origin='ai' ticket, judged
    by the same judge as the caller path (so it carries an ai_verdict)."""
    org, raw = _make_org_token()
    _seed_lens(org, auto_review="partial")
    _fake_pipeline(monkeypatch, confidence="partial")
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM(['{"verdict": "approve", "reasoning": "sound"}']),
    )
    try:
        body = _query(raw)
        tickets = _tickets(org)
        assert len(tickets) == 1
        rid, origin, ai_verdict, state = tickets[0]
        assert rid == body["request_id"]
        assert origin == "ai"
        assert ai_verdict == "approve" and state == "approved"  # judge ran, caller-path flow

        # re-running the flagger for the same request never double-tickets
        query_api._auto_review(uuid.UUID(str(org)), str(body["request_id"]))
        assert len(_tickets(org)) == 1
    finally:
        _cleanup(org)


@needs_db
def test_auto_review_off_never_tickets(monkeypatch: pytest.MonkeyPatch) -> None:
    org, raw = _make_org_token()
    _seed_lens(org, auto_review="off")
    _fake_pipeline(monkeypatch, confidence="unverified")
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _k, **_: ScriptedLLM(['{"verdict": "approve", "reasoning": "sound"}']),
    )
    try:
        _query(raw)
        assert _tickets(org) == []
    finally:
        _cleanup(org)
