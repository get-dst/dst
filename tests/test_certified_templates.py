"""Templates as certified answers — yaml round-trip, the
apply gate helper (validate + render before every downstream gate), and the
match-anchor rule (first sample-bound question). DB-free."""

from __future__ import annotations

import yaml

from services.certify.store import CertifiedAnswer
from services.lenses.demo import jaffle_customer_value, jaffle_customer_value_bundle
from services.lenses.repo import render_lens_repo
from services.llm.registry import ResolvedModel
from services.project.apply import _anchor_question, _template_gate_sql

_SLOTS = {"period": {"type": "date_range"}}
_TEMPLATE_SQL = (
    "SELECT count(*) AS n FROM customers "
    "WHERE first_order >= {period.start} AND first_order < {period.end}"
)


def _template_answer() -> CertifiedAnswer:
    return CertifiedAnswer(
        id="t1",
        lens="customer_value",
        question="how many new customers in {period}?",
        sql=_TEMPLATE_SQL,
        created_by="maija",
        created_at="2026-08-01T00:00:00+00:00",
        slots=_SLOTS,
        sample_bindings=[{"period": "2026-Q2"}],
    )


def test_template_keys_round_trip_through_the_yaml() -> None:
    plain = CertifiedAnswer(
        id="p1",
        lens="customer_value",
        question="how many customers?",
        sql="SELECT count(*) FROM customers",
        created_by="maija",
        created_at="2026-08-01T00:00:00+00:00",
    )
    tree = render_lens_repo(
        jaffle_customer_value_bundle(), certified_answers=[_template_answer(), plain]
    )
    pairs = yaml.safe_load(tree["certified_answers.yaml"])
    template, rendered_plain = pairs[0], pairs[1]
    assert template["slots"] == _SLOTS
    assert template["sample_bindings"] == [{"period": "2026-Q2"}]
    # Plain pairs stay byte-identical: no template keys appear on them.
    assert "slots" not in rendered_plain and "sample_bindings" not in rendered_plain


def test_gate_helper_renders_the_first_sample_binding() -> None:
    model = jaffle_customer_value()
    answer = {
        "question": "how many new customers in {period}?",
        "sql": _TEMPLATE_SQL,
        "slots": _SLOTS,
        "sample_bindings": [{"period": "2026-Q2"}],
    }
    gated, errors = _template_gate_sql(answer, str(answer["question"]), _TEMPLATE_SQL, model)
    assert errors == [] and gated is not None
    assert "'2026-04-01'" in gated and "'2026-07-01'" in gated and "{" not in gated


def test_gate_helper_rejects_bad_templates_with_named_errors() -> None:
    model = jaffle_customer_value()
    bad = {
        "question": "q",
        "sql": _TEMPLATE_SQL,
        "slots": _SLOTS,
        "sample_bindings": [],  # untestable
    }
    gated, errors = _template_gate_sql(bad, "q", _TEMPLATE_SQL, model)
    assert gated is None and any("sample_bindings" in e for e in errors)


def test_plain_pairs_pass_through_the_gate_helper_untouched() -> None:
    model = jaffle_customer_value()
    sql = "SELECT count(*) FROM customers"
    gated, errors = _template_gate_sql({"question": "q", "sql": sql}, "q", sql, model)
    assert gated == sql and errors == []


def test_anchor_question_is_the_first_sample_bound_question() -> None:
    answer = {
        "question": "how many new customers in {period}?",
        "slots": _SLOTS,
        "sample_bindings": [{"period": "2026-Q2"}, {"period": "2025"}],
    }
    assert _anchor_question(answer) == "how many new customers in 2026-Q2?"
    assert _anchor_question({"question": "plain question?"}) == "plain question?"


# ── the bind gate + serve path ───────────────────────────────────────────────


def test_bind_gate_accepts_a_valid_proposal_and_canonicalizes() -> None:
    from services.contracts.fakes import ScriptedLLM
    from services.runtime.assembly import certified_bind

    llm = ScriptedLLM(['{"match": true, "bindings": {"period": "2026-q3"}}'])
    bound = certified_bind(llm, "m", "new customers in Q3 2026?", _template_answer())
    # date_range values keep their validated raw form (rendering derives dates).
    assert bound == {"period": "2026-q3"}


def test_bind_gate_fails_closed() -> None:
    from services.contracts.fakes import ScriptedLLM
    from services.runtime.assembly import certified_bind

    template = _template_answer()
    # explicit no-match
    assert (
        certified_bind(ScriptedLLM(['{"match": false, "bindings": {}}']), "m", "q", template)
        is None
    )
    # garbage output
    assert certified_bind(ScriptedLLM(["let me think..."]), "m", "q", template) is None
    # invalid proposed value (not the canonical grammar)
    bad = ScriptedLLM(['{"match": true, "bindings": {"period": "last quarter"}}'])
    assert certified_bind(bad, "m", "q", template) is None

    # provider explosion
    class _Boom:
        def complete(self, **_kw):  # type: ignore[no-untyped-def]
            raise RuntimeError("down")

    assert certified_bind(_Boom(), "m", "q", template) is None  # type: ignore[arg-type]


def test_select_generators_renders_a_bound_template() -> None:
    from services.contracts.fakes import ScriptedLLM
    from services.contracts.lens_config import LensConfig
    from services.runtime.assembly import AssembledInputs, select_generators

    model = jaffle_customer_value()
    assembled = AssembledInputs(
        model=model,
        prose=[],
        certified=_template_answer(),
        certification="certified",
        certified_sql=None,
        data_as_of=None,
        counts={},
        certified_match="parameterized",
        certified_bound={"period": "2026-Q3"},
    )
    gen, escalate, mode, match = select_generators(
        assembled,
        ResolvedModel(ScriptedLLM(), "fake", "m"),
        config=LensConfig(name="t", display_name="T"),
    )
    assert (mode, match) == ("certified", "parameterized") and escalate is None
    served = gen.generate(
        question="q", semantic_model=model, prose_context=[], dialect=model.dialect
    )
    assert "'2026-07-01'" in served.sql and "'2026-10-01'" in served.sql
    assert "{" not in served.sql


def test_parameterized_trust_summary_names_the_values() -> None:
    from services.config import settings
    from services.connectors.duckdb import DuckDBConnector
    from services.contracts.fakes import ScriptedLLM
    from services.contracts.response import CertifiedProvenance
    from services.runtime.answer import AnswerComposer
    from services.runtime.generator import FixedSQLGenerator
    from services.runtime.pipeline import run_query

    res = run_query(
        question="how many new customers in 2026-Q2?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=DuckDBConnector(settings.duckdb_jaffle_path),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(ScriptedLLM(["Some customers."])),
        certification="certified",
        certified_match="parameterized",
        certified_provenance=CertifiedProvenance(
            cert_id="c1",
            certified_by="maija",
            certified_at="2026-08-01T00:00:00+00:00",
            bound_values={"period": "2026-Q2"},
        ),
    )
    assert res.response.certified_match == "parameterized"
    assert res.response.trust_summary is not None
    assert "your values: period=2026-Q2" in res.response.trust_summary
    assert "approved SQL shape" in res.response.trust_summary


# ── detection-only template candidates ───────────────────────────────────────


def test_template_candidates_cluster_literal_families() -> None:
    from services.evals.distill import template_candidates

    q2 = CertifiedAnswer(
        "a1",
        "t",
        "revenue in Q2 2026?",
        "SELECT SUM(amount) FROM orders WHERE d >= '2026-04-01' AND d < '2026-07-01'",
        "m",
        "2026-08-01",
    )
    q3 = CertifiedAnswer(
        "a2",
        "t",
        "revenue in Q3 2026?",
        "SELECT SUM(amount) FROM orders WHERE d >= '2026-07-01' AND d < '2026-10-01'",
        "m",
        "2026-08-01",
    )
    other = CertifiedAnswer(
        "a3", "t", "how many orders?", "SELECT count(*) FROM orders", "m", "2026-08-01"
    )
    retired = CertifiedAnswer(
        "a4",
        "t",
        "revenue in 2024?",
        "SELECT SUM(amount) FROM orders WHERE d >= '2024-01-01' AND d < '2025-01-01'",
        "m",
        "2026-08-01",
        status="retired",
    )
    out = template_candidates([q2, q3, other, retired], "t", "duckdb")
    (cand,) = out  # one family; the singleton and the retired row never cluster
    assert cand.kind == "certified" and cand.target.startswith("template: revenue in Q2")
    assert "differ only in" in cand.diff_after
    assert "revenue in Q3 2026?" in cand.diff_after


def test_template_candidates_skip_templates_and_stable_repeats() -> None:
    from services.evals.distill import template_candidates

    already = _template_answer()  # slots set — never re-clusters
    twin_a = CertifiedAnswer("b1", "t", "q one?", "SELECT count(*) FROM t", "m", "2026-08-01")
    twin_b = CertifiedAnswer("b2", "t", "q two?", "SELECT count(*) FROM t", "m", "2026-08-01")
    assert template_candidates([already, twin_a, twin_b], "t", "duckdb") == []
