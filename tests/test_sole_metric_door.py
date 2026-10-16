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

from datetime import date

import pytest
from fastapi.testclient import TestClient

from services.app import app
from services.config import settings
from services.contracts.fakes import fake_llm_providers
from services.contracts.protocols import LLMResult
from services.lenses.demo import jaffle_customer_value
from services.router import RouteDecision
from services.runtime import timewindow
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
    ("question", "window"),
    [
        ("What is the revenue in September?", ("2026-09-01", "2026-09-30")),  # a bare month
        ("revenue for Q3 2026", ("2026-07-01", "2026-09-30")),
        ("What was our revenue last month", ("2026-09-01", "2026-09-30")),
        ("revenue during 2025", ("2025-01-01", "2025-12-31")),
    ],
)
def test_a_metric_with_a_stated_period_compiles_the_window(
    question: str, window: tuple[str, str]
) -> None:
    """ "What is the on-time delivery rate in September" names one metric and
    a period, nothing else: the period is the one slot the text settles on
    its own, through the same window reading typed serving uses. A bare month
    is the most recent one on or before the lens clock."""
    intent = sole_metric_intent(question, MODEL, today=date(2026, 10, 16))
    assert intent is not None and intent.metrics == ["revenue"]
    assert [(f.field, f.op, f.value) for f in intent.filters] == [
        ("order_date", ">=", window[0]),
        ("order_date", "<=", window[1]),
    ]


def test_a_bare_month_is_placed_on_or_before_today() -> None:
    assert timewindow.place_bare_month("revenue in september", date(2026, 3, 1)) == (
        "revenue in september 2025"
    )
    assert timewindow.place_bare_month("revenue in may", date(2026, 10, 1)) == "revenue in may"
    # two bare months are two readings — left for the usual path to ask about
    assert (
        sole_metric_intent("revenue in September and October", MODEL, today=date(2026, 10, 16))
        is None
    )


@pytest.mark.parametrize(
    "question",
    [
        "What is the revenue by status?",  # a breakdown
        "revenue last month in Finland",  # a period and a value
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


@needs_db
@pytest.mark.parametrize("typed_serving", ["off", "auto"])
def test_the_served_ledger_reads_construction_on_both_typed_settings(
    monkeypatch: pytest.MonkeyPatch, typed_serving: str
) -> None:
    """The compiled metric is not only the SQL: the resolution ledger the
    caller reads must say ``construction``, with the metric as a declared
    slot — an ``attributed`` ledger here means generation ran after all."""
    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(settings, "typed_serving", typed_serving)
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider", lambda _key, **_: _NeverGenerates()
    )
    _dead_embedder(monkeypatch)
    auth = {"Authorization": f"Bearer {raw}"}
    try:
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "what is revenue?", "format": "structured"},
            headers=auth,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok", body
        ledger = body["resolution"]
        assert ledger is not None and ledger["method"] == "construction", ledger
        assert any(
            s["kind"] == "metric" and s["name"] == "revenue" and s["source"] == "declared"
            for s in ledger["slots"]
        ), ledger
    finally:
        _cleanup(settings.database_admin_url, org)


@needs_db
def test_a_metric_named_by_its_label_constructs_through_the_api_door(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody says `otd_rate`; they say "on-time delivery rate". A metric's
    label and aliases are read word for word like its name, so the bare
    question reaches the compiled path and the ledger says construction."""
    import copy

    from services.contracts.semantic_model import Metric
    from services.db.session import org_session
    from services.lenses import store
    from services.lenses.demo import jaffle_customer_value_bundle

    org, raw = _make_org_token(settings.database_admin_url)
    bundle = copy.deepcopy(jaffle_customer_value_bundle())
    orders = next(e for e in bundle.semantic_model.entities if e.name == "orders")
    orders.metrics.append(
        Metric(
            name="otd_rate",
            agg="avg",
            expr="orders.amount",
            label="on-time delivery rate",
            aliases=["OTD"],
        )
    )
    with org_session(org) as session:
        store.create_lens(session, bundle)
        store.publish(session, "customer_value")
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(settings, "typed_serving", "off")
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider", lambda _key, **_: _NeverGenerates()
    )
    _dead_embedder(monkeypatch)
    auth = {"Authorization": f"Bearer {raw}"}
    try:
        for q in ("What is the on-time delivery rate?", "what's our OTD"):
            r = client.post(
                "/v1/lenses/customer_value/query",
                json={"q": q, "format": "structured"},
                headers=auth,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "ok", body
            assert body["resolution"]["method"] == "construction", body["resolution"]
            assert "avg(" in body["sql"].lower() and "otd_rate" in body["sql"].lower()
    finally:
        _cleanup(settings.database_admin_url, org)
