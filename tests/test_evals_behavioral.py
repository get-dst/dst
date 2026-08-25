"""The case transition: behavioral expectations + migrate.

Value-shaped eval cases are superseded by certified answers; what stays in
evals/cases.yaml is the slim behavioral surface ({question, expect:
clarify|refuse, term?}) that certification cannot express. Four seams under
test: the ``expect``/``term`` columns behavioral cases live in since migration
0031 (DB-gated round trip, plus the legacy expected_answer marker a pre-0031 row
still decodes from), run_behavioral's response-SHAPE scoring through the real
pipeline, and `dst evals migrate`'s local file-to-file rewrite of a
scaffolded project. Everything but the storage round trip is DB-free.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text

from services.config import settings
from services.contracts.eval import EvalCase, parse_behavioral_marker
from services.contracts.fakes import ScriptedLLM
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    SemanticModel,
)
from services.contracts.warehouse import QueryResult
from services.db.session import org_session
from services.evals import store as eval_store
from services.evals.runner import run_behavioral
from services.evals.service import _row_to_case
from services.runtime.answer import AnswerComposer
from services.runtime.assembly import AssembledInputs
from services.runtime.generator import GroundedSQLGenerator


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


def _org() -> object:
    """A throwaway org to hang cases off (same admin helper as test_evals_store)."""
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        return c.execute(
            text("INSERT INTO org (name) VALUES ('EvalBehavioralTest') RETURNING id")
        ).scalar_one()


def _cleanup(org: object) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM eval_case WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


AMBIGUOUS_VALUE = Definition(
    term="value",
    body="ASK the user: lifetime value or order amount?",
    status="ambiguous",
    possible_mappings=[
        "lifetime value - customers.customer_lifetime_value",
        "order amount - orders.amount",
    ],
)


def _model(definitions: list[Definition]) -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity(
                name="projects",
                source=EntitySource(connection="wh", table="projects"),
                primary_key=["id"],
                fields=[Field(name="id", type="string")],
            )
        ],
        definitions=definitions,
    )


class _CountingConnector:
    kind = "duckdb"

    def execute(self, sql: str, *, read_only: bool = True, row_limit: int | None = None):
        return QueryResult(columns=["n"], rows=[[3]])


def _case(question: str, expect: str, term: str | None = None) -> EvalCase:
    return EvalCase(
        id="c_" + question[:8],
        lens="t",
        question=question,
        expect=expect,  # type: ignore[arg-type]
        term=term,
        source="authored",
        status="approved",
    )


def _run(cases: list[EvalCase], scripts: list[str], definitions: list[Definition]):
    # Retry-before-fail: a failing case re-runs up to
    # GATE_ATTEMPTS times, so a deterministic-failure script must repeat for
    # every attempt or the retry drains it mid-loop.
    from services.evals.runner import GATE_ATTEMPTS

    llm = ScriptedLLM(scripts * GATE_ATTEMPTS)
    model = _model(definitions)
    assembled = AssembledInputs(
        model=model,
        prose=[],
        certified=None,
        certification="none",
        certified_sql=None,
        data_as_of=None,
        counts={},
    )
    return run_behavioral(
        connector=_CountingConnector(),
        lens=model.lens,
        cases=cases,
        assemble_for=lambda _q: assembled,
        generators_for=lambda _a: (GroundedSQLGenerator(llm), None),
        composer=AnswerComposer(llm),
    )


# ── storage: real expect/term columns (migration 0031) ───────────────────────


@needs_db
def test_behavioral_case_round_trips_through_the_expect_term_columns() -> None:
    """expect/term are their own columns now — no marker, and expected_answer is
    left alone as the prose oracle its name promises."""
    org = _org()
    try:
        with org_session(org) as s:
            eval_store.create_case(
                s,
                "behave",
                "what is the average value of a customer?",
                "authored",
                expect="clarify",
                term="value",
                status="approved",
            )
            eval_store.create_case(s, "behave", "what is our NPS?", "authored", expect="refuse")
        with org_session(org) as s:
            rows = {r.question: r for r in eval_store.list_cases(s, "behave")}
        clarify = rows["what is the average value of a customer?"]
        assert (clarify.expect, clarify.term) == ("clarify", "value")
        assert clarify.expected_answer is None and clarify.expected_sql is None
        refuse = rows["what is our NPS?"]
        assert (refuse.expect, refuse.term) == ("refuse", None)
        # …and the row reaches the runner as a typed EvalCase.
        case = _row_to_case(clarify)
        assert (case.expect, case.term) == ("clarify", "value")
    finally:
        _cleanup(org)


@needs_db
def test_a_legacy_marker_row_still_decodes_into_a_typed_case() -> None:
    """LEGACY READ PATH: a row written before migration 0031 (or by an older
    wheel after it) still carries the marker in expected_answer. Reading it must
    yield the same typed case — insert the raw marker under the columns' backs."""
    org = _org()
    try:
        admin = create_engine(settings.database_admin_url)
        with admin.begin() as c:
            c.execute(
                text(
                    "INSERT INTO eval_case (org_id, lens, question, source, expected_answer) "
                    "VALUES (:o, 'behave', 'legacy q', 'authored', 'expect:clarify term=value')"
                ),
                {"o": org},
            )
        with org_session(org) as s:
            (row,) = eval_store.list_cases(s, "behave")
        assert row.expect is None and row.expected_answer == "expect:clarify term=value"
        case = _row_to_case(row)
        assert (case.expect, case.term) == ("clarify", "value")
        assert case.expected_answer is None  # the marker never leaks out as prose
    finally:
        _cleanup(org)


@needs_db
def test_migration_0031_converts_marker_rows_and_re_running_it_is_a_no_op() -> None:
    """The data migration, on the shapes it will actually meet. Runs the shipped
    UPDATE verbatim (loaded from the migration file) against rows this test
    plants, so the conversion and its idempotency guard are pinned, not assumed."""
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "_m0031", root / "migrations" / "versions" / "0031_eval_case_expect.py"
    )
    assert spec is not None and spec.loader is not None
    m0031 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m0031)
    convert = m0031.UPGRADE[-1]  # the ALTERs already ran; this is the data pass

    org = _org()
    try:
        admin = create_engine(settings.database_admin_url)
        planted = [
            ("marker", "expect:clarify term=value", ("clarify", "value")),
            ("bare", "expect:refuse", ("refuse", None)),
            ("blank term", "expect:clarify term=", ("clarify", None)),
            ("prose oracle", "42", (None, None)),  # must survive untouched
            ("near miss", "expect:nonsense", (None, None)),
        ]
        with admin.begin() as c:
            for question, answer, _want in planted:
                c.execute(
                    text(
                        "INSERT INTO eval_case (org_id, lens, question, source, expected_answer) "
                        "VALUES (:o, 'behave', :q, 'authored', :a)"
                    ),
                    {"o": org, "q": question, "a": answer},
                )
            c.execute(text(convert))

        def _read() -> dict[str, tuple[str | None, str | None, str | None]]:
            with org_session(org) as s:
                return {
                    r.question: (r.expect, r.term, r.expected_answer)
                    for r in eval_store.list_cases(s, "behave")
                }

        after = _read()
        for question, answer, want in planted:
            expect, term, remaining = after[question]
            assert (expect, term) == want, question
            # A converted marker clears expected_answer; a non-marker keeps it.
            assert remaining == (None if want[0] else answer), question

        # Idempotent: the same statement again changes nothing.
        with admin.begin() as c:
            c.execute(text(convert))
        assert _read() == after
    finally:
        _cleanup(org)


def test_the_legacy_marker_decoder_reads_markers_and_leaves_prose_alone() -> None:
    # Literal marker strings, not a round trip through an encoder: nothing writes
    # these any more, so the on-disk legacy form is what has to stay decodable.
    assert parse_behavioral_marker("expect:clarify term=value") == ("clarify", "value")
    assert parse_behavioral_marker("expect:refuse") == ("refuse", None)
    assert parse_behavioral_marker("expect:clarify term=") == ("clarify", None)
    # non-markers stay prose oracles
    assert parse_behavioral_marker("42") == (None, None)
    assert parse_behavioral_marker(None) == (None, None)
    assert parse_behavioral_marker("expect:nonsense") == (None, None)
    # a payload read back decodes into the typed fields
    case = EvalCase(
        id="c1",
        lens="t",
        question="q",
        expected_answer="expect:clarify term=value",
        source="authored",
    )
    assert case.expect == "clarify" and case.term == "value"
    assert case.expected_answer is None
    # prose in expected_answer is left exactly where it is
    prose = EvalCase(id="c2", lens="t", question="q", expected_answer="42", source="authored")
    assert prose.expect is None and prose.expected_answer == "42"


# ── run_behavioral: response shape is the score ──────────────────────────────


def test_clarify_case_passes_on_the_demo_ambiguity() -> None:
    # The deterministic pre-check fires before any generator — the exact
    # regression the behavioral surface exists to pin (matrix v2 finding).
    answering = '{"sql": "SELECT COUNT(projects.id) AS n FROM projects AS projects"}'
    outcome = _run(
        [_case("what is the average value of a customer?", "clarify", term="value")],
        [answering],  # the model WOULD answer; the shape pin must not care
        [AMBIGUOUS_VALUE],
    )
    assert outcome.run.mode == "behavioral"
    assert [r.grade for r in outcome.results] == ["pass"]
    assert outcome.run.score == 1.0


def test_clarify_case_fails_when_the_response_answers() -> None:
    answering = '{"sql": "SELECT COUNT(projects.id) AS n FROM projects AS projects"}'
    outcome = _run(
        [_case("how many projects do we have?", "clarify")],
        [answering, "We have 3 projects."],
        [AMBIGUOUS_VALUE],
    )
    (result,) = outcome.results
    assert result.grade == "fail" and not result.passed
    assert result.reason is not None and "expected a clarification" in result.reason


def test_clarify_term_must_match_when_pinned() -> None:
    workload = Definition(
        term="workload",
        body="ASK",
        status="ambiguous",
        possible_mappings=["usage - a", "projects - b"],
    )
    outcome = _run(
        [_case("how many workloads do we have?", "clarify", term="value")],
        ['{"clarify": {"term": "workload", "question": "Which?", "options": ["a"]}}'],
        [workload],
    )
    (result,) = outcome.results
    assert result.grade == "fail"
    assert result.reason == "clarified on 'workload', expected term 'value'"


def test_refuse_case_passes_on_no_answer_and_fails_on_an_answer() -> None:
    refused = _run(
        [_case("what is our NPS?", "refuse")],
        ['{"no_answer": "no survey data is modeled"}'],
        [],
    )
    assert [r.grade for r in refused.results] == ["pass"]
    answered = _run(
        [_case("how many projects do we have?", "refuse")],
        ['{"sql": "SELECT COUNT(projects.id) AS n FROM projects AS projects"}', "3 projects."],
        [],
    )
    (result,) = answered.results
    assert result.grade == "fail"
    assert result.reason is not None and "expected a refusal" in result.reason


def test_answer_case_passes_on_a_data_answer_and_fails_on_a_refusal() -> None:
    # The must-answer pin — refusal is measured in BOTH directions. A
    # lens that regresses into refusing an answerable question fails this case.
    answered = _run(
        [_case("how many projects do we have?", "answer")],
        ['{"sql": "SELECT COUNT(projects.id) AS n FROM projects AS projects"}', "3 projects."],
        [],
    )
    assert [r.grade for r in answered.results] == ["pass"]
    refused = _run(
        [_case("how many projects do we have?", "answer")],
        ['{"no_answer": "no project data is modeled"}'],
        [],
    )
    (result,) = refused.results
    assert result.grade == "fail"
    assert result.reason is not None and "expected a data answer" in result.reason
    assert "refused" in result.reason


def test_value_cases_are_not_scored_by_the_behavioral_runner() -> None:
    value_case = EvalCase(
        id="v1", lens="t", question="q", expected_sql="SELECT 1", source="authored"
    )
    outcome = _run([value_case], ["irrelevant"], [])
    assert outcome.results == [] and outcome.run.score is None


# ── dst evals migrate: local file-to-file, no server ─────────────────────


def _scaffold(tmp_path: Path) -> Path:
    from services.cli.init import run_init

    root = tmp_path / "proj"
    ns = argparse.Namespace(dir=str(root), name=None, warehouse=None, example=None, yes=True)
    assert run_init(ns) == 0
    return root


def test_migrate_moves_value_cases_into_certified_answers(tmp_path, capsys) -> None:
    from services.cli.main import _evals_migrate

    root = _scaffold(tmp_path)
    lens_dir = root / "lenses" / "customer_value"
    cases_path = lens_dir / "evals" / "cases.yaml"
    header = "\n".join(
        line for line in cases_path.read_text(encoding="utf-8").splitlines() if line.startswith("#")
    )
    cases_path.write_text(
        header
        + "\n"
        + yaml.safe_dump(
            [
                {
                    "question": "How many repeat customers?",
                    "expected_sql": "SELECT 19",
                    "status": "approved",
                    "created_by": "alex",
                },
                {
                    "question": "what is the average value of a customer?",
                    "expect": "clarify",
                    "term": "value",
                    "status": "approved",
                },
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert _evals_migrate(argparse.Namespace(dir=str(root))) == 0
    out = capsys.readouterr().out
    assert (
        "customer_value: migrated 1 case(s) into certified_answers.yaml; 1 behavioral kept" in out
    )
    assert "dst apply" in out  # the honest next step

    cert_text = (lens_dir / "certified_answers.yaml").read_text(encoding="utf-8")
    assert cert_text.startswith("# Approved question->SQL pairs")  # header survives
    assert yaml.safe_load(cert_text) == [
        {
            "question": "How many repeat customers?",
            "sql": "SELECT 19",
            "source": "evals:migrated",
            "verified_by": "alex",  # created_by carried as provenance
        }
    ]
    cases_text = cases_path.read_text(encoding="utf-8")
    assert cases_text.startswith("# Eval cases")  # header survives the rewrite
    assert yaml.safe_load(cases_text) == [
        {
            "question": "what is the average value of a customer?",
            "expect": "clarify",
            "term": "value",
            "status": "approved",
        }
    ]

    # Round trip 2: the same value case re-added is SKIPPED (already certified),
    # never duplicated; behavioral entries ride through untouched.
    cases_path.write_text(
        yaml.safe_dump(
            [
                {"question": "How many repeat customers?", "expected_sql": "SELECT 19"},
                {"question": "what is the average value of a customer?", "expect": "clarify"},
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert _evals_migrate(argparse.Namespace(dir=str(root))) == 0
    out = capsys.readouterr().out
    assert "skipped 'How many repeat customers?' — already in certified_answers.yaml" in out
    assert len(yaml.safe_load((lens_dir / "certified_answers.yaml").read_text("utf-8"))) == 1
    assert len(yaml.safe_load(cases_path.read_text(encoding="utf-8"))) == 1

    # The rewritten tree still loads as a lens source (the apply entrypoint).
    from services.project.loader import load_lens_source

    files = {
        p.relative_to(lens_dir).as_posix(): p.read_text(encoding="utf-8")
        for p in lens_dir.rglob("*")
        if p.is_file()
    }
    source = load_lens_source(files)
    assert [c["expect"] for c in source.eval_cases] == ["clarify"]


def test_migrate_of_every_case_leaves_no_live_placeholder(tmp_path, capsys) -> None:
    """When every case migrates, the rewritten cases.yaml keeps its comment head
    and nothing live — a `[]` here would re-plant the append trap the scaffold
    dropped (appending an entry after `[]` is malformed YAML)."""
    from services.cli.main import _evals_migrate

    root = _scaffold(tmp_path)
    cases_path = root / "lenses" / "customer_value" / "evals" / "cases.yaml"
    cases_path.write_text(
        cases_path.read_text(encoding="utf-8")
        + "- question: How many repeat customers?\n  expected_sql: SELECT 19\n"
        + "  status: approved\n",
        encoding="utf-8",
    )
    assert _evals_migrate(argparse.Namespace(dir=str(root))) == 0
    assert "migrated 1 case(s)" in capsys.readouterr().out
    cases_text = cases_path.read_text(encoding="utf-8")
    assert cases_text.startswith("# Eval cases")  # head survives
    assert yaml.safe_load(cases_text) is None  # comment-only again — append-safe
    assert yaml.safe_load(cases_text + "- question: q?\n  expect: refuse\n") == [
        {"question": "q?", "expect": "refuse"}
    ]


def test_migrate_without_value_cases_is_an_honest_no_op(tmp_path, capsys) -> None:
    from services.cli.main import _evals_migrate

    root = _scaffold(tmp_path)
    assert _evals_migrate(argparse.Namespace(dir=str(root))) == 0
    assert "no value cases — nothing to migrate" in capsys.readouterr().out
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _evals_migrate(argparse.Namespace(dir=str(empty))) == 0
    assert "nothing to migrate" in capsys.readouterr().out
