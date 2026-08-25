"""The agent-vs-agent arms, and the fairness rules that make them comparable.

Every test here pins a rigging failure the harness must not have: a strict
JSON parse that grades formatting as task difficulty, an 800-char history that
hides the agent's own SQL, a turn budget that prices a 3.5 ms tool like an 80 s
one, and an instruction to restate the question verbatim that makes the loop
pointless. Offline throughout — ScriptedLLM plants the protocol, so these pin
the harness, not a model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.benchmark.agentic import (
    AgenticBaselineLane,
    AgenticDstLane,
    AgenticMemoLane,
    Budget,
    Memo,
    extract_step,
)
from services.benchmark.certified import CertifiedIndex
from services.benchmark.conventions import MEMO_BYTES, memo_file, render_conventions
from services.benchmark.lanes import DstLane
from services.benchmark.questions import Question
from services.benchmark.runner import (
    QuestionResult,
    agreement,
    is_silent_wrong,
    pass_hat_k,
    run_benchmark,
)
from services.benchmark.world import load_world, schema_summary
from services.contracts.fakes import HashEmbedder, ScriptedLLM
from services.contracts.protocols import CacheableBlock, LLMResult, Message
from services.contracts.semantic_model import Definition
from services.lenses.describe import describe_model

VERBS = {"sql", "ask", "describe", "done"}


@pytest.fixture()
def mini_world(tmp_path: Path) -> tuple[Path, dict]:
    crm = tmp_path / "csv" / "crm"
    crm.mkdir(parents=True)
    crm.joinpath("customers.csv").write_text(
        "customer_id,customer_name\nC1,Vaskilahden kaupunki\nC2,Tammiketju Oy\n", encoding="utf-8"
    )
    db = tmp_path / "world.duckdb"
    load_world(tmp_path / "csv", db)
    return db, {"customers_count": 2}


COUNT_Q = [
    Question(
        id="q-count",
        category="counts",
        question="How many customers?",
        oracle_path=["customers_count"],
        kind="count",
    )
]
COUNT_SQL = "SELECT COUNT(*) AS n FROM crm.customers"


class CyclingLLM:
    """ScriptedLLM that wraps around, so each agent run replays the same script."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self._i = 0

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        text = self._responses[self._i % len(self._responses)]
        self._i += 1
        return LLMResult(text=text, input_tokens=1, output_tokens=1)


class RecordingLLM:
    """ScriptedLLM plus the calls it saw — for asserting on prompts and history."""

    def __init__(self, responses: list[str]) -> None:
        self._inner = ScriptedLLM(responses)
        self.systems: list[str] = []
        self.messages: list[list[Message]] = []

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        self.systems.append("\n".join(b.text for b in system))
        self.messages.append(list(messages))
        return self._inner.complete(
            system=system,
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )


# --- protocol extraction: formatting is not task difficulty -------------------


def test_extract_step_accepts_fenced_bare_and_embedded_json():
    assert extract_step('{"sql": "SELECT 1"}', VERBS) == {"sql": "SELECT 1"}
    fenced = '```json\n{"sql": "SELECT 1"}\n```'
    assert extract_step(fenced, VERBS) == {"sql": "SELECT 1"}
    prose = 'Let me look at the customers table.\n\n{"sql": "SELECT 1"}\n\nThat should do it.'
    assert extract_step(prose, VERBS) == {"sql": "SELECT 1"}
    assert extract_step("I am not going to reply in JSON.", VERBS) is None


def test_extract_step_survives_braces_inside_sql_strings():
    text = '{"sql": "SELECT \'{not json}\' AS x, \\"quoted\\" FROM t"}'
    step = extract_step(text, VERBS)
    assert step is not None and "not json" in str(step["sql"])


def test_extract_step_takes_the_last_verb_object_not_a_narrated_example():
    text = 'First I could try {"sql": "SELECT 1"} but actually:\n{"sql": "SELECT 2"}'
    assert extract_step(text, VERBS) == {"sql": "SELECT 2"}


def test_protocol_failures_are_counted_and_do_not_consume_the_call_budget(mini_world):
    db, oracle = mini_world
    llm = ScriptedLLM(
        [
            "I think I should start by looking at the customers table.",  # unparseable
            f'{{"sql": "{COUNT_SQL}"}}',
            '{"done": true}',
        ]
    )
    # One tool call is the WHOLE budget: if the nudge consumed it, nothing runs.
    lane = AgenticBaselineLane(llm, db, schema_summary(db), model="scripted", budget=Budget(1))
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.correct, result.reason
    assert result.calls == 1
    assert result.protocol_failures == 1


def test_a_model_that_never_complies_stops_and_reports_protocol_failures(mini_world):
    db, oracle = mini_world
    lane = AgenticBaselineLane(
        ScriptedLLM(["never json"]), db, schema_summary(db), model="scripted", budget=Budget(6)
    )
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.calls == 0
    assert result.protocol_failures >= 4  # nudged repeatedly, then gave up
    assert result.outcome == "declined"  # a declined run, visibly a PROTOCOL decline
    assert "unparseable" in (result.error or "")


def test_a_protocol_broken_run_earns_nothing_on_an_absent_question(mini_world):
    """The flattery this harness used to pay: `absent` questions reward a
    decline, and a JSON-broken agent returns no rows, so it scored a point for
    being unable to format. A run with zero tool calls never engaged."""
    db, oracle = mini_world
    absent = [
        Question(
            id="q-absent",
            category="absence",
            question="How many support tickets did we close?",
            oracle_path=["customers_count"],
            kind="absent",
        )
    ]
    broken = AgenticBaselineLane(
        ScriptedLLM(["never json"]), db, schema_summary(db), model="scripted", budget=Budget(4)
    )
    (result,) = run_benchmark([broken], absent, oracle)
    assert not result.correct and result.outcome == "declined"
    assert "protocol failure" in result.reason

    # A real refusal — the agent looked, found nothing, and said so — still scores.
    honest = AgenticBaselineLane(
        ScriptedLLM(['{"sql": "SELECT 1 WHERE false"}', '{"done": true}']),
        db,
        schema_summary(db),
        model="scripted",
        budget=Budget(4),
    )
    (ok,) = run_benchmark([honest], absent, oracle)
    assert ok.correct


def test_protocol_failure_rate_is_a_first_class_report_column(mini_world):
    from services.benchmark.runner import render_markdown

    db, oracle = mini_world
    lane = AgenticBaselineLane(
        ScriptedLLM(["never json"]), db, schema_summary(db), model="scripted", budget=Budget(2)
    )
    report = render_markdown(run_benchmark([lane], COUNT_Q, oracle))
    assert "protocol-fail" in report
    assert "| 100.0% |" in report  # every run hit the protocol, not the task


# --- history: an agent must see the SQL it just ran --------------------------


def test_assistant_history_is_not_truncated(mini_world):
    db, oracle = mini_world
    long_sql = COUNT_SQL + " -- " + "x" * 1500  # a real analytics query's length
    llm = RecordingLLM([f'{{"sql": "{long_sql}"}}', '{"done": true}'])
    lane = AgenticBaselineLane(llm, db, schema_summary(db), model="scripted", budget=Budget(4))
    run_benchmark([lane], COUNT_Q, oracle)
    history = "".join(m.content for m in llm.messages[-1] if m.role == "assistant")
    assert long_sql in history  # the old 800-char cut hid the agent's own query


# --- budget: equal budget must mean equal cost -------------------------------


def test_budget_parse_and_label():
    assert Budget.parse("4") == Budget(4, Budget().seconds, Budget().tokens)
    assert Budget.parse("4:120") == Budget(4, 120.0, Budget().tokens)
    assert Budget.parse("4:120:9000") == Budget(4, 120.0, 9000)
    assert Budget(8, 600, 200_000).label == "8c-600s-200kt"
    with pytest.raises(ValueError, match="empty budget"):
        Budget.parse("")


def test_wall_clock_budget_binds_independently_of_tool_calls(mini_world):
    db, oracle = mini_world
    llm = ScriptedLLM([f'{{"sql": "{COUNT_SQL}"}}'])
    lane = AgenticBaselineLane(
        llm, db, schema_summary(db), model="scripted", budget=Budget(tool_calls=99, seconds=0.0)
    )
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.calls == 0 and "wall-clock" in (result.error or "")


def test_token_budget_binds_independently_of_tool_calls(mini_world):
    db, oracle = mini_world
    # ScriptedLLM bills 2 tokens per call — one call exhausts a 2-token budget.
    llm = ScriptedLLM([f'{{"sql": "{COUNT_SQL}"}}'])
    lane = AgenticBaselineLane(
        llm, db, schema_summary(db), model="scripted", budget=Budget(tool_calls=99, tokens=2)
    )
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.calls == 1 and result.correct  # ran once, then the tokens were gone


# --- the answer the agent designates ----------------------------------------


def test_agent_designates_its_answer_instead_of_restating_the_question(mini_world):
    db, oracle = mini_world
    llm = ScriptedLLM(
        [
            f'{{"sql": "{COUNT_SQL}"}}',  # step 1 — the right answer
            '{"sql": "SELECT 999 AS n"}',  # step 2 — a later, wrong probe
            '{"done": true, "answer_from": 1}',
        ]
    )
    lane = AgenticBaselineLane(llm, db, schema_summary(db), model="scripted", budget=Budget(6))
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.correct, result.reason  # graded step 1, not the last observation


def test_an_out_of_range_designation_falls_back_to_the_last_step(mini_world):
    db, oracle = mini_world
    llm = ScriptedLLM([f'{{"sql": "{COUNT_SQL}"}}', '{"done": true, "answer_from": 7}'])
    lane = AgenticBaselineLane(llm, db, schema_summary(db), model="scripted", budget=Budget(6))
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.correct, result.reason


def test_both_sql_arms_and_the_lens_arm_get_the_same_shape_contract(mini_world):
    db, _ = mini_world
    schema = schema_summary(db)
    baseline = AgenticBaselineLane(RecordingLLM([]), db, schema, model="scripted")
    memo = AgenticMemoLane(RecordingLLM([]), db, schema, [Memo("notes")], model="scripted")
    inner = DstLane(ScriptedLLM([]), db, model="scripted", name="lens")
    dst = AgenticDstLane(inner, RecordingLLM([]), model="scripted")
    prompts = [lane._loop._system for lane in (baseline, memo, dst)]
    contract = "graded exactly as it stands"
    assert all(contract in p for p in prompts)
    assert not any("verbatim" in p for p in prompts)  # the old rig, gone


# --- the lens arm: describe is free, observations carry trust labels ---------


def test_describe_is_free_of_the_call_budget_and_shows_what_the_lens_knows(mini_world):
    db, oracle = mini_world
    llm = ScriptedLLM(
        [
            '{"describe": true}',
            '{"ask": "How many customers do we have?"}',
            COUNT_SQL,  # inner lens: generation
            "There are 2 customers.",  # inner lens: composition
            '{"done": true}',
        ]
    )
    inner = DstLane(
        llm,
        db,
        model="scripted",
        instructions="Return one row with one number for totals.",
        definitions=[Definition(term="customer", body="a billed account, not a lead")],
        name="lens",
    )
    # One tool call of budget: describe must not have eaten it.
    lane = AgenticDstLane(inner, llm, model="scripted", budget=Budget(1))
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.correct, result.reason
    assert result.calls == 1 and result.free_calls == 1
    # Zero LLM calls, zero warehouse hits — and it carries the real knowledge.
    assert "billed account, not a lead" in lane._describe
    assert "Return one row with one number" in lane._describe


def test_observations_carry_sql_certification_and_confidence(mini_world):
    db, oracle = mini_world
    index = CertifiedIndex([("How many customers?", COUNT_SQL)], HashEmbedder())
    llm = ScriptedLLM(
        [
            '{"ask": "How many customers?"}',
            "There are 2 customers.",  # certified: generation skipped
            '{"done": true}',
        ]
    )
    inner = DstLane(llm, db, model="scripted", certified_index=index, name="lens")
    lane = AgenticDstLane(inner, llm, model="scripted", budget=Budget(3))
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.correct, result.reason
    assert result.certification == "certified"
    transcript = result.transcript or ""
    assert "certification: certified" in transcript
    assert "confidence:" in transcript
    assert "sql: SELECT COUNT(*)" in transcript


# --- MEMO: the control that makes any arm interpretable ----------------------


def _lens_with_conventions(db: Path) -> DstLane:
    return DstLane(
        ScriptedLLM([]),
        db,
        model="scripted",
        instructions="Totals are one row, one number.",
        definitions=[Definition(term="active customer", body="invoiced in the last 90 days")],
        name="lens",
    )


def test_memo_arm_carries_the_lens_conventions_into_its_prompt(mini_world):
    db, oracle = mini_world
    text = memo_file(_lens_with_conventions(db))
    llm = RecordingLLM([f'{{"sql": "{COUNT_SQL}"}}', '{"done": true}'])
    lane = AgenticMemoLane(
        llm, db, schema_summary(db), [Memo(text)], model="scripted", budget=Budget(4)
    )
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.correct, result.reason
    assert lane.name == "agent-memo"
    prompt = llm.systems[0]
    assert "invoiced in the last 90 days" in prompt  # the definition, as prose
    assert "Totals are one row, one number." in prompt  # the standing orders
    assert "crm.customers" in prompt  # still knows what physically exists


def test_memo_is_built_at_cubes_measured_size(mini_world):
    """4 KB is the published size for a semantic-context memo of this kind.
    Bigger is a different experiment; smaller is a strawman."""
    db, _ = mini_world
    lens = DstLane(
        ScriptedLLM([]),
        db,
        model="scripted",
        instructions="Totals are one row, one number.",
        definitions=[Definition(term=f"term-{i}", body="x" * 500) for i in range(40)],
        name="lens",
    )
    text = memo_file(lens)
    assert MEMO_BYTES == 4096
    assert len(text) <= MEMO_BYTES
    assert len(text) > MEMO_BYTES * 0.8  # the budget is spent, not wasted
    # Packed most-decisive-first: the standing orders survive the truncation.
    assert "Totals are one row, one number." in text


def test_memo_cap_is_adjustable_so_information_parity_is_one_flag_away(mini_world):
    """The cap is an asymmetry AGAINST the memo arms: the lens's retrieved prose
    is not capped at 4 KB. The harness names it at run time; this pins the knob
    that removes it."""
    from services.contracts.protocols import ContextChunk

    db, _ = mini_world
    long_context = "## House rule\n" + ("every euro is net of VAT. " * 400)
    lens = DstLane(
        ScriptedLLM([]),
        db,
        model="scripted",
        context_chunks=[ContextChunk(text=long_context, source="definitions.md")],
        name="lens",
    )
    assert len(memo_file(lens)) <= MEMO_BYTES  # capped by default
    parity = memo_file(lens, cap=len(long_context) * 2)
    assert len(parity) > MEMO_BYTES
    assert parity.count("every euro is net of VAT") > memo_file(lens).count(
        "every euro is net of VAT"
    )


def test_memo_and_the_lens_describe_hold_the_same_knowledge(mini_world):
    """Information parity is structural: both are rendered from the same lens."""
    db, _ = mini_world
    inner = DstLane(
        ScriptedLLM([]),
        db,
        model="scripted",
        instructions="Totals are one row, one number.",
        definitions=[Definition(term="active customer", body="invoiced in the last 90 days")],
        certified_index=CertifiedIndex([("How many customers?", COUNT_SQL)], HashEmbedder()),
        name="lens",
    )
    prose = memo_file(inner)
    lens_view = memo_file(inner, certified_sql=False)
    for fact in ("invoiced in the last 90 days", "Totals are one row, one number."):
        assert fact in prose and fact in lens_view
    # The one deliberate asymmetry, and it favours the razor, not the product.
    assert COUNT_SQL in prose and COUNT_SQL not in lens_view
    assert "How many customers?" in lens_view


def test_memo_plus_lets_the_agent_append_and_carries_it_to_the_next_question(mini_world):
    """MEMO+ is what agent memory does today: the file grows from experience."""
    db, oracle = mini_world
    memo = Memo("# CONVENTIONS.md\n", mutable=True)
    llm = RecordingLLM(
        [
            '{"remember": "customer_name lives on crm.customers, join on customer_id"}',
            f'{{"sql": "{COUNT_SQL}"}}',
            '{"done": true}',
        ]
    )
    lane = AgenticMemoLane(llm, db, schema_summary(db), [memo], model="scripted", budget=Budget(2))
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.correct, result.reason
    assert result.free_calls == 1 and result.calls == 1  # remember cost no tool call
    assert memo.notes() == ["customer_name lives on crm.customers, join on customer_id"]
    # A later run reads the grown file.
    assert "join on customer_id" in memo.read()


def test_a_static_memo_refuses_to_be_written_to():
    memo = Memo("# CONVENTIONS.md\n")
    assert memo.append("something") == "This file is read-only."
    assert memo.notes() == []


def test_memo_times_m_gives_each_session_its_own_copy(mini_world):
    """MEMO×M: M teams, M copies, maintained separately — the drift substrate."""
    db, oracle = mini_world
    memos = [Memo("# CONVENTIONS.md\n", mutable=True) for _ in range(3)]
    llm = CyclingLLM(
        [
            '{"remember": "session note"}',
            f'{{"sql": "{COUNT_SQL}"}}',
            '{"done": true}',
        ]
    )
    lane = AgenticMemoLane(
        llm, db, schema_summary(db), memos, model="scripted", budget=Budget(2), name="agent-memo-x3"
    )
    run_benchmark([lane], COUNT_Q, oracle, repeat=2)
    # Two runs, round-robin over three sessions: two files moved, one did not.
    assert sum(1 for m in memos if m.notes()) == 2
    assert sum(1 for m in memos if not m.notes()) == 1


def test_render_conventions_flags_ambiguous_terms_instead_of_burying_them():
    description = describe_model(
        "lens",
        display_name="Sales",
        description="the sales lens",
        model=_model_with(
            definitions=[
                Definition(
                    term="active",
                    body="two teams disagree",
                    status="ambiguous",
                    possible_mappings=["billing - finance.invoices", "login - app.sessions"],
                )
            ]
        ),
    )
    text = render_conventions(description)
    assert "DO NOT GUESS" in text
    assert "billing - finance.invoices" in text


def test_describe_model_carries_the_lens_instructions():
    """The standing orders reached the generator and never the caller."""
    description = describe_model(
        "lens",
        display_name="Sales",
        description="the sales lens",
        model=_model_with(instructions="Monetary answers are euros as plain numbers."),
    )
    assert description.instructions == "Monetary answers are euros as plain numbers."


def _model_with(*, definitions=None, instructions=None):
    from services.contracts.semantic_model import SemanticModel

    return SemanticModel(
        lens="lens",
        dialect="duckdb",
        definitions=definitions or [],
        ai_instructions=instructions,
    )


# --- consistency: pass^k and agreement --------------------------------------


def _r(lane: str, qid: str, ok: bool, sig: str) -> QuestionResult:
    return QuestionResult(qid, "c", lane, ok, "", None, None, answer_signature=sig)


def test_pass_hat_k_punishes_an_arm_that_is_right_but_not_repeatable():
    # steady: 2/2 correct on both questions. flaky: 1/2 on both — same accuracy
    # as a coin, but pass^2 collapses (tau-bench's point).
    results = [
        _r("steady", "q1", True, "a"),
        _r("steady", "q1", True, "a"),
        _r("steady", "q2", True, "b"),
        _r("steady", "q2", True, "b"),
        _r("flaky", "q1", True, "a"),
        _r("flaky", "q1", False, "z"),
        _r("flaky", "q2", True, "b"),
        _r("flaky", "q2", False, "y"),
    ]
    assert pass_hat_k(results, 1) == {"steady": 1.0, "flaky": 0.5}
    assert pass_hat_k(results, 2) == {"steady": 1.0, "flaky": 0.0}
    assert pass_hat_k(results, 5) == {"steady": None, "flaky": None}  # n < k, say nothing


def test_agreement_scores_the_answer_not_its_correctness():
    results = [
        _r("stable-wrong", "q1", False, "a"),
        _r("stable-wrong", "q1", False, "a"),
        _r("scattered", "q1", False, "a"),
        _r("scattered", "q1", False, "b"),
    ]
    # Reliably wrong is a different problem from unpredictable — both get said.
    assert agreement(results) == {"stable-wrong": 1.0, "scattered": 0.5}


# --- experiment A: enforcement under caller pressure --------------------------


def test_pressure_twins_keep_the_oracle_and_contest_the_delivery():
    from services.benchmark.pressure import pressure_twins

    (twin,) = pressure_twins(COUNT_Q, kind="override")
    assert twin.oracle_path == COUNT_Q[0].oracle_path  # same right answer
    assert twin.tier == "pressure" and twin.id.endswith("-override")
    assert "does not apply" in twin.question  # the caller countermands, in-band
    assert twin.pressure == ()

    (esc,) = pressure_twins(COUNT_Q, kind="escalate")
    assert len(esc.pressure) == 3  # escalating pushback, one per tool call

    (premise,) = pressure_twins(COUNT_Q, kind="false-premise")
    assert "As you know" in premise.question  # the wrong convention, stated as fact

    with pytest.raises(ValueError, match="unknown pressure kind"):
        pressure_twins(COUNT_Q, kind="sulking")


def test_absent_questions_get_no_pressure_twin(mini_world):
    """Their right answer is a decline, so an arm that flails under pressure and
    returns nothing would score as if it had held the line."""
    from services.benchmark.pressure import pressure_twins

    absent = Question(
        id="q-absent",
        category="absence",
        question="How many support tickets?",
        oracle_path=["customers_count"],
        kind="absent",
    )
    assert pressure_twins([absent], kind="escalate") == []
    assert len(pressure_twins([absent, *COUNT_Q], kind="escalate")) == 1


def test_escalating_pressure_reaches_the_agent_between_tool_calls(mini_world):
    from services.benchmark.pressure import pressure_twins

    db, oracle = mini_world
    (twin,) = pressure_twins(COUNT_Q, kind="escalate")
    llm = RecordingLLM([f'{{"sql": "{COUNT_SQL}"}}', f'{{"sql": "{COUNT_SQL}"}}', '{"done": true}'])
    lane = AgenticBaselineLane(llm, db, schema_summary(db), model="scripted", budget=Budget(4))
    run_benchmark([lane], [twin], oracle)
    conversation = "".join(m.content for m in llm.messages[-1] if m.role == "user")
    assert "That is not the shape I asked for" in conversation  # after step 1
    assert "I am the owner of this data" in conversation  # after step 2


def test_enforcement_section_reports_the_drop_not_just_the_level(mini_world):
    from services.benchmark.runner import render_markdown

    calm = QuestionResult("q1", "c", "arm", True, "", None, None, tier="discriminating")
    caved = QuestionResult(
        "q1-override", "pressure/c", "arm", False, "", None, None, tier="pressure"
    )
    report = render_markdown([calm, caved])
    assert "Enforcement — compliance under caller pressure" in report
    assert "-100pp" in report or "−100pp" in report


# --- experiment B: metric drift ----------------------------------------------


def test_drift_counts_distinct_answers_across_sessions_callers_and_stacks(mini_world):
    from services.benchmark.drift import render_drift, run_drift, summarize_drift

    db, _ = mini_world
    question = COUNT_Q[0]

    def steady() -> AgenticBaselineLane:
        return AgenticBaselineLane(
            ScriptedLLM([f'{{"sql": "{COUNT_SQL}"}}', '{"done": true}']),
            db,
            schema_summary(db),
            model="scripted",
            budget=Budget(2),
        )

    scatter = iter(["SELECT 1 AS n", "SELECT 2 AS n", "SELECT 3 AS n", "SELECT 4 AS n"])

    def drifting() -> AgenticBaselineLane:
        return AgenticBaselineLane(
            ScriptedLLM([f'{{"sql": "{next(scatter)}"}}', '{"done": true}']),
            db,
            schema_summary(db),
            model="scripted",
            budget=Budget(2),
        )

    runs = run_drift(
        {"steady": {"stack-a": steady}, "drifting": {"stack-a": drifting}},
        question,
        callers=["cfo", "analyst"],
        sessions=2,
    )
    assert len(runs) == 8  # 2 arms × 1 stack × 2 sessions × 2 callers
    cells = {c.arm: c for c in summarize_drift(runs)}
    assert cells["steady"].distinct == 1 and cells["steady"].modal_share == 1.0
    assert cells["drifting"].distinct == 4  # a different number every session
    report = render_drift(question, runs)
    assert "Distinct ↓" in report and "consumer stack" in report


def test_drift_gives_every_session_a_fresh_lane(mini_world):
    """Each cell builds a NEW lane, so a mutable memo cannot leak between
    sessions — session independence is the thing being counted."""
    from services.benchmark.drift import run_drift

    db, _ = mini_world
    built: list[Memo] = []

    def factory() -> AgenticMemoLane:
        memo = Memo("# CONVENTIONS.md\n", mutable=True)
        built.append(memo)
        return AgenticMemoLane(
            ScriptedLLM([f'{{"sql": "{COUNT_SQL}"}}', '{"done": true}']),
            db,
            schema_summary(db),
            [memo],
            model="scripted",
            budget=Budget(2),
        )

    run_drift({"memo": {"s": factory}}, COUNT_Q[0], callers=[None], sessions=3)
    assert len(built) == 3 and len({id(m) for m in built}) == 3


# --- SWR: the headline ---------------------------------------------------------


def test_silent_wrong_rate_separates_undetected_from_flagged_wrongs():
    def q(outcome: str, confidence: str | None) -> QuestionResult:
        return QuestionResult(
            "q",
            "c",
            "arm",
            outcome == "correct",
            "",
            None,
            None,
            outcome=outcome,
            confidence=confidence,
        )

    assert is_silent_wrong(q("wrong", None))  # raw SQL: silent by construction
    assert is_silent_wrong(q("wrong", "verified"))  # confidently wrong = the worst case
    assert not is_silent_wrong(q("wrong", "unverified"))  # wrong, but it said so
    assert not is_silent_wrong(q("declined", None))  # never delivered an answer
    assert not is_silent_wrong(q("correct", None))


def test_report_leads_with_swr_and_prices_accuracy_on_a_pareto(mini_world):
    from services.benchmark.runner import render_markdown

    cheap = QuestionResult("q1", "c", "cheap", True, "", None, None, ai_cost_usd=0.001)
    dear = QuestionResult("q1", "c", "dear", False, "", None, None, ai_cost_usd=0.10)
    report = render_markdown([cheap, dear])
    assert "SWR ↓" in report
    assert "measures the presence of a file" in report  # the razor, stated in the artifact
    assert "Accuracy–cost Pareto" in report
    assert "dominated" in report  # the dear-and-wrong lane is not on the frontier


def test_consistency_table_appears_only_when_questions_repeat(mini_world):
    from services.benchmark.runner import render_markdown

    db, oracle = mini_world
    lane = AgenticBaselineLane(
        ScriptedLLM([f'{{"sql": "{COUNT_SQL}"}}', '{"done": true}']),
        db,
        schema_summary(db),
        model="scripted",
        budget=Budget(4),
    )
    single = render_markdown(run_benchmark([lane], COUNT_Q, oracle))
    assert "Consistency" not in single
    repeated = render_markdown(run_benchmark([lane], COUNT_Q, oracle, repeat=3))
    assert "Consistency (3 runs/question)" in repeated
    assert "pass^2" in repeated and "agreement" in repeated


# --- fairness rule 7: every arm knows whose behalf it asks on -----------------


PRINCIPALS = {"cfo": "the CFO (CFO)"}
CALLER_Q = Question(
    id="q-caller",
    category="user-context",
    question="What was our revenue in March?",
    oracle_path=["customers_count"],
    kind="scalar",
    caller="cfo",
)


def _first_user_turn(llm: RecordingLLM) -> str:
    return next(m.content for m in llm.messages[0] if m.role == "user")


def test_every_arm_opens_with_the_principals_identity(mini_world):
    db, _ = mini_world
    schema = schema_summary(db)
    for build in (
        lambda llm: AgenticBaselineLane(llm, db, schema, model="scripted", principals=PRINCIPALS),
        lambda llm: AgenticMemoLane(
            llm, db, schema, [Memo("notes")], model="scripted", principals=PRINCIPALS
        ),
        lambda llm: AgenticDstLane(
            DstLane(ScriptedLLM([]), db, model="scripted", name="lens"),
            llm,
            model="scripted",
            principals=PRINCIPALS,
        ),
    ):
        llm = RecordingLLM(['{"done": true}'])
        build(llm).answer(CALLER_Q)
        opening = _first_user_turn(llm)
        assert opening.startswith("Asking on behalf of: the CFO (CFO)."), opening
        assert CALLER_Q.question in opening


def test_identity_only_no_caller_or_unknown_caller_means_the_bare_question(mini_world):
    db, _ = mini_world
    schema = schema_summary(db)
    # No caller on the question → bare text, even with a registry present.
    llm = RecordingLLM(['{"done": true}'])
    AgenticBaselineLane(llm, db, schema, model="scripted", principals=PRINCIPALS).answer(COUNT_Q[0])
    assert _first_user_turn(llm) == COUNT_Q[0].question
    # Caller unknown to the registry → bare text, never a KeyError.
    llm = RecordingLLM(['{"done": true}'])
    stranger = Question(
        id="q-stranger",
        category="counts",
        question="How many customers?",
        oracle_path=["customers_count"],
        kind="count",
        caller="nobody",
    )
    AgenticBaselineLane(llm, db, schema, model="scripted", principals=PRINCIPALS).answer(stranger)
    assert _first_user_turn(llm) == stranger.question


def test_the_agent_protocol_names_the_active_dialect(mini_world):
    """The prompt's dialect word comes from the warehouse seam — a Snowflake
    leg must never instruct the agent to write DuckDB SQL, which is what a
    hardcoded prose dialect in the prompt produces."""
    from services.benchmark.warehouse import DuckDBWarehouse

    db, _ = mini_world
    schema = schema_summary(db)

    class SnowyStub(DuckDBWarehouse):
        dialect = "snowflake"
        dialect_word = "Snowflake"

    for warehouse, word, absent in (
        (db, "DuckDB", "Snowflake"),
        (SnowyStub(db), "Snowflake", "DuckDB"),
    ):
        for lane in (
            AgenticBaselineLane(RecordingLLM([]), warehouse, schema, model="scripted"),
            AgenticMemoLane(RecordingLLM([]), warehouse, schema, [Memo("notes")], model="scripted"),
        ):
            assert f"read-only {word} SELECT" in lane._loop._system
            assert absent not in lane._loop._system


def test_a_path_still_builds_the_duckdb_warehouse_and_lanes_run_unchanged(mini_world):
    """Pre-seam call sites pass a Path; behavior must be identical."""
    db, oracle = mini_world
    llm = ScriptedLLM([f'{{"sql": "{COUNT_SQL}"}}', '{"done": true}'])
    lane = AgenticBaselineLane(llm, db, schema_summary(db), model="scripted")
    (result,) = run_benchmark([lane], COUNT_Q, oracle)
    assert result.correct, result.reason


def test_snowflake_warehouse_introspects_through_the_connector():
    """The Snowflake leg's catalog → schema text, tables, structural model —
    against a stub connector, so no credentials are needed in CI."""
    from services.benchmark.warehouse import SnowflakeWarehouse
    from services.contracts.warehouse import QueryResult

    catalog = [
        ("BRONZE", "DEALS", "DEAL_ID", "NUMBER"),
        ("BRONZE", "DEALS", "STAGE", "TEXT"),
        ("GOLD", "KPIS", "WIN_RATE", "DOUBLE"),
    ]

    class StubConn:
        def execute(self, sql: str, **kw: object) -> QueryResult:
            assert "information_schema.columns" in sql
            return QueryResult(columns=["s", "t", "c", "d"], rows=[list(r) for r in catalog])

    wh = SnowflakeWarehouse(StubConn(), ("bronze", "gold"))  # type: ignore[arg-type]
    assert wh.dialect == "snowflake" and wh.dialect_word == "Snowflake"
    assert wh.tables() == ["BRONZE.DEALS", "GOLD.KPIS"]
    summary = wh.schema_summary()
    assert "BRONZE.DEALS:" in summary and "WIN_RATE DOUBLE" in summary
    model = wh.structural_model()
    assert model.dialect == "snowflake"
    assert [e.source.table for e in model.entities] == ["BRONZE.DEALS", "GOLD.KPIS"]
    scoped = wh.structural_model(include={"GOLD.KPIS"})
    assert [e.source.table for e in scoped.entities] == ["GOLD.KPIS"]


def test_load_callers_splits_identity_from_conventions(tmp_path: Path):
    from services.benchmark.questions import load_callers

    path = tmp_path / "callers.yaml"
    path.write_text(
        "callers:\n"
        "  - id: cfo\n"
        "    name: the CFO\n"
        "    role: CFO\n"
        "    answer_with: Finance definitions. Revenue = net invoiced by issue date.\n",
        encoding="utf-8",
    )
    registry, principals = load_callers(path)
    # The registry keeps the full answering note — the lens's runtime-context rail.
    assert registry["cfo"].startswith("the CFO (CFO). Finance definitions.")
    # Principals carry identity ONLY: leaking answer_with here would hand every
    # arm the caller's conventions outside its own delivery channel.
    assert principals["cfo"] == "the CFO (CFO)"
    assert "Revenue" not in principals["cfo"]
