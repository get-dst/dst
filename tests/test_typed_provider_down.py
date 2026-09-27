"""A typed-decision provider that fails does not error the answers a lens can still give.

The failure: the typed-decision provider answered every call with an HTTP error
(402 billing, 520), and every question errored with a 502, although the lens
declared ``untyped_fallback`` and raw-SQL generation was right there. The
provider's exception left the typed generator, left the pipeline, and reached
the app's provider-error handler, one warning per request.

Pinned here: when the typed lane cannot run because its provider fails, a
question the caller accepts untyped is served by raw-SQL generation with the
UNTYPED line naming the failure; a typed-only question is refused with the
failure named (never a 502); the failure is logged once per burst, and /ready
reports it until a typed decision succeeds again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from services.app import app
from services.config import settings
from services.contracts.fakes import EchoQueryGenerator, ScriptedLLM, fake_llm_providers
from services.contracts.protocols import Decision
from services.lenses.demo import jaffle_customer_value
from services.llm import registry
from services.llm.typesafe import TypesafeDecider
from services.runtime.pipeline import PipelineResult, run_query
from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver
from tests.test_pipeline import _conn
from tests.test_query_api import _cleanup, _make_org_token, _seed_lens, needs_db

client = TestClient(app)

COUNT = "SELECT count(*) AS n FROM customers"
QUESTION = "How many customers are there?"
FAILURES = [(402, "HTTP 402"), (520, "HTTP 520"), ("timeout", "timed out")]


def _failing(kind: int | str) -> TypesafeDecider:
    """The typed-decision client against a provider that fails every call."""

    def handler(request: httpx.Request) -> httpx.Response:
        if kind == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        body = {"error": {"type": "billing_error", "message": "credit balance too low"}}
        return httpx.Response(int(kind), json=body)

    return TypesafeDecider("k", transport=httpx.MockTransport(handler), attempts=1)


def _ask(decider: Any, *, fallback: bool) -> PipelineResult:
    return run_query(
        question=QUESTION,
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=TypedIntentGenerator(TypedResolver(decider)),
        escalate_generator=EchoQueryGenerator(COUNT) if fallback else None,
        composer=None,
    )


@pytest.mark.parametrize(("kind", "named"), FAILURES)
def test_with_a_fallback_the_answer_is_served_untyped_naming_the_failure(
    kind: int | str, named: str
) -> None:
    res = _ask(_failing(kind), fallback=True)
    assert res.trace.status == "ok", res.trace.error
    assert res.response.sql and "count(*)" in res.response.sql.lower()
    assert res.response.resolution is not None and res.response.resolution.typed is False
    untyped = [n for n in res.response.degraded if n.startswith("UNTYPED: ")]
    assert untyped == [
        f"UNTYPED: served by raw-SQL generation — typed decisions unavailable: typesafe {named}"
    ], res.response.degraded


@pytest.mark.parametrize(("kind", "named"), FAILURES)
def test_without_a_fallback_the_question_is_refused_naming_the_failure(
    kind: int | str, named: str
) -> None:
    res = _ask(_failing(kind), fallback=False)
    assert res.response.status == "refused" and res.trace.status == "refused"
    assert res.response.sql is None and res.response.data is None
    assert f"typed decisions unavailable: typesafe {named}" in res.response.answer
    assert any(f"typesafe {named}" in n for n in res.response.degraded), res.response.degraded
    assert res.trace.degraded == res.response.degraded


def test_an_ambiguous_term_the_provider_cannot_decide_clarifies_as_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reading of an ambiguous term is the first typed decision a question
    can need. With the provider failing it is not decided, and the lexical
    clarification stands, exactly as on an install with no typed decider, with
    the failure disclosed."""
    from services.runtime import pipeline as pipeline_mod
    from tests.test_typed_reading import QUESTION as AMBIGUOUS
    from tests.test_typed_reading import _run

    monkeypatch.setattr(pipeline_mod, "typed_decider", lambda: _failing(402))
    out = _run(AMBIGUOUS)
    assert out.response.status == "clarification", out.response.answer
    assert any(
        "typed decisions unavailable: typesafe HTTP 402" in n for n in out.response.degraded
    ), out.response.degraded


class _Answers:
    """A decider that answers: the first preferred option on offer, else none."""

    def __init__(self, *prefer: str) -> None:
        self._prefer = prefer

    def decide(self, *, question: str, context: str, options: Any, allow_none: bool) -> Decision:
        names = [o.name for o in options]
        chosen = next((p for p in self._prefer if p in names), None)
        return Decision(chosen=chosen, probs={chosen or "none": 1.0}, provider="t")


def _typed_ok() -> None:
    res = _ask(_Answers("aggregate", "customers"), fallback=True)
    assert res.response.resolution is not None


def test_the_failure_is_logged_once_per_burst(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="dst"):
        for _ in range(3):
            _ask(_failing(402), fallback=True)
        _typed_ok()
        _ask(_failing(520), fallback=True)
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    down = [m for m in warned if "typed decisions unavailable" in m]
    assert len(down) == 2, warned
    assert "typesafe HTTP 402" in down[0] and "typesafe HTTP 520" in down[1]


def test_ready_reports_the_failing_provider_until_a_typed_decision_succeeds() -> None:
    assert not client.get("/ready").json()["typed_decisions"].startswith("degraded")
    _ask(_failing(402), fallback=True)
    _ask(_failing(402), fallback=False)
    typed = client.get("/ready").json()["typed_decisions"]
    assert typed.startswith("degraded"), typed
    assert "typesafe HTTP 402" in typed and "2 " in typed
    _typed_ok()
    assert not client.get("/ready").json()["typed_decisions"].startswith("degraded")


@pytest.mark.needs_db
def test_ready_status_is_degraded_while_the_provider_fails(live_client: TestClient) -> None:
    assert live_client.get("/ready").json()["status"] == "ready"
    _ask(_failing(520), fallback=True)
    assert live_client.get("/ready").json()["status"] == "degraded"
    _typed_ok()
    assert live_client.get("/ready").json()["status"] == "ready"


@needs_db
def test_the_query_door_serves_or_refuses_instead_of_a_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through /v1: the typed lane is chosen (a typed-decision
    provider is configured) and its provider fails on every call."""
    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    generated = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
    make: Callable[..., ScriptedLLM] = lambda _key, **_: ScriptedLLM(  # noqa: E731
        [generated, "There are 100 customers."]
    )
    monkeypatch.setattr("services.llm.anthropic_provider.AnthropicProvider", make)
    monkeypatch.setattr(registry, "resolve_decider", lambda **_: _failing(402))
    auth = {"Authorization": f"Bearer {raw}"}
    url = "/v1/lenses/customer_value/query"
    try:
        r = client.post(url, json={"q": QUESTION, "allow_untyped": True}, headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok", body
        assert (
            "UNTYPED: served by raw-SQL generation — typed decisions unavailable: typesafe HTTP 402"
        ) in body["degraded"]

        r = client.post(url, json={"q": QUESTION}, headers=auth)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "refused", body
        assert "typed decisions unavailable: typesafe HTTP 402" in body["answer"]
    finally:
        _cleanup(settings.database_admin_url, org)
