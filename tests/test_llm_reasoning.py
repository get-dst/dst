"""Reasoning-mode models get thinking headroom; everyone else is untouched.

A reasoning model spends its thinking against `max_tokens` BEFORE any answer
lands, so caps tuned for non-reasoning models return EMPTY completions on one:
deepseek-v4-flash yields 0 chars of SQL at 1024 and valid SQL at 8192. The
headroom lives in the provider layer so call sites keep expressing answer size
— these pin that it applies where it should and nowhere else.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from services.config import ProviderConfig
from services.contracts.protocols import CacheableBlock, Message
from services.llm import registry
from services.llm.anthropic_provider import AnthropicProvider
from services.llm.openai_compat import OpenAICompatProvider
from services.llm.reasoning import THINKING_HEADROOM, is_reasoning, with_headroom

_REPLY = {
    "choices": [{"message": {"content": "SELECT 1"}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
}


class _Capture:
    body: dict[str, Any]


def _patch_post(monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any] = _REPLY) -> _Capture:
    cap = _Capture()

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> httpx.Response:
        cap.body = json
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr("services.llm.openai_compat.httpx.post", fake_post)
    return cap


# ── (a) a reasoning provider gets headroom above the requested cap ────────────
def test_flagged_provider_adds_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _patch_post(monkeypatch)
    llm = OpenAICompatProvider(api_key="k", base_url="https://api.example.com", reasoning=True)
    llm.complete(
        system=[],
        messages=[Message("user", "q")],
        model="house-model",
        temperature=0.0,
        max_tokens=400,
    )
    # The judge's 400 is what it wants of VERDICT; thinking is stacked on top.
    assert cap.body["max_tokens"] == 400 + THINKING_HEADROOM
    assert cap.body["max_tokens"] > 400


def test_name_heuristic_catches_the_model_we_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _patch_post(monkeypatch)
    llm = OpenAICompatProvider(api_key="k", base_url="https://api.deepseek.com")
    llm.complete(
        system=[],
        messages=[Message("user", "q")],
        model="deepseek-v4-flash",
        temperature=0.0,
        max_tokens=1024,
    )
    assert cap.body["max_tokens"] == 1024 + THINKING_HEADROOM


def test_anthropic_thinking_by_default_models_get_headroom() -> None:
    seen: dict[str, Any] = {}

    class _Messages:
        def create(self, **kw: Any) -> Any:
            seen.update(kw)
            block = type("B", (), {"type": "text", "text": "ok"})()
            usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
            return type("R", (), {"content": [block], "usage": usage, "stop_reason": "end_turn"})()

    p = AnthropicProvider(api_key="k")
    p._client = type("C", (), {"messages": _Messages()})()  # type: ignore[assignment]
    # We never send `thinking`, so only models that think by DEFAULT spend the cap on it.
    p.complete(system=[], messages=[], model="claude-opus-5", temperature=0.0, max_tokens=1024)
    assert seen["max_tokens"] == 1024 + THINKING_HEADROOM


# ── (b) a normal provider's request is byte-identical to before ───────────────
def test_non_reasoning_request_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _patch_post(monkeypatch)
    llm = OpenAICompatProvider(api_key="k", base_url="https://api.example.com")
    llm.complete(
        system=[CacheableBlock("a")],
        messages=[Message("user", "q")],
        model="llama3",
        temperature=0.1,
        max_tokens=64,
    )
    assert cap.body == {
        "model": "llama3",
        "messages": [{"role": "system", "content": "a"}, {"role": "user", "content": "q"}],
        "temperature": 0.1,
        "max_tokens": 64,  # untouched — no headroom, no cost change, no behaviour change
        "stream": False,
    }


def test_anthropic_4x_family_is_untouched() -> None:
    seen: dict[str, Any] = {}

    class _Messages:
        def create(self, **kw: Any) -> Any:
            seen.update(kw)
            block = type("B", (), {"type": "text", "text": "ok"})()
            usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
            return type("R", (), {"content": [block], "usage": usage, "stop_reason": "end_turn"})()

    p = AnthropicProvider(api_key="k")
    p._client = type("C", (), {"messages": _Messages()})()  # type: ignore[assignment]
    # These do NOT think unless the request asks, and AnthropicProvider never asks.
    for model in ("claude-sonnet-4-6", "claude-haiku-4-5"):
        p.complete(system=[], messages=[], model=model, temperature=0.0, max_tokens=1024)
        assert seen["max_tokens"] == 1024, model


def test_headroom_covers_the_measured_thinking_requirement() -> None:
    """The headroom is a measurement, not a round number.

    deepseek-v4-flash spends 16,384 reasoning tokens and returns ZERO content at
    a 16,384 wire cap (finish_reason "length"), then spends 24,104 and produces
    valid SQL at 32,768. The generator's real ask is 8192, so the wire cap has
    to clear that 24,104 with the answer's own budget still intact — an 8192
    headroom does not, which makes every multi-step question fail silently.
    """
    measured_thinking_tokens = 24_104
    generator_ask = 8192
    wire_cap = with_headroom(generator_ask, "deepseek-v4-flash", None)
    assert wire_cap - measured_thinking_tokens >= generator_ask, (
        f"wire cap {wire_cap} leaves only {wire_cap - measured_thinking_tokens} tokens "
        f"for the answer after measured thinking; need >= {generator_ask}"
    )
    # Additive headroom must stay under the tightest current provider ceiling
    # (64k on Haiku 4.5; 128k elsewhere) or the request 400s instead.
    assert wire_cap <= 64_000, wire_cap


def test_config_flag_beats_the_heuristic_both_ways() -> None:
    # An install that knows better than the name markers wins in both directions —
    # that is what keeps the heuristic from rotting into a release dependency.
    assert with_headroom(400, "deepseek-v4-flash", False) == 400
    assert with_headroom(400, "some-house-model", True) == 400 + THINKING_HEADROOM
    assert is_reasoning("deepseek-v4-flash", None) is True
    assert is_reasoning("llama3", None) is False


# ── (c) empty replies are loud instead of silently absorbed ───────────────────
def test_empty_completion_logs_actionably(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Reasoning models answer with content=null when thinking ate the budget;
    # callers' `or fallback` then reports success with garbage (patch.py pasted
    # the user's raw correction note in as a definition body this way).
    _patch_post(monkeypatch, {"choices": [{"message": {"content": None}}]})
    llm = OpenAICompatProvider(api_key="k", base_url="https://api.example.com")
    with caplog.at_level(logging.WARNING, logger="dst"):
        res = llm.complete(
            system=[],
            messages=[Message("user", "q")],
            model="mystery-model",
            temperature=0.0,
            max_tokens=400,
        )
    assert res.text == ""  # never None — LLMResult.text is a str
    assert "empty completion" in caplog.text
    assert "mystery-model" in caplog.text and "400" in caplog.text


def test_empty_completion_logs_the_cap_actually_sent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # An operator reading this line must not be told 400 when we really sent 8592.
    _patch_post(monkeypatch, {"choices": [{"message": {"content": ""}}]})
    llm = OpenAICompatProvider(api_key="k", base_url="https://api.example.com", reasoning=True)
    with caplog.at_level(logging.WARNING, logger="dst"):
        llm.complete(
            system=[],
            messages=[Message("user", "q")],
            model="house-model",
            temperature=0.0,
            max_tokens=400,
        )
    assert str(400 + THINKING_HEADROOM) in caplog.text


def test_non_empty_completion_stays_quiet(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_post(monkeypatch)
    llm = OpenAICompatProvider(api_key="k", base_url="https://api.example.com")
    with caplog.at_level(logging.WARNING, logger="dst"):
        res = llm.complete(
            system=[],
            messages=[Message("user", "q")],
            model="llama3",
            temperature=0.0,
            max_tokens=64,
        )
    assert res.text == "SELECT 1"
    assert caplog.text == ""


# ── the flag reaches the wire through the registry ────────────────────────────
def test_registry_threads_the_flag_to_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry.settings,
        "providers",
        {
            "house": ProviderConfig(
                type="openai-compatible",
                api_key="sk-h",
                base_url="https://api.house.co",
                models=["house-model"],
                reasoning=True,
            )
        },
    )
    assert registry.specs()["house"].reasoning is True
    resolved = registry.resolve("house/house-model")
    assert resolved is not None
    inner = resolved.llm.inner  # type: ignore[union-attr]  # RetryingLLM wraps it
    assert inner._reasoning is True
