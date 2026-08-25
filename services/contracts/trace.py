"""The full per-response trace — logged for every call.

Also the review artifact and the training data for the improvement flywheel.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from services.contracts.response import Citation
from services.contracts.verification import VerificationReport


class TraceLog(BaseModel):
    request_id: str
    org_id: str
    lens: str
    caller: str  # the principal — the person the credential resolves to
    # The acting client (MCP client name, or "mcp"; None on a direct API call that did
    # not self-identify). A label for attribution — "person `caller`, through `agent`".
    agent: str | None = None
    question: str
    # The prose chunks that rode in the prompt, by source (the answer-contract
    # marker is an instruction, not a source — excluded, as it is from citations).
    context_refs: list[str] = Field(default_factory=list)
    sql: str | None = None
    valid: bool | None = None
    row_count: int | None = None
    sample: list[list[object]] | None = None  # only if lens.log_samples
    answer: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    definition_used: str | None = None
    confidence: str | None = None  # the verification grade (verified | partial | unverified)
    verification: VerificationReport | None = None  # per-check breakdown
    certification: str = "none"  # certified | assisted | none
    # Governance capabilities that could not run (services/contracts/response.py
    # ::QueryResponse.degraded). Persisted so "how many answers did we serve while
    # certified matching was down?" is a query, not an archaeology project.
    degraded: list[str] = Field(default_factory=list)
    latency_ms: dict[str, float] = Field(default_factory=dict)  # per-stage
    ai_input_tokens: int | None = None
    ai_output_tokens: int | None = None
    ai_cost_usd: float | None = None
    wh_bytes: int | None = None
    wh_cost_usd: float | None = None
    # "refused" = the lens declined: the question isn't answerable from its
    # data (absence signal) — distinct from "rejected" (guard) and "error".
    status: Literal["ok", "deny", "rejected", "error", "refused", "clarification"] = "ok"
    error: str | None = None
    # Which serving prompt-set produced this trace — stamped at persist
    # time from runtime.prompt_version so regressions attribute to prompt edits.
    prompt_hash: str | None = None
    # Which generation path ran (certified | intent | grounded — assembly.generator_tier)
    # and how many repair attempts it consumed. The tier picks the PROMPT, so a
    # regression that only shows on one tier is otherwise invisible in the log;
    # `dst lens prompt` prints the same tier's prompt for a question.
    generator_tier: str | None = None
    repairs: int = 0
