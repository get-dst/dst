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

The same bound covers each warehouse step of a served request (``warehouse_bounded``):
a connection the warehouse accepted and never answered held the request, and every
request queued behind it, for as long as the process lived.

The worker thread cannot be interrupted (Python has no thread kill): the point
is that the REQUEST stops waiting and says why. The orphan is a daemon, so it
finishes into nothing and never holds up process exit. What it holds can still be
let go: a step registers how (``on_abandon`` — a connector interrupts its
statement), and the bound runs that before it gives up.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections.abc import Callable

from services.config import settings

log = logging.getLogger("dst")


class ServingTimeout(RuntimeError):
    """A per-request model/embedding call exceeded the serving timeout."""


# The prefix of the apply endpoint's own 5xx detail for a step past its
# deadline. The CLI reads it to tell dst's verdict ("rolled back, nothing
# deployed") from a proxy's 502/503/504, which means upstream never answered
# and the apply may still be running.
APPLY_ABORTED_PREFIX = "apply aborted: "


class ApplyStepTimeout(RuntimeError):
    """A warehouse-touching step of an apply exceeded ``DST_APPLY_STEP_TIMEOUT_S``.

    Carries the step and the bound so the endpoint can name both. The engine's
    per-step ``except Exception`` handlers — a failed probe is that connection's
    error, a failed certified re-probe is a warning — must let this one through:
    the deadline is the apply's verdict, not the step's."""

    def __init__(self, step: str, seconds: float, bound: str = "DST_APPLY_STEP_TIMEOUT_S") -> None:
        self.step = step
        self.seconds = seconds
        super().__init__(
            f"{step} did not return within {seconds:g}s ({bound}): the "
            "warehouse is not responding. Nothing was deployed; prior state keeps serving"
        )


class WarehouseTimeout(RuntimeError):
    """A warehouse step did not return within its deadline.

    Carries the step and the bound so every surface can name both. A timeout is
    the request's verdict, never a repairable failure: asking a warehouse that is
    not answering again only waits again, so the serving pipeline ends the request
    on it and the door answers 504. ``bound`` names the setting behind the
    deadline, when one does."""

    def __init__(self, step: str, seconds: float, bound: str | None = None) -> None:
        self.step = step
        self.seconds = seconds
        setting = f" ({bound})" if bound else ""
        super().__init__(
            f"{step} did not return within {seconds:g}s{setting}: the warehouse is not "
            "responding. This request was abandoned; later requests are unaffected"
        )


class Stalled(Exception):
    """The worker outlived its bound (never a driver's own TimeoutError)."""


_abandon_hooks: contextvars.ContextVar[list[Callable[[], None]] | None] = contextvars.ContextVar(
    "dst_abandon_hooks", default=None
)


def on_abandon(hook: Callable[[], None]) -> None:
    """Have *hook* run if the bound this code runs under gives up on it, before the
    bound raises. The stalled thread cannot be killed, but what it holds can be let
    go: a statement interrupted, a shared connection retired. Outside a bound, a
    no-op."""
    hooks = _abandon_hooks.get()
    if hooks is not None:
        hooks.append(hook)


def run_bounded[T](name: str, fn: Callable[[], T], seconds: float) -> T:
    """Run *fn* on a daemon thread named *name*; raise ``Stalled`` if it has not
    returned within *seconds*, after running what it registered with
    ``on_abandon``. It runs in a copy of the caller's context, so the ambient
    request state (query tag, attribution) travels with it."""
    out: list[T] = []
    failure: list[BaseException] = []
    hooks: list[Callable[[], None]] = []
    context = contextvars.copy_context()
    context.run(_abandon_hooks.set, hooks)

    def _run() -> None:
        try:
            out.append(context.run(fn))
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            failure.append(exc)

    worker = threading.Thread(target=_run, name=name, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        for hook in hooks:
            try:
                hook()
            except Exception:  # noqa: BLE001 — the stall is the verdict; a failed release is logged
                log.exception("%s: releasing what the stalled call held failed", name)
        raise Stalled
    if failure:
        raise failure[0]
    return out[0]


def call_bounded[T](what: str, fn: Callable[[], T]) -> T:
    """Run *fn* under the serving timeout; raise ``ServingTimeout`` if it stalls.

    *what* names the stage in the error and the log line ("SQL generation"), so a
    wedged upstream is legible from the trace alone. ``DST_SERVING_TIMEOUT_S <= 0``
    disables the bound (the escape hatch for a deliberately slow install).
    """
    seconds = settings.serving_timeout_s
    if seconds <= 0:
        return fn()
    try:
        return run_bounded(f"dst-serving-{what}", fn, seconds)
    except Stalled:
        log.error("%s exceeded the %.0fs serving timeout — abandoning the call", what, seconds)
        raise ServingTimeout(
            f"{what} did not return within the {seconds:.0f}s serving timeout "
            "(DST_SERVING_TIMEOUT_S) — the model or embedding provider is not responding; "
            "retry, or raise the timeout if this lens's answers legitimately take longer"
        ) from None


def apply_bounded[T](step: str, fn: Callable[[], T]) -> T:
    """Run one warehouse-touching step of an apply under ``DST_APPLY_STEP_TIMEOUT_S``;
    raise ``ApplyStepTimeout`` naming *step* if it stalls.

    The apply holds the org's apply lock and its transaction while *fn* runs, so
    the orphaned worker is a daemon and the REQUEST is what stops waiting: the
    endpoint rolls back, the lock goes with the transaction, and the next apply
    proceeds. ``0`` disables the bound.
    """
    seconds = settings.apply_step_timeout_s
    started = time.perf_counter()
    try:
        return fn() if seconds <= 0 else run_bounded(f"dst-apply-{step}", fn, seconds)
    except Stalled:
        log.error("apply: %s exceeded the %gs step deadline — abandoning it", step, seconds)
        raise ApplyStepTimeout(step, seconds) from None
    finally:
        log.info("apply: %s took %.2fs", step, time.perf_counter() - started)


def apply_first_open(connection: str, connector: object) -> None:
    """Pay *connector*'s one-time cost in this process (its ``warm``: on
    MotherDuck, loading the extension, a download on a machine that never loaded
    it, and the first attach) before a step deadline starts, under
    ``DST_WAREHOUSE_FIRST_OPEN_TIMEOUT_S``. A cold machine's first apply aborted
    at the probe's deadline while a warm one opens in well under a second: the
    step deadline measures the warehouse, not the download. Past this bound the
    apply aborts the same way, naming the open and the setting. A connector with
    nothing to warm, or one already open, costs nothing here."""
    warm = getattr(connector, "warm", None)
    if warm is None:
        return
    step = f"the first open of connection '{connection}'"
    seconds = settings.warehouse_first_open_timeout_s
    started = time.perf_counter()
    try:
        if seconds <= 0:
            warm()
        else:
            run_bounded("dst-apply-first-open", warm, seconds)
    except (Stalled, WarehouseTimeout):
        log.error("apply: %s exceeded the %gs bound — abandoning it", step, seconds)
        raise ApplyStepTimeout(step, seconds, "DST_WAREHOUSE_FIRST_OPEN_TIMEOUT_S") from None
    finally:
        log.info("apply: %s took %.2fs", step, time.perf_counter() - started)


def warehouse_bounded[T](step: str, fn: Callable[[], T], *, connection: str) -> T:
    """Run one warehouse step of a served request (the dry run, the query, a value
    probe) under ``DST_SERVING_TIMEOUT_S``; raise ``WarehouseTimeout`` naming *step*
    if it stalls. Each step logs its name, *connection* and seconds at INFO, so a
    slow request is measurable from the server log.

    A connection that never answers used to hold the request for as long as the
    process lived, and a connector's own statement timeout cannot help while it is
    still connecting. The orphaned worker is a daemon; the REQUEST stops waiting
    and says which step it gave up on. ``0`` disables the bound."""
    seconds = settings.serving_timeout_s
    started = time.perf_counter()
    try:
        return fn() if seconds <= 0 else run_bounded(f"dst-warehouse-{step}", fn, seconds)
    except Stalled:
        log.error("%s exceeded the %gs serving timeout — abandoning it", step, seconds)
        raise WarehouseTimeout(step, seconds, "DST_SERVING_TIMEOUT_S") from None
    finally:
        log.info(
            "serving: %s on connection '%s' took %.2fs",
            step,
            connection,
            time.perf_counter() - started,
        )
