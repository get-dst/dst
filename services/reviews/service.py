"""Review orchestration: create a ticket for a traced request, run the AI judge,
and set state (auto-approve on a confident pass, else escalate to a human).

A trace whose verification carries a failed adversarial check
pre-fills the ticket's correction with the challenge text, so opening a review on
a challenged answer seeds the patch drafter (Track A) instead of just warning.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from services.certify import store as certify_store
from services.contracts.correction import CorrectionDelta
from services.contracts.protocols import LLMProvider
from services.llm import registry
from services.reviews import store
from services.reviews.judge import judge_trace
from services.reviews.store import Ticket, Trace

log = logging.getLogger("dst")

# A certified answer at/above this similarity is treated as the approved reference.
_REFERENCE_THRESHOLD = 0.9


def judge_llm() -> tuple[LLMProvider | None, str]:
    """The review judge's (provider, model) — shared by POST /v1/reviews and the
    auto_review flagger so every ticket is judged the same way.

    Judge policy: a dedicated cheap provider is trusted to judge; when fast and
    smart live on the SAME provider, pay for the smart model instead."""
    fast_ref, smart_ref = registry.tier("fast"), registry.tier("smart")
    same = registry.split_ref(fast_ref)[0] == registry.split_ref(smart_ref)[0]
    ref = smart_ref if same else fast_ref
    resolved = registry.resolve(ref)
    # Keyless: no client, and the name degrades to a LABEL for the ticket — the
    # only shape where a name may travel without the client that earned it.
    return (resolved.llm, resolved.name) if resolved else (None, ref or "unconfigured")


def _certified_reference(session: Session, trace: Trace) -> str | None:
    """The approved SQL for a near-identical certified question, if one exists."""
    embedder = registry.resolve_embedder()
    if embedder is None:
        return None
    try:
        qvec = embedder.embed([trace.question])[0]
        hits = certify_store.search(session, trace.lens, qvec, k=1)
    except Exception:
        log.exception("certified reference lookup failed for review of %s", trace.request_id)
        return None
    if hits and hits[0].score >= _REFERENCE_THRESHOLD:
        return hits[0].answer.sql
    return None


def _challenge_correction(trace: Trace) -> CorrectionDelta | None:
    """A correction pre-filled from the trace's failed adversarial check.

    fold_challenges serialized the unresolved blocking challenge into
    ``request_log.verification`` as the "adversary" check, its reason already
    naming what is wrong ("<assumption>: <reason>; …") — exactly a scope delta
    the patch drafter can turn into a fix. Used only when the caller
    supplied no correction of their own.
    """
    checks = (trace.verification or {}).get("checks")
    if not isinstance(checks, list):
        return None
    for check in checks:
        if not isinstance(check, dict):
            continue
        if check.get("name") == "adversary" and check.get("status") == "fail":
            note = str(check.get("reason") or "").strip()
            if note:
                return CorrectionDelta(kind="scope", note=note)
    return None


def open_review(
    session: Session,
    trace: Trace,
    llm: LLMProvider | None,
    model: str = "claude-sonnet-4-6",
    correction: CorrectionDelta | None = None,
    origin: str = "human",
) -> Ticket:
    if correction is None:
        # A confirmed adversarial challenge seeds the fix, never overrides the caller.
        correction = _challenge_correction(trace)
    ticket = store.create_ticket(session, trace, correction, origin=origin)
    if llm is None:
        # No LLM configured: escalate straight to a human reviewer.
        store.set_ai_result(session, ticket.ticket_id, "", "AI judge unavailable", "needs_human")
        ticket.state = "needs_human"
        return ticket

    reference_sql = _certified_reference(session, trace)
    verdict, reasoning = judge_trace(llm, trace, model, reference_sql=reference_sql)
    # A confident approve resolves; anything else awaits a human sign-off. An empty
    # judge reply arrives as NO_VERDICT ("") and lands here UNRULED — same shape as
    # the no-LLM branch above: the queue shows no ai_verdict, so nobody mistakes a
    # provider outage for the judge having found fault.
    state = "approved" if verdict == "approve" else "needs_human"
    # The judge's resolved model name is the machine actor behind this verdict —
    # blank when the judge returned NO_VERDICT, so an outage never claims a ruling.
    judged_by = model if verdict else ""
    store.set_ai_result(session, ticket.ticket_id, verdict, reasoning, state, judged_by=judged_by)
    ticket.ai_verdict = verdict
    ticket.ai_reasoning = reasoning
    ticket.state = state
    ticket.judged_by = judged_by
    return ticket
