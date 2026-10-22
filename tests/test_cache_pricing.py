"""Prompt-cache tokens are priced; a provider id keeps a model id's colon; a
model is priced by the endpoint it is called through.

The defects: every call's prompt-cache reads and writes were recorded and
never priced, so each cost figure for a model that caches (every Anthropic
call, OpenAI's cached input) was a floor reading like a total; a model id with
a colon (``llama3:8b``) was cut at the colon when a charge went through its
provider id, so it was priced as another model or not at all; and
``gpt-6-luna`` carried its Decisions API price (no output charge) onto chat
completions, where its output is billed.

Pinned here: the rates are the providers' published ones (cited in cost.py),
a cache token with no rate leaves its call unpriced and is said, each client
reports its cache split, a served answer's row costs what its calls cost,
the routing row too, and an anthropic model with no cache rate is named on
readiness.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.config import ProviderConfig, settings
from services.contracts.fakes import ScriptedLLM, fake_llm_providers
from services.contracts.protocols import (
    CacheableBlock,
    Decision,
    DecisionUsage,
    LLMResult,
    Message,
)
from services.llm import registry
from services.llm.anthropic_provider import AnthropicProvider
from services.llm.openai_compat import OpenAICompatProvider
from services.observability import cost
from services.observability import usage as ai_usage
from tests.test_embedder_degraded import _GEN_JSON
from tests.test_query_api import _cleanup, _make_org_token, _seed_lens, needs_db

client = TestClient(app)
M = 1_000_000
K = 100_000


# ── the published rates ──────────────────────────────────────────────────────


def test_cache_reads_and_writes_are_priced_at_the_published_rates() -> None:
    # Claude Sonnet 4.6: hits $0.30, 5-minute writes $3.75, 1-hour writes $6.
    assert cost.ai_cost_usd("claude-sonnet-4-6", 0, 0, cache_read_tokens=M) == 0.30
    assert cost.ai_cost_usd("claude-sonnet-4-6", 0, 0, cache_write_tokens=M) == 3.75
    assert (
        cost.ai_cost_usd("claude-sonnet-4-6", 0, 0, cache_write_tokens=M, cache_write_long_tokens=M)
        == 6.0
    )
    # Claude Opus 5.5 reads at its own 0.05x rate, $0.20.
    assert cost.ai_cost_usd("claude-opus-5-5", 0, 0, cache_read_tokens=M) == 0.20
    # One call, every kind at once: 1M in, 1M out, 1M read, 1M written of
    # which 400K for the hour, on Claude Haiku 4.5 ($1, $5, $0.10, $1.25, $2).
    one = cost.ai_cost_usd(
        "claude-haiku-4-5",
        M,
        M,
        cache_read_tokens=M,
        cache_write_tokens=M,
        cache_write_long_tokens=400_000,
    )
    assert one == pytest.approx(1.0 + 5.0 + 0.10 + 0.6 * 1.25 + 0.4 * 2.0)
    # A dated snapshot id takes its model's cache rates.
    assert cost.ai_cost_usd("claude-haiku-4-5-20251001", 0, 0, cache_read_tokens=M) == 0.10
    # OpenAI's cached input, and gpt-6-luna's 30-minute and 12-hour writes
    # (on a prompt under its 272K long-context step).
    assert cost.ai_cost_usd("gpt-5.4-mini", 0, 0, cache_read_tokens=M) == 0.075
    assert cost.ai_cost_usd("gpt-6-luna", 0, 0, cache_write_tokens=K) == 0.0125
    assert (
        cost.ai_cost_usd("gpt-6-luna", 0, 0, cache_write_tokens=K, cache_write_long_tokens=K)
        == 0.025
    )
    # Over it, the long-context rates: $0.02 read, $0.25 and $0.50 writes.
    assert cost.ai_cost_usd("gpt-6-luna", 0, 0, cache_read_tokens=M) == 0.02
    assert cost.ai_cost_usd("gpt-6-luna", 0, 0, cache_write_tokens=M) == 0.25


def test_cache_tokens_tip_a_prompt_into_its_long_rate() -> None:
    # Claude Haiku 5.5: a prompt is its input and cache tokens together.
    short = cost.ai_cost_usd("claude-haiku-5-5", 50_000, 0, cache_read_tokens=50_000)
    long = cost.ai_cost_usd("claude-haiku-5-5", 50_000, 0, cache_read_tokens=50_001)
    assert short == pytest.approx(50_000 * 0.10 / M + 50_000 * 0.01 / M, abs=1e-6)
    assert long == pytest.approx(50_000 * 0.50 / M + 50_001 * 0.05 / M, abs=1e-6)


def test_a_cache_token_with_no_rate_leaves_the_call_unpriced(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # gpt-5.5 lists cached input but no write rate; o3-pro lists no cached input.
    assert cost.ai_cost_usd("gpt-5.5", 10, 10, cache_write_tokens=10) is None
    assert cost.ai_cost_usd("o3-pro", 10, 10, cache_read_tokens=10) is None
    assert cost.ai_cost_usd("o3-pro", 10, 10) is not None  # no cache tokens, priced
    # A declared model with no cache rates; then with them.
    monkeypatch.setattr(settings, "ai_pricing", {"house-model-7": (1.0, 2.0)})
    assert cost.ai_cost_usd("house-model-7", M, 0, cache_read_tokens=1) is None
    monkeypatch.setattr(settings, "ai_pricing", {"house-model-7": (1.0, 2.0, 0.5, 1.5)})
    assert cost.ai_cost_usd("house-model-7", M, 0, cache_read_tokens=M) == 1.5
    assert cost.ai_cost_usd("house-model-7", 0, 0, cache_write_tokens=M) == 1.5
    assert (
        cost.ai_cost_usd("house-model-7", 0, 0, cache_write_long_tokens=M, cache_write_tokens=M)
        is None
    )

    # Recorded: unpriced, and said, naming the cache.
    monkeypatch.setattr(ai_usage, "_WARNED", set())
    with caplog.at_level(logging.WARNING, logger="dst"), ai_usage.collecting() as calls:
        ai_usage.record("openai-compatible", "gpt-5.5", 10, 10, cache_write_tokens=10)
    assert calls[0].cost_usd is None
    assert any("prompt-cache" in r.getMessage() for r in caplog.records)
    assert ai_usage.totals(calls)[2] is None


def test_a_pricing_entry_takes_two_to_five_rates() -> None:
    from services.config import check_ai_pricing

    assert check_ai_pricing({"m": (1.0, 2.0, 0.1, 1.25, 2.0)})
    with pytest.raises(ValueError, match="expected \\[input, output\\]"):
        check_ai_pricing({"m": (1.0,)})


# ── each client reports its cache split ─────────────────────────────────────


class _Messages:
    def __init__(self, usage: Any) -> None:
        self.usage = usage
        self.sent: dict[str, Any] = {}

    def create(self, **request: Any) -> Any:
        self.sent = request
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
            usage=self.usage,
        )


def _anthropic(usage: Any) -> AnthropicProvider:
    provider = AnthropicProvider("k")
    provider._client = SimpleNamespace(messages=_Messages(usage))  # type: ignore[assignment]
    return provider


def _ask(provider: Any, model: str, ttl: str = "5m") -> LLMResult:
    return provider.complete(  # type: ignore[no-any-return]
        system=[CacheableBlock("lens context", ttl=ttl)],  # type: ignore[arg-type]
        messages=[Message("user", "how many customers?")],
        model=model,
        temperature=0.0,
        max_tokens=64,
    )


def test_the_anthropic_client_reports_its_cache_split() -> None:
    usage = SimpleNamespace(
        input_tokens=1_000,
        output_tokens=200,
        cache_read_input_tokens=50_000,
        cache_creation_input_tokens=30_000,
        cache_creation=SimpleNamespace(
            ephemeral_5m_input_tokens=10_000, ephemeral_1h_input_tokens=20_000
        ),
    )
    with ai_usage.collecting() as calls:
        result = _ask(_anthropic(usage), "claude-sonnet-4-6")
    assert (result.cache_read_tokens, result.cache_creation_tokens) == (50_000, 30_000)
    assert result.cache_write_long_tokens == 20_000
    (call,) = calls
    assert (call.cache_read_tokens, call.cache_write_tokens) == (50_000, 30_000)
    assert call.cache_write_long_tokens == 20_000
    expected = (1_000 * 3.0 + 200 * 15.0 + 50_000 * 0.30 + 10_000 * 3.75 + 20_000 * 6.0) / M
    assert call.cost_usd == pytest.approx(expected)
    assert cost.result_cost_usd("claude-sonnet-4-6", result) == pytest.approx(expected)


@pytest.mark.parametrize(("ttl", "long"), [("5m", 0), ("1h", 30_000)])
def test_an_unsplit_write_is_never_priced_below_what_was_asked(ttl: str, long: int) -> None:
    usage = SimpleNamespace(
        input_tokens=1,
        output_tokens=1,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=30_000,
        cache_creation=None,
    )
    assert _ask(_anthropic(usage), "claude-sonnet-4-6", ttl).cache_write_long_tokens == long


def test_an_openai_style_client_records_cached_input_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 12_000,
            "completion_tokens": 100,
            "prompt_tokens_details": {
                "cached_tokens": 4_000,
                "cache_write_tokens": 7_000,
                "cache_write_tokens_details": {"ttl_30m_tokens": 3_000, "ttl_12h_tokens": 4_000},
            },
        },
    }

    def post(url: str, **_: Any) -> httpx.Response:
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr("services.llm.openai_compat.httpx.post", post)
    with ai_usage.collecting() as calls:
        result = _ask(OpenAICompatProvider("k", "https://gw.example/v1"), "gpt-6-luna")
    assert (result.input_tokens, result.cache_read_tokens) == (1_000, 4_000)
    (call,) = calls
    assert (call.input_tokens, call.cache_write_tokens, call.cache_write_long_tokens) == (
        1_000,
        7_000,
        4_000,
    )
    expected = (1_000 * 0.10 + 100 * 0.50 + 4_000 * 0.01 + 3_000 * 0.125 + 4_000 * 0.25) / M
    assert call.cost_usd == pytest.approx(expected)


# ── a colon inside a model id ───────────────────────────────────────────────


def test_a_model_id_with_a_colon_survives_its_provider_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert cost.decision_model("openai-compatible:llama3:8b") == "llama3:8b"
    assert cost.decision_model("vote:llama3:8b:k=5") == "llama3:8b"
    assert cost.decision_model("vote:deepseek-v4-flash:k=5") == "deepseek-v4-flash"
    assert cost.decision_model("typesafe:jev-latest") == "jev-latest"
    assert cost.decision_model("claude-haiku-4-5") == "claude-haiku-4-5"
    monkeypatch.setattr(settings, "ai_pricing", {"llama3:8b": (0.5, 1.0), "llama3": (9.0, 9.0)})
    assert cost.decisions_cost_usd({"vote:llama3:8b:k=5": (M, M)}) == 1.5
    # What a spend site charges from the provider's records.
    with ai_usage.collecting() as calls:
        ai_usage.record("openai-compatible", "llama3:8b", M, M)
    spent = ai_usage.as_spent(calls)
    assert (
        cost.decisions_cost_usd({p: (u.input_tokens, u.output_tokens) for p, u in spent})
        == calls[0].cost_usd
        == 1.5
    )


# ── a price by endpoint ─────────────────────────────────────────────────────


def test_gpt_6_luna_bills_output_over_chat_and_not_as_a_decision() -> None:
    # Chat completions: $0.10 in, $0.50 out.
    assert cost.ai_cost_usd("gpt-6-luna", K, M) == 0.51
    assert cost.decisions_cost_usd({"vote:gpt-6-luna:k=5": (0, M)}) == 0.50
    # The Decisions API: input only, no output or cache charge.
    assert cost.decisions_cost_usd({"openai-decisions:gpt-6-luna": (K, M)}) == 0.01
    assert cost.ai_cost_usd("gpt-6-luna", K, M, endpoint="openai-decisions") == 0.01
    # Its long-context input over 272K, $0.20.
    assert cost.ai_cost_usd("gpt-6-luna", M, M, endpoint="openai-decisions") == 0.20
    assert (
        cost.ai_cost_usd("gpt-6-luna", 0, 0, cache_read_tokens=M, endpoint="openai-decisions")
        == 0.0
    )
    # Recorded calls: the decider's provider id names the endpoint.
    with ai_usage.collecting() as calls:
        ai_usage.record("openai-compatible", "gpt-6-luna", 0, M)
        ai_usage.record("openai-decisions:gpt-6-luna", "gpt-6-luna", K, M)
    assert [c.cost_usd for c in calls] == [0.50, 0.01]


def test_an_endpoint_price_can_be_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_pricing", {"openai-decisions:gpt-6-luna": (0.08, 0.0)})
    assert cost.decisions_cost_usd({"openai-decisions:gpt-6-luna": (M, M)}) == 0.08
    assert cost.ai_cost_usd("gpt-6-luna", K, M) == 0.51  # chat keeps its own price


# ── readiness names a model whose cache has no rate ─────────────────────────


def test_readiness_names_an_anthropic_model_with_no_cache_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        settings,
        "providers",
        {"claude": ProviderConfig(type="anthropic", api_key="k", fast_model="claude-house-1")},
    )
    monkeypatch.setattr(settings, "ai_pricing", {"claude-house-1": (1.0, 5.0)})
    note = registry.unpriced_note() or ""
    assert "claude-house-1" in note and "prompt-cache" in note
    monkeypatch.setattr(settings, "ai_pricing", {"claude-house-1": (1.0, 5.0, 0.1, 1.25, 2.0)})
    assert registry.unpriced_note() is None


# ── the rows cost what their calls cost ─────────────────────────────────────


IN, OUT, READ, WRITE, LONG = 900, 60, 40_000, 12_000, 5_000


class _CachingLLM(ScriptedLLM):
    """A scripted client that reports cache reads and writes, as the
    Anthropic one does."""

    def complete(self, **kw: Any) -> LLMResult:
        text_ = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        ai_usage.record(
            "anthropic",
            kw["model"],
            IN,
            OUT,
            cache_read_tokens=READ,
            cache_write_tokens=WRITE,
            cache_write_long_tokens=LONG,
        )
        return LLMResult(
            text=text_,
            input_tokens=IN,
            output_tokens=OUT,
            cache_read_tokens=READ,
            cache_creation_tokens=WRITE,
            cache_write_long_tokens=LONG,
        )


def _row_and_calls(org: object, rid: str) -> tuple[float | None, list[float | None]]:
    with create_engine(settings.database_admin_url).begin() as c:
        row = c.execute(
            text("SELECT ai_cost_usd FROM request_log WHERE request_id = :r"), {"r": rid}
        ).scalar_one()
        calls = [
            r[0]
            for r in c.execute(
                text("SELECT cost_usd FROM ai_call WHERE request_id = :r ORDER BY id"), {"r": rid}
            )
        ]
    return row, calls


@needs_db
def test_a_served_answer_costs_what_its_calls_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.runtime.generator import GroundedSQLGenerator

    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)
    monkeypatch.setattr(settings, "providers", fake_llm_providers())
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0, **_: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider",
        lambda _key, **_: _CachingLLM([_GEN_JSON, "There are 19."]),
    )
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    try:
        r = client.post(
            "/v1/lenses/customer_value/query",
            json={"q": "how many repeat customers?"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert r.status_code == 200, r.text
        row, calls = _row_and_calls(org, r.json()["request_id"])
        assert len(calls) == 2 and None not in calls
        assert row == pytest.approx(sum(c for c in calls if c is not None), abs=1e-6)
    finally:
        _cleanup(settings.database_admin_url, org)


@needs_db
def test_the_routing_row_costs_what_its_calls_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.router import RouteDecision

    org, raw = _make_org_token(settings.database_admin_url)
    _seed_lens(org)

    def route(caller: Any, q: str) -> RouteDecision:
        with ai_usage.purpose("route"):
            _CachingLLM(["none"]).complete(model="claude-haiku-4-5")
        decided = Decision(
            chosen=None,
            probs=None,
            provider="vote:claude-haiku-4-5:k=1",
            usage=DecisionUsage(calls=1.0, input_tokens=IN, output_tokens=OUT),
        )
        return RouteDecision(
            False, None, 0.2, [], "no lens covers this", decided_by="decider", decision=decided
        )

    monkeypatch.setattr("services.api.route._route", route)
    monkeypatch.setattr("services.api.route.lens_refusal_named", lambda q, caller: None)
    monkeypatch.setattr("services.api.route._not_for_named", lambda q, caller, lens: None)
    try:
        r = client.post(
            "/v1/query", json={"q": "weather tomorrow?"}, headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.status_code == 200, r.text
        with create_engine(settings.database_admin_url).begin() as c:
            stored = c.execute(
                text(
                    "SELECT (decision->>'cost_usd')::float8 FROM routing_decision WHERE org_id = :o"
                ),
                {"o": org},
            ).scalar_one()
            (call,) = c.execute(
                text("SELECT cost_usd FROM ai_call WHERE org_id = :o"), {"o": org}
            ).scalars()
        assert call is not None and stored == pytest.approx(call, abs=1e-6)
    finally:
        with create_engine(settings.database_admin_url).begin() as c:
            c.execute(text("DELETE FROM routing_decision WHERE org_id = :o"), {"o": org})
        _cleanup(settings.database_admin_url, org)


def test_a_deepseek_cache_hit_is_unpriced_not_guessed() -> None:
    # DeepSeek prices cache hits in CNY by time of day; dst carries no rate,
    # so a call that reports hits is unpriced rather than priced at a guess.
    assert cost.ai_cost_usd("deepseek-v4-flash", 10, 10, cache_read_tokens=10) is None
    assert cost.ai_cost_usd("deepseek-v4-pro", 10, 10, cache_read_tokens=10) is None
    assert cost.ai_cost_usd("deepseek-v4-flash", 10, 10) is not None
