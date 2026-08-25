"""Shared contracts (the seams) — import, don't edit."""

from __future__ import annotations

from services.contracts.lens_config import (
    AccessConfig,
    AccessRule,
    LensConfig,
    LoggingConfig,
    ModelConfig,
    RateLimitConfig,
)
from services.contracts.protocols import (
    CacheableBlock,
    Connector,
    ContextChunk,
    Embedder,
    GeneratedQuery,
    LLMProvider,
    LLMResult,
    Message,
    QueryGenerator,
)
from services.contracts.response import Citation, DataPayload, QueryResponse
from services.contracts.semantic_model import (
    Definition,
    Dimension,
    Entity,
    EntitySource,
    Field,
    Join,
    Metric,
    SampleQuery,
    SemanticModel,
    SharedProvenance,
)
from services.contracts.shared_semantic import (
    SelectEntity,
    SelectSpec,
    SharedEntity,
    SharedJoin,
    asset_content_hash,
    asset_hash,
)
from services.contracts.trace import TraceLog
from services.contracts.warehouse import (
    ColumnSchema,
    DryRunResult,
    QueryResult,
    SchemaSnapshot,
    TableSchema,
)

__all__ = [
    "AccessConfig",
    "AccessRule",
    "CacheableBlock",
    "Citation",
    "ColumnSchema",
    "Connector",
    "ContextChunk",
    "DataPayload",
    "Definition",
    "Dimension",
    "DryRunResult",
    "Embedder",
    "Entity",
    "EntitySource",
    "Field",
    "GeneratedQuery",
    "Join",
    "LLMProvider",
    "LLMResult",
    "LensConfig",
    "LoggingConfig",
    "Message",
    "Metric",
    "ModelConfig",
    "QueryGenerator",
    "QueryResponse",
    "QueryResult",
    "RateLimitConfig",
    "SampleQuery",
    "SelectEntity",
    "SelectSpec",
    "SharedEntity",
    "SharedJoin",
    "SharedProvenance",
    "asset_content_hash",
    "asset_hash",
    "SchemaSnapshot",
    "SemanticModel",
    "TableSchema",
    "TraceLog",
]
