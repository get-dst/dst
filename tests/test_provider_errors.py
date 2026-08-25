"""Upstream provider failures raise ProviderError and map to 502, not a raw 500."""

from __future__ import annotations

import asyncio

import anthropic
import httpx
import pytest

from services.contracts.errors import ProviderError
from services.llm.anthropic_provider import AnthropicProvider
from services.llm.openai_compat import OpenAICompatProvider


def test_openai_compat_maps_upstream_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def overloaded(url: str, **kw: object) -> httpx.Response:
        return httpx.Response(529, request=httpx.Request("POST", url), text="overloaded")

    monkeypatch.setattr(httpx, "post", overloaded)
    p = OpenAICompatProvider(api_key="k", base_url="http://upstream")
    with pytest.raises(ProviderError) as ei:
        p.complete(system=[], messages=[], model="m", temperature=0.0, max_tokens=8)
    assert ei.value.status == 529 and ei.value.provider == "openai-compatible"


def test_openai_compat_maps_connection_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(url: str, **kw: object) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "post", unreachable)
    p = OpenAICompatProvider(api_key="k", base_url="http://upstream")
    with pytest.raises(ProviderError):
        p.complete(system=[], messages=[], model="m", temperature=0.0, max_tokens=8)


def test_anthropic_maps_sdk_errors() -> None:
    class _Messages:
        def create(self, **kw: object) -> None:
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))

    p = AnthropicProvider(api_key="k")
    p._client = type("C", (), {"messages": _Messages()})()  # type: ignore[assignment]
    with pytest.raises(ProviderError) as ei:
        p.complete(system=[], messages=[], model="m", temperature=0.0, max_tokens=8)
    assert ei.value.provider == "anthropic"


def test_anthropic_maps_sdk_signature_mismatch() -> None:
    # anthropic 1.x removed `temperature` from messages.create(); the resulting
    # TypeError must surface as a ProviderError naming the installed SDK version,
    # not an unhandled 500.
    class _Messages:
        def create(self, **kw: object) -> None:
            raise TypeError("Messages.create() got an unexpected keyword argument 'temperature'")

    p = AnthropicProvider(api_key="k")
    p._client = type("C", (), {"messages": _Messages()})()  # type: ignore[assignment]
    with pytest.raises(ProviderError) as ei:
        p.complete(system=[], messages=[], model="m", temperature=0.0, max_tokens=8)
    assert ei.value.provider == "anthropic"
    assert anthropic.__version__ in str(ei.value)
    assert "anthropic<1" in str(ei.value)


def test_app_maps_provider_error_to_502() -> None:
    from services.app import _provider_error, app

    assert ProviderError in app.exception_handlers
    resp = asyncio.run(_provider_error(None, ProviderError("voyage", "overloaded")))  # type: ignore[arg-type]
    assert resp.status_code == 502
    assert b"voyage" in resp.body
