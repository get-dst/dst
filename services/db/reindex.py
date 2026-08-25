"""`dst reindex` — re-embed every stored vector with the configured embedder.

Changing the embedding model or dimension strands the stored vectors: pgvector
columns are typed to the old dim and their contents were produced by the old
model. The flip runs in three resumable phases:

  A. mark — NULL every embedding in one transaction (retyping the columns and
     rebuilding the hnsw indexes when the dim changes). NULL means "pending".
  B. re-embed — batches of pending rows, one transaction each. A crash resumes
     at the remaining NULLs: NULL always means "pending", so phase A is skipped
     when NULLs exist at the target dim. (context_chunk is NOT NULL outside a
     reindex; certified_answer may hold NULLs in steady state — answers promoted
     without an embedder — which this same phase backfills.)
  C. flip — restore NOT NULL (context_chunk only; certified_answer stays
     nullable for no-embedder promotions), promote every re-embedded certified
     answer out of 'pending_embedding' back to 'active' (reindex is the
     sanctioned recovery from an unmatchable corpus), and write embedding_meta
     LAST; only then does the write-path guard (services/db/embedding_meta.py)
     accept the new embedder.

Runs on the admin engine: ALTER needs table ownership, and re-embedding spans
every org (RLS bypass). Until the flip, a running API keeps serving reads and
the guard keeps refusing mismatched writes — no torn state is observable.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from services.context.ingest import _embed_batched
from services.contracts.protocols import Embedder
from services.db import embedding_meta
from services.db.session import admin_engine

# Every pgvector column in the schema: (table, text column its vectors are made from).
_TABLES = (("context_chunk", "text"), ("certified_answer", "question"))
# Cache tables: emptied by a reindex instead of re-embedded — their rows rebuild
# lazily (router_anchor: publish pre-warms, /v1/query repairs). They still
# get retyped on a dim change and guarded by embedding_meta like every vector table.
_CACHE_TABLES = ("router_anchor",)
# certified_answer is NOT here: it stays nullable (migration 0027) so certify-from-review
# works before any embedding provider is configured.
_NOT_NULL_TABLES = ("context_chunk",)
_INDEX = {
    "context_chunk": "ix_context_chunk_embedding",
    "certified_answer": "ix_certified_answer_embedding",
}


def _anchor_text(question: str, slots: object, samples: object) -> str:
    """A template row (slots set) anchors on its first sample-bound
    question; the driver hands jsonb back decoded, but tolerate strings."""
    import json

    from services.certify.binding import render_question

    if isinstance(samples, str | bytes | bytearray):
        samples = json.loads(samples)
    if slots and isinstance(samples, list) and samples:
        return render_question(question, samples[0])
    return question


def _vec(values: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


def _col_dim(conn: Connection, table: str) -> int:
    """The column's current dimension — pgvector stores it as the atttypmod."""
    return int(
        conn.execute(
            text(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = CAST(:t AS regclass) AND attname = 'embedding'"
            ),
            {"t": table},
        ).scalar_one()
    )


def _pending(conn: Connection, table: str) -> int:
    return int(
        conn.execute(text(f"SELECT count(*) FROM {table} WHERE embedding IS NULL")).scalar_one()
    )


def size_empty_columns(model: str, dim: int) -> bool:
    """Fresh-install auto-size: when NO rows
    exist in any vector table, retyping the columns to the configured
    embedder's dim is free — do it so the zero-config local tier works out of
    the box. Never touches a table with rows (reindex is the honest path
    there). Returns True when a retype happened."""
    tables = [t for t, _ in _TABLES] + list(_CACHE_TABLES)
    with admin_engine.begin() as conn:
        dims = {t: _col_dim(conn, t) for t in tables}
        if all(d == dim for d in dims.values()):
            return False
        counts = {
            t: int(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()) for t in tables
        }
        if any(counts.values()):
            return False
        for table in tables:
            if dims[table] == dim:
                continue
            if table in _INDEX:
                conn.execute(text(f"DROP INDEX IF EXISTS {_INDEX[table]}"))
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({dim})"))
            if table in _INDEX:
                conn.execute(
                    text(
                        f"CREATE INDEX {_INDEX[table]} ON {table} "
                        "USING hnsw (embedding vector_cosine_ops)"
                    )
                )
        embedding_meta.set_meta(conn, model, dim)
    return True


def run_reindex(embedder: Embedder, batch: int = 64) -> int:
    model, dim = embedding_meta.identity(embedder)
    with admin_engine.begin() as conn:
        stored = embedding_meta.read_meta(conn)
        dims = {t: _col_dim(conn, t) for t in [t for t, _ in _TABLES] + list(_CACHE_TABLES)}
        pending = sum(_pending(conn, t) for t, _ in _TABLES)

    if stored == (model, dim) and all(d == dim for d in dims.values()) and pending == 0:
        print(f"already indexed with {model} (dim {dim}) — nothing to do")
        return 0

    if pending == 0 or any(d != dim for d in dims.values()):
        # Phase A — one transaction, so "any NULLs exist" is all-or-nothing. Runs even
        # with pending rows when the dim changes: those NULLs are unembedded certified
        # answers, not a resumable reindex at the target dim (phase B can't cast a
        # new-dim vector into the old columns).
        with admin_engine.begin() as conn:
            # Active ⟹ embedded is a CHECK constraint now, so the marking
            # UPDATE must demote FIRST — during a reindex an active answer
            # genuinely cannot match, and saying so is the point of the state.
            conn.execute(
                text(
                    "UPDATE certified_answer SET status = 'pending_embedding' "
                    "WHERE status = 'active'"
                )
            )
            for table in _CACHE_TABLES:
                # Cache rows are never re-embedded in place — empty them (they
                # rebuild lazily under the new embedder) and retype if needed.
                conn.execute(text(f"DELETE FROM {table}"))
                if dims[table] != dim:
                    conn.execute(
                        text(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({dim})")
                    )
            for table, _ in _TABLES:
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN embedding DROP NOT NULL"))
                conn.execute(text(f"UPDATE {table} SET embedding = NULL"))
                if dims[table] != dim:
                    # Retype needs the dependent index gone and every value NULL
                    # (a filled vector(N) won't cast to vector(M)).
                    conn.execute(text(f"DROP INDEX {_INDEX[table]}"))
                    conn.execute(
                        text(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector({dim})")
                    )
                    conn.execute(
                        text(
                            f"CREATE INDEX {_INDEX[table]} ON {table} "
                            "USING hnsw (embedding vector_cosine_ops)"
                        )
                    )
        print(f"marked all rows for re-embedding with {model} (dim {dim})")
    else:
        print(f"resuming: {pending} row(s) still pending")

    # Phase B — a committed transaction per batch; rerun picks up remaining NULLs.
    for table, text_col in _TABLES:
        done = 0
        while True:
            with admin_engine.begin() as conn:
                if table == "certified_answer":
                    # A template's match anchor is its first
                    # sample-bound question, never the placeholder text.
                    rows = conn.execute(
                        text(
                            "SELECT id, question, slots, sample_bindings "
                            "FROM certified_answer WHERE embedding IS NULL LIMIT :n"
                        ),
                        {"n": batch},
                    ).all()
                    texts = [_anchor_text(r[1], r[2], r[3]) for r in rows]
                else:
                    rows = conn.execute(
                        text(
                            f"SELECT id, {text_col} FROM {table} WHERE embedding IS NULL LIMIT :n"
                        ),
                        {"n": batch},
                    ).all()
                    texts = [r[1] for r in rows]
                if not rows:
                    break
                vectors = _embed_batched(embedder, texts)
                for row, vec in zip(rows, vectors, strict=True):
                    conn.execute(
                        text(f"UPDATE {table} SET embedding = CAST(:e AS vector) WHERE id = :i"),
                        {"e": _vec(vec), "i": row[0]},
                    )
            done += len(rows)
        print(f"{table}: re-embedded {done} row(s)")

    # Phase C — the flip commits the reindex: meta LAST, in the same transaction
    # that restores NOT NULL, so the guard never accepts a half-reindexed store.
    with admin_engine.begin() as conn:
        for table in _NOT_NULL_TABLES:
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN embedding SET NOT NULL"))
        # Reindex IS the sanctioned recovery — every answer that now has a
        # vector goes back to matchable. (One that still has none kept its NULL
        # because embedding failed for it; it stays visibly pending.)
        promoted = conn.execute(
            text(
                "UPDATE certified_answer SET status = 'active' "
                "WHERE status = 'pending_embedding' AND embedding IS NOT NULL"
            )
        ).rowcount
        embedding_meta.set_meta(conn, model, dim)
    if promoted:
        print(f"certified_answer: {promoted} answer(s) promoted back to active (matchable again)")
    print(f"reindex complete: {model} (dim {dim})")
    return 0
