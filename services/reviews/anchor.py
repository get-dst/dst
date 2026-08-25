"""Judge calibration over decided review tickets.

The judge's verdicts auto-approve tickets and score the health lane, but its
reliability was never measured. Every ticket where BOTH the AI judge and a
human ruled is a labeled anchor row — no new table, no labeling ceremony:
the review queue's normal operation accumulates the anchor set.

Two reads on it:
- ``agreement()`` — the stored (ai_verdict, human_verdict) pairs, free, no LLM:
  the judge's historical calibration against humans.
- ``rejudge()`` — re-score the same traces with TODAY'S judge: ``vs_human`` is
  its current calibration; ``vs_stored`` isolates judge drift (a model or
  prompt change moved the verdicts even though the traces are identical).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.contracts.protocols import LLMProvider
from services.reviews import store
from services.reviews.judge import judge_trace

# An UNRULED ticket is not an anchor row: the judge was unavailable or returned an
# empty reply (judge.NO_VERDICT), so there is no judgment to calibrate against —
# counting "" as a label would report a provider outage as judge/human disagreement.
_PAIRS = text(
    "SELECT ticket_id, request_id, ai_verdict, human_verdict FROM review "
    "WHERE ai_verdict IS NOT NULL AND ai_verdict <> '' AND human_verdict IS NOT NULL "
    "ORDER BY created_at DESC LIMIT :limit"
)


@dataclass(frozen=True)
class AnchorReport:
    n: int
    agreement: float | None  # exact verdict agreement rate; None when n == 0
    kappa: float | None  # Cohen's kappa (chance-corrected); None when undefined
    confusion: dict[str, int]  # "ai_verdict:human_verdict" → count


def _report(pairs: list[tuple[str, str]]) -> AnchorReport:
    n = len(pairs)
    if n == 0:
        return AnchorReport(n=0, agreement=None, kappa=None, confusion={})
    agree = sum(1 for a, h in pairs if a == h) / n
    ai_counts = Counter(a for a, _ in pairs)
    human_counts = Counter(h for _, h in pairs)
    labels = set(ai_counts) | set(human_counts)
    chance = sum(ai_counts[label] * human_counts[label] for label in labels) / (n * n)
    kappa = None if chance == 1 else (agree - chance) / (1 - chance)
    confusion = Counter(f"{a}:{h}" for a, h in pairs)
    return AnchorReport(
        n=n,
        agreement=round(agree, 3),
        kappa=round(kappa, 3) if kappa is not None else None,
        confusion=dict(confusion),
    )


def agreement(session: Session, *, limit: int = 500) -> AnchorReport:
    """Stored judge-vs-human agreement over decided tickets. No LLM calls."""
    rows = session.execute(_PAIRS, {"limit": limit}).all()
    return _report([(str(r[2]), str(r[3])) for r in rows])


@dataclass(frozen=True)
class RejudgeReport:
    n: int
    vs_human: AnchorReport  # today's judge against the human ground truth
    vs_stored: AnchorReport  # today's judge against its own past verdicts (drift)


def rejudge(session: Session, llm: LLMProvider, model: str, *, limit: int = 50) -> RejudgeReport:
    """Re-score the anchor traces with today's judge. Costs one judge call per row."""
    rows = session.execute(_PAIRS, {"limit": limit}).all()
    vs_human: list[tuple[str, str]] = []
    vs_stored: list[tuple[str, str]] = []
    for _ticket_id, request_id, stored_ai, human in rows:
        trace = store.get_trace(session, str(request_id))
        if trace is None:
            continue  # the request_log row aged out — skip, don't guess
        verdict, _reasoning = judge_trace(llm, trace, model)
        if not verdict:
            continue  # NO_VERDICT: the judge said nothing — not a data point, skip
        vs_human.append((verdict, str(human)))
        vs_stored.append((verdict, str(stored_ai)))
    return RejudgeReport(n=len(vs_human), vs_human=_report(vs_human), vs_stored=_report(vs_stored))
