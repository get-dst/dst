"""One usage record per provider call, with what the call was for.

Every provider client records each call it completes (``record``): the
model, the tokens the provider reported, the price, and the purpose the
caller declared around it (``purpose``). Records collect into the list a
request, an eval case or a command opened (``collecting``); a call made with
none open is not kept. The serving doors write the request's records to
``ai_call`` beside its request_log row, and an eval case carries its own on
the eval result row.

Purpose is a context, not an argument: a decider's votes are calls of the
routing, the typed slot or the equivalence gate that asked for the decision,
and the decider cannot know which. Each call site that asks a model for
something declares it; ``tests/test_ai_call_purposes.py`` fails on one that
does not.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from services.contracts.protocols import DecisionUsage
from services.observability import cost

log = logging.getLogger("dst")

Purpose = Literal[
    "route",  # which lens answers the question
    "decide",  # a typed slot, or the reading of an ambiguous term
    "judge",  # grading an answer: the typed judge, the review judge, the adversary
    "equivalence",  # is this question a paraphrase of an approved one
    "bind",  # a certified template's slot values from the question
    "generate",  # SQL, including a repair
    "compose",  # the answer's prose
    "other",  # authoring and maintenance commands
]


@dataclass(frozen=True)
class AICall:
    purpose: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    # Every token written to the prompt cache; the long-lived part of them
    # (Anthropic's 1-hour cache, OpenAI's 12-hour) apart, at its own rate.
    cache_write_tokens: int = 0
    cache_write_long_tokens: int = 0
    # None: the model has no price, or the call reports cache tokens it has
    # no cache rate for (ai_pricing names none).
    cost_usd: float | None = 0.0


_PURPOSE: contextvars.ContextVar[str] = contextvars.ContextVar("ai_purpose", default="other")
# Every open collection, outermost first: a call is kept in each, so a site
# that charges what one step billed (a nested collection) leaves the request's
# record whole.
_CALLS: contextvars.ContextVar[tuple[list[AICall], ...]] = contextvars.ContextVar(
    "ai_calls", default=()
)


@contextmanager
def purpose(name: Purpose) -> Iterator[None]:
    """Every provider call made inside the block is for *name*."""
    token = _PURPOSE.set(name)
    try:
        yield
    finally:
        _PURPOSE.reset(token)


def active() -> bool:
    return bool(_CALLS.get())


def current() -> list[AICall]:
    """The records of the innermost open collection so far (empty when none
    is open): the calls a request has made up to now."""
    open_ = _CALLS.get()
    return list(open_[-1]) if open_ else []


@contextmanager
def collecting() -> Iterator[list[AICall]]:
    """Collect the records of every call made inside the block (threads
    started through ``in_context`` or ``call_bounded`` included)."""
    calls: list[AICall] = []
    token = _CALLS.set((*_CALLS.get(), calls))
    try:
        yield calls
    finally:
        _CALLS.reset(token)


def in_context[T](fn: Callable[..., T]) -> Callable[..., T]:
    """*fn* bound to the caller's context, for a worker thread: its calls
    are recorded where the caller's are, under the caller's purpose."""
    ctx = contextvars.copy_context()

    def run(*args: object, **kwargs: object) -> T:
        return ctx.copy().run(fn, *args, **kwargs)

    return run


_WARNED: set[str] = set()


def record(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_long_tokens: int = 0,
) -> None:
    """One completed provider call. Kept when a collection is open. Priced
    with its prompt-cache tokens, through the endpoint its provider id names
    (``openai-decisions:gpt-6-luna`` is not priced as the chat model)."""
    open_ = _CALLS.get()
    if not open_:
        return
    call = AICall(
        purpose=_PURPOSE.get(),
        provider=provider,
        model=model,
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cache_read_tokens=int(cache_read_tokens),
        cache_write_tokens=int(cache_write_tokens),
        cache_write_long_tokens=int(cache_write_long_tokens),
        cost_usd=cost.ai_cost_usd(
            model,
            int(input_tokens),
            int(output_tokens),
            cache_read_tokens=int(cache_read_tokens),
            cache_write_tokens=int(cache_write_tokens),
            cache_write_long_tokens=int(cache_write_long_tokens),
            endpoint=cost.decision_endpoint(provider),
        ),
    )
    if call.cost_usd is None and model not in _WARNED:
        _WARNED.add(model)
        if cost.price_of(model) is None:
            log.warning(
                "model %s has no price: its calls are recorded unpriced and AI cost totals are "
                "a floor — declare it under ai_pricing in dst.yaml (USD per 1M tokens: "
                "[input, output])",
                model,
            )
        else:
            log.warning(
                "model %s reported prompt-cache tokens it has no cache price for: its calls are "
                "recorded unpriced and AI cost totals are a floor — declare its cache rates "
                "under ai_pricing in dst.yaml (USD per 1M tokens: [input, output, cache read, "
                "cache write, long-lived cache write])",
                model,
            )
    for calls in open_:
        calls.append(call)


def as_spent(calls: list[AICall]) -> list[tuple[str, DecisionUsage]]:
    """*calls* as ``(provider, usage)`` spend pairs, the shape an answer's row
    is charged from (priced by the endpoint and model the id names). Their
    prompt-cache tokens are not in the pairs: ``surcharge`` charges them."""
    return [
        (
            f"{cost.decision_endpoint(c.provider)}:{c.model}",
            DecisionUsage(calls=1.0, input_tokens=c.input_tokens, output_tokens=c.output_tokens),
        )
        for c in calls
    ]


def surcharge(calls: list[AICall]) -> float | None:
    """What *calls* cost beyond their uncached input and output tokens: the
    prompt-cache reads and writes, and a long-prompt rate the cache tokens
    tipped the call into. A request's row is charged each call's tokens as
    the call site reported them, which carry no cache; adding this makes the
    row what the providers billed. None when a call's cache tokens have no
    price (its row is unpriced, not charged as if they were free)."""
    total: float | None = 0.0
    for c in calls:
        if not (c.cache_read_tokens or c.cache_write_tokens):
            continue
        tokens_only = cost.ai_cost_usd(
            c.model,
            c.input_tokens,
            c.output_tokens,
            endpoint=cost.decision_endpoint(c.provider),
        )
        if tokens_only is None:
            continue  # the model is unpriced: the row already is
        total = cost.add_cost(total, None if c.cost_usd is None else c.cost_usd - tokens_only)
    return None if total is None else round(total, 6)


def totals(calls: list[AICall]) -> tuple[int, int, float | None]:
    """``(input_tokens, output_tokens, cost_usd)`` over *calls*; the cost is
    None when any call is unpriced, never a partial sum."""
    total: float | None = 0.0
    for c in calls:
        total = cost.add_cost(total, c.cost_usd)
    return (
        sum(c.input_tokens for c in calls),
        sum(c.output_tokens for c in calls),
        None if total is None else round(total, 6),
    )


def spend_fields(calls: list[AICall]) -> dict[str, Any]:
    """*calls*' totals as the eval tables name them."""
    tokens_in, tokens_out, usd = totals(calls)
    return {"ai_input_tokens": tokens_in, "ai_output_tokens": tokens_out, "ai_cost_usd": usd}


_INSERT = (
    "INSERT INTO ai_call (org_id, request_id, lens, caller, agent, purpose, provider, model, "
    "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
    "cache_write_long_tokens, cost_usd) "
    "VALUES (:org_id, :request_id, :lens, :caller, :agent, :purpose, :provider, :model, "
    ":input_tokens, :output_tokens, :cache_read_tokens, :cache_write_tokens, "
    ":cache_write_long_tokens, :cost_usd)"
)


def write(
    org_id: object,
    request_id: str,
    calls: list[AICall],
    *,
    lens: str | None = None,
    caller: str | None = None,
    agent: str | None = None,
) -> None:
    """Persist *calls* under *request_id*. Off the response path; a failure
    is logged, never raised into the answer."""
    if not calls:
        return
    from sqlalchemy import text

    from services.db.session import org_session

    try:
        with org_session(org_id) as session:  # type: ignore[arg-type]
            session.execute(
                text(_INSERT),
                [
                    {
                        "org_id": org_id,
                        "request_id": request_id,
                        "lens": lens,
                        "caller": caller,
                        "agent": agent,
                        "purpose": c.purpose,
                        "provider": c.provider,
                        "model": c.model,
                        "input_tokens": c.input_tokens,
                        "output_tokens": c.output_tokens,
                        "cache_read_tokens": c.cache_read_tokens,
                        "cache_write_tokens": c.cache_write_tokens,
                        "cache_write_long_tokens": c.cache_write_long_tokens,
                        "cost_usd": c.cost_usd,
                    }
                    for c in calls
                ],
            )
    except Exception:
        log.exception("ai_call rows for %s were not written", request_id)
