"""Per-lens pgvector store. Every read/write is filtered by lens (namespace) + RLS."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

# Last live context-serving failure (question embed or chunk search). Serving is
# fail-open by design — queries proceed context-free — so without this flag a bad
# embedding key degrades every answer with nothing but a buried log line. Set on
# failure, cleared on the next success, surfaced by /ready's embedding status.
LAST_SERVING_ERROR: dict[str, str] = {}


def _vec(values: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


def delete_source(session: Session, lens: str, source: str) -> int:
    res = session.execute(
        text("DELETE FROM context_chunk WHERE lens = :l AND source = :s"),
        {"l": lens, "s": source},
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def list_sources(session: Session, lens: str) -> list[dict[str, object]]:
    """Per-source chunk counts + freshness for a lens's context layer."""
    rows = session.execute(
        text(
            "SELECT source, count(*), max(created_at) FROM context_chunk "
            "WHERE lens = :l GROUP BY source ORDER BY source"
        ),
        {"l": lens},
    ).all()
    return [
        {"source": r[0], "chunks": int(r[1]), "last_indexed": r[2].isoformat() if r[2] else None}
        for r in rows
    ]


def overview(session: Session) -> list[dict[str, object]]:
    """Org-wide context layer: one row per (kind, source, lens) with chunk count and
    freshness. The kind is the source-key prefix (github/notion/slack/linear/gdrive/
    note/upload). The caller aggregates these into the cross-lens 'what the model
    knows' view."""
    rows = session.execute(
        text(
            "SELECT split_part(source, ':', 1) AS kind, source, lens, "
            "count(*) AS chunks, max(created_at) AS last_indexed "
            "FROM context_chunk GROUP BY kind, source, lens"
        )
    ).all()
    return [
        {
            "kind": r[0],
            "source": r[1],
            "lens": r[2],
            "chunks": int(r[3]),
            "last_indexed": r[4].isoformat() if r[4] else None,
        }
        for r in rows
    ]


def list_chunks(
    session: Session, lens: str, source: str | None = None, limit: int = 300
) -> list[dict[str, object]]:
    """The actual chunk texts for a lens (optionally one source) — for inspection."""
    where = "lens = :l" + (" AND source = :s" if source else "")
    params: dict[str, object] = {"l": lens, "k": limit}
    if source:
        params["s"] = source
    rows = session.execute(
        text(
            f"SELECT id, source, text FROM context_chunk WHERE {where} "
            "ORDER BY source, created_at LIMIT :k"
        ),
        params,
    ).all()
    return [{"id": str(r[0]), "source": r[1], "text": r[2]} for r in rows]


def upsert_chunks(
    session: Session,
    lens: str,
    source: str,
    chunks: list[str],
    embeddings: list[list[float]],
) -> int:
    delete_source(session, lens, source)
    for chunk, emb in zip(chunks, embeddings, strict=True):
        session.execute(
            text(
                "INSERT INTO context_chunk (org_id, lens, source, text, embedding) VALUES "
                "(NULLIF(current_setting('app.current_org', true), '')::uuid, "
                ":l, :s, :t, CAST(:e AS vector))"
            ),
            {"l": lens, "s": source, "t": chunk, "e": _vec(emb)},
        )
    return len(chunks)


@dataclass
class Hit:
    text: str
    source: str
    score: float


def search(session: Session, lens: str, query_embedding: list[float], k: int = 6) -> list[Hit]:
    rows = session.execute(
        text(
            "SELECT text, source, 1 - (embedding <=> CAST(:q AS vector)) AS score "
            "FROM context_chunk WHERE lens = :l "
            "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
        ),
        {"q": _vec(query_embedding), "l": lens, "k": k},
    ).all()
    return [Hit(text=r[0], source=r[1], score=float(r[2])) for r in rows]
