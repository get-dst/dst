"""RetryingLLM: transient failures (429, transport blips) retry with backoff;
real API errors re-raise immediately. Without it a transient network outage
silently converts whole benchmark lanes into crashes that grade as low scores."""

from __future__ import annotations

import pytest

from services.contracts.errors import ProviderError
from services.contracts.protocols import LLMResult
from services.llm.retry import RetryingLLM


class _Flaky:
    def __init__(self, failures: list[Exception]) -> None:
        self._failures = list(failures)
        self.calls = 0

    def complete(self, **_kw) -> LLMResult:
        self.calls += 1
        if self._failures:
            raise self._failures.pop(0)
        return LLMResult(text="ok", input_tokens=1, output_tokens=1)


def _complete(llm: RetryingLLM) -> LLMResult:
    return llm.complete(system=[], messages=[], model="m", temperature=0.0, max_tokens=8)


@pytest.mark.parametrize(
    "exc",
    [
        ProviderError("anthropic", "Connection error."),
        ProviderError("openai-compatible", "[Errno 8] nodename nor servname provided"),
        ProviderError("anthropic", "overloaded_error: 529"),
        ProviderError("anthropic", "429 too many requests"),
    ],
)
def test_transient_errors_retry_through(exc: Exception) -> None:
    inner = _Flaky([exc, exc])
    out = _complete(RetryingLLM(inner, attempts=3, base_delay=0.0))
    assert out.text == "ok" and inner.calls == 3


def test_non_transient_errors_raise_immediately() -> None:
    inner = _Flaky([ProviderError("anthropic", "invalid x-api-key")])
    with pytest.raises(ProviderError):
        _complete(RetryingLLM(inner, attempts=5, base_delay=0.0))
    assert inner.calls == 1


def test_status_code_beats_string_heuristics() -> None:
    # An attached HTTP status is authoritative: 500 retries even with a bland
    # message; 400 raises immediately even if the message mentions "429".
    inner = _Flaky([ProviderError("openai-compatible", "upstream error", status=500)])
    out = _complete(RetryingLLM(inner, attempts=2, base_delay=0.0))
    assert out.text == "ok" and inner.calls == 2

    inner = _Flaky([ProviderError("openai-compatible", "max 429 tokens exceeded", status=400)])
    with pytest.raises(ProviderError):
        _complete(RetryingLLM(inner, attempts=3, base_delay=0.0))
    assert inner.calls == 1


def test_exhausted_transient_reraises() -> None:
    exc = ProviderError("anthropic", "Connection error.")
    inner = _Flaky([exc] * 3)
    with pytest.raises(ProviderError):
        _complete(RetryingLLM(inner, attempts=3, base_delay=0.0))
    assert inner.calls == 3
