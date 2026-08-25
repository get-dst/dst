"""Per-caller rate limiting — a sliding-window counter keyed by org/lens/caller.

v1 is in-process (per worker); adequate for a single-instance pilot. A multi-instance
deploy should back this with Redis or a Postgres counter — the `check` interface stays
the same. `per_caller_rpm <= 0` means unlimited.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

_WINDOW_SECONDS = 60.0
_hits: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def check(key: str, per_minute: int, *, now: float | None = None) -> bool:
    """Record a hit for `key`; return False if it would exceed `per_minute`."""
    if per_minute <= 0:
        return True
    t = time.monotonic() if now is None else now
    cutoff = t - _WINDOW_SECONDS
    with _lock:
        dq = _hits[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= per_minute:
            return False
        dq.append(t)
        return True


def retry_after(key: str, *, now: float | None = None) -> int:
    """Seconds until the oldest hit in the window expires (for Retry-After)."""
    t = time.monotonic() if now is None else now
    with _lock:
        dq = _hits.get(key)
        if not dq:
            return 1
        return max(1, int(_WINDOW_SECONDS - (t - dq[0])) + 1)


def reset() -> None:
    """Clear all counters (test helper)."""
    with _lock:
        _hits.clear()
