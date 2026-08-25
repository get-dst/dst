"""Shared error types crossing the provider seam (WI: error taxonomy).

An upstream model provider failing (overload, timeout, bad gateway, bad key)
is not a dst bug and must not surface as a raw 500 — providers raise
ProviderError and the app maps it to a 502 with the provider named.
"""

from __future__ import annotations


class ProviderError(Exception):
    def __init__(self, provider: str, detail: str, *, status: int | None = None) -> None:
        self.provider = provider
        self.status = status
        self.detail = detail
        note = f" (HTTP {status})" if status else ""
        super().__init__(f"{provider}{note}: {detail}")
