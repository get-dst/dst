"""LLM-as-judge over a reasoning trace.

Audits the chain (question -> definition -> SQL -> answer), not just the conclusion.
Returns a verdict (approve | changes | reject) and a short rationale — or
``NO_VERDICT`` when the model said nothing at all, which is an error, not a ruling.
"""

from __future__ import annotations

import json
import logging
import re

from services.contracts.protocols import CacheableBlock, Decider, LLMProvider, Message, Option
from services.contracts.resolution import DecisionRecord
from services.contracts.warehouse import QueryResult
from services.reviews.store import Trace
from services.runtime import decision_policy, faithfulness

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


_DATA_AS_OF = re.compile(r"\s*\(data as of [^)]*\)", re.IGNORECASE)
_SCOPE_OPTIONS = [
    Option(
        "within_scope",
        "the SQL and the answer stay within what the question asked — its metric, "
        "grain, filters and time window — with no invented exclusions or extra claims",
    ),
    Option(
        "overreach",
        "the SQL or the answer goes beyond or beside the question (a different "
        "grain, window, population, or a claim the question did not ask for)",
    ),
]


def judge_typed(
    trace: Trace,
    decider: Decider,
    *,
    reference_sql: str | None = None,
    policies: dict[str, decision_policy.Policy] | None = None,
) -> tuple[str, str, list[DecisionRecord]]:
    """The judge as a composition of checks — deterministic where it can be,
    one typed decision where judgment is needed — instead of a free-text rubric.

    1. Groundedness is DETERMINISTIC: every numeric claim in the prose must
       match a groundable value in the rows (faithfulness.numeric_check). With
       no rows available the check is skipped and says so; a structured serve
       (prose intentionally empty) is never "no data"; the data_as_of footer
       is not a claim. A failed grounding is a `reject`.
    2. Scope is ONE typed decision (within_scope | overreach) under the policy:
       act on within_scope → approve; act on overreach → changes; not sure
       enough → changes, saying it is unsure — never a verdict invented from
       vibes. When the ticket carries a correction, the corrected SQL is what
       the decision sees (the original is what the reviewer said was wrong).

    Returns (verdict, reasoning, the decisions made) — verdict in
    approve | changes | reject, like the rubric judge, so callers fold it the
    same way.
    """
    sql = trace.correction_sql or trace.sql
    if not sql:
        return "changes", "no SQL to review", []
    prose = _DATA_AS_OF.sub("", trace.answer or "").strip()
    notes: list[str] = []
    if trace.correction_sql:
        notes.append("grading the correction's SQL, not the original")
    # 1. grounding
    if trace.correction_sql:
        notes.append("grounding not graded: the correction has not executed")
    elif trace.structured or not prose:
        notes.append("structured serve: no prose to ground (not 'no data')")
    elif trace.rows is None:
        notes.append(
            f"grounding not graded: rows not available "
            f"(row_count={trace.row_count if trace.row_count is not None else 'unknown'})"
        )
    else:
        columns = trace.columns or [f"c{i}" for i in range(len(trace.rows[0]) if trace.rows else 0)]
        status, reason = faithfulness.numeric_check(
            prose, QueryResult(columns=columns, rows=trace.rows), sql_text=sql
        )
        if status == "fail":
            return "reject", f"ungrounded: {reason}", []
        notes.append("grounded: every numeric claim matches the rows")
    # 2. scope, as one typed decision
    context = (
        "Review whether an analytics answer stays within the question. "
        f"Definition applied: {trace.definition_used or '(none)'}. "
        + (f"Approved reference SQL: {reference_sql}. " if reference_sql else "")
    )
    question = (
        f"Question: {trace.question}\nSQL: {sql}\nAnswer: {prose or '(structured, no prose)'}"
    )
    d = decider.decide(question=question, context=context, options=_SCOPE_OPTIONS, allow_none=False)
    verdict = decision_policy.verdict(d, "judge_scope", policies)
    record = DecisionRecord(
        slot="judge_scope",
        chosen=d.chosen,
        p=d.p,
        runner_up=d.runner_up,
        margin=d.margin,
        verdict=verdict,
        provider=d.provider,
    )
    if verdict == "act" and d.chosen == "within_scope":
        return "approve", "; ".join([*notes, "within scope"]), [record]
    if verdict == "act":
        return (
            "changes",
            "; ".join([*notes, "overreach: the SQL or answer goes beyond the question"]),
            [record],
        )
    return (
        "changes",
        "; ".join([*notes, f"scope unsure ({decision_policy.describe(d, 2)}) — a human decides"]),
        [record],
    )


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
