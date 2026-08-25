"""LLM-as-judge over a reasoning trace.

Audits the chain (question -> definition -> SQL -> answer), not just the conclusion.
Returns a verdict (approve | changes | reject) and a short rationale — or
``NO_VERDICT`` when the model said nothing at all, which is an error, not a ruling.
"""

from __future__ import annotations

import json
import logging
import re

from services.contracts.protocols import CacheableBlock, LLMProvider, Message
from services.reviews.store import Trace

log = logging.getLogger("dst")

_SYSTEM = (
    "You are a senior data reviewer auditing an AI analyst's answer. Audit the whole chain "
    "(question → definition → SQL → answer), not just the conclusion, against this rubric:\n"
    "1. SQL fidelity — does the SQL answer the question and apply the stated definition?\n"
    "2. Groundedness — does the answer follow from the SQL's result, with no invented "
    "numbers? A number explicitly attributed to a declared source ('per the table "
    "profile', 'per the <term> definition') is grounded, not invented.\n"
    "3. Scope — does it stay within the question (no overreach)?\n"
    "If an APPROVED REFERENCE query is given, the SQL should be equivalent to it; material "
    "divergence is a problem. Output ONLY a JSON object, no prose and no markdown fences: "
    '{"verdict": "approve" | "changes" | "reject", "reasoning": "<one or two sentences naming '
    'the concrete issue and fix>"}. '
    "Use 'approve' only if every rubric point passes; 'changes' for fixable issues; 'reject' if "
    "the answer is unsupported or wrong."
)

_VALID = {"approve", "changes", "reject"}

NO_VERDICT = ""
"""Returned in place of a verdict when the judge model replied with NOTHING.

An empty reply is an infrastructure failure — a dropped call, an exhausted quota,
a refusal that never rendered — and converting it into a governance ruling
(``verdict="changes"`` with the reasoning "(empty judge response)") makes a
provider outage read as the judge finding fault with every answer it touched.

Callers must treat it as UNRULED, never as a decision: ``needs_human`` on a
ticket, ``errored`` on an eval case, skipped in judge calibration. Falsy so the
``verdict == "approve"`` tests everywhere can never pass on it by accident."""


def _extract_json(text: str) -> dict[str, object] | None:
    """Tolerant JSON parse: a small/fast judge model often wraps its object in
    ```fences``` or trailing prose. Strip fences, then fall back to the first
    {...} block — the same resilience the rest of the codebase already applies."""
    t = re.sub(r"^```[a-zA-Z]*", "", text.strip()).strip()
    t = re.sub(r"```$", "", t).strip()
    block = re.search(r"\{.*\}", t, re.DOTALL)
    for candidate in (t, block.group(0) if block else None):
        if candidate is None:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def judge_trace(
    llm: LLMProvider,
    trace: Trace,
    model: str = "claude-sonnet-4-6",
    *,
    reference_sql: str | None = None,
) -> tuple[str, str]:
    reference = f"\nApproved reference SQL: {reference_sql}" if reference_sql else ""
    user = (
        f"Question: {trace.question}\n"
        f"Definition applied: {trace.definition_used or '(none)'}\n"
        f"SQL: {trace.sql or '(none)'}\n"
        f"Rows returned: {trace.row_count if trace.row_count is not None else '(unknown)'}\n"
        f"Answer: {trace.answer or '(none)'}\n"
        f"Stated confidence: {trace.confidence or '(none)'}"
        f"{reference}"
    )
    res = llm.complete(
        system=[CacheableBlock(_SYSTEM)],
        messages=[Message("user", user)],
        model=model,
        temperature=0.0,
        max_tokens=400,
    )
    raw = (res.text or "").strip()
    if not raw:
        # Nothing came back. There is no reasoning to surface and nothing to
        # parse — anything we returned here would be a verdict we invented.
        log.error(
            "review judge returned an EMPTY response (model=%s, request=%s, lens=%s) — "
            "leaving the trace UNRULED; check the judge provider's credentials and quota",
            model,
            trace.request_id,
            trace.lens,
        )
        return NO_VERDICT, "Judge returned an empty response — unruled, needs a human."
    data = _extract_json(raw)
    if data is None:
        # Unparseable but not empty — surface what the judge actually said so the
        # human reviewer can act on its reasoning instead of a dead-end placeholder.
        snippet = raw if len(raw) <= 600 else raw[:600] + "…"
        return "changes", f"Judge did not return valid JSON — its raw response:\n{snippet}"
    verdict = str(data.get("verdict", "")).lower()
    reasoning = str(data.get("reasoning", "")).strip()
    if verdict not in _VALID:
        return "changes", reasoning or "Unrecognized verdict; escalating."
    return verdict, reasoning
