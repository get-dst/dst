"""`temperature` is sent only to Claude models that accept it.

Models from 4.7 on reject sampling parameters with a 400, so a call that
carried `temperature` on every request failed on every newer model. The
provider reads the version from the model name; a name it cannot read keeps
the parameter, and a 400 that names it is retried once without it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from services.contracts.protocols import CacheableBlock, Message
from services.llm import anthropic_provider
from services.llm.anthropic_provider import AnthropicProvider, takes_temperature


@pytest.mark.parametrize(
    ("model", "takes"),
    [
        ("claude-haiku-4-5", True),
        ("claude-sonnet-4-6", True),
        ("claude-sonnet-4-5-20250929", True),
        ("claude-opus-4-20250514", True),
        ("claude-3-5-sonnet-20241022", True),
        ("claude-opus-4-7", False),
        ("claude-opus-4-8", False),
        ("claude-sonnet-5", False),
        ("claude-sonnet-5-5", False),
        ("claude-opus-5-5", False),
        ("claude-fable-5-1", False),
        ("gateway-default", True),
    ],
)
def test_the_version_decides(model: str, takes: bool) -> None:
    assert takes_temperature(model) is takes


class _Messages:
    def __init__(self, refuse: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refuse = refuse

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self.refuse and "temperature" in kw:
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            raise anthropic.BadRequestError(
                "temperature: this model does not support sampling parameters",
                response=httpx.Response(400, request=request),
                body=None,
            )
        usage = SimpleNamespace(input_tokens=1, output_tokens=1)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn", usage=usage
        )


def _call(provider: AnthropicProvider, model: str) -> str:
    return provider.complete(
        system=[CacheableBlock(text="s")],
        messages=[Message(role="user", content="q")],
        model=model,
        temperature=0.0,
        max_tokens=64,
    ).text


def _provider(messages: _Messages) -> AnthropicProvider:
    provider = AnthropicProvider("test-key")
    provider._client = SimpleNamespace(messages=messages)  # type: ignore[assignment]
    return provider


def test_newer_models_get_no_temperature_older_ones_keep_it() -> None:
    messages = _Messages()
    provider = _provider(messages)
    assert _call(provider, "claude-opus-5-5") == "ok"
    assert _call(provider, "claude-haiku-4-5") == "ok"
    assert "temperature" not in messages.calls[0]
    assert messages.calls[1]["temperature"] == 0.0


def test_a_refusal_naming_temperature_is_retried_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anthropic_provider, "_REFUSES_TEMPERATURE", set())
    messages = _Messages(refuse=True)
    provider = _provider(messages)
    assert _call(provider, "gateway-default") == "ok"
    assert [("temperature" in c) for c in messages.calls] == [True, False]
    # Remembered: the next call to that model goes out without it.
    assert _call(provider, "gateway-default") == "ok"
    assert "temperature" not in messages.calls[-1] and len(messages.calls) == 3
