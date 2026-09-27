"""Is the typed-decision provider answering? The serving path's view of it.

A typed-decision provider that fails (billing, a 5xx, a timeout, the network)
takes the typed lane down for every question at once. The typed generator turns
that failure into ``TypedUnavailable``, which the pipeline serves by raw-SQL
generation where the lens or the caller accepts it (disclosed UNTYPED, the
failure named) and refuses, named, where it does not. Either way it is never a
502, and never silent.

The operator needs to hear it once, not once per request: the first failure of
a burst logs a WARNING, the rest are counted, and the first typed resolution
that succeeds afterwards logs the recovery. ``/ready`` reports the state.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from services.contracts.errors import ProviderError
from services.llm.vote_decider import DeciderStarved

log = logging.getLogger("dst")

UNAVAILABLE = "typed decisions unavailable"


class TypedUnavailable(RuntimeError):
    """The typed lane could not run for this question: its provider failed."""

    def __init__(self, what: str) -> None:
        self.what = what
        super().__init__(f"{UNAVAILABLE}: {what}")


def provider_failure(exc: BaseException) -> str | None:
    """A decision-provider failure in a few words ("typesafe HTTP 402",
    "typesafe timed out"), or None when ``exc`` is not one: anything else is a
    defect and keeps propagating."""
    if isinstance(exc, ProviderError):
        if exc.status is not None:
            return f"{exc.provider} HTTP {exc.status}"
        return f"{exc.provider} {exc.detail}"[:120]
    if isinstance(exc, DeciderStarved):
        return "the decider returned no usable reply"
    return None


_lock = threading.Lock()
_what: str | None = None
_since: datetime | None = None
_failed = 0


def failed(what: str) -> None:
    """One request found the provider failing."""
    global _what, _since, _failed
    with _lock:
        first = _what is None
        if first:
            _since = datetime.now(UTC)
        _what = what
        _failed += 1
    if first:
        log.warning(
            "%s: %s — answers fall back to raw-SQL generation (disclosed UNTYPED) "
            "where the lens or the caller accepts it; typed-only questions are "
            "refused. Logged once until a typed decision succeeds; /ready tracks it",
            UNAVAILABLE,
            what,
        )


def succeeded() -> None:
    """A typed resolution reached the provider and got its decisions."""
    global _what, _since, _failed
    with _lock:
        if _what is None:
            return
        what, since, n = _what, _since, _failed
        _what, _since, _failed = None, None, 0
    log.info(
        "typed decisions recovered (%s since %s, %d request(s) affected)",
        what,
        since.isoformat(timespec="seconds") if since else "?",
        n,
    )


def status() -> str | None:
    """``/ready``'s view: None while no failure stands, else what failed,
    since when, and how many requests it has touched."""
    with _lock:
        if _what is None:
            return None
        since = _since.isoformat(timespec="seconds") if _since else "?"
        return f"degraded ({_what} since {since}, {_failed} request(s) affected)"


def reset() -> None:
    """Forget any standing failure (tests)."""
    global _what, _since, _failed
    with _lock:
        _what, _since, _failed = None, None, 0
