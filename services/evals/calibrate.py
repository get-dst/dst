"""Grade the deciders over the corpus `dst test` already owns.

Two free labelled sets, no authoring:

- **lens**: every certified answer and behavioral case belongs to ONE lens, so
  routing its question over the org's published lenses has a gold label — a
  proxy (a question two lenses legitimately cover counts as a miss), said so in
  the report header.
- **certified_equivalent**: two active certified answers in one lens whose SQL
  normalises to the same text are gold ``equivalent`` (same query, two
  wordings); two whose SQL differs are gold ``different``. Pairs are capped
  per lens so the sweep stays bounded.

Only decisions the decider actually made are recorded: the cosine-verbatim
fast path is not a decider decision and is skipped, never counted as a
measured one. Pure over injected pieces — offline-testable with fakes.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable

import sqlglot

from services.certify.store import CertifiedAnswer, is_active
from services.contracts.protocols import Decider, LLMProvider
from services.evals.calibration import GradedDecision
from services.router import CoverageProfile, Router
from services.router.decider import LlmDecider, route_with_llm
from services.runtime import assembly

PAIR_CAP = 30  # equivalence pairs per lens — bounded cost


def _each[T, R](items: list[T], fn: Callable[[T], R | None], concurrency: int) -> list[R]:
    """``fn`` over every item — on a thread pool when ``concurrency`` > 1 —
    in item order, dropping the Nones (a failed or unmeasured decision is a
    missing record, never a crash). The lane's cost is the provider's round
    trips; wide, it is those divided by the workers."""

    def safe(item: T) -> R | None:
        try:
            return fn(item)
        except Exception:  # noqa: BLE001 — a starved decision is a missing record
            return None

    workers = max(1, min(concurrency, len(items)))
    if workers == 1:
        results = [safe(i) for i in items]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(safe, items))
    return [r for r in results if r is not None]


def lens_decisions(
    router: Router,
    profiles_by_lens: dict[str, CoverageProfile],
    decider: LlmDecider,
    labeled: list[tuple[str, str]],
    *,
    concurrency: int = 1,
    all_lenses: bool = False,
) -> list[GradedDecision]:
    """Route every (question, gold lens) and grade what the decider measured."""

    def one(item: tuple[str, str]) -> GradedDecision | None:
        question, gold = item
        d = route_with_llm(router, profiles_by_lens, decider, question, all_lenses=all_lenses)
        if d.decision is None:
            return None  # cosine fast path or outage: the decider decided nothing
        return GradedDecision(
            decider=d.decision.provider,
            slot="lens",
            chosen=d.decision.chosen,
            gold=gold,
            p=d.decision.p,
            margin=d.decision.margin,
        )

    return _each(labeled, one, concurrency)


def _canon(sql: str, dialect: str) -> str:
    try:
        return sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect).lower()
    except Exception:  # noqa: BLE001 — unparseable SQL pairs with nothing
        return sql.strip().lower()


def equivalence_pairs(
    answers: list[CertifiedAnswer], dialect: str, cap: int = PAIR_CAP
) -> list[tuple[CertifiedAnswer, CertifiedAnswer, str]]:
    """``(approved, asked, gold)`` — same normalised SQL ⇒ equivalent, else different.
    Equivalent pairs first (they are rare and the informative half), then
    different pairs, capped."""
    active = [a for a in answers if is_active(a.status) and not a.slots]
    canon = {a.id: _canon(a.sql, dialect) for a in active}
    same = [
        (a, b, "equivalent")
        for a, b in itertools.combinations(active, 2)
        if canon[a.id] == canon[b.id]
    ]
    diff = [
        (a, b, "different")
        for a, b in itertools.combinations(active, 2)
        if canon[a.id] != canon[b.id]
    ]
    return (same + diff)[:cap]


def equivalence_decisions(
    llm: LLMProvider,
    model: str,
    answers: list[CertifiedAnswer],
    dialect: str,
    *,
    k: int,
    cap: int = PAIR_CAP,
    decider: Decider | None = None,
    concurrency: int = 1,
) -> list[GradedDecision]:
    def one(pair: tuple[CertifiedAnswer, CertifiedAnswer, str]) -> GradedDecision:
        approved, asked, gold = pair
        d = assembly.equivalence_decision(
            llm, model, asked.question, approved.question, k=k, decider=decider
        )
        return GradedDecision(
            decider=d.provider,
            slot="certified_equivalent",
            chosen=d.chosen,
            gold=gold,
            p=d.p,
            margin=d.margin,
        )

    return _each(equivalence_pairs(answers, dialect, cap), one, concurrency)
