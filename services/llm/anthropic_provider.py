"""Anthropic (Claude) implementation of the `LLMProvider` seam, with prompt caching.

System blocks marked cacheable get an ephemeral cache_control breakpoint so the lens
semantic model / system context is cached across calls (per the claude-api guidance).
"""

from __future__ import annotations

from typing import Any

import anthropic

from services.contracts.errors import ProviderError
from services.contracts.protocols import CacheableBlock, LLMResult, Message
from services.llm.reasoning import completion_text, with_headroom


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

        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=cap,
                temperature=temperature,
                system=sys_blocks,  # type: ignore[arg-type]
                messages=msgs,  # type: ignore[arg-type]
            )
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
        return LLMResult(
            text=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )
