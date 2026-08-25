"""Certified answers: certification flows through the pipeline (LLM skipped) +
the per-lens store round-trips with vector search."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from services.certify import store
from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import ScriptedLLM
from services.contracts.response import CertifiedProvenance
from services.db.session import org_session
from services.lenses.demo import jaffle_customer_value
from services.llm.registry import ResolvedModel
from services.runtime.answer import AnswerComposer
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import run_query


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


def test_certified_answer_skips_llm_and_flags_certification() -> None:
    # FixedSQLGenerator serves an approved SQL; only the composer calls the LLM.
    composer_llm = ScriptedLLM(["There are some customers."])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=DuckDBConnector(settings.duckdb_jaffle_path),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(composer_llm),
        certification="certified",
    )
    assert res.trace.status == "ok"
    assert res.response.certification == "certified"
    assert res.trace.certification == "certified"
    assert res.response.sql and "customers" in res.response.sql.lower()
    # generation consumed no LLM tokens — only the single compose call did
    assert res.trace.ai_input_tokens == 1


def test_certified_provenance_rides_on_the_response() -> None:
    composer_llm = ScriptedLLM(["There are some customers."])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=DuckDBConnector(settings.duckdb_jaffle_path),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(composer_llm),
        certification="certified",
        certified_match="exact",
        certified_provenance=CertifiedProvenance(
            cert_id="cert-1", certified_by="maija", certified_at="2026-06-01T00:00:00+00:00"
        ),
    )
    prov = res.response.certified_provenance
    assert prov is not None
    assert (prov.cert_id, prov.certified_by) == ("cert-1", "maija")
    # The match band rides to the caller so an agent can branch on it.
    assert res.response.certified_match == "exact"


def test_certified_match_band_flows_from_assembly() -> None:
    # Select_generators names which band served — "exact" for the strong
    # page binding, the lookup's band for a certified answer, None for generation.
    from services.certify.store import CertifiedAnswer
    from services.contracts.lens_config import LensConfig
    from services.runtime.assembly import AssembledInputs, select_generators

    model = jaffle_customer_value()
    config = LensConfig(name="t", display_name="T")
    base = dict(model=model, prose=[], certified_sql=None, data_as_of=None, counts={})

    def _select(assembled: AssembledInputs):
        _gen, _esc, mode, match = select_generators(
            assembled, ResolvedModel(ScriptedLLM(), "fake", "m"), config=config
        )
        return mode, match

    page = AssembledInputs(  # type: ignore[arg-type]
        **{**base, "certified_sql": "SELECT 1"}, certified=None, certification="none"
    )
    assert _select(page) == ("certified", "exact")

    answer = CertifiedAnswer(
        id="c1",
        lens="t",
        question="q",
        sql="SELECT 1",
        created_by="maija",
        created_at="2026-06-01",
        status="active",
    )
    equiv = AssembledInputs(  # type: ignore[arg-type]
        **base, certified=answer, certification="certified", certified_match="equivalent"
    )
    assert _select(equiv) == ("certified", "equivalent")

    plain = AssembledInputs(**base, certified=None, certification="assisted")  # type: ignore[arg-type]
    assert _select(plain) == ("assisted", None)


def test_certified_equivalence_gate() -> None:
    # F8c: a near-miss certified hit is promoted to a deterministic serve only when a
    # cheap model confirms the incoming question is a paraphrase of the approved one.
    from services.api.query import _certified_equivalent

    yes = ScriptedLLM(["yes"])
    no = ScriptedLLM(["no — different window"])
    assert _certified_equivalent(yes, "m", "revenue last quarter?", "what was revenue last quarter")
    assert not _certified_equivalent(no, "m", "revenue last year?", "revenue last quarter?")


def _vec(i: int) -> list[float]:
    v = [0.0] * 1024
    v[i] = 1.0
    return v


def _org() -> object:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        return c.execute(
            text("INSERT INTO org (name) VALUES ('CertApi') RETURNING id")
        ).scalar_one()


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM certified_answer WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_certified_store_search_and_delete() -> None:
    org = _org()
    try:
        with org_session(org) as s:
            id_a = store.create(s, "churn", "how many active?", "SELECT 1", _vec(0), "admin")
            store.create(s, "churn", "revenue?", "SELECT 2", _vec(1), "admin")
        with org_session(org) as s:
            hits = store.search(s, "churn", _vec(0), k=5)
            assert hits and hits[0].answer.sql == "SELECT 1"
            assert hits[0].score > 0.99  # identical vector
            # Provenance rides on every hit
            assert hits[0].answer.created_by == "admin"
            assert hits[0].answer.created_at  # ISO timestamp
            listed = store.list_for_lens(s, "churn")
            assert len(listed) == 2
            assert all(a.created_at for a in listed)
        with org_session(org) as s:
            assert store.delete(s, id_a) == 1
            assert len(store.list_for_lens(s, "churn")) == 1
    finally:
        _cleanup(org)
