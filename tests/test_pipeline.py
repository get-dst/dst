"""End-to-end runtime over jaffle (deterministic, ScriptedLLM)."""

from __future__ import annotations

from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.protocols import CacheableBlock, ContextChunk, LLMResult, Message
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.contracts.warehouse import QueryResult
from services.lenses.demo import jaffle_customer_value
from services.runtime.answer import AnswerComposer
from services.runtime.assembly import ANSWER_CONTRACT, ANSWER_CONTRACT_SOURCE
from services.runtime.generator import FixedSQLGenerator, GroundedSQLGenerator
from services.runtime.pipeline import FETCH_CAP, PipelineResult, run_query


def _conn() -> DuckDBConnector:
    return DuckDBConnector(settings.duckdb_jaffle_path)


def test_pipeline_happy_path() -> None:
    gen_json = (
        '{"sql": "SELECT count(*) AS n, round(avg(customer_lifetime_value), 2) AS avg_clv '
        'FROM customers WHERE number_of_orders > 1", '
        '"definition_used": "repeat_customer", "rationale": "count + avg clv for repeat"}'
    )
    answer = "Repeat customers have a measurable average lifetime value."
    llm = ScriptedLLM([gen_json, answer])

    res = run_query(
        question="How many repeat customers, and their average lifetime value?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )

    assert res.trace.status == "ok"
    assert res.response.sql and "customers" in res.response.sql
    assert res.response.data is not None and res.response.data.rows
    assert res.response.answer == answer
    assert res.response.definition_used == "repeat_customer"
    # numberless prose → nothing numerically verifiable → honest "partial"
    assert res.response.confidence == "partial"
    assert res.response.verification is not None
    numeric = res.response.verification.check("numeric_grounding")
    assert numeric is not None and numeric.status == "skip"
    citation_types = {c.type for c in res.response.citations}
    assert {"definition", "sql"} <= citation_types
    # the executed result actually came from the warehouse
    assert res.trace.row_count == 1
    # Every stage carries real latency, and total covers them
    for stage in ("generate", "execute", "compose", "total"):
        assert stage in res.trace.latency_ms, stage
    assert res.trace.latency_ms["total"] >= res.trace.latency_ms["execute"]


def test_rows_only_serve_skips_the_composer_and_keeps_the_governance() -> None:
    """``composer=None`` — what the API's ``format: "structured"`` maps to.

    The compose round-trip dominates a certified answer's latency and no
    programmatic caller reads the prose it buys. Skipping it must cost nothing a caller
    relies on for trust: rows, SQL, citations and the deterministic checks all survive,
    and ``answer`` is a deterministic marker — never the empty string that every caller
    in this repo reads as an error."""
    gen_json = (
        '{"sql": "SELECT count(*) AS n FROM customers WHERE number_of_orders > 1", '
        '"definition_used": "repeat_customer"}'
    )
    llm = ScriptedLLM([gen_json])
    res = run_query(
        question="How many repeat customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=None,
    )

    assert res.trace.status == "ok"
    assert res.response.data is not None and res.response.data.rows
    # ONE model call in the whole request — the generator's. The composer never ran, so
    # there is no compose stage to time either.
    assert res.trace.ai_input_tokens == 1
    assert "compose" not in res.trace.latency_ms
    # Citations are governance, not prose: they outlive the composer.
    assert {c.type for c in res.response.citations} == {"definition", "sql"}
    assert res.response.answer and "no prose was composed" in res.response.answer
    report = res.response.verification
    assert report is not None
    # The numeric check did not RUN — an honest skip, and one that must not quietly
    # cost the grade the way a numberless prose answer's skip does.
    numeric = report.check("numeric_grounding")
    assert numeric is not None and numeric.status == "skip"
    assert res.response.confidence == "verified"


def test_rows_only_serve_still_states_a_truncated_payload() -> None:
    """The truncation note is deterministic governance, appended by the pipeline itself —
    a rows-only answer that goes short in silence is the same wrong answer as a prose one
    that does."""
    rows = [[i] for i in range(300)]
    res = run_query(
        question="list the customers",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=rows)),
        generator=FixedSQLGenerator("SELECT customer_id AS n FROM customers"),
        composer=None,
        max_rows=100,
    )
    assert res.response.data is not None and len(res.response.data.rows) == 100
    assert "only the first 100 of 300" in res.response.answer
    # ...and never the composer's prompt-budget note, which describes a summary that
    # was never written.
    assert "the summary above" not in res.response.answer


def test_truncation_is_a_stamped_fact_on_the_envelope() -> None:
    """`truncated` rides the ENVELOPE, not only `data` — format=prose drops
    the payload, and a caller must never parse English to learn the window was short."""
    rows = [[i] for i in range(300)]
    res = run_query(
        question="list the customers",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=rows)),
        generator=FixedSQLGenerator("SELECT customer_id AS n FROM customers"),
        composer=None,
        max_rows=100,
    )
    assert res.response.truncated is not None
    assert res.response.truncated.returned == 100
    assert res.response.truncated.total == 300
    # …and the code-appended prose line states what the window does not cover.
    assert "totals beyond this window are not stated" in res.response.answer


def test_fetch_capped_truncation_stamps_no_total_it_cannot_stand_behind() -> None:
    rows = [[i] for i in range(FETCH_CAP + 1)]
    res = run_query(
        question="list the customers",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=rows)),
        generator=FixedSQLGenerator("SELECT customer_id AS n FROM customers"),
        composer=None,
        max_rows=100,
    )
    assert res.response.truncated is not None
    assert res.response.truncated.returned == 100
    # past the fetch cap the true count is unknown — never a number
    assert res.response.truncated.total is None
    assert "totals beyond this window are not stated" in res.response.answer


def test_a_complete_payload_stamps_no_truncation() -> None:
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[42]])),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=None,
    )
    assert res.response.truncated is None
    assert "totals beyond this window" not in res.response.answer


def test_pipeline_rejects_out_of_scope() -> None:
    llm = ScriptedLLM(['{"sql": "SELECT * FROM raw_payments", "definition_used": null}'])
    res = run_query(
        question="show me all raw payments",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "rejected"
    assert res.trace.error
    assert res.response.data is None
    # The refusal must not echo the ungoverned SQL;
    # the trace keeps it for observability.
    assert res.response.sql is None
    assert res.trace.sql and "raw_payments" in res.trace.sql


_GOOD = '{"sql": "SELECT count(*) AS n FROM customers", "definition_used": null}'
_BAD = '{"sql": "SELECT * FROM raw_payments", "definition_used": null}'
_ANSWER = "There are some customers."


def test_pipeline_self_repairs_after_guard_reject() -> None:
    # First generation is out of scope (guard rejects); the repair attempt fixes it.
    llm = ScriptedLLM([_BAD, _GOOD, _ANSWER])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.response.sql and "customers" in res.response.sql.lower()
    assert "raw_payments" not in (res.response.sql or "")
    # two generate calls + one compose call were all counted
    assert res.trace.ai_input_tokens == 3


def test_pipeline_traces_the_generation_path_and_what_it_cost() -> None:
    """The trace names which prompt answered and what the repair loop cost.

    The tier PICKS the prompt (grounded and intent serialize the model
    differently), so a regression that only shows on one of them was invisible in
    request_log; `repairs` is the same story for the retry budget. `context_refs`
    was declared on the trace since 0002 and assigned by nothing at all — it now
    carries the prose that actually rode in the prompt, minus the answer contract
    (an instruction, not a source, exactly as the composer's citations have it).
    """
    llm = ScriptedLLM([_BAD, _GOOD, _ANSWER])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
        prose_context=[
            ContextChunk(text="repeat customers are the priority", source="runbook.md"),
            ContextChunk(text=ANSWER_CONTRACT, source=ANSWER_CONTRACT_SOURCE),
        ],
        generator_tier="grounded",
    )
    assert res.trace.status == "ok"
    assert res.trace.generator_tier == "grounded"
    assert res.trace.repairs == 1  # the guard rejected once, the repair landed
    assert res.trace.context_refs == ["runbook.md"]


def test_pipeline_escalates_to_stronger_generator() -> None:
    # The cheap generator only knows the bad SQL; the escalate generator knows the good one.
    primary = GroundedSQLGenerator(ScriptedLLM([_BAD]), model="claude-haiku-4-5")
    escalate = GroundedSQLGenerator(ScriptedLLM([_GOOD]), model="claude-sonnet-4-6")
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=primary,
        escalate_generator=escalate,
        composer=AnswerComposer(ScriptedLLM([_ANSWER])),
    )
    assert res.trace.status == "ok"
    assert res.response.sql and "customers" in res.response.sql.lower()


def test_pipeline_rejected_after_repairs_exhausted() -> None:
    llm = ScriptedLLM([_BAD])  # repeats the bad SQL on every attempt
    res = run_query(
        question="show raw payments",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "rejected"
    assert res.response.data is None
    assert res.response.sql is None  # never hand back what the guard refused
    assert res.trace.sql  # …but the trace holds the evidence


def test_pipeline_rejection_keeps_the_candidate_a_failed_repair_erased() -> None:
    """Blank-SQL rejections: the REPAIR attempt came back with no SQL in it
    (a thinking-starved reply bills tokens with empty content), and that empty string
    overwrote the candidate the guard had actually refused — so request_log held
    status='rejected' with sql=''. The trace must keep the real candidate, and the
    caller must still never see it."""
    llm = ScriptedLLM([_BAD, ""])  # out-of-scope SQL, then a reply carrying no SQL
    res = run_query(
        question="show raw payments",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "rejected"
    assert res.trace.sql and "raw_payments" in res.trace.sql
    # …and the reason describes THAT SQL, not the empty repair that followed it
    assert "SELECT * is not allowed" in (res.trace.error or "")
    assert res.response.sql is None


def test_pipeline_traces_no_sql_at_all_when_nothing_was_generated() -> None:
    """No attempt produced SQL: the trace must say so — a NULL sql with the guard's
    empty-generation reason, not the empty string that read as "the SQL was lost".
    And it lands as an ERROR: an empty generation is dst's own
    fault, never a governed decline — booking it as `rejected` was how a total
    generation outage read as governance being appropriately cautious."""
    llm = ScriptedLLM([""])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "error"
    assert res.error_kind == "generation"
    assert res.trace.sql is None  # nothing was generated — not an empty string
    assert "no SQL" in (res.trace.error or "")


def test_unparseable_generated_sql_is_an_error_not_a_decline() -> None:
    """dst emitting invalid SQL is a FAULT — it lands where faults
    are counted and alertable, the caller is told it's a dst defect (not told
    to rephrase a fine question), and the repair names the SYNTAX."""
    bad = "SELECT count(*) FROM customers WHERE ("  # unclosed paren, twice
    llm = _RecordingLLM([f'{{"sql": "{bad}"}}'])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "error"
    assert res.error_kind == "generation"
    assert "dst defect" in res.response.answer
    assert "in-scope" not in res.response.answer  # the question WAS in scope
    # The repair prompt named the syntax, never the policy.
    assert any("SYNTAX defect" in p for p in llm.prompts)
    assert not any("rejected by the guard" in p for p in llm.prompts)


def test_unbalanced_backtick_is_named_precisely() -> None:
    """A stray backtick opens a runaway quoted identifier and the
    tokenizer error quotes text far from the defect. The guard names it first."""
    from services.contracts.semantic_model import SemanticModel as _SM
    from services.runtime import sql_guard

    model = jaffle_customer_value().model_copy(update={"dialect": "bigquery"})
    assert isinstance(model, _SM)
    r = sql_guard.check("SELECT 1 FROM `proj`.ds.t` AS t", model)
    assert not r.ok and r.kind == "syntax"
    assert "unbalanced backtick" in (r.reason or "")


def test_out_of_scope_is_still_a_rejection() -> None:
    # REGRESSION: the governance meaning of `rejected` survives.
    llm = ScriptedLLM(['{"sql": "SELECT * FROM raw_payments"}'])
    res = run_query(
        question="show raw payments",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "rejected"


class _RaisingConnector:
    kind = "duckdb"

    def introspect(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def dry_run(self, sql):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def execute(self, sql, *, read_only=True, row_limit=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("warehouse exploded")


def test_pipeline_execution_error_is_graceful() -> None:
    llm = ScriptedLLM([_GOOD])  # guard-valid SQL, but execution raises
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_RaisingConnector(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "error"
    assert "warehouse exploded" in (res.trace.error or "")
    assert res.response.data is None


# --- Filter guarantee: a governed metric's --------------------------------------
# --- filters are enforced by code at the guard seam, not by a prompt line.    ---


def _sales_model() -> SemanticModel:
    """The probe shape: BigQuery lens, `bookings` governed by a new-customer filter."""
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
                    Field(name="is_new_customer", type="boolean"),
                ],
                metrics=[
                    Metric(
                        name="bookings",
                        agg="sum",
                        expr="deals.contract_value_eur",
                        filters=["deals.is_new_customer = true"],
                    )
                ],
            )
        ],
    )


_BOOKINGS_UNFILTERED = (
    '{"sql": "SELECT deals.segment AS segment, SUM(deals.contract_value_eur) AS bookings '
    'FROM northwind.crm.deals AS deals GROUP BY deals.segment", '
    '"definition_used": null}'
)
_BOOKINGS_FILTERED = (
    '{"sql": "SELECT deals.segment AS segment, SUM(deals.contract_value_eur) AS bookings '
    "FROM northwind.crm.deals AS deals WHERE deals.is_new_customer = true "
    'GROUP BY deals.segment", '
    '"definition_used": null}'
)


class _RecordingLLM(ScriptedLLM):
    """ScriptedLLM that also keeps every user prompt — to assert the repair
    feedback names the metric and the exact filter."""

    def __init__(self, responses: list[str]) -> None:
        super().__init__(responses)
        self.prompts: list[str] = []

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        self.prompts.append("\n".join(m.content for m in messages))
        return super().complete(
            system=system,
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )


def _bookings_result() -> QueryResult:
    return QueryResult(columns=["segment", "bookings"], rows=[["smb", 120000.0]])


def test_pipeline_repairs_unfiltered_governed_metric() -> None:
    """SUM over the metric expr GROUP BY segment without the
    filter → guard-class repair with the metric and filter named → the scripted
    second attempt WITH the filter passes."""
    llm = _RecordingLLM([_BOOKINGS_UNFILTERED, _BOOKINGS_FILTERED, "Bookings by segment."])
    res = run_query(
        question="What were total bookings by segment?",
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_sales_model(),
        connector=FakeConnector(result=_bookings_result()),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.response.sql and "is_new_customer" in res.response.sql
    # the repair feedback carried the precise instruction, not a vague hint
    assert "metric 'bookings' REQUIRES deals.is_new_customer = true" in llm.prompts[1]
    # ...and the FULL governed aggregation to paste (the bare condition alone
    # is not enough — the model re-emits its own unfiltered form)
    assert (
        "SUM(CASE WHEN deals.is_new_customer = true THEN deals.contract_value_eur END)"
        in llm.prompts[1]
    )


def test_pipeline_escalation_gets_filter_feedback() -> None:
    """The live wiring: the escalation model receives the exact filters to AND in."""
    escalate_llm = _RecordingLLM([_BOOKINGS_FILTERED])
    res = run_query(
        question="What were total bookings by segment?",
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_sales_model(),
        connector=FakeConnector(result=_bookings_result()),
        generator=GroundedSQLGenerator(
            ScriptedLLM([_BOOKINGS_UNFILTERED]), model="claude-haiku-4-5"
        ),
        escalate_generator=GroundedSQLGenerator(escalate_llm),
        composer=AnswerComposer(ScriptedLLM(["Bookings by segment."])),
    )
    assert res.trace.status == "ok"
    assert res.response.sql and "is_new_customer" in res.response.sql
    assert "metric 'bookings' REQUIRES deals.is_new_customer = true" in escalate_llm.prompts[0]


def test_pipeline_rejects_when_filter_still_missing_after_repairs() -> None:
    """A silently unfiltered governed metric is a WRONG answer — after repairs
    are exhausted the request is rejected with the filter named, never served."""
    llm = ScriptedLLM([_BOOKINGS_UNFILTERED])  # repeats the violation on every attempt
    res = run_query(
        question="What were total bookings by segment?",
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_sales_model(),
        connector=FakeConnector(result=_bookings_result()),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "rejected"
    assert res.response.data is None
    assert "governed metric filter missing" in (res.trace.error or "")
    # The caller-facing answer is actionable: it names what governs the metric
    # and suggests a way forward, not just guard internals.
    assert (
        "this lens's 'bookings' metric always applies deals.is_new_customer = true"
        in res.response.answer
    )
    # Never "try rephrasing" (it cannot help, and it misdiagnoses a filter
    # deadlock as a phrasing problem) — the remediation names a real move.
    assert "try rephrasing" not in res.response.answer
    assert "include the filter" in res.response.answer or "by name" in res.response.answer
    # The refusal must not carry the unfiltered SQL it refused to serve —
    # "here's your refusal, and here's the wrong SQL" reads as contradictory.
    assert res.response.sql is None
    assert res.trace.sql and "bookings" in res.trace.sql.lower()


def test_pipeline_filtered_sql_passes_untouched() -> None:
    llm = ScriptedLLM([_BOOKINGS_FILTERED, "Bookings by segment."])
    res = run_query(
        question="What were total bookings by segment?",
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_sales_model(),
        connector=FakeConnector(result=_bookings_result()),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.trace.ai_input_tokens == 2  # one generate + one compose: no repair burned


def test_pipeline_certified_sql_is_exempt_from_filter_check() -> None:
    """Trusted SQL (a human approved that exact query) bypasses the filter check
    the same way it bypasses table trust — even when it reads unfiltered."""
    unfiltered_sql = (
        "SELECT deals.segment AS segment, SUM(deals.contract_value_eur) AS bookings "
        "FROM northwind.crm.deals AS deals GROUP BY deals.segment"
    )
    res = run_query(
        question="What were total bookings by segment?",
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_sales_model(),
        connector=FakeConnector(result=_bookings_result()),
        generator=FixedSQLGenerator(unfiltered_sql),
        composer=AnswerComposer(ScriptedLLM(["Bookings by segment."])),
        certification="certified",
    )
    assert res.trace.status == "ok"
    assert res.response.sql and "is_new_customer" not in res.response.sql


# --- Selection is a visible boundary: a metric the curator -------------------
# --- DROPPED from the lens's selection refuses deterministically — no LLM,  ---
# --- no warehouse — instead of being reconstructed from raw columns.        ---


class _ExplodingGenerator:
    model = "never-called"

    def generate(self, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("generator must not run for an excluded metric")


def _web_model(*, selected: bool) -> SemanticModel:
    metrics = [Metric(name="session_count", agg="count", expr="sessions.session_id")]
    excluded = ["conversion_rate"]
    if selected:
        metrics.append(Metric(name="conversion_rate", agg="avg", expr="sessions.converted"))
        excluded = []
    return SemanticModel(
        lens="web",
        dialect="duckdb",
        entities=[
            Entity(
                name="sessions",
                source=EntitySource(connection="wh", table="sessions"),
                fields=[
                    Field(name="session_id", type="integer"),
                    Field(name="converted", type="boolean"),
                ],
                metrics=metrics,
            )
        ],
        excluded_metrics=excluded,
    )


def test_pipeline_refuses_excluded_metric_deterministically() -> None:
    """One doctrine, two entry points: the by-name refusal speaks the SAME
    contract as the composed-shape guard — the metric, the lens.yaml-selection/
    certify fix — via the shared text-builder, plus the sibling-lens hint when
    exactly one other lens carries the metric."""
    res = run_query(
        question="What was our conversion rate last month?",
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_web_model(selected=False),
        connector=_RaisingConnector(),  # would explode: the warehouse is never touched
        generator=_ExplodingGenerator(),  # would explode: no LLM is consulted
        composer=AnswerComposer(ScriptedLLM()),
        metric_carriers=lambda _m: ["growth"],
    )
    assert res.trace.status == "rejected"
    err = res.trace.error or ""
    assert "the question asks for 'conversion_rate' by name" in err
    assert "add 'conversion_rate' to this lens's selection in lens.yaml" in err
    assert "certify this answer" in err
    assert "lens 'growth' carries 'conversion_rate'" in err  # the sibling hint
    assert "conversion_rate" in res.response.answer  # decline-with-path, caller-facing
    assert res.response.sql is None and res.response.data is None
    assert res.trace.ai_input_tokens == 0  # not one token spent


def test_pipeline_excluded_metric_matches_underscored_form() -> None:
    res = run_query(
        question="show conversion_rate by channel",
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_web_model(selected=False),
        connector=_RaisingConnector(),
        generator=_ExplodingGenerator(),
        composer=AnswerComposer(ScriptedLLM()),
    )
    assert res.trace.status == "rejected"
    assert "conversion_rate" in (res.trace.error or "")


def test_pipeline_selected_metric_answers_normally() -> None:
    """The same question against a lens that SELECTS the metric answers."""
    llm = ScriptedLLM(
        [
            '{"sql": "SELECT avg(converted) AS conversion_rate FROM sessions", '
            '"definition_used": null}',
            "Conversion is healthy.",
        ]
    )
    res = run_query(
        question="What was our conversion rate last month?",
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_web_model(selected=True),
        connector=FakeConnector(result=QueryResult(columns=["conversion_rate"], rows=[[0.42]])),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.response.data is not None


def test_pipeline_unrelated_question_unaffected_by_exclusions() -> None:
    llm = ScriptedLLM(
        ['{"sql": "SELECT count(*) AS n FROM sessions", "definition_used": null}', "Sessions."]
    )
    res = run_query(
        question="How many sessions did we have?",
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_web_model(selected=False),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[7]])),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"


def test_pipeline_certified_sql_is_exempt_from_exclusion_check() -> None:
    """A human approved that exact question→SQL — the boundary check stands down."""
    res = run_query(
        question="What was our conversion rate last month?",
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_web_model(selected=False),
        connector=FakeConnector(result=QueryResult(columns=["conversion_rate"], rows=[[0.42]])),
        generator=FixedSQLGenerator(
            "SELECT AVG(sessions.converted) AS conversion_rate FROM sessions AS sessions"
        ),
        composer=AnswerComposer(ScriptedLLM(["Conversion is healthy."])),
        certification="certified",
    )
    assert res.trace.status == "ok"


def test_pipeline_knob_lets_a_by_name_question_serve_unverified() -> None:
    """serve_ungoverned_shapes governs BOTH entry points of the selection
    boundary: a question naming the excluded metric proceeds to generation and
    the composition serves at confidence: unverified (the knob
    was dead on this path — the by-name refusal ignored it)."""
    llm = ScriptedLLM(
        [
            '{"sql": "SELECT avg(converted) AS conversion_rate FROM sessions", '
            '"definition_used": null}',
            "About 42% of sessions convert.",
        ]
    )
    res = run_query(
        question="What was our conversion rate last month?",
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_web_model(selected=False),
        connector=FakeConnector(result=QueryResult(columns=["conversion_rate"], rows=[[0.42]])),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
        serve_ungoverned_shapes=True,
    )
    assert res.trace.status == "ok"
    assert res.response.confidence == "unverified"
    # The grade comes from the knob's own fold (governed_shape), not from a
    # grounding coincidence — the knob's promise is enforced, not incidental.
    assert res.response.verification is not None
    shape = res.response.verification.check("governed_shape")
    assert shape is not None and shape.status == "fail"
    assert res.response.data is not None


# --- A dropped metric COMPOSED from raw columns refuses with the            ---
# --- path (grouped flag, division in prose); the lens knob                  ---
# --- serve_ungoverned_shapes opts back into serving it unverified.          ---


def _composed_shape_model() -> SemanticModel:
    """The lens under test: the selection carries only `session_count`; the shared
    layer also defines the governed numerator and the conversion_rate ratio,
    both dropped — names AND shapes land on the compiled model."""
    return SemanticModel(
        lens="web",
        dialect="duckdb",
        entities=[
            Entity(
                name="sessions",
                source=EntitySource(connection="wh", table="sessions"),
                fields=[
                    Field(name="session_id", type="integer"),
                    Field(name="converted", type="boolean"),
                    Field(name="device", type="string"),
                ],
                metrics=[Metric(name="session_count", agg="count", expr="sessions.session_id")],
            )
        ],
        excluded_metrics=["conversion_rate", "converted_sessions"],
        excluded_metric_shapes={
            "sessions": [
                Metric(
                    name="converted_sessions",
                    agg="count",
                    expr="sessions.session_id",
                    filters=["sessions.converted = true"],
                ),
                Metric(
                    name="conversion_rate",
                    type="ratio",
                    numerator="converted_sessions",
                    denominator="session_count",
                ),
            ]
        },
    )


# The SQL under test: the model never says "conversion rate" in
# the question (that refuses by NAME), it rebuilds the ratio's operands.
_ACT6E_GEN = (
    '{"sql": "SELECT sessions.converted, COUNT(sessions.session_id) AS session_count '
    'FROM sessions AS sessions GROUP BY sessions.converted", "definition_used": null}'
)
_ACT6E_QUESTION = "What share of our sessions end up converted?"
_ACT6E_ROWS = QueryResult(columns=["converted", "session_count"], rows=[[False, 600], [True, 300]])


def test_pipeline_refuses_composed_shape_with_the_path() -> None:
    """The repro: refusal naming conversion_rate and the file-first fix —
    never a prose ratio at low confidence."""
    res = run_query(
        question=_ACT6E_QUESTION,
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_composed_shape_model(),
        connector=_RaisingConnector(),  # would explode: the warehouse is never touched
        generator=GroundedSQLGenerator(ScriptedLLM([_ACT6E_GEN])),
        composer=AnswerComposer(ScriptedLLM()),
    )
    assert res.trace.status == "rejected"
    err = res.trace.error or ""
    assert "conversion_rate" in err
    assert "add 'conversion_rate' to this lens's selection in lens.yaml" in err
    assert "certify this answer" in err
    assert "conversion_rate" in res.response.answer  # decline-with-path, caller-facing
    # nothing served: no data, no SQL echo (the trace keeps the evidence)
    assert res.response.data is None and res.response.sql is None
    assert res.trace.sql and "GROUP BY" in res.trace.sql


def test_pipeline_knob_serves_the_composition_unverified() -> None:
    """serve_ungoverned_shapes: the lens owner opted back in — the same
    composition serves exactly as before the guard, graded unverified."""
    llm = ScriptedLLM([_ACT6E_GEN, "About 33.3% of sessions end up converted."])
    res = run_query(
        question=_ACT6E_QUESTION,
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_composed_shape_model(),
        connector=FakeConnector(result=_ACT6E_ROWS),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
        serve_ungoverned_shapes=True,
    )
    assert res.trace.status == "ok"
    assert res.response.confidence == "unverified"
    # The grade comes from the knob's own fold (governed_shape), not from a
    # grounding coincidence — the knob's promise is enforced, not incidental.
    assert res.response.verification is not None
    shape = res.response.verification.check("governed_shape")
    assert shape is not None and shape.status == "fail"
    assert res.response.data is not None


def test_pipeline_selected_shapes_unaffected_by_shape_boundary() -> None:
    """Selected-metric questions serve normally with shapes present — bare,
    and broken down by an unrelated dimension."""
    for gen_json, result in (
        (
            '{"sql": "SELECT COUNT(sessions.session_id) AS n FROM sessions AS sessions", '
            '"definition_used": null}',
            QueryResult(columns=["n"], rows=[[900]]),
        ),
        (
            '{"sql": "SELECT sessions.device, COUNT(sessions.session_id) AS n '
            'FROM sessions AS sessions GROUP BY sessions.device", "definition_used": null}',
            QueryResult(columns=["device", "n"], rows=[["mobile", 500], ["desktop", 400]]),
        ),
    ):
        llm = ScriptedLLM([gen_json, "Sessions served."])
        res = run_query(
            question="How many sessions did we have?",
            lens_name="web",
            org_id="org-1",
            caller="analyst",
            semantic_model=_composed_shape_model(),
            connector=FakeConnector(result=result),
            generator=GroundedSQLGenerator(llm),
            composer=AnswerComposer(llm),
        )
        assert res.trace.status == "ok", res.trace.error
        assert res.response.data is not None


def test_pipeline_certified_sql_is_exempt_from_shape_check() -> None:
    """A human approved that exact question→SQL — the shape boundary stands down."""
    res = run_query(
        question=_ACT6E_QUESTION,
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_composed_shape_model(),
        connector=FakeConnector(result=_ACT6E_ROWS),
        generator=FixedSQLGenerator(
            "SELECT sessions.converted, COUNT(sessions.session_id) AS session_count "
            "FROM sessions AS sessions GROUP BY sessions.converted"
        ),
        composer=AnswerComposer(ScriptedLLM(["Served under certification."])),
        certification="certified",
    )
    assert res.trace.status == "ok"


def test_pipeline_sibling_hint_only_in_the_exactly_one_case() -> None:
    """'lens `growth` carries conversion_rate' appears iff EXACTLY ONE other
    lens in the org selects the metric."""
    for carriers, hinted in (
        (["growth"], True),
        ([], False),
        (["growth", "marketing"], False),
    ):
        res = run_query(
            question=_ACT6E_QUESTION,
            lens_name="web",
            org_id="org-1",
            caller="analyst",
            semantic_model=_composed_shape_model(),
            connector=_RaisingConnector(),
            generator=GroundedSQLGenerator(ScriptedLLM([_ACT6E_GEN])),
            composer=AnswerComposer(ScriptedLLM()),
            metric_carriers=lambda _m, c=carriers: list(c),
        )
        assert res.trace.status == "rejected"
        hint = "lens 'growth' carries 'conversion_rate'"
        assert (hint in (res.trace.error or "")) is hinted, carriers


# --- Time-grain guarantee: GROUP BY on a raw TIMESTAMP     -------------------
# --- default_time_field repairs to DATE_TRUNC, rejects naming the fix.     ---


def _events_model() -> SemanticModel:
    return SemanticModel(
        lens="web",
        dialect="duckdb",
        entities=[
            Entity(
                name="sessions",
                source=EntitySource(connection="wh", table="sessions"),
                default_time_field="started_at",
                primary_key=["session_id"],
                fields=[
                    Field(name="session_id", type="integer"),
                    Field(name="started_at", type="timestamp"),
                ],
            )
        ],
    )


_RAW_GROUPED = (
    '{"sql": "SELECT sessions.started_at AS period, count(*) AS n FROM sessions AS sessions '
    'GROUP BY sessions.started_at", "definition_used": null}'
)
_BUCKETED = (
    '{"sql": "SELECT DATE_TRUNC(\'month\', sessions.started_at) AS period, count(*) AS n '
    "FROM sessions AS sessions GROUP BY DATE_TRUNC('month', sessions.started_at)\", "
    '"definition_used": null}'
)


def test_pipeline_repairs_raw_timestamp_grouping() -> None:
    """The case: 'per month' grouped by the raw TIMESTAMP → guard-class
    repair naming DATE_TRUNC with the asked period → the bucketed retry serves."""
    llm = _RecordingLLM([_RAW_GROUPED, _BUCKETED, "Sessions per month."])
    res = run_query(
        question="How many sessions per month in 2025?",
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_events_model(),
        connector=FakeConnector(
            result=QueryResult(columns=["period", "n"], rows=[["2025-01-01", 10]])
        ),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert "date_trunc" in (res.response.sql or "").lower()
    assert "DATE_TRUNC('month', started_at)" in llm.prompts[1]
    assert "never the raw timestamp" in llm.prompts[1]


def test_pipeline_rejects_raw_timestamp_grouping_after_repairs() -> None:
    llm = ScriptedLLM([_RAW_GROUPED])  # repeats the raw grouping on every attempt
    res = run_query(
        question="How many sessions per month in 2025?",
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_events_model(),
        connector=FakeConnector(
            result=QueryResult(columns=["period", "n"], rows=[["2025-01-01", 10]])
        ),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "rejected"
    # the rejection names the fix, not just the failure
    assert "DATE_TRUNC('month', started_at)" in (res.trace.error or "")
    assert res.response.sql is None
    assert res.trace.sql and "started_at" in res.trace.sql


def test_pipeline_bucketed_timestamp_passes_untouched() -> None:
    llm = ScriptedLLM([_BUCKETED, "Sessions per month."])
    res = run_query(
        question="How many sessions per month in 2025?",
        lens_name="web",
        org_id="org-1",
        caller="analyst",
        semantic_model=_events_model(),
        connector=FakeConnector(
            result=QueryResult(columns=["period", "n"], rows=[["2025-01-01", 10]])
        ),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.trace.ai_input_tokens == 2  # one generate + one compose: no repair burned


def test_pipeline_refuses_unanswerable_with_named_gap() -> None:
    """The absence signal: when the generator declines because the lens's data
    cannot answer the question, the pipeline refuses with the gap named —
    status 'refused', no SQL, no warehouse touch, no repair consumed. A zero
    from the wrong table would be a wrong answer; this is the alternative."""
    llm = ScriptedLLM(['{"no_answer": "no support-ticket data exists in this lens"}'])

    res = run_query(
        question="How many open support tickets do we have?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
        max_repairs=1,
    )

    assert res.trace.status == "refused"
    assert res.response.sql is None and res.response.data is None
    assert "no support-ticket data" in res.response.answer
    assert res.trace.error == "no support-ticket data exists in this lens"
    # the decline must not trigger the repair cascade (one LLM call only)
    assert res.trace.ai_input_tokens == 1


# --- Dry-run gate: validate — and budget-check where the engine ------------
# --- estimates cost — before any job runs.                                ---


class _DryRunConnector:
    kind = "bigquery"

    def __init__(self, *, valid: bool = True, bytes_estimated: int | None = None) -> None:
        self.valid = valid
        self.bytes_estimated = bytes_estimated
        self.executes = 0

    def introspect(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def dry_run(self, sql):  # type: ignore[no-untyped-def]
        from services.contracts.warehouse import DryRunResult

        return DryRunResult(
            valid=self.valid,
            bytes_estimated=self.bytes_estimated,
            error=None if self.valid else "table not found: `x.y.z`",
        )

    def execute(self, sql, *, read_only=True, row_limit=None):  # type: ignore[no-untyped-def]
        self.executes += 1
        return QueryResult(columns=["n"], rows=[[1]])


def test_dryrun_gate_blocks_invalid_sql_before_execution() -> None:
    llm = ScriptedLLM([_GOOD])  # guard-valid, but the warehouse disagrees
    conn = _DryRunConnector(valid=False)
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=conn,
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "error"
    assert "table not found" in (res.trace.error or "")
    assert conn.executes == 0  # the invalid query never ran
    assert "dryrun" in res.trace.latency_ms


def test_dryrun_gate_blocks_over_budget_queries() -> None:
    llm = ScriptedLLM([_GOOD])
    conn = _DryRunConnector(valid=True, bytes_estimated=5_000_000_000)
    conn.max_bytes = 1_000_000_000  # type: ignore[attr-defined]
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=conn,
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "error"
    assert "budget" in (res.trace.error or "")
    assert conn.executes == 0  # the expensive job never started


def test_dryrun_gate_passes_valid_in_budget_queries() -> None:
    llm = ScriptedLLM([_GOOD, "There are customers."])
    conn = _DryRunConnector(valid=True, bytes_estimated=10)
    conn.max_bytes = 1_000_000_000  # type: ignore[attr-defined]
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=conn,
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert conn.executes == 1


# --- Attribution reads the SERVED SQL, not the generator's self-report.  --------
# --- The model only ever names definitions, so a governed metric's authored   ---
# --- expression drove answers that reported definition_used: null.            ---


def test_pipeline_attributes_the_metric_whose_expression_the_sql_carries() -> None:
    """The served SQL contained the metric's authored expression verbatim
    and the response still reported definition_used: null, so governed-share
    reporting undercounted. The scripted generation reports null here too."""
    llm = ScriptedLLM([_BOOKINGS_FILTERED, "Bookings by segment."])
    res = run_query(
        question="What were total bookings by segment?",
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_sales_model(),
        connector=FakeConnector(result=_bookings_result()),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.response.definition_used == "bookings"
    assert res.trace.definition_used == "bookings"


def test_pipeline_attributes_a_definition_referenced_with_bare_columns() -> None:
    """A definition's sql_expr is table-qualified (`customers.number_of_orders > 1`)
    while the SQL uses the column bare — the qualifier-insensitive matcher already
    in verification is what makes this attribution hold."""
    gen_json = (
        '{"sql": "SELECT count(*) AS n FROM customers WHERE number_of_orders > 1", '
        '"definition_used": null}'
    )
    llm = ScriptedLLM([gen_json, "Some customers ordered more than once."])
    res = run_query(
        question="how many customers ordered more than once?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.response.definition_used == "repeat_customer"


def test_pipeline_attribution_never_overrides_a_declared_self_report() -> None:
    """A self-report naming a DECLARED term is never second-guessed — but one
    naming a term the model never declared is dropped and attribution reads
    the served SQL instead. Basis printed `Computed as \\`X\\`` for invented
    terms — a fabricated provenance, which is worse than no basis line at all."""
    sql_json = (
        '{"sql": "SELECT deals.segment AS segment, SUM(deals.contract_value_eur) AS bookings '
        "FROM northwind.crm.deals AS deals WHERE deals.is_new_customer = true "
        'GROUP BY deals.segment", "definition_used": "%s"}'
    )
    declared = ScriptedLLM([sql_json % "bookings", "Bookings by segment."])
    res = run_query(
        question="What were total bookings by segment?",
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_sales_model(),
        connector=FakeConnector(result=_bookings_result()),
        generator=GroundedSQLGenerator(declared),
        composer=AnswerComposer(declared),
    )
    assert res.response.definition_used == "bookings"

    invented = ScriptedLLM([sql_json % "new_business", "Bookings by segment."])
    res = run_query(
        question="What were total bookings by segment?",
        lens_name="sales_comp",
        org_id="org-1",
        caller="analyst",
        semantic_model=_sales_model(),
        connector=FakeConnector(result=_bookings_result()),
        generator=GroundedSQLGenerator(invented),
        composer=AnswerComposer(invented),
    )
    # The undeclared term is gone; the SQL carries the governed bookings
    # expression, so attribution lands on the real metric.
    assert res.response.definition_used == "bookings"


def test_pipeline_attributes_nothing_when_no_governed_expression_is_used() -> None:
    """Unrelated SQL stays null — attribution must not manufacture governed share."""
    llm = ScriptedLLM([_GOOD, _ANSWER])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.response.definition_used is None


# --- Truncation is never silent: a 271-row answer came back as               ---
# --- exactly 200 rows, and the only tell was the round number.               ---

_LIST_SQL = "SELECT customers.customer_id, customers.customer_name FROM customers"
_LIST_GEN = f'{{"sql": "{_LIST_SQL}", "definition_used": null}}'
_LIST_ANSWER = "Here is every customer."


def _customer_rows(n: int) -> QueryResult:
    return QueryResult(
        columns=["customer_id", "customer_name"], rows=[[i, f"customer {i}"] for i in range(n)]
    )


def _list_query(
    *, rows: int, max_rows: int, compose_rows: int
) -> tuple[_RecordingLLM, PipelineResult]:
    llm = _RecordingLLM([_LIST_GEN, _LIST_ANSWER])
    return llm, run_query(
        question="list every customer",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=_customer_rows(rows)),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
        max_rows=max_rows,
        compose_rows=compose_rows,
    )


def test_pipeline_says_when_the_payload_is_truncated() -> None:
    """The case: 271 rows in, 200 out. The payload carries the flag AND the full
    count, the report fails not_truncated, and the prose says so."""
    _llm, res = _list_query(rows=271, max_rows=200, compose_rows=200)
    assert res.trace.status == "ok"
    data = res.response.data
    assert data is not None
    assert len(data.rows) == 200
    assert data.truncated is True
    assert data.row_count == 271
    assert res.response.verification is not None
    check = res.response.verification.check("not_truncated")
    assert check is not None and check.status == "fail"
    assert "271" in (check.reason or "")
    assert "only the first 200 of 271 result rows" in res.response.answer
    # …and the persisted trace holds what the caller was actually told.
    assert res.trace.row_count == 271
    assert "only the first 200 of 271" in (res.trace.answer or "")


def test_pipeline_decouples_the_payload_cap_from_the_compose_budget() -> None:
    """The row-per-entity deliverable: every row comes back (nothing truncated)
    while the composer still reads only its 200-row prompt budget — and the prose
    names the slice it read instead of implying it summarized everything."""
    llm, res = _list_query(rows=271, max_rows=1000, compose_rows=200)
    data = res.response.data
    assert data is not None
    assert len(data.rows) == 271 and data.truncated is False and data.row_count == 271
    assert res.response.verification is not None
    check = res.response.verification.check("not_truncated")
    assert check is not None and check.status == "pass"
    assert "the first 200 of 271 result rows" in res.response.answer
    # the composer prompt names the slice it was handed, not a bare "truncated"
    assert "Result (271 rows, showing the first 200)" in llm.prompts[-1]


def test_pipeline_adds_no_note_when_nothing_was_cut() -> None:
    _llm, res = _list_query(rows=12, max_rows=1000, compose_rows=200)
    data = res.response.data
    assert data is not None
    assert len(data.rows) == 12 and data.truncated is False and data.row_count == 12
    assert res.response.answer == _LIST_ANSWER  # no note appended at all
    assert "Result (12 rows, all shown)" in _llm.prompts[-1]


# --- …and the fetch cap is not a row count. An earlier fix measured it as        ---
# --- len(result.rows) AFTER the connector applied LIMIT FETCH_CAP, so a 21,056-  ---
# --- row answer reported row_count=5000 as if exact, and a lens whose            ---
# --- max_rows_to_return reached FETCH_CAP went back to truncating in silence.    ---


def test_pipeline_past_the_fetch_cap_reports_a_floor_not_an_exact_count() -> None:
    """More rows than FETCH_CAP: the count is a FLOOR and every surface says so."""
    _llm, res = _list_query(rows=FETCH_CAP + 2000, max_rows=1000, compose_rows=200)
    data = res.response.data
    assert data is not None
    assert len(data.rows) == 1000
    assert data.truncated is True
    # never more than the cap was fetched, and the count is flagged inexact
    assert data.row_count == FETCH_CAP
    assert data.row_count_exact is False
    assert f"more than {FETCH_CAP}" in res.response.answer
    # and it must NOT tell the caller to raise a knob that cannot help them
    assert "max_rows_to_return" not in res.response.answer
    assert res.response.verification is not None
    check = res.response.verification.check("not_truncated")
    assert check is not None and check.status == "fail"
    assert "more than" in (check.reason or "")


def test_pipeline_fetch_cap_truncation_is_never_silent_even_at_the_payload_cap() -> None:
    """The regression: with max_rows_to_return at/above FETCH_CAP
    the payload cap never bites, so truncation used to report False with the prose
    claiming the full result was in the payload — while rows were dropped."""
    _llm, res = _list_query(rows=21056, max_rows=FETCH_CAP, compose_rows=200)
    data = res.response.data
    assert data is not None
    assert len(data.rows) == FETCH_CAP
    assert data.truncated is True  # was False — the silent case
    assert data.row_count_exact is False
    assert "the full result is in the data payload" not in res.response.answer
    assert f"more than {FETCH_CAP}" in res.response.answer
    assert res.response.verification is not None
    check = res.response.verification.check("not_truncated")
    assert check is not None and check.status == "fail"


def test_pipeline_exactly_the_fetch_cap_is_an_exact_count() -> None:
    """The boundary: a result of exactly FETCH_CAP rows was NOT cut, so the count
    is exact and the fetch-cap language must not appear."""
    _llm, res = _list_query(rows=FETCH_CAP, max_rows=FETCH_CAP, compose_rows=200)
    data = res.response.data
    assert data is not None
    assert len(data.rows) == FETCH_CAP
    assert data.row_count == FETCH_CAP and data.row_count_exact is True
    assert data.truncated is False
    assert "more than" not in res.response.answer
    assert res.response.verification is not None
    check = res.response.verification.check("not_truncated")
    assert check is not None and check.status == "pass"


def test_pipeline_past_the_fetch_cap_never_hands_the_composer_an_exact_total() -> None:
    """The prompt must not offer a precise total the model can write into prose."""
    llm, _res = _list_query(rows=FETCH_CAP + 1, max_rows=1000, compose_rows=200)
    assert f"Result ({FETCH_CAP}+ rows, showing the first 200)" in llm.prompts[-1]


def test_pipeline_certified_truncation_demotes_the_grade() -> None:
    """Approved SQL served short is still a short answer — the certification badge
    must not vouch for rows the caller never received."""
    res = run_query(
        question="list every customer",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=_customer_rows(271)),
        generator=FixedSQLGenerator(_LIST_SQL),
        composer=AnswerComposer(ScriptedLLM([_LIST_ANSWER])),
        certification="certified",
        max_rows=200,
    )
    assert res.trace.status == "ok"
    assert res.response.confidence == "partial"  # never "verified" over a short payload
    assert res.response.data is not None and res.response.data.truncated is True
    assert "only the first 200 of 271 result rows" in res.response.answer


# --- The response says WHAT IT IS (`dst query` used to exit 0              ---
# --- when no SQL ever ran, with the reason as prose in `answer`).          ---


def _statuses(res: PipelineResult) -> tuple[str, str]:
    return res.response.status, res.trace.status


def test_response_status_mirrors_the_trace_for_every_outcome() -> None:
    """The trace has always carried the outcome; the RESPONSE carried only prose,
    so the caller — the one who has to branch — was the only party that could not
    see it. Every path must agree, or the exit code the CLI derives is a lie."""
    ok = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(ScriptedLLM([_GOOD, "There are 100 customers."])),
        composer=AnswerComposer(ScriptedLLM(["There are 100 customers."])),
    )
    assert _statuses(ok) == ("ok", "ok")
    assert ok.response.data is not None  # only "ok" carries an answer

    rejected = run_query(
        question="show me all raw payments",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(
            ScriptedLLM(['{"sql": "SELECT * FROM raw_payments", "definition_used": null}'])
        ),
        composer=AnswerComposer(ScriptedLLM(["x"])),
        max_repairs=0,
    )
    assert _statuses(rejected) == ("rejected", "rejected")
    assert rejected.response.data is None

    error = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_RaisingConnector(),
        generator=GroundedSQLGenerator(ScriptedLLM([_GOOD])),
        composer=AnswerComposer(ScriptedLLM(["x"])),
    )
    assert _statuses(error) == ("error", "error")
    assert error.response.data is None

    refused = run_query(
        question="How many open support tickets do we have?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(
            ScriptedLLM(['{"no_answer": "no support-ticket data exists in this lens"}'])
        ),
        composer=AnswerComposer(ScriptedLLM(["x"])),
        max_repairs=1,
    )
    # A decline is its OWN outcome, never folded into "error": it is the product
    # working, and a caller must be able to tell the two apart.
    assert _statuses(refused) == ("refused", "refused")
    assert refused.response.data is None


def test_a_served_answer_is_the_only_status_ok() -> None:
    """The default is "ok", so the invariant worth pinning is the converse: a
    response that carries data is never anything else."""
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=_conn(),
        generator=GroundedSQLGenerator(ScriptedLLM([_GOOD, "100."])),
        composer=AnswerComposer(ScriptedLLM(["100."])),
    )
    assert res.response.status == "ok"
    assert res.response.verification is not None and res.response.confidence is not None


# --- the figure gate — the composer cannot invent ---


def test_figure_gate_retries_composition_once_and_recovers() -> None:
    """First compose invents a figure; the retry — with the failure named
    in-prompt — quotes the rows and serves normally."""
    llm = _RecordingLLM(["There are 99 customers.", "There are 42 customers."])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[42]])),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.response.answer == "There are 42 customers."
    assert res.response.composition is None
    assert res.response.verification is not None
    numeric = res.response.verification.check("numeric_grounding")
    assert numeric is not None and numeric.status == "pass"
    # exactly one retry, and its prompt named the invented figure
    assert len(llm.prompts) == 2
    assert "states 99" in llm.prompts[-1]


def test_figure_gate_falls_back_to_the_data_block_after_two_failures() -> None:
    """Twice-failed prose is withheld: the data block serves behind a
    deterministic frame, `composition: fallback` on the envelope, and the
    failing report kept as the evidence (grade unverified → auto_review)."""
    llm = ScriptedLLM(["There are 99 customers."])  # last repeats: retry fails too
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[42]])),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(llm),
    )
    assert res.trace.status == "ok"
    assert res.response.composition == "fallback"
    # The draft SENTENCE never ships; the fallback QUOTES the rejected figure
    # in its reason so the class is self-diagnosing.
    assert "There are 99 customers." not in res.response.answer
    assert "the answer states 99" in res.response.answer
    assert "in `data`" in res.response.answer
    assert res.response.data is not None and res.response.data.rows == [[42]]
    assert res.response.confidence == "unverified"
    assert res.response.verification is not None
    numeric = res.response.verification.check("numeric_grounding")
    assert numeric is not None and numeric.status == "fail"
    # citations are governance and outlive the withheld prose
    assert {c.type for c in res.response.citations} == {"sql"}
    # first compose + one retry — never a third attempt
    assert res.trace.ai_input_tokens == 2


def test_fallback_renders_the_rows_not_just_their_count() -> None:
    """A withheld answer had the CORRECT values sitting in the payload while
    the caller read a row-count shrug as failure. The
    withheld frame now carries the rows themselves — a deterministic rendering
    of the data, which is exactly what withholding the PROSE degrades to."""
    llm = ScriptedLLM(["Total revenue was 9,999,999."])  # a figure the rows don't hold
    res = run_query(
        question="how much revenue did we recognize?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(
            result=QueryResult(
                columns=["currency", "revenue"],
                rows=[["USD", 4291996.73], ["EUR", 675728.8], ["GBP", 279395.26]],
            )
        ),
        generator=FixedSQLGenerator(
            "SELECT currency, SUM(amount) AS revenue FROM orders GROUP BY currency"
        ),
        composer=AnswerComposer(llm),
    )
    assert res.response.composition == "fallback"
    # The draft sentence never ships (the reason may quote the figure —
    # the audit follow-up: the fallback names the rejected token).
    assert "Total revenue was 9,999,999." not in res.response.answer
    # …but the real per-currency rows do, in the answer text itself.
    assert "USD | 4291996.73" in res.response.answer
    assert "GBP | 279395.26" in res.response.answer


def test_compose_prompt_forbids_cross_row_totals_on_grouped_results() -> None:
    # The upstream half: the composer is TOLD the rows are the answer
    # before it reaches for a scalar; single-row results carry no such line.
    from services.contracts.protocols import GeneratedQuery
    from services.runtime.answer import compose_prompt

    grouped = QueryResult(columns=["currency", "revenue"], rows=[["USD", 1.0], ["EUR", 2.0]])
    _, user = compose_prompt(
        question="how much revenue?",
        generated=GeneratedQuery(sql="SELECT 1"),
        result=grouped,
        semantic_model=jaffle_customer_value(),
    )
    assert "NEVER add rows together" in user
    single = QueryResult(columns=["n"], rows=[[42]])
    _, user = compose_prompt(
        question="how many?",
        generated=GeneratedQuery(sql="SELECT 1"),
        result=single,
        semantic_model=jaffle_customer_value(),
    )
    assert "NEVER add rows together" not in user


def test_point_in_time_answers_state_the_snapshot_date_unprompted() -> None:
    """Marts run behind, and 'current' resolved against
    a lagging snapshot is self-evident the moment the date is in the prose."""
    llm = ScriptedLLM(["There are 42 customers."])
    res = run_query(
        question="how many customers?",
        lens_name="customer_value",
        org_id="o",
        caller="a",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[42]])),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(llm),
        data_as_of="2026-08-19T04:00:00+00:00",
    )
    assert "(data as of 2026-08-19)" in res.response.answer
    # …and never twice when the prose already states it.
    assert res.response.answer.count("2026-08-19") == 1


def test_certified_prose_is_exempt_from_the_figure_gate() -> None:
    """Out of the figure gate's territory, untouched: certified prose is never re-composed by
    the gate — a numeric fail there demotes the grade (partial) and serves."""
    llm = ScriptedLLM(["Revenue was 99."])
    res = run_query(
        question="how much revenue?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=jaffle_customer_value(),
        connector=FakeConnector(result=QueryResult(columns=["n"], rows=[[42]])),
        generator=FixedSQLGenerator("SELECT count(*) AS n FROM customers"),
        composer=AnswerComposer(llm),
        certification="certified",
    )
    # one compose call, no retry, the prose serves as composed
    assert res.trace.ai_input_tokens == 1
    assert res.response.composition is None
    assert "Revenue was 99." in res.response.answer
    assert res.response.confidence == "partial"
