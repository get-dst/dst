"""Retry wrapper for the `LLMProvider` seam.

A 429 — or a transient network failure (DNS blip, dropped connection,
provider 5xx) — must never surface as a customer-facing 502 when a short
backoff would have absorbed it. Real API errors (auth, bad request, context
overflow) re-raise immediately.

Two callers, two policies:
- serving (wrapped at the registry): attempts=3, base_delay=1.0 — worst case
  adds ~3s of sleep to a request, never minutes.
- benchmark ladders: attempts=5, base_delay=15.0 — a long ladder must survive
  a sustained rate-limit window; without it a DNS outage silently grades the
  crashed cells as near-zero scores instead of failing loudly.
"""

from __future__ import annotations

import time

from services.contracts.errors import ProviderError
from services.contracts.protocols import CacheableBlock, LLMProvider, LLMResult, Message

_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 529}


class RetryingLLM:
    """Wraps any LLMProvider with exponential backoff on transient failures."""

    def __init__(self, inner: LLMProvider, *, attempts: int = 3, base_delay: float = 1.0) -> None:
        self.inner = inner  # public: callers/tests introspect the wrapped client
        self._attempts = attempts
        self._base_delay = base_delay

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        last: Exception | None = None
        for attempt in range(self._attempts):
            try:
                return self.inner.complete(
                    system=system,
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 — inspect, re-raise non-transient
                if not _is_transient(exc) or attempt == self._attempts - 1:
                    raise
                last = exc
                time.sleep(self._base_delay * (2**attempt))
        raise last if last else RuntimeError("unreachable")


def _is_transient(exc: Exception) -> bool:
    """Rate limits, 5xx, and transport blips retry; real API errors (auth, bad
    request, context overflow) re-raise immediately."""
    # Providers attach the HTTP status when there was one — trust it over strings.
    if isinstance(exc, ProviderError) and exc.status is not None:
        return exc.status in _TRANSIENT_STATUS
    name = type(exc).__name__
    text = str(exc)
    if "RateLimit" in name or "429" in text:
        return True
    transport = (
        "Connection error",
        "ConnectError",
        "ReadTimeout",
        "timed out",
        "nodename",  # macOS DNS: [Errno 8] nodename nor servname provided
        "Errno 8",
        "overloaded",  # anthropic 529
        "529",
        "502",
        "503",
    )
    return any(marker in text for marker in transport)
