"""Calibration of the deciders — the regime must be measured, never asserted.

A decider's probability is worth exactly as much as its calibration: p = 0.8
should be right about 80% of the time. This module scores a set of graded
decisions per decider — accuracy, Brier score, expected calibration error over
equal-width bins, and the coverage curve (at threshold t: what share of
decisions would act, and how often those are wrong) the threshold policy is
tuned against.

Honesty rules (rule 4, rule 8):
- a decider whose decisions carry no probability reports UNTESTED, never a
  number — nothing was measured;
- a cell with fewer than ``MIN_N`` decisions prints ``n too small`` instead of
  a figure that would be read as one;
- an empty set is reported as UNTESTED, never as clean.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MIN_N = 20
BINS = 10
THRESHOLDS = (0.6, 0.7, 0.8, 0.9, 0.95)


@dataclass(frozen=True)
class GradedDecision:
    decider: str  # provider id, e.g. "vote:deepseek-v4-flash:k=5"
    slot: str  # lens | certified_equivalent | metric
    chosen: str | None
    gold: str | None
    p: float | None  # p(chosen); None = nothing measured
    margin: float | None = None

    @property
    def correct(self) -> bool:
        return self.chosen == self.gold


def brier(records: list[GradedDecision]) -> float | None:
    """Mean squared error of p(chosen) against correctness (0 = perfect)."""
    measured = [r for r in records if r.p is not None]
    if not measured:
        return None
    return sum(((r.p or 0.0) - (1.0 if r.correct else 0.0)) ** 2 for r in measured) / len(measured)


def bins(records: list[GradedDecision], n_bins: int = BINS) -> list[dict[str, float | int]]:
    """Equal-width bins over p(chosen): n, mean confidence, accuracy per bin."""
    measured = [r for r in records if r.p is not None]
    out: list[dict[str, float | int]] = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        cell = [
            r
            for r in measured
            if (lo <= (r.p or 0.0) < hi) or (i == n_bins - 1 and (r.p or 0.0) == 1.0)
        ]
        if not cell:
            continue
        out.append(
            {
                "lo": lo,
                "hi": hi,
                "n": len(cell),
                "conf": sum(r.p or 0.0 for r in cell) / len(cell),
                "acc": sum(1 for r in cell if r.correct) / len(cell),
            }
        )
    return out


def ece(records: list[GradedDecision], n_bins: int = BINS) -> float | None:
    """Expected calibration error: bin-weighted |confidence − accuracy|."""
    measured = [r for r in records if r.p is not None]
    if not measured:
        return None
    total = len(measured)
    return sum(
        (int(b["n"]) / total) * abs(float(b["conf"]) - float(b["acc"]))
        for b in bins(records, n_bins)
    )


def coverage_curve(
    records: list[GradedDecision], thresholds: tuple[float, ...] = THRESHOLDS
) -> list[dict[str, float]]:
    """At each act threshold: share of decisions that would act, and the error
    rate among them — what a policy's ``act_p`` buys and costs."""
    measured = [r for r in records if r.p is not None]
    out: list[dict[str, float]] = []
    for t in thresholds:
        acted = [r for r in measured if (r.p or 0.0) >= t]
        out.append(
            {
                "threshold": t,
                "coverage": len(acted) / len(measured) if measured else 0.0,
                "error_among_acted": (
                    sum(1 for r in acted if not r.correct) / len(acted) if acted else 0.0
                ),
            }
        )
    return out


def report(records: list[GradedDecision]) -> dict[str, dict[str, Any]]:
    """Per decider: n, accuracy, brier, ece, uncalibrated_n, bins, curve — or
    ``{"status": "UNTESTED", ...}`` when nothing measured."""
    by: dict[str, list[GradedDecision]] = {}
    for r in records:
        # One row per decider AND slot: the same voting model routing lenses
        # and gating paraphrases are two different calibrations.
        by.setdefault(f"{r.decider} [{r.slot}]", []).append(r)
    out: dict[str, dict[str, Any]] = {}
    for decider, rs in sorted(by.items()):
        measured = [r for r in rs if r.p is not None]
        entry: dict[str, Any] = {
            "n": len(rs),
            "accuracy": sum(1 for r in rs if r.correct) / len(rs),
            "uncalibrated_n": len(rs) - len(measured),
        }
        if not measured:
            entry["status"] = "UNTESTED"
            entry["reason"] = "no probabilities measured"
        elif len(measured) < MIN_N:
            entry["status"] = "n too small"
            entry["reason"] = f"{len(measured)} measured decisions, need {MIN_N}"
        else:
            entry["status"] = "measured"
            entry["brier"] = brier(rs)
            entry["ece"] = ece(rs)
            entry["bins"] = bins(rs)
            entry["curve"] = coverage_curve(rs)
        out[decider] = entry
    return out


def format_report(rep: dict[str, dict[str, Any]]) -> str:
    """Terraform-idiom table under the test summary; one line per decider."""
    if not rep:
        return "calibration: UNTESTED — no decisions recorded"
    lines = ["calibration (per decider):"]
    for decider, e in rep.items():
        head = f"  {decider:<48} n={e['n']:<4} acc={e['accuracy']:.0%}"
        if e["status"] != "measured":
            lines.append(f"{head}  {e['status']} ({e['reason']})")
            continue
        lines.append(f"{head}  brier={e['brier']:.3f} ece={e['ece']:.3f}")
        for c in e["curve"]:
            lines.append(
                f"      act at p>={c['threshold']:.2f}: coverage {c['coverage']:.0%}, "
                f"error among acted {c['error_among_acted']:.0%}"
            )
    return "\n".join(lines)
