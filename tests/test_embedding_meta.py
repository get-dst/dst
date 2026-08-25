"""embedding_meta lifecycle + the write-path guard (scratch DB)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.contracts.fakes import HashEmbedder
from services.db import embedding_meta
from services.db.session import org_session


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


@pytest.fixture()
def org() -> Iterator[str]:
    """A scratch org with the global embedding_meta row cleared before and after —
    the guard tests must not see (or leave) claims from other suite tests."""
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))
        oid = c.execute(text("INSERT INTO org (name) VALUES ('Meta') RETURNING id")).scalar_one()
    try:
        yield str(oid)
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM embedding_meta"))
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
        admin.dispose()


class _Small(HashEmbedder):
    dim = 256


@needs_db
def test_first_write_claims_the_meta_row(org: str) -> None:
    emb = HashEmbedder()
    with org_session(org) as s:
        assert embedding_meta.read_meta(s) is None
        embedding_meta.guard_write(s, emb)
        assert embedding_meta.read_meta(s) == ("HashEmbedder", 1024)
    # …and the claim persists across sessions; a matching embedder keeps passing.
    with org_session(org) as s:
        embedding_meta.guard_write(s, emb)
        assert embedding_meta.read_meta(s) == ("HashEmbedder", 1024)


@needs_db
def test_mismatched_write_is_rejected_naming_reindex(org: str) -> None:
    with org_session(org) as s:
        embedding_meta.guard_write(s, HashEmbedder())
    with org_session(org) as s:
        with pytest.raises(embedding_meta.EmbeddingMismatchError, match="dst reindex"):
            embedding_meta.guard_write(s, _Small())
        # The rejected attempt must not have overwritten the claim.
        assert embedding_meta.read_meta(s) == ("HashEmbedder", 1024)


@needs_db
def test_guard_runs_on_the_ingest_write_path(org: str) -> None:
    from services.context.ingest import ingest_text

    with org_session(org) as s:
        ingest_text(s, "lensM", "note:m", "Churn means 90 days inactive.", HashEmbedder())
        assert embedding_meta.read_meta(s) == ("HashEmbedder", 1024)
    with org_session(org) as s:
        with pytest.raises(embedding_meta.EmbeddingMismatchError, match="dst reindex"):
            ingest_text(s, "lensM", "note:m2", "Another note.", _Small())


@needs_db
def test_identity_prefers_the_model_attribute() -> None:
    from services.context.openai_embedder import OpenAICompatEmbedder

    emb = OpenAICompatEmbedder("k", "http://x", "nomic-embed", dim=768)
    assert embedding_meta.identity(emb) == ("nomic-embed", 768)
    assert embedding_meta.identity(HashEmbedder()) == ("HashEmbedder", 1024)


class _Local384(HashEmbedder):
    """The local tier's shape: dim 384 vs the migrations' vector(1024)."""

    dim = 384
    model = "BAAI/bge-small-en-v1.5"


@needs_db
def test_fresh_install_column_mismatch_is_actionable_not_a_500(org: str) -> None:
    # Probe finding: empty meta + a 384-dim embedder used to CLAIM the row and
    # then explode mid-INSERT on the vector(1024) columns (raw psycopg 500).
    # The guard now checks the physical columns FIRST and names the cure.
    with org_session(org) as s:
        assert embedding_meta.read_meta(s) is None  # fresh: nothing claimed
        with pytest.raises(
            embedding_meta.EmbeddingMismatchError, match=r"vector\(1024\).*384.*reindex"
        ):
            embedding_meta.guard_write(s, _Local384())
        assert embedding_meta.read_meta(s) is None  # and nothing was claimed


@needs_db
def test_size_empty_columns_round_trip(org: str) -> None:
    from services.db.reindex import _col_dim, size_empty_columns

    admin = create_engine(settings.database_admin_url)
    with admin.connect() as c:
        counts = [
            c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in ("context_chunk", "certified_answer")
        ]
    if any(counts):
        admin.dispose()
        pytest.skip("shared test DB holds vectors — auto-size only acts on empty installs")
    try:
        assert size_empty_columns("BAAI/bge-small-en-v1.5", 384) is True
        with admin.connect() as c:
            assert _col_dim(c, "context_chunk") == 384
            assert _col_dim(c, "certified_answer") == 384
        assert size_empty_columns("BAAI/bge-small-en-v1.5", 384) is False  # idempotent
        # …and the guarded write path now accepts the matching local embedder.
        with org_session(org) as s:
            embedding_meta.guard_write(s, _Local384())
    finally:
        size_empty_columns("HashEmbedder", 1024)  # restore the suite's shape
    with admin.connect() as c:
        assert _col_dim(c, "certified_answer") == 1024
    admin.dispose()
