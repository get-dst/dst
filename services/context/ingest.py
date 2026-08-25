"""Ingest sources into a lens's context store: fetch -> chunk -> embed -> upsert.

Embeds in as few batched calls as possible (Voyage free tier is 3 RPM / 10K TPM
without a payment method), grouped under a per-call character budget.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from services.connectors.github import fetch_paths
from services.context import store
from services.contracts.protocols import Embedder
from services.db import embedding_meta

_CHAR_BUDGET = 28_000  # ~7K tokens/call, under the 10K TPM free-tier limit


def chunk_text(s: str, size: int = 1500, overlap: int = 150) -> list[str]:
    s = s.strip()
    if not s:
        return []
    if len(s) <= size:
        return [s]
    out: list[str] = []
    step = max(size - overlap, 1)
    i = 0
    while i < len(s):
        out.append(s[i : i + size])
        i += step
    return out


def _embed_batched(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    batch: list[str] = []
    size = 0
    for t in texts:
        if batch and size + len(t) > _CHAR_BUDGET:
            out.extend(embedder.embed(batch))
            batch, size = [], 0
        batch.append(t)
        size += len(t)
    if batch:
        out.extend(embedder.embed(batch))
    return out


def ingest_text(
    session: Session,
    lens: str,
    source: str,
    body: str,
    embedder: Embedder,
) -> dict[str, int]:
    """Ingest a single text doc (file upload or manual note) under `source`."""
    chunks = chunk_text(body)
    if not chunks:
        return {"chunks": 0}
    embedding_meta.guard_write(session, embedder)
    embeddings = _embed_batched(embedder, chunks)
    total = store.upsert_chunks(session, lens, source, chunks, embeddings)
    return {"chunks": total}


def ingest_github(
    session: Session,
    lens: str,
    repo: str,
    paths: list[str],
    embedder: Embedder,
    ref: str = "main",
    token: str | None = None,
) -> dict[str, int]:
    files = fetch_paths(repo, paths, ref=ref, token=token)
    items: list[tuple[str, str]] = []  # (source, chunk)
    for path, content in files:
        source = f"github:{repo}/{path}"
        for chunk in chunk_text(f"# {path}\n\n{content}"):
            items.append((source, chunk))
    if not items:
        return {"files": len(files), "chunks": 0}

    embedding_meta.guard_write(session, embedder)
    embeddings = _embed_batched(embedder, [c for _, c in items])

    by_source: dict[str, list[tuple[str, list[float]]]] = defaultdict(list)
    for (source, chunk), emb in zip(items, embeddings, strict=True):
        by_source[source].append((chunk, emb))

    total = 0
    for source, pairs in by_source.items():
        total += store.upsert_chunks(
            session, lens, source, [p[0] for p in pairs], [p[1] for p in pairs]
        )
    return {"files": len(files), "chunks": total}
