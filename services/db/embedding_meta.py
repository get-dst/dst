"""Embedding metadata — which (model, dim) the stored vectors were produced by.

One global row (``embedding_meta``, migration 0024): embedders are install-level
config, so vectors from different models/dims can never share the pgvector columns.
Every embed-for-write path calls :func:`guard_write` first — the first-ever write
claims the row; a later write with a different embedder fails with the actionable
"run `dst reindex`" error instead of silently mixing incompatible vectors.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from services.contracts.protocols import Embedder


class EmbeddingMismatchError(RuntimeError):
    """The configured embedder doesn't match what the stored vectors were made with."""


def identity(embedder: Embedder) -> tuple[str, int]:
    """The (model, dim) a guard/reindex records for *embedder*. Real embedders
    expose ``model``; fakes without one are identified by class name."""
    return getattr(embedder, "model", type(embedder).__name__), int(embedder.dim)


def read_meta(conn: Session | Connection) -> tuple[str, int] | None:
    row = conn.execute(text("SELECT embedding_model, embedding_dim FROM embedding_meta")).first()
    return (row[0], int(row[1])) if row is not None else None


_VECTOR_TABLES = ("context_chunk", "certified_answer", "router_anchor")


def column_dim(conn: Session | Connection, table: str) -> int:
    """The physical pgvector column's dimension (atttypmod)."""
    return int(
        conn.execute(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = CAST(:t AS regclass) AND attname = 'embedding'"
            ),
            {"t": table},
        ).scalar_one()
    )


def guard_write(session: Session, embedder: Embedder) -> None:
    """Claim-or-check before writing embeddings. First write claims the meta row
    (inside the caller's transaction — rolls back with it); a mismatch raises.

    The PHYSICAL columns are checked first: on a fresh install the meta row is
    empty, so a smaller-dim embedder would happily claim it and then explode
    mid-INSERT with a raw psycopg error (the local tier's dim 384 against the
    migrations' vector(1024)). The guard turns
    that into the actionable reindex message; `dst migrate` auto-sizes
    empty installs so most users never see it."""
    model, dim = identity(embedder)
    for table in _VECTOR_TABLES:
        col = column_dim(session, table)
        if col != dim:
            raise EmbeddingMismatchError(
                f"embedding columns are vector({col}) but the configured embedder "
                f"{model} produces dim {dim} — run `dst reindex` to retype and "
                f"re-embed (instant when no vectors exist yet)"
            )
    session.execute(
        text(
            "INSERT INTO embedding_meta (embedding_model, embedding_dim) "
            "VALUES (:m, :d) ON CONFLICT (id) DO NOTHING"
        ),
        {"m": model, "d": dim},
    )
    stored = read_meta(session)
    if stored != (model, dim):
        assert stored is not None  # the insert above claims it when empty
        raise EmbeddingMismatchError(
            f"embedding config mismatch: stored vectors were written with "
            f"{stored[0]} (dim {stored[1]}) but the configured embedder is "
            f"{model} (dim {dim}) — run `dst reindex` to re-embed everything, "
            f"or restore the previous embedding provider config"
        )


def set_meta(conn: Connection, model: str, dim: int) -> None:
    """The reindex flip: record the new (model, dim) as what the columns now hold."""
    conn.execute(
        text(
            "INSERT INTO embedding_meta (embedding_model, embedding_dim) VALUES (:m, :d) "
            "ON CONFLICT (id) DO UPDATE SET embedding_model = :m, embedding_dim = :d, "
            "updated_at = now()"
        ),
        {"m": model, "d": dim},
    )
