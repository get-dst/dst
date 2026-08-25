"""OpenAICompatProvider — one wire shape for every OpenAI-style endpoint."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from services.contracts.protocols import CacheableBlock, Message
from services.llm.deepseek_provider import DeepSeekProvider
from services.llm.openai_compat import OpenAICompatProvider


class _Capture:
    url: str
    headers: dict[str, str]
    body: dict[str, Any]


def _patch_post(monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any]) -> _Capture:
    cap = _Capture()

    def fake_post(
        url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float
    ) -> httpx.Response:
        cap.url, cap.headers, cap.body = url, headers, json
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr("services.llm.openai_compat.httpx.post", fake_post)
    return cap


_REPLY = {
    "choices": [{"message": {"content": "42"}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
}


def test_flattens_system_blocks_and_joins_url(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _patch_post(monkeypatch, _REPLY)
    llm = OpenAICompatProvider(api_key="sk-x", base_url="http://ollama.local:11434/v1/")
    res = llm.complete(
        system=[CacheableBlock("a"), CacheableBlock("b")],
        messages=[Message("user", "q")],
        model="llama3",
        temperature=0.1,
        max_tokens=64,
    )
    assert cap.url == "http://ollama.local:11434/v1/chat/completions"  # trailing slash handled
    assert cap.headers["Authorization"] == "Bearer sk-x"
    assert cap.body["messages"][0] == {"role": "system", "content": "a\n\nb"}
    assert cap.body["messages"][1] == {"role": "user", "content": "q"}
    assert cap.body["model"] == "llama3"
    assert (res.text, res.input_tokens, res.output_tokens) == ("42", 7, 3)
    assert res.cache_read_tokens == 0  # caching is an Anthropic concept


def test_missing_usage_reports_zero_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    llm = OpenAICompatProvider(api_key="k", base_url="https://api.example.com")
    res = llm.complete(
        system=[], messages=[Message("user", "q")], model="m", temperature=0.0, max_tokens=8
    )
    assert (res.input_tokens, res.output_tokens) == (0, 0)


def test_deepseek_is_the_generic_provider_at_its_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _patch_post(monkeypatch, _REPLY)
    llm = DeepSeekProvider("sk-d")
    llm.complete(
        system=[],
        messages=[Message("user", "q")],
        model="deepseek-v4-flash",
        temperature=0.2,
        max_tokens=8,
    )
    assert cap.url == "https://api.deepseek.com/chat/completions"
    assert isinstance(llm, OpenAICompatProvider)
