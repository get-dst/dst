"""Active ⟹ embedded — a certified answer that can never match SAYS so.

The bug class: an answer lands ``status='active'`` with ``embedding IS NULL``
because the install's embedder was absent when the apply ran. Certified
matching is pgvector cosine, so a NULL vector can never be returned — the
corpus is active, verified, invisible, and every surface reports success.

The degraded write still lands (a human APPROVE must not dead-end on provider
config) but it lands as ``pending_embedding``: a CHECK constraint
makes the lying variant unrepresentable, and `dst apply` / `dst test`
name the condition and the recovery (`dst reindex`) for as long as it lasts.
"""

from __future__ import annotations

import argparse

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.config import settings
from services.contracts.fakes import HashEmbedder
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
from services.lenses.repo import render_lens_repo
from services.llm.registry import ResolvedModel
from services.project.apply import DEGRADED
from services.semantic.files import render_semantic_files

client = TestClient(app)


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


class BrokenEmbedder(HashEmbedder):
    """An embedder that is present, configured, and down.
    Keeps HashEmbedder's identity so embedding_meta stays claimable either way."""

    model = "HashEmbedder"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding provider unreachable")


@pytest.fixture()
def org(monkeypatch):
    from services.project import apply as apply_engine

    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    # Pin the smart tier unresolvable so the certify self-test degrades to its
    # loud skip instead of dialing an ambient provider key.
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))  # claim cleanly with HashEmbedder
        oid = c.execute(text("INSERT INTO org (name) VALUES ('P27T') RETURNING id")).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    with org_session(oid) as s:
        connection_store.create_connection(
            s, "jaffle", "duckdb", {"path": settings.duckdb_jaffle_path}, None
        )
        s.commit()
    yield oid, {"Authorization": f"Bearer {raw}"}
    with admin.begin() as c:
        for table in (
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "semantic_asset",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
        c.execute(text("DELETE FROM embedding_meta"))
    admin.dispose()


_Q = "How many repeat customers are there?"
_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 1"
_Q2 = "How many customers in total?"
_SQL2 = "SELECT count(*) FROM customers"
_WARNING = "have no embedding — they can never match; run `dst reindex`"


def _files(answers: list[dict[str, object]]) -> dict[str, str]:
    entities, definitions = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions))
    for path, content in render_lens_repo(jaffle_customer_value_bundle()).items():
        files[f"lenses/customer_value/{path}"] = content
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        answers, sort_keys=False, allow_unicode=True
    )
    # eval_gate: off — this file pins the ORDER of embedding degradations
    # (sorted last); under the block default a keyless env adds its own
    # "gate SKIPPED" degradation line, which is not what's under test here.
    cfg = yaml.safe_load(files["lenses/customer_value/lens.yaml"])
    cfg["eval_gate"] = "off"
    files["lenses/customer_value/lens.yaml"] = yaml.safe_dump(cfg, sort_keys=False)
    return files


def _apply(headers: dict[str, str], files: dict[str, str]) -> dict[str, object]:
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    return next(e for e in out if e.get("lens") == "customer_value")


def _rows(oid: str) -> list[tuple[str, str, bool]]:
    """(question, status, embedded) straight from the table — the assertions
    must never read the condition through the same status column under test."""
    admin = create_engine(settings.database_admin_url)
    with admin.connect() as c:
        rows = c.execute(
            text(
                "SELECT question, status, embedding IS NOT NULL FROM certified_answer "
                "WHERE org_id = :o ORDER BY question"
            ),
            {"o": oid},
        ).all()
    admin.dispose()
    return [(r[0], r[1], bool(r[2])) for r in rows]


# ── the write ────────────────────────────────────────────────────────────────


@needs_db
def test_working_embedder_lands_active_and_embedded(org) -> None:
    oid, headers = org
    row = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert "certified answers: created 1, updated 0, unchanged 0" in row["applied"]
    assert not any(_WARNING in w for w in row["warnings"])
    assert _rows(oid) == [(_Q, "active", True)]
    with org_session(oid) as s:  # embedded ⇒ matchable, the whole point
        hits = certify_store.search(s, "customer_value", HashEmbedder().embed([_Q])[0], k=1)
        assert hits and hits[0].answer.question == _Q


@needs_db
def test_raising_embedder_lands_pending_never_a_silent_active(org, monkeypatch) -> None:
    """The embedder is configured but broken: the apply must not sink, and the
    row must not lie."""
    from services.project import apply as apply_engine

    oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", BrokenEmbedder)
    row = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))

    assert row["action"] != "rejected" and not row["errors"]
    assert any("embedding provider failed" in w for w in row["warnings"])
    assert any(_WARNING in w for w in row["warnings"])
    assert _rows(oid) == [(_Q, "pending_embedding", False)]


@needs_db
def test_no_provider_lands_pending_and_stays_visible_on_later_applies(org, monkeypatch) -> None:
    """The degradation is permanent, not first-apply-only: without this, only
    the FIRST apply warns and every apply after it reports success while the
    corpus stays unmatchable."""
    from services.project import apply as apply_engine

    _oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", lambda: None)
    first = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert any("stored unembedded" in w for w in first["warnings"])
    assert any(_WARNING in w for w in first["warnings"])

    # An identical re-apply skips the publish path — the standing corpus
    # degradation must survive the skip and keep warning.
    second = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert second["action"] == "unchanged"
    assert any(_WARNING in w for w in second["warnings"])


@needs_db
def test_apply_warns_on_a_preexisting_unembedded_row(org) -> None:
    """The warning is corpus-wide, not fresh-only: a healthy apply carrying a
    healthy answer still names the sibling row that can never match."""
    oid, headers = org
    _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    with org_session(oid) as s:  # certified while the provider was gone
        certify_store.create(s, "customer_value", _Q2, _SQL2, None, "review")

    row = _apply(headers, _files([{"question": _Q, "sql": _SQL}, {"question": _Q2, "sql": _SQL2}]))
    # A corpus that can never match is a DEGRADATION — prefixed and sorted
    # last so it survives a wall of per-definition lint above it.
    assert row["warnings"][-1] == f"{DEGRADED}1 certified answers {_WARNING}"
    assert ("How many customers in total?", "pending_embedding", False) in _rows(oid)


@needs_db
def test_authored_status_round_trips_so_pending_never_churns(org) -> None:
    """pending_embedding is derived state: the file keeps carrying the INTENT
    (`status: active`) and re-applying it is not an edit."""
    oid, headers = org
    with org_session(oid) as s:
        certify_store.create(s, "customer_value", _Q, _SQL, None, "review")
    row = _apply(headers, _files([{"question": _Q, "sql": _SQL, "status": "active"}]))
    assert "certified answers: created 0, updated 0, unchanged 1" in row["applied"]
    assert _rows(oid) == [(_Q, "pending_embedding", False)]
    # …and the pull renders the intent, never the derived state (or the next
    # apply would reject its own export).
    with org_session(oid) as s:
        answers = certify_store.list_for_lens(s, "customer_value")
    tree = render_lens_repo(jaffle_customer_value_bundle(), certified_answers=answers)
    assert "status" not in yaml.safe_load(tree["certified_answers.yaml"])[0]


# ── the invariant, structurally ──────────────────────────────────────────────


@needs_db
def test_store_create_demotes_and_the_db_refuses_active_without_a_vector(org) -> None:
    oid, _headers = org
    with org_session(oid) as s:
        certify_store.create(s, "customer_value", _Q, _SQL, None, "review")
    assert _rows(oid) == [(_Q, "pending_embedding", False)]

    # The store's own re-activate path demotes rather than lies…
    with org_session(oid) as s:
        answer = certify_store.list_for_lens(s, "customer_value")[0]
        certify_store.update(s, answer.id, sql=_SQL, status="active")
    assert _rows(oid) == [(_Q, "pending_embedding", False)]

    # …and any path that skips the store hits the constraint instead of landing.
    with pytest.raises(IntegrityError, match="certified_answer_active_embedded"):
        with org_session(oid) as s:
            s.execute(text("UPDATE certified_answer SET status = 'active' WHERE embedding IS NULL"))
            s.commit()


@needs_db
def test_an_arriving_vector_promotes_a_pending_answer(org) -> None:
    """The store's other half: an embedding landing through update (the
    anchor re-embed) makes the answer matchable, so it goes back to active."""
    oid, _headers = org
    with org_session(oid) as s:
        certify_store.create(s, "customer_value", _Q, _SQL, None, "review")
        answer = certify_store.list_for_lens(s, "customer_value")[0]
        certify_store.update(s, answer.id, sql=_SQL, embedding=HashEmbedder().embed([_Q])[0])
    assert _rows(oid) == [(_Q, "active", True)]


# ── `dst test` ───────────────────────────────────────────────────────────


@needs_db
def test_dst_test_names_the_unembedded_answers(org, monkeypatch, capsys) -> None:
    """The sweep TESTS them (their oracle is fine) but must never let a corpus
    that cannot match read as green silence."""
    from services.contracts.fakes import ScriptedLLM
    from services.evals import certified_suite
    from services.evals.certified_suite import CertifiedCaseResult, CertifiedSuiteOutcome
    from services.runtime import assembly

    oid, headers = org
    _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    with org_session(oid) as s:
        certify_store.create(s, "customer_value", _Q2, _SQL2, None, "review")

    monkeypatch.setattr(
        "services.llm.registry.resolve", lambda _ref: ResolvedModel(ScriptedLLM([]), "fake", "m")
    )
    monkeypatch.setattr(
        assembly, "eval_harness", lambda *_a, **_k: (lambda a: None, lambda a: None)
    )
    monkeypatch.setattr(
        certified_suite,
        "run_certified_suite",
        lambda *, answers, lens, **_k: CertifiedSuiteOutcome(
            lens,
            [CertifiedCaseResult(a.id, a.question, {}, {}, passed=True) for a in answers],
        ),
    )
    from services.cli.main import _test

    assert _test(argparse.Namespace(lens="customer_value", all=False, org="P27T")) == 0
    out = capsys.readouterr().out
    assert f"customer_value: 1 certified answers {_WARNING}" in out
    # Unembedded ⇒ untestable was never true: the sweep still ran both.
    assert f"PASS  P27T/customer_value: {_Q2}" in out
