"""Adversarial reviewer over a composed answer.

Distinct from the rubric judge (`reviews/judge.py`): instead of scoring the chain
against a rubric, it challenges the answer's *assumptions* — did "active/churned/
qualified" use the right definition? were internal/test accounts excluded? is the
grain/time window what the question implied? is there a more defensible reading?
Opt-in per lens; the raised challenges fold into the verification report
(`verification.fold_challenges`), where an unresolved blocking challenge caps the
grade — a raised challenge means "not verified", never silence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from services.contracts.protocols import CacheableBlock, LLMProvider, Message
from services.contracts.semantic_model import SemanticModel

Severity = Literal["blocking", "note"]

_SYSTEM = (
    "You are an adversarial data reviewer. Your job is NOT to score the answer — it is "
    "to challenge its assumptions. Given a question, the SQL that ran, and the answer, "
    "look for the wrong-but-plausible failure modes:\n"
    "1. Definition choice — does a term like active/churned/qualified use the lens's "
    "stated definition, or a plausible-but-different one?\n"
    "2. Exclusions — were internal/test/demo accounts or cancelled rows excluded when "
    "the question implies real business activity?\n"
    "3. Grain and time window — does the SQL aggregate at the grain and over the window "
    "the question implied?\n"
    "4. Alternative readings — is there a more defensible interpretation of the question "
    "that would change the result?\n"
    "Raise a challenge ONLY when you can name the specific assumption at risk; an empty "
    "list is the correct response to a defensible answer. Respond with strict JSON: "
    '{"challenges": [{"assumption": "<the assumption at risk>", '
    '"severity": "blocking" | "note", "reason": "<one or two sentences>"}]}. '
    "Use 'blocking' only when the assumption, if wrong, changes the answer materially; "
    "'note' for caveats worth surfacing."
)


@dataclass
class Challenge:
    """One challenged assumption. `blocking` caps the verification grade; `note`
    is surfaced but never demotes."""

    assumption: str
    severity: Severity
    reason: str


def _definitions_block(semantic_model: SemanticModel) -> str:
    lines = [f"- {d.term}: {d.body}" for d in semantic_model.definitions]
    return "\n".join(lines) if lines else "(none)"


def challenge(
    llm: LLMProvider,
    *,
    question: str,
    sql: str,
    answer: str,
    semantic_model: SemanticModel,
    model: str = "claude-sonnet-4-6",
) -> list[Challenge]:
    """One adversarial round-trip → the list of challenged assumptions.

    Fails open to a non-demoting `note` on unparseable output: a broken reviewer
    response carries no signal about the answer, so it must be visible without
    vetoing it (the judge's parse-failure escalation covers the rubric tier).
    """
    user = (
        f"Question: {question}\n"
        f"Lens definitions:\n{_definitions_block(semantic_model)}\n"
        f"SQL: {sql}\n"
        f"Answer: {answer}"
    )
    res = llm.complete(
        system=[CacheableBlock(_SYSTEM)],
        messages=[Message("user", user)],
        model=model,
        temperature=0.0,
        max_tokens=600,
    )
    try:
        data = json.loads(res.text.strip())
        raw = data.get("challenges", [])
        if not isinstance(raw, list):
            raise ValueError("challenges is not a list")
    except (json.JSONDecodeError, AttributeError, ValueError):
        return [
            Challenge(
                assumption="adversary_output",
                severity="note",
                reason="Adversary response could not be parsed; no challenges extracted.",
            )
        ]
    out: list[Challenge] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        assumption = str(item.get("assumption", "")).strip()
        reason = str(item.get("reason", "")).strip()
        severity = str(item.get("severity", "")).strip().lower()
        if not assumption and not reason:
            continue
        out.append(
            Challenge(
                assumption=assumption or "(unnamed assumption)",
                # An unrecognized severity is surfaced, not promoted to a veto.
                severity="blocking" if severity == "blocking" else "note",
                reason=reason,
            )
        )
    return out
