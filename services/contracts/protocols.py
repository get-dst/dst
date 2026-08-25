"""The seam protocols every implementation binds to.

`Connector` (warehouse), `LLMProvider` (Claude), `Embedder` (openai-compat/Voyage), and
`QueryGenerator` (grounded SQL). Owned here — implementations import, don't edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from services.contracts.profile import (
    LOW_CARDINALITY_MAX,
    SAMPLE_MAX_ROWS,
    JoinCandidate,
    TableProfile,
    TableSampleSpec,
)
from services.contracts.response import ClarificationRequest
from services.contracts.semantic_model import SemanticModel
from services.contracts.warehouse import DryRunResult, QueryResult, SchemaSnapshot


# ── LLM value types ──────────────────────────────────────────────────────────
@dataclass
class CacheableBlock:
    text: str
    cache: bool = True
    ttl: Literal["5m", "1h"] = "5m"  # cache lifetime; "1h" suits a fixed per-lens prefix


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


# ── Context / generation value types ─────────────────────────────────────────
@dataclass
class ContextChunk:
    text: str
    source: str
    locator: str | None = None
    score: float = 0.0


@dataclass
class GeneratedQuery:
    sql: str
    rationale: str | None = None
    definition_used: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    # Set when the generator declines because the model's tables cannot answer
    # the question (the data does not exist in this lens). A named gap — the
    # absence signal — not a failure. sql is empty when set.
    no_answer_reason: str | None = None
    # Set when the question hinges on an AMBIGUOUS governed term: the caller must
    # pick a meaning before SQL exists. sql is empty when set.
    clarification: ClarificationRequest | None = None


# ── Protocols ────────────────────────────────────────────────────────────────
@runtime_checkable
class Connector(Protocol):
    kind: str

    def introspect(self) -> SchemaSnapshot: ...

    def dry_run(self, sql: str) -> DryRunResult: ...

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult: ...


@runtime_checkable
class TargetedIntrospect(Protocol):
    """A connector that can resolve a requested table subset against the FULL
    catalog, before any listing cap.

    An optional capability for wide warehouses: `introspect()` may cap its
    listing (and mark the snapshot `truncated`), which used to make `--tables`
    match against the capped universe — no table outside it was reachable by
    any spelling. Names match at any qualification depth
    (`table`, `dataset.table`, `project.dataset.table`)."""

    def introspect_tables(self, wanted: list[str]) -> SchemaSnapshot: ...


@runtime_checkable
class WriteProbe(Protocol):
    """A connector that can prove write access by creating + dropping a scratch table.

    Used by connection evaluation (mgmt) when a connection requests write access; the
    probe is the only definitive proof the credential can actually write. It must leave
    nothing behind (always drops its throwaway table).
    """

    def probe_write(self) -> None: ...


@runtime_checkable
class CatalogProfiler(Protocol):
    """A connector that can profile tables from the engine's own catalog.

    Cheap metadata only — descriptions, partitioning/clustering, row counts,
    last-updated, catalog-native column stats — never a table scan. An optional
    capability (like WriteProbe): a connector without it still gets a basic
    profile derived from ``introspect()`` via
    `services.contracts.profile.profiles_from_snapshot`, so adding this protocol
    breaks no existing `Connector` implementation.
    """

    def profile_catalog(self) -> list[TableProfile]:
        """Profile every visible table from catalog metadata. No table scans."""
        ...

    def catalog_join_candidates(self) -> list[JoinCandidate]:
        """Join keys the engine itself declares (foreign keys); [] where none exist."""
        ...


@runtime_checkable
class SamplingProfiler(Protocol):
    """A connector that can sample table data under hard guards.

    An optional capability (like CatalogProfiler): the sampling pass fills what
    the catalog pass couldn't — observed null rates, cardinality
    (APPROX_COUNT_DISTINCT where the dialect has it), min/max for time + numeric
    columns, enum literals (``top_values``) for low-cardinality columns, and a
    MAX(partition/time column) probe for ``last_updated_logical``.

    Implementations must keep every query read-only and capped: at most
    ``max_rows`` rows per table via the dialect's sampling clause (USING SAMPLE /
    TABLESAMPLE / SAMPLE / LIMIT), plus a bytes budget where the engine bills
    scans (BigQuery dry-runs first and refuses over-budget queries). A column
    whose spec says ``shape_only`` never has literal values collected — shape
    (nulls/cardinality) only. A table that cannot be sampled within budget is
    omitted from the result so its catalog profile stands.
    """

    def sample_profile(
        self,
        tables: list[TableSampleSpec],
        *,
        max_rows: int = SAMPLE_MAX_ROWS,
        max_distinct_for_enum: int = LOW_CARDINALITY_MAX,
    ) -> list[TableProfile]:
        """Sample the specified tables/columns; profiles carry ``source="sampled"``."""
        ...


@runtime_checkable
class LLMProvider(Protocol):
    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult: ...


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class QueryGenerator(Protocol):
    def generate(
        self,
        *,
        question: str,
        semantic_model: SemanticModel,
        prose_context: list[ContextChunk],
        dialect: str,
        feedback: str | None = None,
    ) -> GeneratedQuery: ...


__all__ = [
    "CacheableBlock",
    "CatalogProfiler",
    "Connector",
    "ContextChunk",
    "Embedder",
    "GeneratedQuery",
    "LLMProvider",
    "LLMResult",
    "Message",
    "QueryGenerator",
    "SamplingProfiler",
    "WriteProbe",
]
