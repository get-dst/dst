"""A question that names one governed metric and nothing else is served by the
compiler, not by generation.

The structured door computed such a question right every time while the prose
door's generated reading of it could come back wrong. So the query door reads
the text first: exactly one metric named word for word, the rest of the
question a plain interrogative frame, and the compiled intent serves — on the
direct door and through the router alike — before any model writes SQL. A
question carrying anything more (a period, a breakdown, a value, a limit)
takes the usual path.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.app import app
from services.config import settings
from services.contracts.fakes import fake_llm_providers
from services.contracts.protocols import LLMResult
from services.lenses.demo import jaffle_customer_value
from services.router import RouteDecision
from services.runtime.typed_resolver import sole_metric_intent
from tests.test_embedder_degraded import _dead_embedder
from tests.test_query_api import _cleanup, _make_org_token, _seed_lens, needs_db

client = TestClient(app)
MODEL = jaffle_customer_value()


@pytest.mark.parametrize(
    "question",
    [
        "What is the revenue?",
        "revenue",
        "What's our total revenue",
        "How much revenue do we have?",
        "Show me the revenue, please.",
    ],
)
def test_a_bare_metric_question_settles_to_its_intent(question: str) -> None:
    intent = sole_metric_intent(question, MODEL)
    assert intent is not None
    assert intent.entity == "orders" and intent.metrics == ["revenue"]
    assert not intent.dimensions and not intent.filters and intent.grain is None


@pytest.mark.parametrize(
    "question",
    [
        "What is the revenue by status?",  # a breakdown
        "revenue last month",  # a period
        "revenue in Finland",  # a value
        "top 5 customers by revenue",  # a listing and a limit
        "revenue per month",  # over time
        "revenue and order count",  # two metrics
        "How many customers do we have?",  # names no metric
        "What is the average order value trend?",  # a word the frame does not hold
    ],
)
def test_anything_beyond_the_metric_name_leaves_the_path(question: str) -> None:
    assert sole_metric_intent(question, MODEL) is None


class _NeverGenerates:
    """An LLM the door must not reach for SQL on this question."""

    def complete(self, **_kw: object) -> LLMResult:
        raise AssertionError("generation was consulted for a sole-metric question")


@needs_db
def test_the_query_door_serves_the_compiled_metric_without_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(settings, "typed_serving", "off")
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider", lambda _key, **_: _NeverGenerates()
    )
    _dead_embedder(monkeypatch)
    auth = {"Authorization": f"Bearer {raw}"}
    try:
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "What is the revenue?", "format": "structured"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok", body
        assert body["sql"] and "sum(" in body["sql"].lower() and "amount" in body["sql"].lower()
        assert body["definition_used"] == "revenue"
        assert not any(n.startswith("UNTYPED") for n in body["degraded"]), body["degraded"]

        # The routed door is the same hop.
        decision = RouteDecision(True, "customer_value", 0.9, [], "covered")
        monkeypatch.setattr("services.api.route._route", lambda caller, q: decision)
        r = client.post(
            "/v1/query", json={"q": "What is the revenue?", "format": "structured"}, headers=auth
        )
        assert r.status_code == 200, r.text
        routed = r.json()
        assert routed["covered"] is True
        assert routed["answer"]["sql"] == body["sql"]
    finally:
        _cleanup(settings.database_admin_url, org)
