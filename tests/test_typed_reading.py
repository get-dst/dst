"""An ambiguous term's reading as a typed decision (reading.py).

The lexical rail pins a reading the question literally names; the residue
used to clarify no matter how plainly the wording pointed at one reading.
Under typed serving that residue is one measured decision over the declared
readings: act pins (disclosed, on the ledger), below the bar clarifies with
the record on the clarification's trace. The pinned reading rides the typed
resolver's entity and metric decisions the way it rides the JSON prompt.
"""

from __future__ import annotations

from datetime import date

import pytest

from services.contracts.fakes import ScriptedDecider, ScriptedLLM
from services.contracts.protocols import Decision, Option
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.runtime import pipeline as pipeline_mod
from services.runtime.generator import GroundedSQLGenerator
from services.runtime.pipeline import run_query
from services.runtime.reading import decide_reading
from services.runtime.typed_resolver import TypedResolver

REVENUE = Definition(
    term="revenue",
    body="ASK unless the question or the caller resolves it",
    status="ambiguous",
    possible_mappings=[
        "net invoiced revenue (Finance / CFO world) - invoices.amount, "
        "sum of invoices.amount by issue date",
        "bookings (Sales / VP Sales world) - deals.value, sum of deals.value by close date",
    ],
)
NET = "net invoiced revenue (Finance / CFO world)"
BOOKINGS = "bookings (Sales / VP Sales world)"


def _model() -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity(
                name="invoices",
                source=EntitySource(connection="wh", table="invoices"),
                primary_key=["id"],
                fields=[Field(name="id", type="string"), Field(name="amount", type="number")],
                metrics=[Metric(name="total_amount", agg="sum", expr="invoices.amount")],
            )
        ],
        definitions=[REVENUE],
    )


def _decision(chosen: str | None, p: float, runner: float = 0.0) -> Decision:
    other = BOOKINGS if chosen != BOOKINGS else NET
    probs = {chosen or "none": p, other: runner}
    return Decision(chosen=chosen, probs=probs, provider="scripted")


def test_decide_reading_acts_pins_and_records_the_probability() -> None:
    picked, record = decide_reading(
        ScriptedDecider([_decision(NET, 0.96, 0.04)]), "gross invoiced revenue?", REVENUE
    )
    assert picked is not None and picked.mapping == REVENUE.possible_mappings[0]
    assert picked.term == "revenue" and picked.audience is None and picked.others == [BOOKINGS]
    assert "typed decision" in picked.via and "0.96" not in picked.via  # p rides the ledger
    assert record.slot == "reading" and record.verdict == "act" and record.p == 0.96


def test_decide_reading_below_the_bar_or_none_never_pins() -> None:
    unsure, rec = decide_reading(
        ScriptedDecider([_decision(NET, 0.55, 0.45)]), "our revenue?", REVENUE
    )
    assert unsure is None and rec.verdict == "clarify"
    none, rec2 = decide_reading(ScriptedDecider([_decision(None, 1.0)]), "revenue?", REVENUE)
    assert none is None and rec2.chosen is None and rec2.verdict != "act"
    # A name outside the declared readings is a provider fault, never a pin.
    stray, rec3 = decide_reading(
        ScriptedDecider([Decision(chosen="ebitda", probs={"ebitda": 1.0}, provider="x")]),
        "revenue?",
        REVENUE,
    )
    assert stray is None and rec3.verdict == "clarify"


class _Summing:
    kind = "duckdb"

    def execute(self, sql: str, *, read_only: bool = True, row_limit: int | None = None):
        from services.contracts.warehouse import QueryResult

        return QueryResult(columns=["total"], rows=[[3]])


def _run(question: str):
    from services.runtime.answer import AnswerComposer

    llm = ScriptedLLM(
        [
            '{"sql": "SELECT SUM(invoices.amount) AS total FROM invoices AS invoices", '
            '"definition_used": null, "rationale": "r"}',
            "The total was 3.",
        ]
    )
    return run_query(
        question=question,
        lens_name="t",
        org_id="org-1",
        caller="test",
        semantic_model=_model(),
        connector=_Summing(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )


QUESTION = "what was our gross invoiced revenue last month?"  # names no decisive term


def test_without_a_typed_decider_the_residue_still_clarifies(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pipeline_mod, "typed_decider", lambda: None)
    out = _run(QUESTION)
    assert out.response.status == "clarification"
    assert out.trace.resolution is None  # nothing was decided


def test_a_confident_typed_reading_pins_discloses_and_rides_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
):
    decider = ScriptedDecider([_decision(NET, 0.97, 0.03)])
    monkeypatch.setattr(pipeline_mod, "typed_decider", lambda: decider)
    out = _run(QUESTION)
    assert out.response.status == "ok", out.response.answer
    assert decider.calls[0]["options"] == [NET, BOOKINGS]
    assert any(r.startswith("ambiguity-resolution: revenue -> ") for r in out.trace.context_refs)
    assert "Reading applied: 'revenue' was read as net invoiced revenue" in out.response.answer
    assert "typed decision" in out.response.answer
    ledger = out.response.resolution
    assert ledger is not None
    rec = next(d for d in ledger.decisions if d.slot == "reading")
    assert rec.verdict == "act" and rec.chosen == NET and rec.p == 0.97


def test_an_unsure_typed_reading_clarifies_with_the_record_on_the_trace(
    monkeypatch: pytest.MonkeyPatch,
):
    decider = ScriptedDecider([_decision(NET, 0.6, 0.4)])
    monkeypatch.setattr(pipeline_mod, "typed_decider", lambda: decider)
    out = _run(QUESTION)
    assert out.response.status == "clarification"
    assert out.response.clarification is not None
    assert out.response.clarification.options == REVENUE.possible_mappings
    assert out.trace.resolution is not None
    [rec] = out.trace.resolution.decisions
    assert rec.slot == "reading" and rec.verdict == "clarify" and rec.p == 0.6


class _Capturing:
    """Every context the resolver hands the decider, so a note can be found."""

    def __init__(self) -> None:
        self.contexts: list[str] = []

    def decide(self, *, question: str, context: str, options: list[Option], allow_none: bool):
        self.contexts.append(context)
        names = [o.name for o in options]
        chosen = "aggregate" if "aggregate" in names else names[0]
        if any(n in {"none", "engaged"} for n in names) and "aggregate" not in names:
            chosen = "none" if "none" in names else "engaged"
        return Decision(
            chosen=None if chosen == "none" else chosen,
            probs={chosen: 1.0, **{n: 0.0 for n in names if n != chosen}},
            provider="cap",
        )


def test_the_pinned_reading_rides_the_entity_and_metric_decisions() -> None:
    cap = _Capturing()
    note = "GOVERNED RESOLUTION — the ambiguous term 'revenue' is pinned to: bookings."
    TypedResolver(cap, domains={}, today=date(2026, 9, 17)).resolve(  # type: ignore[arg-type]
        "total revenue", _model(), notes=[note]
    )
    metric_ctx = next(c for c in cap.contexts if c.startswith("Which governed metric"))
    assert note in metric_ctx
    assert all(note not in c for c in cap.contexts if c.startswith("What kind of answer"))
