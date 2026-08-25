"""Per-lens vector store retrieval + namespace isolation (offline)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.context import store
from services.contracts.fakes import HashEmbedder
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


@needs_db
def test_context_namespace_isolation() -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        org = c.execute(text("INSERT INTO org (name) VALUES ('Ctx') RETURNING id")).scalar_one()
    try:
        emb = HashEmbedder()
        a_texts = ["alpha lifetime value", "beta order status"]
        with org_session(org) as s:
            store.upsert_chunks(s, "lensA", "github:a", a_texts, emb.embed(a_texts))
            store.upsert_chunks(s, "lensB", "github:b", ["gamma other"], emb.embed(["gamma other"]))

        with org_session(org) as s:
            qvec = emb.embed(["alpha lifetime value"])[0]
            hits_a = store.search(s, "lensA", qvec, k=5)
            hits_b = store.search(s, "lensB", qvec, k=5)

        # lensA sees only its own chunks (per-lens namespace)
        assert len(hits_a) == 2
        assert {h.source for h in hits_a} == {"github:a"}
        # exact-match query ranks the matching chunk first
        assert "alpha" in hits_a[0].text
        # lensB never sees lensA's chunks
        assert all(h.source == "github:b" for h in hits_b)
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})  # cascades
