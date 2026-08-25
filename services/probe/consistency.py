"""Ground-truth-free wrongness via consistency — and its validation.

A cold-start customer hands us no answer key. But we can still detect wrongness:
ask the same question K ways and if the answers disagree, at least one is wrong —
no oracle required. ``contradictory`` = the K answers diverge; ``consistent`` =
they agree (but may be *consistently* wrong — the known blind spot).

The decisive question this module measures: **does consistency-disagreement
predict real wrongness?** On a world where we DO have ground truth (the proving ground) we
cross every answer's consistency signal against its actual correctness and read
the 2×2 — catch-rate, false-alarm-rate, and the dangerous ``consistent & wrong``
blind spot. If contradictory ≫ predicts wrong, the cold-start Audit is real.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_REL_TOL = 1e-3


def _close(a: float, b: float, *, rel_tol: float = _REL_TOL) -> bool:
    return abs(a - b) <= max(abs(b) * rel_tol, 0.5)


def is_consistent(numbers: list[float | None], *, rel_tol: float = _REL_TOL) -> bool | None:
    """Do the answers that came back agree? None if fewer than 2 are assessable."""
    vals = [n for n in numbers if n is not None]
    if len(vals) < 2:
        return None
    return all(_close(v, vals[0], rel_tol=rel_tol) for v in vals)


@dataclass
class ConsistencyRow:
    question: str
    numbers: list[float | None]  # one per phrasing, None on error
    consistent: bool | None  # None = couldn't assess
    actual_correct: bool  # mode-2 truth: the natural phrasing's answer matched


@dataclass
class ConsistencyReport:
    rows: list[ConsistencyRow] = field(default_factory=list)

    @property
    def assessable(self) -> list[ConsistencyRow]:
        return [r for r in self.rows if r.consistent is not None]

    def cell(self, *, consistent: bool, correct: bool) -> int:
        return sum(
            1 for r in self.assessable if r.consistent is consistent and r.actual_correct is correct
        )

    @property
    def consistent_correct(self) -> int:
        return self.cell(consistent=True, correct=True)

    @property
    def consistent_wrong(self) -> int:  # the blind spot — consistently wrong
        return self.cell(consistent=True, correct=False)

    @property
    def contradictory_wrong(self) -> int:  # caught without an oracle
        return self.cell(consistent=False, correct=False)

    @property
    def contradictory_correct(self) -> int:  # false alarm
        return self.cell(consistent=False, correct=True)

    def _rate(self, num: int, den: int) -> float | None:
        return num / den if den else None

    @property
    def p_wrong_given_contradictory(self) -> float | None:
        flagged = self.contradictory_wrong + self.contradictory_correct
        return self._rate(self.contradictory_wrong, flagged)

    @property
    def p_wrong_given_consistent(self) -> float | None:
        passed = self.consistent_correct + self.consistent_wrong
        return self._rate(self.consistent_wrong, passed)

    @property
    def catch_rate(self) -> float | None:
        """Of the actually-wrong answers, how many did contradiction flag? (recall)"""
        wrong = self.contradictory_wrong + self.consistent_wrong
        return self._rate(self.contradictory_wrong, wrong)

    @property
    def phi(self) -> float | None:
        """Phi coefficient of (contradictory) vs (wrong) — the predictive strength."""
        a = self.contradictory_wrong
        b = self.contradictory_correct
        c = self.consistent_wrong
        d = self.consistent_correct
        denom = (a + b) * (c + d) * (a + c) * (b + d)
        if denom == 0:
            return None
        return float((a * d - b * c) / denom**0.5)

    def render(self) -> str:
        n = len(self.assessable)
        pc = self.p_wrong_given_contradictory
        pk = self.p_wrong_given_consistent
        out = [
            f"CONSISTENCY ↔ TRUTH  (n={n} assessable of {len(self.rows)})",
            "",
            "                    actually WRONG   actually CORRECT",
            f"  contradictory  →     {self.contradictory_wrong:>3} (caught)        "
            f"{self.contradictory_correct:>3} (false alarm)",
            f"  consistent     →     {self.consistent_wrong:>3} (BLIND SPOT)    "
            f"{self.consistent_correct:>3} (clean pass)",
            "",
            f"  P(wrong | contradictory) = {f'{pc:.0%}' if pc is not None else '—'}   "
            "← a flag should mean wrong",
            f"  P(wrong | consistent)    = {f'{pk:.0%}' if pk is not None else '—'}   "
            "← the residual cold-start risk",
            f"  catch-rate (recall)      = "
            f"{f'{self.catch_rate:.0%}' if self.catch_rate is not None else '—'}   "
            "← of real errors, how many flagged",
            f"  phi (contradictory↔wrong) = {f'{self.phi:+.2f}' if self.phi is not None else '—'}",
        ]
        return "\n".join(out)
