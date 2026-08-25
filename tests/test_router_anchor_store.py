"""Publish-time anchor embeddings: the request path embeds ONLY the
question.

The router_anchor rows are a write-through cache keyed (org, lens, anchor):
sync embeds only new anchors and drops stale ones; scoring runs in pgvector.
A CountingEmbedder proves the load-bearing claim — zero anchor-embedding calls
on a warmed request path — and the mismatch guard falls back to in-memory
embedding (loudly) instead of mixing models. All tests need Postgres.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from services.config import settings
from services.contracts.fakes import HashEmbedder
from services.db import embedding_meta
from services.db.session import org_session
from services.lenses import store
from services.lenses.demo import jaffle_customer_value_bundle
from services.router import CoverageProfile, anchor_store
from services.router.profiles import coverage_profile


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

pytestmark = needs_db


class CountingEmbedder(HashEmbedder):
    """HashEmbedder that counts how many TEXTS it was asked to embed."""

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        return super().embed(texts)


@pytest.fixture()
def org() -> uuid.UUID:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))
        org_id = c.execute(
            text("INSERT INTO org (name) VALUES ('AnchorT') RETURNING id")
        ).scalar_one()
    yield org_id
    with admin.begin() as c:
        c.execute(text("DELETE FROM router_anchor WHERE org_id = :o"), {"o": org_id})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org_id})
        c.execute(text("DELETE FROM embedding_meta"))
    admin.dispose()


def _profile(lens: str, anchors: list[str]) -> CoverageProfile:
    return CoverageProfile(lens=lens, anchors=anchors)


def _count(org_id: uuid.UUID, lens: str) -> int:
    with org_session(org_id) as s:
        return int(
            s.execute(
                text("SELECT count(*) FROM router_anchor WHERE lens = :l"), {"l": lens}
            ).scalar_one()
        )


def test_sync_embeds_once_then_reads_free(org: uuid.UUID) -> None:
    """First sync pays for every anchor; a re-sync with an unchanged profile
    embeds NOTHING — the steady state of the request path."""
    emb = CountingEmbedder()
    prof = _profile("finance", ["total revenue", "gross margin", "operating cost"])
    with org_session(org) as s:
        anchor_store.sync(s, prof, emb)
    assert emb.calls == 3
    assert _count(org, "finance") == 3
    with org_session(org) as s:
        anchor_store.sync(s, prof, emb)
    assert emb.calls == 3  # zero new embedding work


def test_sync_repairs_drift_embedding_only_the_delta(org: uuid.UUID) -> None:
    """A profile that gains one anchor and loses one (certified-def file drift,
    no publish) re-embeds only the new anchor and deletes the stale row."""
    emb = CountingEmbedder()
    with org_session(org) as s:
        anchor_store.sync(s, _profile("finance", ["total revenue", "gross margin"]), emb)
    with org_session(org) as s:
        anchor_store.sync(s, _profile("finance", ["total revenue", "churn rate"]), emb)
    assert emb.calls == 3  # 2 initial + 1 delta, never a full re-embed
    with org_session(org) as s:
        anchors = {
            r[0] for r in s.execute(text("SELECT anchor FROM router_anchor WHERE lens = 'finance'"))
        }
    assert anchors == {"total revenue", "churn rate"}


def test_scored_ranks_by_max_anchor_cosine(org: uuid.UUID) -> None:
    """A verbatim anchor hit scores ~1.0 for its lens and wins — the same recall
    signal Router.scored computes in memory, done in pgvector."""
    emb = HashEmbedder()
    with org_session(org) as s:
        anchor_store.sync(s, _profile("finance", ["total revenue", "gross margin"]), emb)
        anchor_store.sync(s, _profile("sales", ["pipeline coverage", "win rate"]), emb)
    qv = emb.embed(["pipeline coverage"])[0]
    with org_session(org) as s:
        ranked = anchor_store.scored(s, ["finance", "sales"], qv)
    assert [lens for lens, _ in ranked] == ["sales", "finance"]
    assert ranked[0][1] == pytest.approx(1.0, abs=1e-6)


def test_mismatched_embedder_raises_for_reindex(org: uuid.UUID) -> None:
    """Anchors written by one embedder must not silently mix with another —
    sync raises (the route path catches it and embeds in-memory, loudly)."""
    with org_session(org) as s:
        anchor_store.sync(s, _profile("finance", ["total revenue"]), HashEmbedder())

    class OtherModel(HashEmbedder):
        model = "other-model"

    with org_session(org) as s, pytest.raises(embedding_meta.EmbeddingMismatchError):
        anchor_store.sync(s, _profile("finance", ["total revenue"]), OtherModel())


def test_rls_isolates_anchor_rows_between_orgs(org: uuid.UUID) -> None:
    """FORCE RLS on router_anchor: another org's session sees nothing."""
    with org_session(org) as s:
        anchor_store.sync(s, _profile("finance", ["total revenue"]), HashEmbedder())
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        other = c.execute(
            text("INSERT INTO org (name) VALUES ('AnchorT2') RETURNING id")
        ).scalar_one()
    try:
        with org_session(other) as s:
            visible = int(s.execute(text("SELECT count(*) FROM router_anchor")).scalar_one())
        assert visible == 0
        assert _count(org, "finance") == 1
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": other})
        admin.dispose()


def test_delete_lens_cascades_anchor_rows(org: uuid.UUID) -> None:
    bundle = jaffle_customer_value_bundle()
    with org_session(org) as s:
        store.create_lens(s, bundle)
        anchor_store.sync(
            s, _profile(bundle.config.name, ["customer lifetime value"]), HashEmbedder()
        )
        assert store.delete_lens(s, bundle.config.name) == 1
    assert _count(org, bundle.config.name) == 0


def test_warm_prewarms_from_a_bundle(org: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    """The publish hook: warm() builds the coverage profile and stores every
    anchor, best-effort, using the configured embedder."""
    from services.llm import registry

    monkeypatch.setattr(registry, "resolve_embedder", lambda: HashEmbedder())
    bundle = jaffle_customer_value_bundle()
    with org_session(org) as s:
        anchor_store.warm(s, bundle)
    expected = len(coverage_profile(bundle).anchors)
    assert expected > 0
    assert _count(org, bundle.config.name) == expected


def test_request_path_embeds_only_the_question(
    org: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing claim, end to end through _route: with warmed
    anchors, one request = ONE embedding call (the question), zero anchor calls."""
    from services.api import route as route_mod
    from services.governance.credentials import CallerIdentity

    bundle = jaffle_customer_value_bundle()
    emb = CountingEmbedder()
    with org_session(org) as s:
        if not store.lens_exists(s, bundle.config.name):
            store.create_lens(s, bundle)
        store.publish(s, bundle.config.name)
        anchor_store.sync(s, coverage_profile(bundle), emb)
    warmed = emb.calls
    assert warmed > 0

    monkeypatch.setattr(route_mod, "_resolve_embedder", lambda: emb)
    monkeypatch.setattr(route_mod, "_resolve_decider", lambda: None)  # pure cosine path
    caller = CallerIdentity(org_id=org, name="t", groups=[], is_admin=True)
    decision = route_mod._route(caller, "how many customers do we have?")

    assert emb.calls == warmed + 1  # the question — and nothing else
    assert decision is not None  # route or decline, both fine: no anchor re-embed either way


def test_mismatch_falls_back_to_in_memory_routing(
    org: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swapped embedder (pending `dst reindex`) must not break /v1/query:
    _route falls back to in-memory anchor embedding for the request."""
    from services.api import route as route_mod
    from services.governance.credentials import CallerIdentity

    bundle = jaffle_customer_value_bundle()
    with org_session(org) as s:
        if not store.lens_exists(s, bundle.config.name):
            store.create_lens(s, bundle)
        store.publish(s, bundle.config.name)
        anchor_store.sync(s, coverage_profile(bundle), HashEmbedder())

    class OtherModel(CountingEmbedder):
        model = "other-model"

    emb = OtherModel()
    monkeypatch.setattr(route_mod, "_resolve_embedder", lambda: emb)
    monkeypatch.setattr(route_mod, "_resolve_decider", lambda: None)
    caller = CallerIdentity(org_id=org, name="t", groups=[], is_admin=True)
    decision = route_mod._route(caller, "how many customers do we have?")

    assert decision is not None
    # In-memory fallback embedded the anchors + the question for this request.
    assert emb.calls == len(coverage_profile(bundle).anchors) + 1


# ── cross-claim detection ────────────────────────────────────────────────────


@needs_db
def test_cross_claims_names_inseparable_lens_pairs(org: uuid.UUID) -> None:
    """Two lenses sharing a verbatim anchor are inseparable by embedding — a
    modelling problem to report at apply time, never a runtime coin flip."""
    emb = HashEmbedder()
    with org_session(org) as s:
        anchor_store.sync(s, _profile("volume", ["gross booking value", "total volume"]), emb)
        anchor_store.sync(s, _profile("fees", ["gross booking value", "fee take rate"]), emb)
        anchor_store.sync(s, _profile("hr", ["headcount by team"]), emb)
        pairs = anchor_store.cross_claims(s)
    assert [(a, b) for a, b, _ in pairs] == [("fees", "volume")]
    assert pairs[0][2] == pytest.approx(1.0, abs=1e-6)


@needs_db
def test_separable_lenses_report_no_cross_claims(org: uuid.UUID) -> None:
    emb = HashEmbedder()
    with org_session(org) as s:
        anchor_store.sync(s, _profile("finance", ["total revenue"]), emb)
        anchor_store.sync(s, _profile("hr", ["headcount by team"]), emb)
        assert anchor_store.cross_claims(s) == []
