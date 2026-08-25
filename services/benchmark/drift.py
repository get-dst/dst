"""Experiment B — metric drift: how many different answers does one question get?

Metric drift is widely claimed as a problem and rarely measured directly. The
design is deliberately small enough to run and large enough to mean something:

    one question × S sessions × C callers × K consumer stacks → S·C·K answers,
    counted as DISTINCT answers.

A governed lens should return 1. A memo arm returns k > 1: each session holds
its own copy of the file, each caller pulls the phrasing a different way, and
each consumer stack reads the same prose differently. A raw agent returns m > k.

The design point people usually miss: **vary the consumer agent, not just the
seed.** Heterogeneous stacks are the actual production condition — different
teams point different agents at the same warehouse — and a same-model,
same-prompt reseeding measures sampling noise instead. ``stacks`` is a mapping
of stack name to a lane factory, and each factory is called FRESH per session so
a mutable memo cannot leak between sessions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from .grading import answer_signature
from .lanes import LaneAnswer
from .questions import Question
from .runner import Lane


@dataclass(frozen=True)
class DriftRun:
    arm: str
    stack: str
    session: int
    caller: str | None
    signature: str
    error: str | None
    ai_cost_usd: float
    tokens: int


@dataclass(frozen=True)
class DriftCell:
    arm: str
    runs: int
    distinct: int
    signatures: dict[str, int]  # signature -> how many runs landed on it

    @property
    def modal_share(self) -> float:
        return max(self.signatures.values()) / self.runs if self.runs else 0.0


def run_drift(
    arms: dict[str, dict[str, Callable[[], Lane]]],
    question: Question,
    *,
    callers: list[str | None],
    sessions: int = 5,
) -> list[DriftRun]:
    """``arms`` is {arm name: {stack name: fresh-lane factory}}.

    Every (arm, stack, session, caller) cell builds a NEW lane — an independent
    session, with its own memo where the arm has one.
    """
    out: list[DriftRun] = []
    for arm, stacks in arms.items():
        for stack, factory in stacks.items():
            for session in range(sessions):
                for caller in callers:
                    lane = factory()
                    asked = replace(question, caller=caller)
                    try:
                        answer = lane.answer(asked)
                    except Exception as exc:  # noqa: BLE001 — one dead cell, not a dead run
                        answer = LaneAnswer(
                            columns=[], rows=[], sql=None, error=f"lane crashed: {exc}"
                        )
                    out.append(
                        DriftRun(
                            arm=arm,
                            stack=stack,
                            session=session,
                            caller=caller,
                            signature=answer_signature(answer.rows),
                            error=answer.error,
                            ai_cost_usd=answer.ai_cost_usd,
                            tokens=answer.tokens,
                        )
                    )
    return out


def summarize_drift(runs: list[DriftRun]) -> list[DriftCell]:
    cells: list[DriftCell] = []
    for arm in dict.fromkeys(r.arm for r in runs):
        mine = [r for r in runs if r.arm == arm]
        counts: dict[str, int] = {}
        for r in mine:
            counts[r.signature] = counts.get(r.signature, 0) + 1
        cells.append(DriftCell(arm=arm, runs=len(mine), distinct=len(counts), signatures=counts))
    return cells


def render_drift(question: Question, runs: list[DriftRun]) -> str:
    stacks = sorted({r.stack for r in runs})
    callers = sorted({r.caller or "—" for r in runs})
    sessions = len({r.session for r in runs})
    lines = [
        "# Metric drift — distinct answers to one question",
        "",
        f"Question: {question.question}",
        "",
        f"{len(stacks)} consumer stack(s) × {sessions} session(s) × {len(callers)} caller(s) "
        f"= {len(runs) // max(1, len({r.arm for r in runs}))} answers per arm.",
        "Consumer stacks vary, not just the seed — heterogeneous stacks are the "
        "production condition, and reseeding one model measures sampling noise instead.",
        "",
        "| Arm | Answers | Distinct ↓ | Modal share ↑ | ¢ total |",
        "|---|---|---|---|---|",
    ]
    for cell in summarize_drift(runs):
        cents = 100 * sum(r.ai_cost_usd for r in runs if r.arm == cell.arm)
        lines.append(
            f"| {cell.arm} | {cell.runs} | {cell.distinct} | "
            f"{cell.modal_share:.0%} | {cents:.2f}¢ |"
        )
    lines += ["", "## The distinct answers", ""]
    for cell in summarize_drift(runs):
        lines.append(f"**{cell.arm}**")
        for sig, n in sorted(cell.signatures.items(), key=lambda kv: -kv[1]):
            lines.append(f"- ×{n}  `{sig[:160]}`")
        lines.append("")
    return "\n".join(lines) + "\n"
