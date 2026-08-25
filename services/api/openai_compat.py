"""OpenAI-compatible adapter — the *app* door onto dst's governed pipeline.

An app already built on the OpenAI SDK points its client at dst and gets a governed,
cited answer with no new SDK and no MCP host:

    client = OpenAI(base_url="https://…/v1", api_key="dst_…")
    client.chat.completions.create(
        model="dst/<lens>",
        messages=[{"role": "user", "content": "how many repeat customers?"}],
    )

The `model` field selects the lens; the last user message is the question. This is a thin
adapter (like ``services/mcp/server.py``): map the OpenAI wire format → the one shared
core ``run_lens_query`` → map the ``QueryResponse`` back. Governance, allow-lists, rate
limits, and tracing are identical to REST and MCP because it's the same pipeline.

Structured fields (sql, rows, citations, confidence) ride along under a non-standard
``dst`` key; generic OpenAI clients ignore it, dst-aware clients read it.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from services.api.query import run_lens_query
from services.auth.deps import get_caller
from services.contracts.response import QueryResponse
from services.governance.credentials import CallerIdentity
from services.governance.policy import authorize
from services.lenses.store import list_published_for_org

router = APIRouter(prefix="/v1", tags=["openai"])

_MODEL_PREFIX = "dst/"


# --------------------------------------------------------------------------- #
# Request models — extra fields (temperature, top_p, …) are accepted and ignored.
# --------------------------------------------------------------------------- #
class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str
    # Content is a plain string, or OpenAI's list-of-parts form; both are handled.
    content: str | list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str
    messages: list[ChatMessage]
    stream: bool = False


def _openai_error(status: int, message: str) -> JSONResponse:
    """OpenAI-style error envelope so SDK clients raise a clean, typed error."""
    type_ = {
        401: "authentication_error",
        403: "permission_error",
        429: "rate_limit_error",
    }.get(status, "invalid_request_error")
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "code": None}},
    )


def _lens_from_model(model: str) -> str:
    """``dst/<lens>`` (or a bare ``<lens>``) → the lens name."""
    name = model[len(_MODEL_PREFIX) :] if model.startswith(_MODEL_PREFIX) else model
    name = name.strip()
    if not name or name == "dst":
        raise HTTPException(
            status_code=400,
            detail="set model to 'dst/<lens>'; GET /v1/models lists this key's lenses",
        )
    return name


def _text(content: str | list[dict[str, Any]] | None) -> str:
    """Flatten a message's content (string or list-of-parts) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = [str(p.get("text", "")) for p in content if isinstance(p, dict)]
    return " ".join(p for p in parts if p).strip()


def _last_user_question(messages: list[ChatMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user":
            text = _text(msg.content)
            if text:
                return text
    raise HTTPException(status_code=400, detail="no user message with content to answer")


def _dst_extras(resp: QueryResponse) -> dict[str, Any]:
    return {
        "lens": resp.lens,
        # Whether SQL ran at all. The allow-list is hand-written, so a caller on
        # this surface would otherwise get the prose and none of the outcome.
        "status": resp.status,
        "sql": resp.sql,
        "data": resp.data.model_dump() if resp.data else None,
        "definition_used": resp.definition_used,
        "citations": [c.model_dump() for c in resp.citations],
        "confidence": resp.confidence,
        "certification": resp.certification,
        "request_id": resp.request_id,
    }


def _completion(model: str, resp: QueryResponse, created: int) -> dict[str, Any]:
    return {
        "id": resp.request_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": resp.answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "dst": _dst_extras(resp),
    }


def _stream(model: str, resp: QueryResponse, created: int) -> StreamingResponse:
    """Emit a single content delta then [DONE] — a valid SSE stream most clients accept.

    The pipeline composes the whole answer synchronously, so there's nothing to stream
    token-by-token yet; real streaming would wire ``AnswerComposer`` through here later.
    """
    base = {
        "id": resp.request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }

    def chunk(
        delta: dict[str, Any], finish: str | None, extra: dict[str, Any] | None = None
    ) -> str:
        payload = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if extra:
            payload.update(extra)
        return f"data: {json.dumps(payload)}\n\n"

    def gen() -> Any:
        yield chunk({"role": "assistant"}, None)
        yield chunk({"content": resp.answer}, None)
        yield chunk({}, "stop", {"dst": _dst_extras(resp)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/chat/completions")
def chat_completions(
    body: ChatCompletionRequest,
    background: BackgroundTasks,
    caller: CallerIdentity = Depends(get_caller),
) -> Any:
    """OpenAI Chat Completions over a dst lens. ``model`` selects the lens."""
    try:
        lens = _lens_from_model(body.model)
        question = _last_user_question(body.messages)
        resp = run_lens_query(lens, question, caller, background)
    except HTTPException as exc:
        return _openai_error(exc.status_code, str(exc.detail))

    created = int(time.time())
    if body.stream:
        return _stream(body.model, resp, created)
    return _completion(body.model, resp, created)


@router.get("/models")
def list_models(caller: CallerIdentity = Depends(get_caller)) -> dict[str, Any]:
    """The lenses this key may query, as OpenAI model objects (mirrors MCP list_lenses)."""
    created = int(time.time())
    data: list[dict[str, Any]] = []
    for name, _display, _desc, bundle in list_published_for_org(caller.org_id):
        ok, _ = authorize(caller, bundle.config)
        if ok:
            data.append(
                {
                    "id": f"{_MODEL_PREFIX}{name}",
                    "object": "model",
                    "created": created,
                    "owned_by": "dst",
                }
            )
    return {"object": "list", "data": data}
