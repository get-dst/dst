"""Grade a lane's answer against the oracle fact.

The lane returns tabular data (columns + rows). Grading is deliberately strict
about *values* and lenient about *shape*: a correct scalar may arrive as a 1x1
result or as the only numeric cell of a single row. EUR facts are graded to the
cent; ratios to 1e-3 (the oracle rounds to 3 decimals); counts exactly.

A ``top`` fact (``{name, value_eur}``) requires the name to appear in the row
and the value to match — name-only matches are wrong answers that *look* right,
which is exactly the failure mode the benchmark exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Grade:
    correct: bool
    reason: str


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(Decimal(value.replace(" ", "").replace(" ", "").replace(",", ".")))
        except InvalidOperation:
            return None
    return None


def _numbers_in(rows: list[list[object]]) -> list[float]:
    out: list[float] = []
    for row in rows:
        for cell in row:
            n = _as_number(cell)
            if n is not None:
                out.append(n)
    return out


def _close(got: float, want: float, *, tol: float) -> bool:
    return abs(got - want) <= max(tol, abs(want) * 1e-9)


def answer_signature(rows: list[list[object]], *, max_rows: int = 20) -> str:
    """A normalized fingerprint of an answer — the unit of the agreement metric.

    Deliberately ignores column NAMES and row ORDER (``SELECT n`` and
    ``SELECT total`` returning 150.5 are the same answer) and rounds numbers to
    6 significant digits. Consistency is a claim about the number delivered, not
    about the label on it — and it is measured over right AND wrong answers,
    because an arm that is reliably wrong is a different problem from an arm
    that is unpredictable.
    """
    if not rows:
        return "∅"

    def cell(value: object) -> str:
        n = _as_number(value)
        return f"{n:.6g}" if n is not None else str(value).strip().lower()

    body = sorted(" | ".join(cell(c) for c in row) for row in rows[:max_rows])
    return f"{len(rows)}r::" + " ;; ".join(body)


def grade_absent(rows: list[list[object]], error: str | None) -> Grade:
    """For questions whose data does not exist, declining IS the correct
    answer. Any confident number is a hallucination by construction. (v1
    leniency, documented: we accept any no-result outcome — including a
    failed query — as a decline; distinguishing principled refusal from
    accidental failure needs answer-text grading.)"""
    if not rows or not _numbers_in(rows):
        return Grade(True, f"declined / no data — correct ({error or 'no rows'})")
    return Grade(False, f"hallucinated a number for absent data: {_numbers_in(rows)[:3]}")


def grade(kind: str, expected: object, columns: list[str], rows: list[list[object]]) -> Grade:
    if not rows:
        return Grade(False, "empty result")

    if kind in ("scalar", "ratio", "count"):
        assert isinstance(expected, (int, float)), f"oracle fact for {kind} must be numeric"
        tol = {"scalar": 0.02, "ratio": 1e-3, "count": 0.0}[kind]
        if len(rows) != 1:
            return Grade(False, f"expected a single-row answer, got {len(rows)} rows")
        numbers = _numbers_in(rows)
        if not numbers:
            return Grade(False, "single row but no numeric cell (NULL result — bad filter?)")
        if len(numbers) == 1:
            got = numbers[0]
            return (
                Grade(True, f"{got} ≈ {expected}")
                if _close(got, float(expected), tol=tol)
                else Grade(False, f"got {got}, expected {expected}")
            )
        # One row, several numeric cells (e.g. SELECT year, total): accept if
        # exactly one cell matches — ambiguity beyond that is a wrong shape.
        hits = [n for n in numbers if _close(n, float(expected), tol=tol)]
        if len(hits) == 1:
            return Grade(True, f"{hits[0]} ≈ {expected} (one matching cell)")
        return Grade(False, f"no single matching cell in {numbers}, expected {expected}")

    if kind == "top":
        assert isinstance(expected, dict) and "name" in expected and "value_eur" in expected
        if len(rows) != 1:
            return Grade(False, f"expected one top row, got {len(rows)}")
        row = rows[0]
        # Names may span columns (first_name, last_name) — match on the joined text.
        joined = " ".join(c for c in row if isinstance(c, str))
        name_ok = expected["name"] in joined
        value_ok = any(
            _close(n, float(expected["value_eur"]), tol=0.02) for n in _numbers_in([row])
        )
        if name_ok and value_ok:
            return Grade(True, f"{expected['name']} / {expected['value_eur']}")
        if name_ok:
            return Grade(False, f"right name, wrong value (expected {expected['value_eur']})")
        return Grade(False, f"expected {expected['name']}, got {row!r}")

    raise ValueError(f"unknown question kind: {kind}")


# ---------------------------------------------------------------------------
# Stage attribution — a wrong answer names the stage that broke.
# ---------------------------------------------------------------------------

# Pipeline order — a case is tagged with the FIRST stage that failed.
# `determinism` is deliberately absent: it is a property of repeated runs, not
# of one case, and is reported as per-stage agreement (runner.prose_agreement).
STAGES = ("routing", "rows", "grounding")

# Default owners per stage, for the summary (product | authoring | consumer).
# A stage tag without an owner is a number nobody acts on.
STAGE_OWNERS = {
    "routing": "product: thresholds | authoring: use_when",
    "rows": "product: generation | authoring: layer",
    "grounding": "product: composer",
    "unattributed": "triage by hand",
}


def stage_statuses(
    *,
    rows_correct: bool,
    delivered: bool,
    grounding: str | None,
    expected_lens: str | None = None,
    served_lens: str | None = None,
) -> dict[str, str]:
    """Each stage's verdict for one case: ``passed | failed | skipped``.

    ``routing`` grades only when the eval case is labeled AND the lane reports
    the lens it routed to — a lens-pinned run records ``skipped``, never
    ``passed``, because an untested stage must not report clean.
    ``rows`` is the existing oracle grade. ``grounding`` is production's own
    deterministic numeric_grounding verdict carried on the answer (re-running
    faithfulness harness-side without the pipeline's definitions/notes/clock
    would manufacture exactly the false positives that check was tuned to avoid).
    """
    if expected_lens is not None and served_lens is not None:
        routing = "passed" if served_lens == expected_lens else "failed"
    else:
        routing = "skipped"
    rows = ("passed" if rows_correct else "failed") if delivered else "skipped"
    grounds = {"pass": "passed", "fail": "failed", "skip": "skipped"}.get(grounding or "")
    return {"routing": routing, "rows": rows, "grounding": grounds or "skipped"}


def first_failed_stage(stages: dict[str, str], outcome: str) -> str | None:
    """The stage tag for one case: first failed stage in pipeline order.

    ``None`` when the case is not wrong (correct, or declined — a refusal is an
    outcome, not an error). A wrong case no stage explains reports
    ``unattributed`` and gets COUNTED — never ``None``, never dropped: an
    attribution layer that quietly skips hard cases launders the exact
    confusion it exists to kill.
    """
    if outcome != "wrong":
        return None
    for stage in STAGES:
        if stages.get(stage) == "failed":
            return stage
    return "unattributed"


def prose_signature(text: str | None) -> str | None:
    """Fingerprint of the CLAIMS in delivered prose — the unit of composer
    determinism. Two narrations of the same rows agree when they state the same
    numbers (6 significant digits, order-free); wording may vary freely. ``None``
    when the lane delivered no prose. Same-rows runs with differing prose
    signatures are the "same SQL, different responses" defect, attributed to
    the composer. Extraction reuses faithfulness's battle-tested scanner —
    thousands separators, magnitudes ("6.66M"), and the date/token guards; a
    naive regex here would read "1,500" as 1.5 and flag phantom flips."""
    if text is None:
        return None
    from services.runtime.faithfulness import _numbers  # one scanner, two callers

    numbers = sorted(f"{n:.6g}" for n in _numbers(text))
    return " ;; ".join(numbers) if numbers else "∅"
