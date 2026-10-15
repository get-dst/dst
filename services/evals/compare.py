"""Run-over-run compare for ``dst test --compare-last``.

Two runs of the same suite are comparable only when they measured the same
lens: the run carries the published bundle's fingerprint
(``lenses.store.bundle_fingerprint`` in ``eval_run.lens_version``), and a
verdict that differs between two runs with the same fingerprint is a flip of
the machinery — the decider, the generator — not of the authoring. When the
fingerprints differ, the bundle changed between the runs and no flip is
claimed; when the previous run carries no fingerprint, nothing is known and
nothing is claimed either (``lens_fingerprint_changed`` is None).

Cases join on their id (``certified:<id>``, ``slots:<id>``), so a certified
answer graded by the slot lane in one run and executed by the rows lane in
the other is two measurements, never a flip.
"""

from __future__ import annotations

from typing import Any

from services.contracts.eval import EvalResult
from services.evals.store import EvalResultRow, EvalRunRow


def compare_runs(
    previous: EvalRunRow | None,
    previous_results: list[EvalResultRow],
    fingerprint: str,
    results: list[EvalResult],
    score: float,
) -> dict[str, Any]:
    """The ``compare_last`` entry for one lens — see the module doc."""
    out: dict[str, Any] = {
        "previous_run": previous.id if previous else None,
        "previous_score": previous.score if previous else None,
        "score": score,
        "lens_fingerprint": fingerprint,
        "lens_fingerprint_changed": None,
        "flips": [],
    }
    if previous is None:
        return out
    if previous.lens_version is None:
        return out  # recorded before fingerprints: same bundle or not, unknown
    out["lens_fingerprint_changed"] = previous.lens_version != fingerprint
    if out["lens_fingerprint_changed"]:
        return out
    before = {r.case_id: r for r in previous_results}
    out["flips"] = [
        {
            "case_id": r.case_id,
            "question": r.question,
            "from": "pass" if before[r.case_id].passed else "fail",
            "to": "pass" if r.passed else "fail",
        }
        for r in results
        if r.case_id in before and before[r.case_id].passed != r.passed
    ]
    return out


def describe(cmp: dict[str, Any], label: str) -> list[str]:
    """The human lines for one lens's compare."""
    if cmp["previous_run"] is None:
        return [f"compare {label}: no previous test run"]
    prev, score = float(cmp["previous_score"] or 0.0), float(cmp["score"])
    word = "regressed" if score < prev else ("improved" if score > prev else "unchanged")
    head = f"compare {label}: {prev:.0%} → {score:.0%} vs previous run ({word})"
    changed = cmp["lens_fingerprint_changed"]
    if changed is None:
        return [f"{head}; the previous run carries no lens fingerprint — flips not compared"]
    if changed:
        return [f"{head}; the lens changed since the previous run — flips not compared"]
    lines = [f"{head}; same lens, {len(cmp['flips'])} verdict flip(s)"]
    lines += [f"  flip: {f['question']} {f['from']} → {f['to']}" for f in cmp["flips"]]
    return lines
