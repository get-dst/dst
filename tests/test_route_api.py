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
from services.contracts.response import QueryResponse
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
        name: str, q: str, caller: object, background: object, fmt: str = "both"
    ) -> QueryResponse:
        captured["lens"], captured["q"], captured["fmt"] = name, q, fmt
        return _fake_answer(name)

    monkeypatch.setattr(
        "services.api.route._route",
        lambda caller, q: RouteDecision(True, "customer_value", 0.91, [("sales", 0.4)]),
    )
    monkeypatch.setattr("services.api.route.run_lens_query", fake_run)
    try:
        r = client.post(
            "/v1/query",
            json={"q": "how many customers?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["covered"] is True
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
        assert captured == {"lens": "customer_value", "q": "how many customers?", "fmt": "both"}

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
