"""Generic OpenAI-compatible embeddings provider (the `Embedder` seam).

POST {base_url}/embeddings with a bearer key — covers OpenAI, Ollama, vLLM,
and most gateways. The dimension is declared in config (explicit beats
introspection: pgvector columns are typed to it).
"""

from __future__ import annotations

import httpx

from services.contracts.errors import ProviderError


class OpenAICompatEmbedder:
    def __init__(self, api_key: str, base_url: str, model: str, dim: int = 1024) -> None:
        self._api_key = api_key
        self._url = base_url.rstrip("/") + "/embeddings"
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = httpx.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self.model, "input": texts},
                timeout=120.0,
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
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [list(d["embedding"]) for d in data]
