"""Contamination guard — keep 'taught' questions out of the reasoning score.

The certified may answer questions it was built for (the product working); the
benchmark may only SCORE the lens on questions it was not. Three guarantees this
module makes structural rather than a matter of intent:

  1. train/test split — the certified is built from TRAIN; accuracy is measured on a
     disjoint HELD-OUT test set.
  2. overlap detector — ``assert_held_out`` refuses to score any question whose
     exact (normalized) text the certified already answers. Teaching to the test
     cannot happen silently, even by accident.
  3. metric separation — ``SplitMetrics`` reports COVERAGE (what share of real
     questions are pre-answered — a legitimate product metric) APART from
     REASONING ACCURACY (held-out only). There is deliberately no method that
     blends them into one number.

The one-liner: the certified may answer questions it was built for; the benchmark may
only score it on questions it was not.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable
from dataclasses import dataclass

_NORM = re.compile(r"[^a-z0-9]+")


def normalize(question: str) -> str:
    """Case/punctuation/whitespace-insensitive form for exact-match comparison."""
    return _NORM.sub(" ", question.lower()).strip()


def taught_set(certified_questions: Iterable[str]) -> frozenset[str]:
    """The normalized questions the certified answers verbatim — the off-limits set."""
    return frozenset(normalize(q) for q in certified_questions if q and q.strip())


def is_taught(question: str, taught: frozenset[str]) -> bool:
    return normalize(question) in taught


class ContaminationError(AssertionError):
    """Raised when the benchmark is asked to score a question the certified teaches."""


def find_leaked(questions: Iterable[str], taught: frozenset[str]) -> list[str]:
    """Questions in the scoring set whose exact text the certified already answers."""
    return sorted({q for q in questions if is_taught(q, taught)})


def assert_held_out(questions: Iterable[str], taught: frozenset[str]) -> None:
    """Refuse to score any question the certified teaches — scoring it would measure
    lookup, not reasoning. Raises ContaminationError naming the leaks."""
    leaked = find_leaked(questions, taught)
    if leaked:
        raise ContaminationError(
            f"{len(leaked)} benchmark question(s) are verbatim in the certified — scoring them "
            f"would measure lookup, not reasoning. Move them to the train split or drop them: "
            f"{leaked[:3]}{' …' if len(leaked) > 3 else ''}"
        )


@dataclass
class TrainTestSplit:
    train: list[str]  # build the certified from these
    test: list[str]  # the only set scored for reasoning (held-out)


def train_test_split(
    questions: Iterable[str], *, test_fraction: float = 0.5, seed: int = 17
) -> TrainTestSplit:
    """Partition a question bank: TRAIN feeds the certified, TEST is held out for scoring.
    Deterministic by seed so a run is reproducible."""
    items = list(dict.fromkeys(questions))  # de-dupe, preserve first occurrence
    random.Random(seed).shuffle(items)
    cut = round(len(items) * (1.0 - test_fraction))
    return TrainTestSplit(train=items[:cut], test=items[cut:])


@dataclass
class SplitMetrics:
    """Coverage and reasoning, separate by construction.

    Built from graded ``(question, correct)`` pairs and the certified's taught set:
    taught questions land in COVERAGE; the rest are the REASONING denominator.
    There is no blended-accuracy property — that conflation is the whole bug.
    """

    taught: list[str]
    held_out_correct: list[str]
    held_out_wrong: list[str]

    @property
    def total(self) -> int:
        return len(self.taught) + self.held_out_total

    @property
    def held_out_total(self) -> int:
        return len(self.held_out_correct) + len(self.held_out_wrong)

    @property
    def coverage(self) -> float | None:
        """Share of asked questions the certified already pre-answers (a PRODUCT metric)."""
        return len(self.taught) / self.total if self.total else None

    @property
    def reasoning_accuracy(self) -> float | None:
        """Accuracy on HELD-OUT questions only — the capability number."""
        return len(self.held_out_correct) / self.held_out_total if self.held_out_total else None

    def render(self) -> str:
        cov = self.coverage
        acc = self.reasoning_accuracy
        return "\n".join(
            [
                "CONTAMINATION-GUARDED SCORE",
                f"  coverage (pre-answered by certified): {len(self.taught)}/{self.total} = "
                f"{f'{cov:.0%}' if cov is not None else '—'}   [product metric — NOT reasoning]",
                f"  reasoning accuracy (held-out):    {len(self.held_out_correct)}/"
                f"{self.held_out_total} = {f'{acc:.0%}' if acc is not None else '—'}   "
                "[the capability number]",
                "  (taught questions are excluded from the reasoning denominator by construction)",
            ]
        )


def split_metrics(graded: Iterable[tuple[str, bool]], taught: frozenset[str]) -> SplitMetrics:
    """Partition graded (question, correct) pairs into coverage vs held-out reasoning.

    A taught question never counts toward reasoning accuracy — it lands in coverage
    regardless of whether it was answered correctly."""
    taught_qs: list[str] = []
    correct: list[str] = []
    wrong: list[str] = []
    for question, ok in graded:
        if is_taught(question, taught):
            taught_qs.append(question)
        elif ok:
            correct.append(question)
        else:
            wrong.append(question)
    return SplitMetrics(taught=taught_qs, held_out_correct=correct, held_out_wrong=wrong)
