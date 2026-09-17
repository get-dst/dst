"""The resolution ledger rides every rail a data answer rides.

Response, receipt and trace all carry it on the `ok` exit; a non-answer carries
none (it makes no data claim). The intent door resolves by construction, the raw
SQL door by attribution, and a certified serve is overlaid. Receipts signed
before the field existed still verify; once a tag is on a receipt it is pinned
like every other claim.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import ScriptedLLM
from services.contracts.query_intent import QueryIntent
from services.contracts.response import Receipt
from services.lenses.demo import jaffle_customer_value
from services.runtime import receipt
from services.runtime.answer import AnswerComposer
from services.runtime.compiler import compile_intent
from services.runtime.generator import FixedSQLGenerator, GroundedSQLGenerator
from services.runtime.pipeline import run_query


def _run(generator, llm=None, **kw):  # type: ignore[no-untyped-def]
    llm = llm or ScriptedLLM(["A number of customers."])
    return run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=DuckDBConnector(settings.duckdb_jaffle_path),
        generator=generator,
        composer=AnswerComposer(llm),
        **kw,
    )


def test_raw_sql_serve_is_attributed_on_every_rail() -> None:
    gen = '{"sql": "SELECT count(*) AS n FROM customers", "rationale": "count"}'
    res = _run(GroundedSQLGenerator(ScriptedLLM([gen, "There are some customers."])))
    assert res.trace.status == "ok"
    ledger = res.response.resolution
    assert ledger is not None and ledger.method == "attributed"
    assert [(s.kind, s.source) for s in ledger.slots] == [("metric", "inferred")]
    assert ledger.tag == "inferred"
    assert res.trace.resolution == ledger and res.trace.resolution_tag == "inferred"
    assert res.response.receipt is not None
    assert res.response.receipt.resolution_tag == "inferred"


def test_intent_serve_resolves_by_construction() -> None:
    model = jaffle_customer_value()
    intent = QueryIntent(entity="orders", metrics=["revenue"], grain="month")
    gen = FixedSQLGenerator(compile_intent(intent, model), definition_used="revenue", intent=intent)
    res = _run(gen)
    assert res.trace.status == "ok"
    ledger = res.response.resolution
    assert ledger is not None and ledger.method == "construction"
    assert ("metric", "revenue", "declared") in [(s.kind, s.name, s.source) for s in ledger.slots]
    assert ledger.tag == "declared"
    assert res.response.receipt is not None
    assert res.response.receipt.resolution_tag == "declared"


def test_certified_serve_certifies_every_slot() -> None:
    res = _run(
        FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        certification="certified",
    )
    assert res.trace.status == "ok"
    ledger = res.response.resolution
    assert ledger is not None and ledger.tag == "certified"
    assert {s.source for s in ledger.slots} == {"certified"}
    assert res.trace.resolution_tag == "certified"


def test_a_non_answer_carries_no_ledger() -> None:
    res = _run(GroundedSQLGenerator(ScriptedLLM([""])))
    assert res.trace.status != "ok"
    assert res.response.resolution is None
    assert res.trace.resolution is None and res.trace.resolution_tag is None


# ── receipts: old ones verify, new tags are pinned ───────────────────────────


def _legacy_receipt(key: str) -> Receipt:
    """A receipt exactly as a pre-ledger server signed it: no tag in the bytes."""
    fields = {
        "request_id": "req_old",
        "lens": "customer_value",
        "served_at": "2026-08-11T00:00:00+00:00",
        "certification": "none",
        "cert_id": None,
        "confidence": "verified",
        "sql_sha256": receipt.sql_hash("SELECT 1"),
        "data_as_of": "2026-08-11",
    }
    data = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    digest = hmac.new(key.encode(), data, hashlib.sha256).hexdigest()
    return Receipt(**fields, digest=digest)


def test_pre_ledger_receipt_still_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "k1")
    assert receipt.check_signature(_legacy_receipt("k1")) == "valid"


def test_tag_is_pinned_once_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "k1")
    r = receipt.build(
        request_id="r",
        lens="l",
        certification="none",
        cert_id=None,
        confidence="verified",
        sql="SELECT 1",
        data_as_of=None,
        resolution_tag="declared",
    )
    assert receipt.check_signature(r) == "valid"
    assert receipt.check_signature(r.model_copy(update={"resolution_tag": "inferred"})) == "invalid"
    assert receipt.check_signature(r.model_copy(update={"resolution_tag": None})) == "invalid"


def test_measured_decisions_ride_the_ledger() -> None:
    from services.contracts.resolution import DecisionRecord

    rec = DecisionRecord(
        slot="certified_equivalent",
        chosen="equivalent",
        p=0.9,
        margin=0.8,
        verdict="act",
        provider="v",
    )
    res = _run(FixedSQLGenerator("SELECT count(*) AS n FROM customers"), decisions=[rec])
    assert res.trace.status == "ok"
    assert res.response.resolution is not None
    assert res.response.resolution.decisions == [rec]
    assert res.trace.resolution is not None and res.trace.resolution.decisions == [rec]


def test_an_acted_decision_below_its_measured_bar_is_disclosed_not_refused() -> None:
    from services.contracts.resolution import DecisionRecord

    rec = DecisionRecord(
        slot="lens", chosen="t", p=0.7, margin=0.4, verdict="act", provider="typesafe:jev-latest"
    )
    res = _run(FixedSQLGenerator("SELECT count(*) AS n FROM customers"), decisions=[rec])
    assert res.trace.status == "ok"  # served, never refused
    check = next(c for c in res.response.verification.checks if c.name == "decision_confidence")
    assert check.status == "fail" and "p=0.70" in (check.reason or "")
    assert res.response.confidence != "verified"
    assert "below the measured 0.90 bar" in res.response.answer


# ── typed serving on the pipeline: clarify by default, escalate only when allowed ──


def _typed_clarifier():  # type: ignore[no-untyped-def]
    from services.contracts.fakes import ScriptedDecider
    from services.contracts.protocols import Decision
    from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver

    unsure = Decision(chosen="aggregate", probs={"aggregate": 0.5, "listing": 0.45}, provider="v")
    return TypedIntentGenerator(TypedResolver(ScriptedDecider([unsure])))


def test_a_question_that_does_not_type_clarifies_by_default() -> None:
    res = _run(_typed_clarifier())
    assert res.trace.status == "clarification"
    assert res.response.clarification is not None
    assert res.response.clarification.kind == "unresolved_slot"
    assert res.response.resolution is None  # no data claim, no ledger


def test_an_allowed_untyped_escalation_is_disclosed_and_never_typed() -> None:
    from services.contracts.fakes import EchoQueryGenerator

    res = _run(
        _typed_clarifier(),
        escalate_generator=EchoQueryGenerator("SELECT count(*) AS n FROM customers"),
    )
    assert res.trace.status == "ok"
    assert any(
        line.startswith("UNTYPED: served by raw-SQL generation") for line in res.response.degraded
    )
    ledger = res.response.resolution
    assert ledger is not None and ledger.typed is False and ledger.tag == "inferred"
    # The typed attempt's decisions ride the ledger beside the escalation.
    assert [d.slot for d in ledger.decisions] == ["shape"] and ledger.decisions[
        0
    ].verdict == "clarify"
    assert res.trace.resolution is not None and res.trace.resolution.typed is False


def test_a_typed_answer_is_marked_typed() -> None:
    from services.contracts.fakes import ScriptedDecider
    from services.contracts.protocols import Decision
    from services.runtime.typed_resolver import TypedIntentGenerator, TypedResolver

    def sure(name: str | None) -> Decision:
        return Decision(chosen=name, probs={name or "none": 1.0}, provider="v")

    # shape, metric (over every metric of the lens — it names the entity),
    # another(none), filter(none): the jaffle orders entity declares no
    # dimensions, so that slot asks nothing, and the question asks for no
    # breakdown over time, so no grain is posed.
    steps = [
        sure("aggregate"),
        sure("revenue"),
        sure(None),
        sure(None),
    ]
    gen = TypedIntentGenerator(TypedResolver(ScriptedDecider(steps)))
    res = _run(gen)
    assert res.trace.status == "ok", res.trace.error
    assert res.response.resolution is not None and res.response.resolution.typed is True
    assert res.response.resolution.tag == "declared"
    assert [d.slot for d in res.response.resolution.decisions] == [
        "shape",
        "metric",
        "metric",
        "filter",
    ]


def test_a_clarification_is_traced_with_its_slot() -> None:
    res = _run(_typed_clarifier())
    assert res.trace.status == "clarification"
    assert res.trace.clarification is not None
    assert res.trace.clarification.kind == "unresolved_slot"
    assert res.trace.clarification.term == "shape"
