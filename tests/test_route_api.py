"""The "just ask" surface — POST /v1/query routes to a covering lens
(second hop, provenance-stamped) or declines with the uncovered envelope, and every
decision is persisted (routing_decision, migration 0019).

The router + embedder + run_lens_query are monkeypatched so the route/decline
control flow is tested deterministically without an LLM or warehouse (mirrors the
test_query_api.py patterns); persistence is exercised under needs_db.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.response import ConsumerAnswer, QueryResponse
from services.router import RouteDecision

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
        org = c.execute(text("INSERT INTO org (name) VALUES ('RouteT') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    return org, raw


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM routing_decision WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM admin_token WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


def _fake_answer(lens: str) -> QueryResponse:
    return QueryResponse(lens=lens, answer="42 customers", request_id="req_route_1")


def test_query_requires_auth() -> None:
    r = client.post("/v1/query", json={"q": "hi"})
    assert r.status_code == 401


@needs_db
def test_route_runs_the_second_hop_and_stamps_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A covered question routes → governed pipeline runs → answer carries which-lens."""
    org, raw = _make_org_token()
    captured: dict[str, str] = {}

    def fake_run(
        name: str,
        q: str,
        caller: object,
        background: object,
        fmt: str = "both",
        bindings: dict[str, str] | None = None,
        allow_untyped: bool = False,
        purpose: str = "",
    ) -> QueryResponse:
        captured["lens"], captured["q"], captured["fmt"] = name, q, fmt
        captured["purpose"] = purpose
        return _fake_answer(name)

    monkeypatch.setattr(
        "services.api.route._route",
        lambda caller, q: RouteDecision(True, "customer_value", 0.91, [("sales", 0.4)]),
    )
    monkeypatch.setattr("services.api.route.run_lens_query", fake_run)
    try:
        r = client.post(
            "/v1/query",
            json={"q": "how many customers?", "purpose": "sizing the churn model"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["covered"] is True
        # The second hop gets the caller's purpose, same as a direct lens call.
        assert captured["purpose"] == "sizing the churn model"
        assert body["routed_to"] == {
            "lens": "customer_value",
            "score": 0.91,
            # score is anchor similarity, not confidence — the
            # provenance names what actually decided.
            "decided_by": "cosine",
        }
        assert body["answer"]["answer"] == "42 customers"
        # the second hop actually ran the routed lens's pipeline with the question,
        # and the caller's answer shape rode through to it
        assert captured == {
            "lens": "customer_value",
            "q": "how many customers?",
            "fmt": "both",
            "purpose": "sizing the churn model",
        }

        # the decision persisted as a covered route to that lens
        eng = create_engine(settings.database_admin_url)
        with eng.connect() as c:
            row = c.execute(
                text(
                    "SELECT routed_lens, covered, score FROM routing_decision "
                    "WHERE org_id = :o ORDER BY created_at DESC LIMIT 1"
                ),
                {"o": org},
            ).first()
        assert row is not None and row[0] == "customer_value" and row[1] is True
        assert abs(row[2] - 0.91) < 1e-6
    finally:
        _cleanup(org)


@needs_db
@pytest.mark.parametrize("audience", ["engineer", "consumer"])
def test_a_routed_refusal_carries_its_reason(
    monkeypatch: pytest.MonkeyPatch, audience: str
) -> None:
    """A refusal is an outcome, not an error: when the routed lens declines,
    the routed envelope carries the second hop's status and the reason inside
    its sentence — at `answer.status` and `answer.answer` — for the engineer's
    QueryResponse and the consumer's ConsumerAnswer alike."""
    org, raw = _make_org_token()
    reason = "no governed metric of this lens answers the question"
    refused = QueryResponse(
        lens="customer_value",
        status="refused",
        answer=f"I can't answer this from this lens's data: {reason}",
        request_id="req_route_refused",
    )

    def fake_run(*a: object, **k: object) -> QueryResponse | ConsumerAnswer:
        return ConsumerAnswer.of(refused) if audience == "consumer" else refused

    monkeypatch.setattr(
        "services.api.route._route",
        lambda caller, q: RouteDecision(True, "customer_value", 0.9, []),
    )
    monkeypatch.setattr("services.api.route.run_lens_query", fake_run)
    try:
        r = client.post(
            "/v1/query",
            json={"q": "what is the churn rate?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["covered"] is True and body["routed_to"]["lens"] == "customer_value"
        assert body["answer"]["status"] == "refused"
        assert reason in body["answer"]["answer"]
    finally:
        _cleanup(org)


@needs_db
def test_covered_route_persists_even_when_the_second_hop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routed query whose lens pipeline then errors still counts as routed —
    persisting after the second hop made those requests invisible to surface area."""
    org, raw = _make_org_token()

    def boom(*a: object, **k: object) -> QueryResponse:
        raise RuntimeError("warehouse down")

    monkeypatch.setattr(
        "services.api.route._route",
        lambda caller, q: RouteDecision(True, "customer_value", 0.88, []),
    )
    monkeypatch.setattr("services.api.route.run_lens_query", boom)
    try:
        with pytest.raises(RuntimeError, match="warehouse down"):
            client.post(
                "/v1/query",
                json={"q": "how many customers?"},
                headers={"Authorization": f"Bearer {raw}"},
            )

        eng = create_engine(settings.database_admin_url)
        with eng.connect() as c:
            row = c.execute(
                text(
                    "SELECT routed_lens, covered FROM routing_decision "
                    "WHERE org_id = :o ORDER BY created_at DESC LIMIT 1"
                ),
                {"o": org},
            ).first()
        assert row is not None and row[0] == "customer_value" and row[1] is True
    finally:
        _cleanup(org)


@needs_db
def test_decline_returns_uncovered_envelope_never_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uncovered question NEVER runs a pipeline — it returns the honest envelope."""
    org, raw = _make_org_token()

    def boom(*a: object, **k: object) -> QueryResponse:  # the pipeline must NOT run
        raise AssertionError("run_lens_query must not be called on a decline")

    decision = RouteDecision(
        False, None, 0.31, [("sales", 0.31), ("finance", 0.2)], "no governed lens covers this (…)"
    )
    monkeypatch.setattr("services.api.route._route", lambda caller, q: decision)
    monkeypatch.setattr("services.api.route.run_lens_query", boom)
    try:
        r = client.post(
            "/v1/query",
            json={"q": "how many support tickets are open?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["covered"] is False
        assert "no governed lens covers this" in body["reason"]
        # nearest miss is the best alternative, scored below the route threshold
        assert body["nearest_miss"] == {"lens": "sales", "score": 0.31, "decided_by": "cosine"}
        assert "answer" not in body  # never an ungoverned answer

        eng = create_engine(settings.database_admin_url)
        with eng.connect() as c:
            row = c.execute(
                text(
                    "SELECT routed_lens, covered FROM routing_decision "
                    "WHERE org_id = :o ORDER BY created_at DESC LIMIT 1"
                ),
                {"o": org},
            ).first()
        assert row is not None and row[0] is None and row[1] is False
    finally:
        _cleanup(org)


@needs_db
def test_a_decline_names_the_lens_that_exposes_the_table_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router never routes to an exposed table (it governs nothing), so a
    question that names one word for word declines — and the envelope must say
    which lens to ask by name, or the miss reads as "dst has no such data"."""
    from services.db.session import org_session
    from services.lenses import store
    from tests.test_expose_tables import _compile, _config

    org, raw = _make_org_token()
    config = _config(["staging.customers"])
    with org_session(org) as session:
        store.create_lens(session, store.LensBundle(config=config, semantic_model=_compile(config)))
        store.publish(session, "ops")

    decision = RouteDecision(False, None, 0.1, [], "no governed lens covers this.")
    monkeypatch.setattr("services.api.route._route", lambda caller, q: decision)
    try:
        r = client.post(
            "/v1/query",
            json={"q": "how many rows in staging.customers?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["covered"] is False
        assert body["exposed_in"] == [{"lens": "ops", "table": "staging.customers"}]
        assert body["reason"].endswith(
            "; lens 'ops' exposes staging.customers (no governed definitions): ask it by name"
        )
        # The bare name counts too; an unrelated question gets no hint.
        r = client.post(
            "/v1/query", json={"q": "count customers"}, headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.json()["exposed_in"] == [{"lens": "ops", "table": "staging.customers"}]
        r = client.post(
            "/v1/query", json={"q": "open tickets?"}, headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.json()["exposed_in"] == [] and r.json()["reason"] == decision.reason
    finally:
        eng = create_engine(settings.database_admin_url)
        with eng.begin() as c:
            for table in ("lens_version", "lens"):
                c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})
        _cleanup(org)


@needs_db
def test_degraded_decline_carries_the_marker_to_envelope_and_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decider outage declines LOUD — DECIDER_DOWN on the envelope and
    the routing_decision row, never a silent cosine route."""
    org, raw = _make_org_token()
    decision = RouteDecision(
        False,
        None,
        0.86,
        [("board", 0.86)],
        "routing degraded: the coverage decider is unavailable — declining rather than guessing",
        degraded="DECIDER_DOWN",
    )
    monkeypatch.setattr("services.api.route._route", lambda caller, q: decision)
    try:
        r = client.post(
            "/v1/query",
            json={"q": "board numbers?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["covered"] is False
        assert body["degraded"] == "DECIDER_DOWN"
        assert "answer" not in body

        eng = create_engine(settings.database_admin_url)
        with eng.connect() as c:
            row = c.execute(
                text(
                    "SELECT covered, degraded FROM routing_decision "
                    "WHERE org_id = :o ORDER BY created_at DESC LIMIT 1"
                ),
                {"o": org},
            ).first()
        assert row is not None and row[0] is False and row[1] == "DECIDER_DOWN"
    finally:
        _cleanup(org)
