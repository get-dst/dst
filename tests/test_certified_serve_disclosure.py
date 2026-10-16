"""The serve half of a certified answer: what moved, and what governs it.

Two guarantees. A certified answer stores the value a human verified; when the
approved SQL now returns a different value, the serve says so (`restated` fails,
a RESTATED line rides `degraded`, the certify-time prose stays off the wire)
instead of serving yesterday's sentence under today's badge. And the
definitions written over what the approved SQL reads ride the certified answer
the way they ride a generated one: basis line, `definition_used`, ledger.
"""

from __future__ import annotations

from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.response import CertifiedProvenance
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.contracts.warehouse import QueryResult
from services.runtime.answer import AnswerComposer
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import run_query

_SQL = "SELECT SUM(deals.contract_value_eur) AS bookings FROM northwind.crm.deals AS deals"
_Q = "What were total bookings?"
_PROSE = "Total bookings were 120,000 EUR."
_PROVENANCE = CertifiedProvenance(
    cert_id="c1", certified_by="human:ana", certified_at="2026-09-01T10:00:00+00:00"
)


def _model(definitions: list[Definition] | None = None) -> SemanticModel:
    return SemanticModel(
        lens="sales_comp",
        dialect="bigquery",
        entities=[
            Entity(
                name="deals",
                source=EntitySource(connection="bq", table="northwind.crm.deals"),
                fields=[
                    Field(name="segment", type="string"),
                    Field(name="contract_value_eur", type="number"),
                ],
                metrics=[Metric(name="bookings", agg="sum", expr="deals.contract_value_eur")],
            )
        ],
        definitions=definitions or [],
    )


def _serve(value: float, **overrides: object):
    args: dict[str, object] = dict(
        question=_Q,
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_model(),
        connector=FakeConnector(result=QueryResult(columns=["bookings"], rows=[[value]])),
        generator=FixedSQLGenerator(_SQL),
        composer=AnswerComposer(ScriptedLLM(["Total bookings are now 135,000 EUR."])),
        certification="certified",
        certified_match="exact",
        certified_provenance=_PROVENANCE,
        certified_question=_Q,
        verified_prose=_PROSE,
    )
    return run_query(**{**args, **overrides})  # type: ignore[arg-type]


# ─── restated ────────────────────────────────────────────────────────────────


def test_moved_value_fails_restated_and_withholds_the_stored_prose() -> None:
    res = _serve(135000.0, verified_value={"value": "120000.00"})
    assert res.trace.status == "ok"
    report = res.response.verification
    assert report is not None
    check = report.check("restated")
    assert check is not None and check.status == "fail"
    assert report.grade == "partial"
    assert any(
        n.startswith("RESTATED: this certified answer's result moved from 120000.00 to 135000.0")
        and n.endswith("since it was verified on 2026-09-01")
        for n in res.response.degraded
    )
    assert _PROSE not in res.response.answer
    assert "135,000" in res.response.answer  # composed over today's rows
    assert res.response.certification == "certified"


def test_moved_value_serves_rows_only_without_a_composer() -> None:
    res = run_query(
        question=_Q,
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_model(),
        connector=FakeConnector(result=QueryResult(columns=["bookings"], rows=[[135000.0]])),
        generator=FixedSQLGenerator(_SQL),
        composer=None,
        certification="certified",
        certified_match="exact",
        certified_provenance=_PROVENANCE,
        verified_prose=_PROSE,
        verified_value={"value": 120000},
    )
    assert _PROSE not in res.response.answer
    assert res.response.data is not None and res.response.data.rows == [[135000.0]]
    assert any(n.startswith("RESTATED") for n in res.response.degraded)


def test_value_within_printed_precision_passes_and_serves_verbatim() -> None:
    # 120000.00 prints with two decimals; 120000.004 rounds to it.
    res = _serve(120000.004, verified_value={"value": "120000.00"})
    report = res.response.verification
    assert report is not None
    check = report.check("restated")
    assert check is not None and check.status == "pass"
    assert report.grade == "verified"
    assert res.response.answer.startswith(_PROSE)
    assert not any(n.startswith("RESTATED") for n in res.response.degraded)


def test_no_stored_value_skips_never_passes() -> None:
    res = _serve(135000.0, verified_value=None)
    report = res.response.verification
    assert report is not None
    check = report.check("restated")
    assert check is not None and check.status == "skip" and check.reason
    assert res.response.answer.startswith(_PROSE)


def test_template_compares_only_its_sample_binding() -> None:
    sample = {"period": "2026-Q2"}
    other = CertifiedProvenance(
        cert_id="c1",
        certified_by="human:ana",
        certified_at="2026-09-01",
        bound_values={"period": "2026-Q3"},
    )
    res = _serve(
        135000.0,
        verified_value={"value": 120000},
        verified_bindings=sample,
        certified_match="parameterized",
        certified_provenance=other,
    )
    report = res.response.verification
    assert report is not None
    check = report.check("restated")
    assert check is not None and check.status == "skip"
    assert "sample binding" in (check.reason or "")

    same = other.model_copy(update={"bound_values": {"period": "2026-Q2"}})
    res = _serve(
        135000.0,
        verified_value={"value": 120000},
        verified_bindings=sample,
        certified_match="parameterized",
        certified_provenance=same,
    )
    report = res.response.verification
    assert report is not None
    check = report.check("restated")
    assert check is not None and check.status == "fail"


def test_table_value_compares_cell_by_cell() -> None:
    stored = {"columns": ["segment", "bookings"], "rows": [["smb", "100.5"], ["ent", "200"]]}
    moved = QueryResult(columns=["segment", "bookings"], rows=[["ent", 200], ["smb", 101.5]])
    res = run_query(
        question=_Q,
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_model(),
        connector=FakeConnector(result=moved),
        generator=FixedSQLGenerator(
            "SELECT deals.segment AS segment, SUM(deals.contract_value_eur) AS bookings "
            "FROM northwind.crm.deals AS deals GROUP BY deals.segment"
        ),
        composer=None,
        certification="certified",
        certified_match="exact",
        certified_provenance=_PROVENANCE,
        verified_value=stored,
    )
    report = res.response.verification
    assert report is not None
    check = report.check("restated")
    assert check is not None and check.status == "fail"
    assert "bookings 100.5" in (check.reason or "") and "bookings 101.5" in (check.reason or "")


# ─── definitions ride the certified answer ───────────────────────────────────


def test_definition_about_a_read_column_rides_the_certified_answer() -> None:
    about = Definition(
        term="bookings",
        body="Bookings count the contract value of every signed deal, gross of churn.",
        summary="contract value of signed deals, gross of churn",
        about="deals.contract_value_eur",
    )
    res = run_query(
        question=_Q,
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_model([about]),
        connector=FakeConnector(result=QueryResult(columns=["bookings"], rows=[[120000.0]])),
        generator=FixedSQLGenerator(_SQL),
        composer=None,
        certification="certified",
        certified_match="exact",
        certified_provenance=_PROVENANCE,
        verified_value={"value": 120000.0},
    )
    assert res.trace.status == "ok"
    assert res.response.definition_used == "bookings"
    assert res.trace.definition_used == "bookings"
    assert res.response.trust_summary and "Certified answer" in res.response.trust_summary
    assert "Computed as `bookings` — contract value of signed deals" in res.response.trust_summary
    ledger = res.response.resolution
    assert ledger is not None
    assert any(
        s.kind == "definition" and s.name == "bookings" and s.source == "certified"
        for s in ledger.slots
    )


def test_certified_answer_names_the_metric_it_served() -> None:
    """No definition page, but the approved SQL computes a declared metric:
    the basis line names it after the provenance, like a generated answer."""
    res = _serve(120000.0, verified_value={"value": 120000.0})
    assert res.response.trust_summary is not None
    assert res.response.trust_summary.startswith("Certified answer — approved by ana")
    assert "Computed as `bookings` on `deals`. (entity metric)" in res.response.trust_summary
    assert res.response.definition_used == "bookings"
