"""Skill-aware correctness for Definition-Drift findings.

The drift miner finds that one metric is computed N ways; it does NOT say
which way is *right*. The original report chose canon by vote — the value most variants
agree on, ties to the most-run reading. That is exactly wrong when the crowd is wrong:
three analysts summing revenue from a raw ``bronze.netvisor__invoices`` landing and one
from the governed ``gold.fct_revenue_monthly`` mart should NOT crown bronze just because
bronze is busier.

This module decides correctness using the SAME knowledge a lens carries in its skill
packs — the **medallion layering** (gold > silver > bronze) the ``medallion`` preset
encodes, and dimensional discipline. A deterministic **tier prior** runs first
(reproducible, offline, the 80% case): each variant's source tables are classified into
a medallion tier from name hints, and the highest-tier reading wins. An **LLM tiebreak**
(the org's fast model, handed the attached skill instructions as context) runs ONLY when
the prior cannot separate the candidates — same tier, no agreement majority. With no LLM
configured the prior stands and genuine ties fall back to the miner's most-run variant.

``annotate_correctness`` computes the choice once at mine time and stamps it onto the
finding (``canon_index``, ``canon_rationale``, per-variant ``tier``), so the report and
the dashboard read a governed verdict instead of re-deriving a vote.
"""

from __future__ import annotations

import json
import re

from services.contracts.protocols import CacheableBlock, LLMProvider, Message
from services.probe.drift import DriftFinding, DriftVariant

# ── medallion tier classification (the deterministic prior) ───────────────────

# Highest-trust first. A variant's tier is the LOWEST of its source tables' tiers — a
# join that pulls in a raw bronze landing is only as trustworthy as that landing.
TIER_RANK: dict[str, int] = {"gold": 3, "silver": 2, "unknown": 1, "bronze": 0}

# Layer keyword → tier. The same medallion vocabulary the ``medallion`` skill preset and
# the drift miner's structural-token list use, here ordered into the three layers plus
# the dimensional-modeling cues (fct_/dim_/mart) that mark a curated gold table even when
# the schema isn't literally named "gold".
_LAYER_BY_TOKEN: dict[str, str] = {
    # bronze — raw, as-ingested landings
    "bronze": "bronze", "raw": "bronze", "staging": "bronze", "stg": "bronze",
    "src": "bronze", "source": "bronze", "landing": "bronze", "ingest": "bronze",
    "ext": "bronze", "external": "bronze",
    # silver — cleaned, conformed, intermediate
    "silver": "silver", "clean": "silver", "cleaned": "silver", "conformed": "silver",
    "base": "silver", "int": "silver", "intermediate": "silver", "snap": "silver",
    "snapshot": "silver", "stage": "silver",
    # gold — business-ready marts, facts, dimensions
    "gold": "gold", "mart": "gold", "marts": "gold", "dwh": "gold", "core": "gold",
    "analytics": "gold", "reporting": "gold", "report": "gold", "rpt": "gold",
    "fct": "gold", "fact": "gold", "facts": "gold", "dim": "gold", "dimension": "gold",
    "dims": "gold", "agg": "gold", "metrics": "gold", "presentation": "gold",
    "curated": "gold",
}  # fmt: skip

_PART_RE = re.compile(r"[a-z0-9]+")


def classify_table(table: str) -> str:
    """The medallion tier of one (possibly qualified) table name.

    The FIRST token that maps to a layer wins — schema/database parts come first in a
    qualified name and carry the strongest layer signal (``gold.fct_revenue`` → gold,
    ``silver.dim_customers`` → silver), with dimensional cues (``fct_orders`` → gold)
    catching curated tables that don't carry an explicit layer schema. Unrecognized
    names are ``"unknown"`` — no claim either way.
    """
    for token in _PART_RE.findall(table.lower()):
        layer = _LAYER_BY_TOKEN.get(token)
        if layer is not None:
            return layer
    return "unknown"


def variant_tier(variant: DriftVariant) -> str:
    """A variant's tier: the LOWEST tier among its classified source tables.

    A reading that touches any raw bronze table is bronze, however much gold it also
    joins. When no table classifies, the variant is ``"unknown"`` (the prior abstains).
    """
    classified = [classify_table(t) for t in variant.source_tables]
    ranked = [t for t in classified if t != "unknown"]
    if not ranked:
        return "unknown"
    return min(ranked, key=lambda t: TIER_RANK[t])


# ── the agreement rule (restricted to a candidate subset) ─────────────────────


def _scalar(variant: DriftVariant) -> float | None:
    """The variant's single comparable number, when its result is exactly that."""
    rows = variant.observed_rows
    if rows and len(rows) == 1 and len(rows[0]) == 1:
        value = rows[0][0]
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return None


def _most_agreed(variants: list[DriftVariant], candidates: list[int]) -> tuple[int, bool]:
    """Among ``candidates``, the most-agreed reading and whether the choice is ambiguous.

    Mirrors the original ``report._canon_index`` agreement rule but over a subset: group
    by comparable value, the value most variants agree on wins (ties → more total runs),
    then the most-run variant in that group. ``ambiguous`` is True when two distinct
    value-groups tie on (agreement, runs) — i.e. the deterministic pick between them is
    arbitrary order, the case worth an LLM tiebreak.
    """
    groups: dict[float, list[int]] = {}
    for i in candidates:
        value = _scalar(variants[i])
        if value is not None:
            groups.setdefault(value, []).append(i)

    if not groups:  # no comparable numbers — most-run reading, ambiguous on a run tie
        best = max(candidates, key=lambda i: (variants[i].run_count, -i))
        top_runs = variants[best].run_count
        ambiguous = sum(1 for i in candidates if variants[i].run_count == top_runs) > 1
        return best, ambiguous

    def support(indices: list[int]) -> tuple[int, int]:
        return (len(indices), sum(variants[i].run_count for i in indices))

    ranked = sorted(groups.values(), key=support, reverse=True)
    winner_group = ranked[0]
    ambiguous = len(ranked) > 1 and support(ranked[1]) == support(winner_group)
    winner = max(winner_group, key=lambda i: (variants[i].run_count, -i))
    return winner, ambiguous


# ── the canon choice (tier prior + optional LLM tiebreak) ─────────────────────

_PICK_SYSTEM = (
    "You select the single CORRECT reading of a business metric from several SQL "
    "definitions that disagree. Judge trustworthiness by standard analytics "
    "conventions: medallion layering (gold/mart tables are business-ready and beat "
    "re-derivations from silver; bronze/raw/stg_ landings never answer business "
    "questions directly) and dimensional discipline (aggregate measures from the "
    "fact at its declared grain — a reading whose join fans out and inflates SUMs "
    "is wrong). "
    'Respond with strict JSON: {"canon": <index of the correct reading>, "reason": '
    '"<one short sentence>"}. Choose only from the candidate indices offered.'
)


def _candidate_brief(variants: list[DriftVariant], candidates: list[int]) -> str:
    lines = []
    for i in candidates:
        v = variants[i]
        value = _scalar(v)
        observed = f"{value:,.2f}" if value is not None else "n/a"
        tables = ", ".join(v.source_tables) or "(no table)"
        lines.append(
            f"[{i}] tier={v.tier} · tables={tables} · observed={observed} · "
            f"{v.distinguishing} ({v.run_count} runs)"
        )
    return "\n".join(lines)


def _llm_pick(
    finding: DriftFinding,
    candidates: list[int],
    llm: LLMProvider,
    model: str,
) -> tuple[int, str] | None:
    """Ask the model which candidate is correct. None on any malformed answer or an
    index outside the candidate set (caller falls back)."""
    prompt = (
        f"Metric: {finding.metric_intent}\n\n"
        f"Candidate readings (all at the same medallion tier):\n"
        f"{_candidate_brief(finding.variants, candidates)}"
    )
    res = llm.complete(
        system=[CacheableBlock(_PICK_SYSTEM)],
        messages=[Message("user", prompt)],
        model=model,
        temperature=0.0,
        # Reasoning-style fast models spend output budget thinking before emitting
        # content — 200 starved the pick to an empty string (same failure as _llm_name).
        max_tokens=1000,
    )
    try:
        parsed = json.loads(res.text.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    raw = parsed.get("canon")
    if not isinstance(raw, int | str):
        return None
    try:
        index = int(raw)
    except ValueError:
        return None
    if index not in candidates:
        return None
    reason = str(parsed.get("reason") or "").strip()
    return index, reason or "Selected by the org's analytics conventions."


def _deterministic_rationale(finding: DriftFinding, canon_index: int) -> str:
    """Explain the tier-prior choice in one sentence — naming the layer and what lost."""
    canon = finding.variants[canon_index]
    tier = canon.tier or "unknown"
    tables = ", ".join(canon.source_tables) or "the chosen reading"
    lower = [v for i, v in enumerate(finding.variants) if i != canon_index]
    bronze = sum(1 for v in lower if v.tier == "bronze")

    if tier == "gold":
        if bronze:
            n = f"{bronze} competing reading{'s' if bronze != 1 else ''}"
            return (
                f"Reads from the gold layer ({tables}), the business-ready source; "
                f"{n} derive the metric from raw bronze tables, which the medallion "
                "convention treats as not business-ready. Prefer the curated gold "
                "source so answers match governed definitions."
            )
        return (
            f"Reads from the gold layer ({tables}), the business-ready curated source; "
            "the other readings sit lower in the medallion stack."
        )
    if tier == "silver":
        return (
            f"No gold table computes this metric — the silver (cleaned, conformed) "
            f"reading over {tables} is the most trustworthy source available; promoting "
            "it into a governed gold definition is the durable fix."
        )
    if tier == "bronze":
        return (
            "Every reading derives from raw bronze tables; canon is the most-run "
            f"reading ({canon.run_count} runs), but this metric needs a governed gold "
            "definition — none exists yet."
        )
    return (
        "No medallion layer is evident from the table names; canon is the most-agreed "
        f"reading ({canon.run_count} runs)."
    )


def choose_canon(
    finding: DriftFinding,
    *,
    llm: LLMProvider | None = None,
    model: str = "",
) -> tuple[int, str]:
    """The index of the correct reading + a one-line rationale.

    Tier prior first: keep only the highest-medallion-tier variants, then settle them by
    the agreement rule. The LLM is consulted ONLY when that leaves a genuine tie and a
    provider is configured; otherwise the deterministic choice and rationale stand.
    """
    variants = finding.variants
    ranks = [TIER_RANK[v.tier or "unknown"] for v in variants]
    top = max(ranks)
    candidates = [i for i, r in enumerate(ranks) if r == top]

    if len(candidates) == 1:
        idx = candidates[0]
        return idx, _deterministic_rationale(finding, idx)

    winner, ambiguous = _most_agreed(variants, candidates)
    if ambiguous and llm is not None:
        picked = _llm_pick(finding, candidates, llm, model)
        if picked is not None:
            return picked
    return winner, _deterministic_rationale(finding, winner)


def annotate_correctness(
    findings: list[DriftFinding],
    *,
    llm: LLMProvider | None = None,
    model: str = "",
) -> list[DriftFinding]:
    """Stamp every variant with its medallion ``tier`` and every CONFLICT finding with the
    convention-aware ``canon_index`` + ``canon_rationale``. Pure: returns new findings,
    inputs untouched. Duplications carry tiers (for coloring) but no canon — their
    readings are equivalent, so there is nothing to choose between.
    """
    out: list[DriftFinding] = []
    for finding in findings:
        variants = [v.model_copy(update={"tier": variant_tier(v)}) for v in finding.variants]
        annotated = finding.model_copy(update={"variants": variants})
        if annotated.severity == "conflict":
            idx, rationale = choose_canon(annotated, llm=llm, model=model)
            annotated = annotated.model_copy(
                update={"canon_index": idx, "canon_rationale": rationale}
            )
        out.append(annotated)
    return out
