"""Generic OpenAI-compatible chat provider (the `LLMProvider` seam).

One wire shape covers OpenAI, DeepSeek, Ollama, vLLM, Groq, and most self-hosted
gateways: POST {base_url}/chat/completions with a bearer key. System blocks are
flattened into a single system message — prompt caching is an Anthropic concept;
these providers report cache_read_tokens=0 and the trace records that honestly.
"""

from __future__ import annotations

import httpx

from services.contracts.errors import ProviderError
from services.contracts.protocols import CacheableBlock, LLMResult, Message
from services.llm.reasoning import completion_text, with_headroom


class OpenAICompatProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: float = 120.0,
        reasoning: bool | None = None,
    ) -> None:
        self._api_key = api_key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout
        # Whether this provider's models bill thinking against max_tokens.
        # None = decide per model name (services/llm/reasoning.py).
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
        msgs: list[dict[str, str]] = []
        sys_text = "\n\n".join(b.text for b in system)
        if sys_text:
            msgs.append({"role": "system", "content": sys_text})
        msgs.extend({"role": m.role, "content": m.content} for m in messages)

        try:
            resp = httpx.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": model,
                    "messages": msgs,
                    "temperature": temperature,
                    "max_tokens": cap,
                    "stream": False,
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                "openai-compatible",
                exc.response.text[:200] or "upstream error",
                status=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("openai-compatible", str(exc)) from exc
        data = resp.json()
        # Reasoning models put their thinking in `reasoning_content` and can send
        # content=null when the budget ran out before the answer — never None here.
        # `finish_reason` plus the size of that thinking block is what tells an
        # operator WHICH of those happened; without them every empty reply looks
        # like a cap problem even when the cap was never reached.
        choice = data["choices"][0]
        text = completion_text(
            choice["message"]["content"],
            provider="openai-compatible",
            model=model,
            max_tokens=cap,
            finish_reason=choice.get("finish_reason"),
            thinking_chars=len(choice["message"].get("reasoning_content") or ""),
        )
        usage = data.get("usage", {})
        return LLMResult(
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )
