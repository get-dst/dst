"""Model-ref resolution for API handlers — resolve or fail with an actionable 503."""

from __future__ import annotations

from fastapi import HTTPException

from services.contracts.protocols import Embedder
from services.llm import registry


def require_llm(ref: str) -> registry.ResolvedModel:
    resolved = registry.resolve(ref)
    if resolved is None:
        raise HTTPException(
            status_code=503, detail=f"LLM not configured (set {registry.key_env_hint(ref)})"
        )
    return resolved


def require_embedder() -> Embedder:
    embedder = registry.resolve_embedder()
    if embedder is None:
        raise HTTPException(status_code=503, detail=registry.EMBEDDER_HINT)
    return embedder
