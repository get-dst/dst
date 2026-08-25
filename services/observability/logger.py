"""Persist the full per-response trace to request_log (org-scoped, RLS-enforced).

Called off the response path (FastAPI BackgroundTasks) so it never adds latency.
Row samples are written only when present (off by default: a log row outlives
the request that made it).

Off the response path also means a failure here is invisible by construction: the
answer, its `request_id` and exit 0 have already reached the caller. On a
schema two revisions behind, the INSERT raises `UndefinedColumn` for every request and
the only trace of it is the terminal running `serve` — the caller is told nothing, so
the receipt for an answer that was given simply does not exist. Worse, Starlette runs
background tasks in sequence and stops at the first exception, so the tasks queued
after this one (the auto-review flagger) silently stopped running too. Hence: catch,
count, and say so where an operator looks — `/ready` reports `traces`, and the log line
names the cause instead of a bare traceback.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text

from services.contracts.trace import TraceLog
from services.db.session import org_session
from services.runtime.prompt_version import PROMPT_HASH

log = logging.getLogger("dst")

# Process-wide, since boot: how many traces this server failed to persist, and why.
# Not a metric for its own sake — it is the only evidence that exists at all when the
# write fails, because the row that would have carried it is the thing that was lost.
_FAILED_WRITES = 0
_LAST_ERROR = ""

_INSERT = text(
    """
    INSERT INTO request_log (
        org_id, request_id, lens, caller, agent, question, sql, valid, row_count, sample,
        answer, citations, definition_used, confidence, verification, certification,
        latency, ai_input_tokens, ai_output_tokens, ai_cost_usd, wh_bytes, wh_cost_usd,
        status, error, prompt_hash, generator_tier, repairs, context_refs, degraded
    ) VALUES (
        :org_id, :request_id, :lens, :caller, :agent, :question, :sql, :valid, :row_count,
        CAST(:sample AS jsonb), :answer, CAST(:citations AS jsonb), :definition_used,
        :confidence, CAST(:verification AS jsonb), :certification,
        CAST(:latency AS jsonb), :ai_input_tokens, :ai_output_tokens,
        :ai_cost_usd, :wh_bytes, :wh_cost_usd, :status, :error, :prompt_hash,
        :generator_tier, :repairs, CAST(:context_refs AS jsonb), CAST(:degraded AS jsonb)
    )
    """
)


def log_trace(trace: TraceLog) -> None:
    params = {
        "org_id": trace.org_id,
        "request_id": trace.request_id,
        "lens": trace.lens,
        "caller": trace.caller,
        "agent": trace.agent,
        "question": trace.question,
        "sql": trace.sql,
        "valid": trace.valid,
        "row_count": trace.row_count,
        "sample": json.dumps(trace.sample) if trace.sample is not None else None,
        "answer": trace.answer,
        "citations": json.dumps([c.model_dump() for c in trace.citations]),
        "definition_used": trace.definition_used,
        "confidence": trace.confidence,
        "verification": (
            json.dumps(trace.verification.model_dump()) if trace.verification else None
        ),
        "certification": trace.certification,
        "latency": json.dumps(trace.latency_ms),
        "ai_input_tokens": trace.ai_input_tokens,
        "ai_output_tokens": trace.ai_output_tokens,
        "ai_cost_usd": trace.ai_cost_usd,
        "wh_bytes": trace.wh_bytes,
        "wh_cost_usd": trace.wh_cost_usd,
        "status": trace.status,
        # Stamped at persist time: the hash of the serving prompt-set
        # in THIS process — so a trace always names the prompts that served it.
        "error": trace.error,
        "prompt_hash": trace.prompt_hash or PROMPT_HASH,
        "generator_tier": trace.generator_tier,
        "repairs": trace.repairs,
        "context_refs": json.dumps(trace.context_refs),
        # Which governance capabilities could not run for this answer (0037).
        "degraded": json.dumps(trace.degraded),
    }
    try:
        with org_session(trace.org_id) as session:
            session.execute(_INSERT, params)
    except Exception as exc:
        _record_write_failure(trace.request_id, exc)


def _record_write_failure(request_id: str, exc: Exception) -> None:
    """Name the loss, in the words an operator can act on, and remember it.

    Re-raising would only reach Starlette's task runner, which logs a bare traceback
    and then abandons the tasks queued behind this one. Swallowing it silently is what
    made the audit trail evaporate in the first place. So: one CRITICAL line saying
    which answer lost its receipt and why, plus a counter `/ready` reports.
    """
    global _FAILED_WRITES, _LAST_ERROR

    _FAILED_WRITES += 1
    _LAST_ERROR = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
    log.critical(
        "AUDIT TRACE LOST for %s — the answer was served but its request_log row was "
        "NOT written, so it is invisible to the review queue, the drift audit, "
        "`dst test` and `dst correct`. %s (%d lost since this server started)",
        request_id,
        _LAST_ERROR,
        _FAILED_WRITES,
        exc_info=True,
    )


def trace_write_status() -> str:
    """`/ready`'s `traces` field: "ok", or what has been lost and why."""
    if not _FAILED_WRITES:
        return "ok"
    return f"LOSING WRITES — {_FAILED_WRITES} trace(s) not persisted since boot: {_LAST_ERROR}"
