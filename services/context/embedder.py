"""Voyage AI implementation of the `Embedder` seam (voyage-3.5, dim 1024).

The voyageai SDK is an optional extra (it drags numpy) — the OSS default is any
openai-compatible embedding endpoint. Import is deferred so a voyage-less
install only fails here, with the fix in the message, when voyage is configured.
"""

from __future__ import annotations

from types import ModuleType

from services.contracts.errors import ProviderError


def _voyageai() -> ModuleType:
    try:
        import voyageai
    except ImportError as exc:
        raise ProviderError(
            "voyage",
            "voyageai SDK not installed — `uv sync --extra voyage` "
            "(or `pip install 'dst-core[voyage]'`), or switch to an "
            "openai-compatible embedding provider",
        ) from exc
    # Annotated assignment types the module both ways: uninstalled (mypy sees
    # Any via ignore_missing_imports) and installed via the voyage extra.
    module: ModuleType = voyageai
    return module


class VoyageEmbedder:
    def __init__(self, api_key: str, model: str = "voyage-3.5", dim: int = 1024) -> None:
        self._client = _voyageai().Client(api_key=api_key)
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # output_dimension only when non-default: older voyage models reject the
        # parameter, and 1024 is every voyage model's native default anyway.
        try:
            result = self._client.embed(
                texts,
                model=self.model,
                input_type="document",
                output_dimension=self.dim if self.dim != 1024 else None,
            )
        except _voyageai().error.VoyageError as exc:
            raise ProviderError("voyage", str(exc)) from exc
        return [list(e) for e in result.embeddings]
