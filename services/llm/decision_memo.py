"""Decisions shared across the lenses one routed question is typed on.

When the routed lens refuses for data it does not hold, the door types the
next lens that covers the question. Many of that lens's decisions are the
same decision: the same question, context and options (siblings over one
entity offer the same metrics, the same dimensions). Inside ``shared()``,
the typed decider answers a decision it has already answered for this
request from memory, at no cost; a decision whose input differs in any way
(another lens's options) is asked as usual. Identical input is the whole
test, so what is reused is lens-independent by construction.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from services.contracts.protocols import Decider, Decision, DecisionUsage, Option

_Key = tuple[str, str, tuple[Option, ...], bool]
_MEMO: contextvars.ContextVar[dict[_Key, Decision] | None] = contextvars.ContextVar(
    "decision_memo", default=None
)


@contextmanager
def shared() -> Iterator[None]:
    """Decisions made inside the block are answered once each."""
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def _reused(d: Decision) -> Decision:
    # Billed once, where it was asked: the reuse costs nothing.
    return replace(d, usage=DecisionUsage())


class Memoised:
    """A decider answering repeats of a decision from the open memo."""

    def __init__(self, inner: Decider, memo: dict[_Key, Decision]) -> None:
        self._inner = inner
        self._memo = memo
        if hasattr(inner, "decide_many"):
            self.decide_many = self._decide_many

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        key = (question, context, tuple(options), allow_none)
        if key in self._memo:
            return _reused(self._memo[key])
        d = self._inner.decide(
            question=question, context=context, options=options, allow_none=allow_none
        )
        if not d.refused:
            self._memo[key] = d
        return d

    def _decide_many(
        self, question: str, specs: dict[str, tuple[str, list[Option], bool]]
    ) -> dict[str, Decision]:
        keys = {k: (question, ctx, tuple(opts), none) for k, (ctx, opts, none) in specs.items()}
        out = {k: _reused(self._memo[key]) for k, key in keys.items() if key in self._memo}
        ask = {k: v for k, v in specs.items() if k not in out}
        if ask:
            fresh = self._inner.decide_many(question, ask)  # type: ignore[attr-defined]
            for k, d in fresh.items():
                if not d.refused:
                    self._memo[keys[k]] = d
            out.update(fresh)
        return out


def memoised(decider: Decider | None) -> Decider | None:
    """``decider`` answering from the open memo, or as it is when none is open."""
    memo = _MEMO.get()
    if decider is None or memo is None:
        return decider
    return Memoised(decider, memo)
