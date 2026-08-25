"""DeepSeek = the generic OpenAI-compatible provider pointed at api.deepseek.com.

Kept as a named class so existing call sites and the provider registry read
naturally; all wire logic lives in OpenAICompatProvider.
"""

from __future__ import annotations

from services.llm.openai_compat import OpenAICompatProvider

_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(OpenAICompatProvider):
    def __init__(self, api_key: str, base_url: str = _BASE_URL) -> None:
        super().__init__(api_key=api_key, base_url=base_url)
