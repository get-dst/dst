"""LLM provider implementations (the `LLMProvider` seam)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.contracts.protocols import LLMProvider


def resolve_provider(model_name: str) -> "LLMProvider | None":
    """Back-compat shim over the provider registry: model ref → client, or None
    when the provider's key is missing (callers decide whether that is fatal).
    The model name travels separately at these call sites; new code should use
    registry.resolve, whose ResolvedModel carries the client and its wire name
    as one value."""
    from services.llm import registry

    resolved = registry.resolve(model_name)
    return resolved.llm if resolved else None
