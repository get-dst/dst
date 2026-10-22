"""Anthropic (Claude) implementation of the `LLMProvider` seam, with prompt caching.

System blocks marked cacheable get an ephemeral cache_control breakpoint so the lens
semantic model / system context is cached across calls (per the claude-api guidance).
"""

from __future__ import annotations

import re
from typing import Any

import anthropic

from services.contracts.errors import ProviderError
from services.contracts.protocols import CacheableBlock, LLMResult, Message
from services.llm.reasoning import completion_text, with_headroom
from services.observability import usage as ai_usage

# Claude models from 4.7 on reject sampling parameters with a 400 (Claude
# Sonnet 5.5 rejects any non-default value). Through 4.6 they are accepted.
_VERSION = re.compile(r"^claude-(?:[a-z]+-)?(\d+)(?:-(\d{1,2})(?!\d))?")
_LAST_SAMPLED = (4, 6)

# Models this process saw refuse `temperature` although their name did not
# say so: asked once, then never again.
_REFUSES_TEMPERATURE: set[str] = set()


def takes_temperature(model: str) -> bool:
    """Whether a request to *model* may carry `temperature`. A Claude name
    is read for its version; a name that is not one (a gateway's alias) keeps
    the parameter, and a 400 naming it is retried without (see ``complete``)."""
    if model in _REFUSES_TEMPERATURE:
        return False
    found = _VERSION.match(model)
    if found is None:
        return True
    return (int(found.group(1)), int(found.group(2) or 0)) <= _LAST_SAMPLED


class AnthropicProvider:
    def __init__(self, api_key: str, timeout: float = 120.0, reasoning: bool | None = None) -> None:
        # max_retries=0: the registry wraps every provider in RetryingLLM, which
        # owns the retry policy — SDK-internal retries would compound with it.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)
        # We never send `thinking`, so only models that think by DEFAULT spend
        # the budget on it (claude-opus-5 and friends). None = decide per model
        # name; config overrides. See services/llm/reasoning.py.
        self._reasoning = reasoning

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        # The cap that actually goes on the wire: the answer budget the caller
        # asked for, plus thinking headroom when this model needs it.
        cap = with_headroom(max_tokens, model, self._reasoning)
        sys_blocks: list[dict[str, Any]] = []
        for b in system:
            block: dict[str, Any] = {"type": "text", "text": b.text}
            if b.cache:
                cc: dict[str, Any] = {"type": "ephemeral"}
                if b.ttl == "1h":
                    cc["ttl"] = "1h"  # extended cache TTL (GA); default is 5m
                block["cache_control"] = cc
            sys_blocks.append(block)
        msgs: list[dict[str, Any]] = [{"role": m.role, "content": m.content} for m in messages]

        request: dict[str, Any] = {
            "model": model,
            "max_tokens": cap,
            "system": sys_blocks,
            "messages": msgs,
        }
        if takes_temperature(model):
            request["temperature"] = temperature
        try:
            try:
                resp = self._client.messages.create(**request)
            except anthropic.BadRequestError as exc:
                if "temperature" not in request or "temperature" not in str(exc):
                    raise
                _REFUSES_TEMPERATURE.add(model)
                del request["temperature"]
                resp = self._client.messages.create(**request)
        except anthropic.APIError as exc:
            raise ProviderError(
                "anthropic", str(exc), status=getattr(exc, "status_code", None)
            ) from exc
        except TypeError as exc:
            # An SDK signature mismatch (e.g. anthropic 1.x removed `temperature`)
            # is a provider-configuration problem, not an internal error — name the
            # installed version so the fix is a one-liner, not a log dive.
            raise ProviderError(
                "anthropic",
                f"SDK call signature mismatch ({exc}) — installed anthropic "
                f"{anthropic.__version__}; dst expects anthropic<1",
            ) from exc
        # Anthropic spells the same two facts differently: stop_reason "max_tokens"
        # is finish_reason "length", and extended thinking arrives as `thinking`
        # blocks rather than a `reasoning_content` field.
        text = completion_text(
            "".join(
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
            ),
            provider="anthropic",
            model=model,
            max_tokens=cap,
            finish_reason="length" if resp.stop_reason == "max_tokens" else resp.stop_reason,
            thinking_chars=sum(
                len(getattr(b, "thinking", "") or "")
                for b in resp.content
                if getattr(b, "type", "") == "thinking"
            ),
        )
        usage = resp.usage
        written = getattr(usage, "cache_creation_input_tokens", 0) or 0
        result = LLMResult(
            text=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=written,
            cache_write_long_tokens=_long_writes(usage, written, system),
        )
        ai_usage.record(
            "anthropic",
            model,
            result.input_tokens,
            result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_write_tokens=result.cache_creation_tokens,
            cache_write_long_tokens=result.cache_write_long_tokens,
        )
        return result


def _long_writes(usage: Any, written: int, system: list[CacheableBlock]) -> int:
    """The cache writes billed at the 1-hour rate. The usage splits them
    (``cache_creation.ephemeral_1h_input_tokens``); where it does not, the
    writes are all 5-minute when no block asked for an hour, and are counted
    at the 1-hour rate otherwise: a ceiling on the call's cost, never a floor."""
    split = getattr(usage, "cache_creation", None)
    if split is not None:
        return int(getattr(split, "ephemeral_1h_input_tokens", 0) or 0)
    return written if any(b.cache and b.ttl == "1h" for b in system) else 0
