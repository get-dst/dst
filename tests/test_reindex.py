"""`dst reindex` — retype pgvector columns + re-embed (scratch DB).

The smoke test flips the shared test-DB tables 1024→256 and back, restoring
1024 in the fixture teardown so the rest of the suite is untouched.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from services.certify import store as certify_store
from services.config import settings
from services.context import store as context_store
from services.contracts.fakes import HashEmbedder
from services.db import embedding_meta
from services.db.reindex import _col_dim, run_reindex
from services.db.session import admin_engine, org_session


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


class SmallHash(HashEmbedder):
    dim = 256


def _dim(table: str) -> int:
    with admin_engine.connect() as c:
        return _col_dim(c, table)


def _nulls(table: str) -> int:
    with admin_engine.connect() as c:
        return int(
            c.execute(text(f"SELECT count(*) FROM {table} WHERE embedding IS NULL")).scalar_one()
        )


_CHUNK = "Churn means 90 days without an order."
_QUESTION = "How many customers churned last quarter?"


@pytest.fixture()
def seeded_org() -> Iterator[str]:
    """A scratch org holding one context chunk + one certified answer at dim 1024,
    with the meta row claimed by HashEmbedder. Teardown reindexes the shared
    tables back to 1024 if a test left them retyped, then removes everything."""
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))
        oid = c.execute(text("INSERT INTO org (name) VALUES ('Rdx') RETURNING id")).scalar_one()
    emb = HashEmbedder()
    with org_session(oid) as s:
        embedding_meta.guard_write(s, emb)
        context_store.upsert_chunks(s, "lensR", "note:r", [_CHUNK], emb.embed([_CHUNK]))
        certify_store.create(s, "lensR", _QUESTION, "SELECT 1", emb.embed([_QUESTION])[0], "test")
    try:
        yield str(oid)
    finally:
        if any(_dim(t) != 1024 for t in ("context_chunk", "certified_answer", "router_anchor")):
            run_reindex(HashEmbedder())
        with admin.begin() as c:
            c.execute(text("DELETE FROM embedding_meta"))
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})  # cascades rows
        admin.dispose()


@needs_db
def test_reindex_empties_router_anchor_cache(seeded_org: str) -> None:
    """Reindex never re-embeds cache rows — it empties router_anchor
    (rebuilt lazily by publish/read) and retypes its column with the rest."""
    from services.router import CoverageProfile, anchor_store

    with org_session(seeded_org) as s:
        anchor_store.sync(
            s, CoverageProfile(lens="lensR", anchors=["churned customers"]), HashEmbedder()
        )
    small = SmallHash()
    assert run_reindex(small) == 0
    assert _dim("router_anchor") == 256
    with admin_engine.connect() as c:
        left = int(c.execute(text("SELECT count(*) FROM router_anchor")).scalar_one())
    assert left == 0
    # The cache rebuilds lazily under the new embedder — sync is all it takes.
    with org_session(seeded_org) as s:
        anchor_store.sync(s, CoverageProfile(lens="lensR", anchors=["churned customers"]), small)
        n = int(s.execute(text("SELECT count(*) FROM router_anchor")).scalar_one())
    assert n == 1
    # Restore the shared tables for the rest of the suite.
    assert run_reindex(HashEmbedder()) == 0


@needs_db
def test_reindex_1024_to_256_and_back(seeded_org: str) -> None:
    small = SmallHash()
    assert run_reindex(small) == 0

    assert _dim("context_chunk") == 256
    assert _dim("certified_answer") == 256
    assert _nulls("context_chunk") == 0 and _nulls("certified_answer") == 0
    with admin_engine.connect() as c:
        assert embedding_meta.read_meta(c) == ("SmallHash", 256)

    # End-to-end at the new dim: search finds the seeded rows, writes are unblocked.
    with org_session(seeded_org) as s:
        hits = context_store.search(s, "lensR", small.embed([_CHUNK])[0], k=1)
        assert hits and hits[0].text == _CHUNK and hits[0].score > 0.99
        chits = certify_store.search(s, "lensR", small.embed([_QUESTION])[0], k=1)
        assert chits and chits[0].answer.sql == "SELECT 1"
        embedding_meta.guard_write(s, small)  # the old 1024 embedder would now raise

    # Idempotent: a second run is a no-op.
    assert run_reindex(small) == 0
    assert _dim("context_chunk") == 256

    # And back — the teardown also depends on this path staying green.
    assert run_reindex(HashEmbedder()) == 0
    assert _dim("context_chunk") == 1024 and _dim("certified_answer") == 1024
    with admin_engine.connect() as c:
        assert embedding_meta.read_meta(c) == ("HashEmbedder", 1024)
    with org_session(seeded_org) as s:
        hits = context_store.search(s, "lensR", HashEmbedder().embed([_CHUNK])[0], k=1)
        assert hits and hits[0].score > 0.99


@needs_db
def test_reindex_backfills_unembedded_certified_answers(seeded_org: str) -> None:
    """An answer certified without an embedder (NULL embedding, migration 0027) is
    invisible to search until the next reindex embeds it — and the invariant makes that
    state visible while it lasts: it sits in 'pending_embedding' and reindex,
    the sanctioned recovery, promotes it back to active."""
    emb = HashEmbedder()
    question = "What is the average order value?"
    with org_session(seeded_org) as s:
        certify_store.create(s, "lensR", question, "SELECT 2", None, "review")
        stored = {a.question: a.status for a in certify_store.list_for_lens(s, "lensR")}
        assert stored[question] == "pending_embedding"
        hits = certify_store.search(s, "lensR", emb.embed([question])[0], k=5)
        assert all(h.answer.question != question for h in hits)  # unembedded → unmatched

    assert run_reindex(emb) == 0

    assert _nulls("certified_answer") == 0
    with org_session(seeded_org) as s:
        assert {a.status for a in certify_store.list_for_lens(s, "lensR")} == {"active"}
        hits = certify_store.search(s, "lensR", emb.embed([question])[0], k=1)
        assert hits and hits[0].answer.question == question and hits[0].score > 0.99


@needs_db
def test_reindex_resumes_from_pending_nulls(seeded_org: str) -> None:
    """A crash mid-reindex leaves NULL embeddings; a rerun finishes just those."""
    with admin_engine.begin() as c:
        c.execute(text("ALTER TABLE context_chunk ALTER COLUMN embedding DROP NOT NULL"))
        c.execute(text("UPDATE context_chunk SET embedding = NULL"))

    assert run_reindex(HashEmbedder()) == 0

    assert _nulls("context_chunk") == 0
    with admin_engine.connect() as c:
        notnull = c.execute(
            text(
                "SELECT attnotnull FROM pg_attribute "
                "WHERE attrelid = 'context_chunk'::regclass AND attname = 'embedding'"
            )
        ).scalar_one()
        assert notnull is True
        assert embedding_meta.read_meta(c) == ("HashEmbedder", 1024)
    # The resumed row round-trips on search again.
    with org_session(seeded_org) as s:
        hits = context_store.search(s, "lensR", HashEmbedder().embed([_CHUNK])[0], k=1)
        assert hits and hits[0].text == _CHUNK and hits[0].score > 0.99
