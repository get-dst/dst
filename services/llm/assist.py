"""Fast/cheap assist model resolver + a small JSON-completion helper.

Assist tasks (suggesting lens config, GitHub paths) run on the registry's fast
tier — DeepSeek's fast model when configured, else Haiku. Returns (provider, model).
"""

from __future__ import annotations

import json
import re
from typing import Any

from services.contracts.protocols import CacheableBlock, LLMProvider, Message
from services.llm import registry


def assist_llm() -> registry.ResolvedModel | None:
    """The model for in-app assist: the registry's fast tier, or None keyless."""
    return registry.resolve(registry.tier("fast"))


def complete_json(
    llm: LLMProvider, model: str, system: str, user: str, max_tokens: int = 1200
) -> dict[str, Any]:
    """Run a JSON-returning prompt and parse it, tolerating code fences."""
    res = llm.complete(
        system=[CacheableBlock(system)],
        messages=[Message("user", user)],
        model=model,
        temperature=0.2,
        max_tokens=max_tokens,
    )
    body = res.text.strip()
    body = re.sub(r"^```[a-zA-Z]*", "", body).strip()
    body = re.sub(r"```$", "", body).strip()
    parsed: dict[str, Any] = json.loads(body)
    return parsed
