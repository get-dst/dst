"""Deterministic fakes for the seam protocols.

These let any dependent module test the full flow offline — no warehouse, no
network, no non-determinism.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.config import ProviderConfig

from services.contracts.protocols import (
    CacheableBlock,
    ContextChunk,
    GeneratedQuery,
    LLMResult,
    Message,
)
from services.contracts.semantic_model import SemanticModel
from services.contracts.warehouse import DryRunResult, QueryResult, SchemaSnapshot


class FakeConnector:
    """A `Connector` that returns canned schema + rows."""

    kind = "duckdb"

    def __init__(
        self, snapshot: SchemaSnapshot | None = None, result: QueryResult | None = None
    ) -> None:
        self._snapshot = snapshot or SchemaSnapshot(connection="fake", dialect="duckdb")
        self._result = result or QueryResult(columns=["count"], rows=[[42]])

    def introspect(self) -> SchemaSnapshot:
        return self._snapshot

    def dry_run(self, sql: str) -> DryRunResult:
        return DryRunResult(bytes_estimated=0, valid=True)

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        # Honour row_limit like every real connector (they wrap the SQL in
        # `SELECT * FROM (...) LIMIT n`). Ignoring it here made the pipeline's
        # fetch cap invisible to tests, which is how a >FETCH_CAP result shipped
        # reporting the cap as the query's exact row count.
        if row_limit is None or len(self._result.rows) <= row_limit:
            return self._result
        return self._result.model_copy(update={"rows": self._result.rows[:row_limit]})


class ScriptedLLM:
    """An `LLMProvider` that returns pre-set responses in order (last repeats)."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self._responses = responses or ["fake-response"]
        self._i = 0

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        text = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return LLMResult(text=text, input_tokens=1, output_tokens=1)


class HashEmbedder:
    """An `Embedder` producing deterministic pseudo-embeddings (offline, stable)."""

    dim = 1024

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            digest = hashlib.sha256(t.encode()).digest()  # 32 bytes
            vec = [digest[i % len(digest)] / 255.0 for i in range(self.dim)]
            out.append(vec)
        return out


class EchoQueryGenerator:
    """A `QueryGenerator` that emits a fixed SQL string (for pipeline tests)."""

    def __init__(self, sql: str = "SELECT 1") -> None:
        self._sql = sql

    def generate(
        self,
        *,
        question: str,
        semantic_model: SemanticModel,
        prose_context: list[ContextChunk],
        dialect: str,
        feedback: str | None = None,
    ) -> GeneratedQuery:
        return GeneratedQuery(sql=self._sql, rationale="echo", definition_used=None)


def fake_llm_providers(key: str = "test-key") -> dict[str, ProviderConfig]:
    """The standard test provider table: one anthropic-type entry with *key*.

    Tests set ``settings.providers`` to this instead of vendor-named key fields
    (which no longer exist — providers are pure config).
    """
    from services.config import ProviderConfig

    return {"anthropic": ProviderConfig(type="anthropic", api_key=key)}
