"""Generic OpenAI-compatible chat provider (the `LLMProvider` seam).

One wire shape covers OpenAI, DeepSeek, Ollama, vLLM, Groq, and most self-hosted
gateways: POST {base_url}/chat/completions with a bearer key. System blocks are
flattened into a single system message. A provider that caches prompts on its
own (OpenAI, DeepSeek) reports the cached part in the usage's token details:
it is recorded as cache reads and writes apart from the uncached input, and
priced at its own rates; one that reports none records none.
"""

from __future__ import annotations

import httpx

from services.contracts.errors import ProviderError
from services.contracts.protocols import CacheableBlock, LLMResult, Message
from services.llm.reasoning import completion_text, with_headroom
from services.observability import usage as ai_usage


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
        usage = data.get("usage") or {}
        # `prompt_tokens` counts every input token, cached and written ones
        # included; the details split them out (`cached_tokens` is billed
        # reads, `cache_write_tokens` writes, `ttl_12h_tokens` the 12-hour part).
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        read = int(details.get("cached_tokens") or 0)
        written = int(details.get("cache_write_tokens") or 0)
        long = int((details.get("cache_write_tokens_details") or {}).get("ttl_12h_tokens") or 0)
        result = LLMResult(
            text=text,
            input_tokens=max(0, int(usage.get("prompt_tokens", 0)) - read - written),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cache_read_tokens=read,
            cache_creation_tokens=written,
            cache_write_long_tokens=long,
        )
        ai_usage.record(
            "openai-compatible",
            model,
            result.input_tokens,
            result.output_tokens,
            cache_read_tokens=read,
            cache_write_tokens=written,
            cache_write_long_tokens=long,
        )
        return result
