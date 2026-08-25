"""A bound on the serving path's per-request model + embedding calls.

The cold-start failure this exists for: the first query after a lens's first
certified apply hangs for tens of minutes, unlogged, while every other endpoint
on the same server stays healthy. The apply side's embedder cold start is
documented and its client-side timeout is not — and the QUERY side had no bound
anywhere: the per-request embedder
is *constructed* per call, and a local embedder's constructor downloads model
weights (fastembed → HuggingFace), which can stall indefinitely. Provider HTTP
clients carry their own 120s read timeouts; nothing covered the rest.

So every per-request model/embedding call runs under one generous, configurable
bound (``DST_SERVING_TIMEOUT_S``, default 600s — twice the slowest legitimate
generation measured, 250-330s). A wedged upstream surfaces as an error naming
what stalled and for how long, instead of a request that never returns.

The worker thread cannot be interrupted (Python has no thread kill): the point
is that the REQUEST stops waiting and says why. The orphan is a daemon, so it
finishes into nothing and never holds up process exit.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from services.config import settings

log = logging.getLogger("dst")


class ServingTimeout(RuntimeError):
    """A per-request model/embedding call exceeded the serving timeout."""


def call_bounded[T](what: str, fn: Callable[[], T]) -> T:
    """Run *fn* under the serving timeout; raise ``ServingTimeout`` if it stalls.

    *what* names the stage in the error and the log line ("SQL generation"), so a
    wedged upstream is legible from the trace alone. ``DST_SERVING_TIMEOUT_S <= 0``
    disables the bound (the escape hatch for a deliberately slow install).
    """
    seconds = settings.serving_timeout_s
    if seconds <= 0:
        return fn()
    out: list[T] = []
    failure: list[BaseException] = []

    def _run() -> None:
        try:
            out.append(fn())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            failure.append(exc)

    worker = threading.Thread(target=_run, name=f"dst-serving-{what}", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        log.error("%s exceeded the %.0fs serving timeout — abandoning the call", what, seconds)
        raise ServingTimeout(
            f"{what} did not return within the {seconds:.0f}s serving timeout "
            "(DST_SERVING_TIMEOUT_S) — the model or embedding provider is not responding; "
            "retry, or raise the timeout if this lens's answers legitimately take longer"
        )
    if failure:
        raise failure[0]
    return out[0]
