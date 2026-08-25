"""The benchmark process end to end, offline.

A two-table mini-world stands in for the generator output; ScriptedLLM plants
SQL so the test pins the *process* — world build, lane execution, oracle
binding, grading, report — without a live model or a real generated world.
"""

from pathlib import Path

import pytest

from services.benchmark.grading import grade
from services.benchmark.lanes import BaselineLane, DstFeatures
from services.benchmark.questions import Question, load_questions, resolve_oracle
from services.benchmark.runner import render_markdown, run_benchmark, summarize
from services.benchmark.world import load_world, schema_summary
from services.contracts.fakes import ScriptedLLM

STARTER = Path(__file__).parents[1] / "services" / "benchmark" / "questions" / "starter.yaml"


@pytest.fixture()
def mini_world(tmp_path: Path) -> tuple[Path, dict]:
    crm = tmp_path / "csv" / "crm"
    crm.mkdir(parents=True)
    crm.joinpath("customers.csv").write_text(
        "customer_id,customer_name\nC1,Vaskilahden kaupunki\nC2,Tammiketju Oy\n", encoding="utf-8"
    )
    finance = tmp_path / "csv" / "finance"
    finance.mkdir(parents=True)
    finance.joinpath("invoices.csv").write_text(
        "invoice_id,customer_id,net_amount_eur\nI1,C1,100.50\nI2,C2,49.50\nI3,C1,50.00\n",
        encoding="utf-8",
    )
    oracle = {
        "customers_count": 2,
        "invoiced_net_total_eur": 200.0,
        "top_customer_by_net_invoiced": {"name": "Vaskilahden kaupunki", "value_eur": 150.5},
    }
    db = tmp_path / "world.duckdb"
    load_world(tmp_path / "csv", db)
    return db, oracle


QUESTIONS = [
    Question(
        id="q-count",
        category="counts",
        question="How many customers?",
        oracle_path=["customers_count"],
        kind="count",
    ),
    Question(
        id="q-total",
        category="totals",
        question="Total net invoiced?",
        oracle_path=["invoiced_net_total_eur"],
        kind="scalar",
    ),
    Question(
        id="q-top",
        category="tops",
        question="Top customer by net?",
        oracle_path=["top_customer_by_net_invoiced"],
        kind="top",
    ),
]


def test_world_loads_schemas_and_summary(mini_world):
    db, _ = mini_world
    summary = schema_summary(db)
    assert "crm.customers" in summary and "finance.invoices" in summary
    assert "net_amount_eur" in summary


def test_baseline_lane_graded_against_oracle(mini_world):
    db, oracle = mini_world
    llm = ScriptedLLM(
        [
            "```sql\nSELECT COUNT(*) FROM crm.customers\n```",
            "SELECT SUM(net_amount_eur) FROM finance.invoices",
            # Wrong on purpose: right name, wrong value — must be graded wrong.
            "SELECT 'Vaskilahden kaupunki' AS name, 999.0 AS total",
        ]
    )
    lane = BaselineLane(llm, db, schema_summary(db), model="scripted")
    results = run_benchmark([lane], QUESTIONS, oracle)
    by_id = {r.question_id: r for r in results}
    assert by_id["q-count"].correct
    assert by_id["q-total"].correct
    assert not by_id["q-top"].correct and "wrong value" in by_id["q-top"].reason
    (summary,) = summarize(results)
    assert (summary.correct, summary.total) == (2, 3)
    report = render_markdown(results)
    assert "baseline" in report and "66.7%" in report


def test_lane_error_is_a_graded_miss_not_a_crash(mini_world):
    db, oracle = mini_world
    lane = BaselineLane(ScriptedLLM(["SELECT * FROM nonexistent.table"]), db, "", model="scripted")
    results = run_benchmark([lane], QUESTIONS[:1], oracle)
    assert not results[0].correct
    assert "lane error" in results[0].reason


def test_dst_lane_runs_the_real_pipeline(mini_world):
    from services.benchmark.lanes import DstLane

    db, oracle = mini_world
    # Call order in the pipeline: generate → (guard/execute) → compose.
    llm = ScriptedLLM(["SELECT COUNT(*) AS n FROM crm.customers", "There are 2 customers."])
    lane = DstLane(llm, db, model="scripted")
    results = run_benchmark([lane], QUESTIONS[:1], oracle)
    assert results[0].correct, results[0].reason
    assert results[0].lane == "dst"
    assert "crm.customers" in (results[0].sql or "")


def test_certified_rung_serves_approved_sql_without_generation(mini_world):
    from services.benchmark.certified import CertifiedIndex
    from services.benchmark.lanes import DstLane
    from services.contracts.fakes import HashEmbedder

    db, oracle = mini_world
    index = CertifiedIndex(
        [("How many customers?", "SELECT COUNT(*) AS n FROM crm.customers")],
        HashEmbedder(),  # identical text → cosine 1.0; anything else won't match
    )
    # Only the composer needs the LLM — generation is skipped entirely.
    llm = ScriptedLLM(["There are 2 customers."])
    lane = DstLane(llm, db, model="scripted", certified_index=index, name="dst-certified")
    results = run_benchmark([lane], QUESTIONS[:1], oracle)
    assert results[0].correct, results[0].reason
    assert results[0].sql == "SELECT COUNT(*) AS n FROM crm.customers"


def test_certified_templates_match_and_escape():
    from services.benchmark.certified import CertifiedIndex
    from services.contracts.fakes import HashEmbedder

    index = CertifiedIndex(
        [],
        HashEmbedder(),
        templates=[
            (
                r"overdue receivable does the customer named '(?P<name>.+)' have",
                "SELECT x FROM t WHERE name = '{name}'",
            )
        ],
    )
    hit = index.match(
        "How much overdue receivable does the customer named 'Tammiketju Oy' have, in euros?"
    )
    assert hit is not None and hit.sql == "SELECT x FROM t WHERE name = 'Tammiketju Oy'"
    inj = index.match("How much overdue receivable does the customer named 'O''Brien; DROP' have?")
    assert inj is not None and "''" in inj.sql and ";" in inj.sql  # escaped literal, inert
    assert index.match("What was our revenue in March?") is None


def test_certified_index_threshold_rejects_non_matches():
    from services.benchmark.certified import CertifiedIndex
    from services.contracts.fakes import HashEmbedder

    index = CertifiedIndex([("How many customers?", "SELECT 1")], HashEmbedder())
    assert index.match("How many customers?") is not None
    assert index.match("What was revenue in March?") is None  # below 0.95, no serve


def test_feature_stripping_surface():
    full = DstFeatures()
    stripped = full.stripped(["context", "repair"])
    assert not stripped.context and not stripped.repair
    assert full.context and full.repair  # frozen: stripping returns a new config
    with pytest.raises(ValueError, match="unknown features"):
        full.stripped(["warp_drive"])


def test_stale_world_discloses_with_contract_and_not_without(mini_world):
    """The --stale-days pair: same stale world, the contract lane discloses
    (freshness fail -> stale_disclosed True), the stripped lane serves it
    silently fresh — the F-48 shape the experiment exists to count."""
    from services.benchmark.lanes import DstFeatures, DstLane

    db, oracle = mini_world
    contract = DstLane(
        ScriptedLLM(["SELECT COUNT(*) AS n FROM crm.customers", "There are 2 customers."]),
        db,
        model="scripted",
        stale_days=30,
    )
    undeclared = DstLane(
        ScriptedLLM(["SELECT COUNT(*) AS n FROM crm.customers", "There are 2 customers."]),
        db,
        model="scripted",
        features=DstFeatures().stripped(["freshness"]),
        stale_days=30,
        name="dst-without-freshness",
    )
    results = run_benchmark([contract, undeclared], QUESTIONS[:1], oracle)
    by_lane = {r.lane: r for r in results}
    assert by_lane["dst"].stale_disclosed is True
    assert by_lane["dst-without-freshness"].stale_disclosed is False
    # Without --stale-days the signal stays None (freshness not under test).
    plain = DstLane(
        ScriptedLLM(["SELECT COUNT(*) AS n FROM crm.customers", "ok."]), db, model="scripted"
    )
    assert run_benchmark([plain], QUESTIONS[:1], oracle)[0].stale_disclosed is None


STARTER_ORACLE = {
    "overdue_by_customer": {f"Customer {c} Oy": 1.0 for c in "ABCDEFGH"},
}


def test_starter_questions_load_and_kinds_are_valid():
    questions = load_questions(STARTER, STARTER_ORACLE)
    assert len(questions) >= 12
    assert {q.kind for q in questions} <= {"scalar", "ratio", "count", "top", "absent"}
    assert any(q.lang == "fi" for q in questions)  # the Finnish phrasing stays
    # The entity question bound to a customer the world actually contains —
    # never a hardcoded name the generator is free to drop (the Havitek drift).
    (entity,) = [q for q in questions if q.category == "entity-lookup"]
    assert "[[" not in entity.question
    assert entity.oracle_path[1] in STARTER_ORACLE["overdue_by_customer"]
    assert entity.oracle_path[1] in entity.question


def test_oracle_binding_is_deterministic_and_fails_loudly_without_an_oracle(tmp_path):
    from services.benchmark.questions import _bind

    oracle = {"overdue_by_customer": {"B Oy": 1, "A Oy": 2, "C Oy": 3}}
    # 1-based over sorted keys; wraps rather than crashing on a shrunken world.
    assert _bind("[[overdue_by_customer#2]]", oracle) == "B Oy"
    assert _bind("[[overdue_by_customer#5]]", oracle) == "B Oy"
    with pytest.raises(ValueError, match="no oracle was given"):
        _bind("[[overdue_by_customer#1]]", None)
    with pytest.raises(ValueError, match="no node"):
        _bind("[[not_a_node#1]]", oracle)


def test_unresolved_lists_every_orphan_before_any_lane_runs():
    from services.benchmark.questions import unresolved

    questions = load_questions(STARTER, STARTER_ORACLE)
    orphans = unresolved(questions, {"customers_count": 1})
    assert len(orphans) == len(questions) - 1  # everything but counts is orphaned
    assert all(": " in line for line in orphans)  # "id: oracle / path", greppable
    assert unresolved(questions[:0], {}) == []


def test_oracle_path_miss_fails_loudly():
    with pytest.raises(KeyError, match="overdue_by_customer / Lumikanta Oyj"):
        resolve_oracle({"overdue_by_customer": {}}, ["overdue_by_customer", "Lumikanta Oyj"])


def test_scoped_model_and_canon_scope(mini_world):
    from services.benchmark.world import list_tables, scope_from_certified_defs, structural_model

    db, _ = mini_world
    tables = list_tables(db)
    assert set(tables) == {"crm.customers", "finance.invoices"}
    scoped = structural_model(db, include={"crm.customers"})
    assert [e.source.table for e in scoped.entities] == ["crm.customers"]
    # certified scoping: silver/gold always in; bronze only when the certified names it
    scope = scope_from_certified_defs(
        "use bronze.netvisor__invoices per the certified",
        ["silver.a", "gold.b", "bronze.netvisor__invoices", "bronze.noise"],
    )
    assert scope == {"silver.a", "gold.b", "bronze.netvisor__invoices"}


def test_lane_answers_carry_cost_and_latency(mini_world):
    db, oracle = mini_world
    llm = ScriptedLLM(["SELECT COUNT(*) FROM crm.customers"])
    lane = BaselineLane(llm, db, schema_summary(db), model="claude-haiku-4-5")
    results = run_benchmark([lane], QUESTIONS[:1], oracle)
    assert results[0].ai_cost_usd > 0  # ScriptedLLM reports 1 token each way
    assert results[0].latency_ms > 0
    report = render_markdown(results)
    assert "¢/question" in report and "s/question" in report


def test_grading_tolerances():
    assert grade("scalar", 100.0, ["v"], [[100.01]]).correct  # within a cent
    assert not grade("scalar", 100.0, ["v"], [[100.5]]).correct
    assert grade("ratio", 0.616, ["v"], [[0.6162]]).correct
    assert not grade("count", 5, ["v"], [[4]]).correct
    assert not grade("scalar", 100.0, ["v"], []).correct  # empty result is a miss


def test_grading_top_name_split_across_columns():
    # first_name / last_name columns — the live-run grader bug, pinned.
    expected = {"name": "Maria Mattila", "value_eur": 4939200.0}
    cols = ["first_name", "last_name", "total"]
    assert grade("top", expected, cols, [["Maria", "Mattila", 4939200.0]]).correct
    g = grade("top", expected, cols, [["Maria", "Mattila", 999.0]])
    assert not g.correct and "wrong value" in g.reason


def test_grading_null_scalar_says_so():
    g = grade("scalar", 100.0, ["v"], [[None]])
    assert not g.correct and "NULL result" in g.reason


def test_absent_kind_rewards_decline_punishes_hallucination():
    from services.benchmark.grading import grade_absent

    assert grade_absent([], "no SQL in model output").correct  # declining is right
    assert grade_absent([["we have no ticketing data"]], None).correct  # prose, no number
    g = grade_absent([[42.0]], None)
    assert not g.correct and "hallucinated" in g.reason


def test_expand_from_oracle_binds_every_instance():
    from services.benchmark.expand import expand_from_oracle

    oracle = {
        "overdue_by_customer": {"Tammiketju Oy": 10.0, "Havitek Suomi Oy": 5.0},
        "won_value_by_rep": {"Maria Mattila": 99.0},
        "invoiced_net_by_year": {"2025": 1.0, "2026": 2.0},
        "not_a_map": 42,
    }
    questions = expand_from_oracle(oracle)
    assert len(questions) == 5
    ids = [q.id for q in questions]
    assert len(set(ids)) == len(ids)
    for q in questions:
        assert resolve_oracle(oracle, q.oracle_path) is not None  # every binding resolves
    assert any("Havitek Suomi Oy" in q.question for q in questions)


def test_wilson_ci_and_paired_comparison():
    from services.benchmark.runner import paired_vs_first, wilson_ci

    lo, hi = wilson_ci(12, 14)
    assert lo < 12 / 14 < hi and (hi - lo) > 0.2  # n=14 is honest about itself
    lo_big, hi_big = wilson_ci(120, 140)
    assert (hi_big - lo_big) < (hi - lo) / 2  # 10x data shrinks the interval

    def _r(lane: str, qid: str, ok: bool):
        from services.benchmark.runner import QuestionResult

        return QuestionResult(qid, "c", lane, ok, "", None, None)

    results = [
        _r("baseline", "q1", True),
        _r("baseline", "q2", False),
        _r("dst", "q1", True),
        _r("dst", "q2", True),
    ]
    assert paired_vs_first(results) == {"dst": (1, 0)}


def test_parallel_workers_preserve_order_and_results(mini_world):
    from services.contracts.protocols import CacheableBlock, LLMResult, Message

    class ConstantLLM:  # thread-safe, unlike ScriptedLLM
        def complete(self, *, system, messages, model, temperature, max_tokens):
            return LLMResult(text="SELECT COUNT(*) FROM crm.customers")

        _ = (CacheableBlock, Message)  # keep the contract imports honest

    db, oracle = mini_world
    lane = BaselineLane(ConstantLLM(), db, schema_summary(db), model="const")
    seq = run_benchmark([lane], QUESTIONS, oracle, workers=1)
    par = run_benchmark([lane], QUESTIONS, oracle, workers=4)
    assert [r.question_id for r in par] == [r.question_id for r in seq]
    assert [r.correct for r in par] == [r.correct for r in seq]


def test_repeat_runs_each_question_n_times(mini_world):
    db, oracle = mini_world
    llm = ScriptedLLM(["SELECT COUNT(*) FROM crm.customers"])
    lane = BaselineLane(llm, db, schema_summary(db), model="scripted")
    results = run_benchmark([lane], QUESTIONS[:1], oracle, repeat=3)
    assert len(results) == 3
    assert all(r.question_id == "q-count" for r in results)


def test_spider_adapter_offline(tmp_path):
    """Mini Spider repo: JSON-table db, task, expected CSV → materialize, grade."""
    import json as _json

    from services.benchmark import spider

    lite = tmp_path / "spider2-lite"
    dbdir = lite / "resource" / "databases" / "spider2-localdb"
    dbdir.mkdir(parents=True)
    import sqlite3

    sconn = sqlite3.connect(dbdir / "MiniShop.sqlite")
    sconn.execute("CREATE TABLE orders (id INTEGER, amount REAL)")
    sconn.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 10.5), (2, 4.5)])
    sconn.commit()
    sconn.close()
    gold = lite / "evaluation_suite" / "gold" / "exec_result"
    gold.mkdir(parents=True)
    gold.joinpath("local901.csv").write_text("total\n15.0\n", encoding="utf-8")
    lite.joinpath("spider2-lite.jsonl").write_text(
        _json.dumps(
            {
                "instance_id": "local901",
                "db": "MiniShop",
                "question": "Total order amount?",
                "external_knowledge": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    spider._CACHE = tmp_path / "cache"  # keep the test hermetic
    tasks = spider.load_spider_tasks(tmp_path, sample=5)
    assert len(tasks) == 1 and tasks[0].expected == [[["total"], ["15.0"]]]

    db = spider.materialize_db(tmp_path, "MiniShop")
    import duckdb as _duckdb

    assert (
        _duckdb.connect(str(db), read_only=True)
        .execute("SELECT SUM(amount) FROM spider.orders")
        .fetchone()[0]
        == 15.0
    )

    from services.benchmark.lanes import LaneAnswer

    ok = spider.grade_exec(LaneAnswer(columns=["t"], rows=[[15.0]], sql="x"), tasks[0].expected)
    assert ok.correct
    near = spider.grade_exec(
        LaneAnswer(columns=["t"], rows=[[15.0000001]], sql="x"), tasks[0].expected
    )
    assert near.correct  # float tolerance
    bad = spider.grade_exec(LaneAnswer(columns=["t"], rows=[[14.0]], sql="x"), tasks[0].expected)
    assert not bad.correct


def test_agentic_lanes_loop_and_count_calls(mini_world):
    from services.benchmark.agentic import AgenticBaselineLane, AgenticDstLane, Budget
    from services.benchmark.lanes import DstLane

    db, oracle = mini_world
    # baseline agent: explore (wrong query), then the right one, then done.
    llm = ScriptedLLM(
        [
            '{"sql": "SELECT customer_name FROM crm.customers"}',
            '{"sql": "SELECT COUNT(*) AS n FROM crm.customers"}',
            '{"done": true}',
        ]
    )
    lane = AgenticBaselineLane(
        llm, db, schema_summary(db), model="claude-haiku-4-5", budget=Budget(4)
    )
    results = run_benchmark([lane], QUESTIONS[:1], oracle)
    assert results[0].correct, results[0].reason
    assert results[0].calls == 2  # two tool calls, answer taken from the last

    # dst agent: one governed ask, then done (inner lane = real pipeline).
    llm2 = ScriptedLLM(
        [
            '{"ask": "How many customers do we have?"}',
            "SELECT COUNT(*) AS n FROM crm.customers",  # inner lens generation
            "There are 2 customers.",  # inner lens composer
            '{"done": true}',
        ]
    )
    inner = DstLane(llm2, db, model="scripted", name="lens")
    klane = AgenticDstLane(inner, llm2, model="claude-haiku-4-5", budget=Budget(4))
    kres = run_benchmark([klane], QUESTIONS[:1], oracle)
    assert kres[0].correct, kres[0].reason
    assert kres[0].calls == 1  # one governed ask was enough
