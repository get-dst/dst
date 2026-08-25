"""The certified test suite — generation vs certification.

For each active certified answer: its stored SQL executes as the oracle, its
QUESTION runs through the real generation pipeline (constructed without any
certified-matching branch — a certified answer whose SQL would trivially match
must still exercise generation), and the two EXECUTED results compare with the
regression scorer's semantics. A diverging case re-runs generation once before
counting. Offline: ScriptedLLM + the bundled jaffle duckdb fixture.
"""

from __future__ import annotations

import argparse
import json

import duckdb
import pytest

from services.certify.store import CertifiedAnswer
from services.config import settings
from services.connectors.duckdb import DuckDBConnector
from services.contracts.fakes import ScriptedLLM
from services.evals.certified_suite import format_result, run_certified_suite
from services.lenses.demo import jaffle_customer_value_bundle
from services.llm.registry import ResolvedModel
from services.runtime.answer import AnswerComposer
from services.runtime.assembly import AssembledInputs
from services.runtime.generator import GroundedSQLGenerator

MODEL = jaffle_customer_value_bundle().semantic_model

_Q = "How many repeat customers are there?"
_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 1"


def _jaffle_scalar(sql: str) -> object:
    row = duckdb.connect(settings.duckdb_jaffle_path, read_only=True).execute(sql).fetchone()
    return row[0]  # type: ignore[index]


def _answer(
    sql: str = _SQL, question: str = _Q, status: str = "active", answer_id: str = "a1"
) -> CertifiedAnswer:
    return CertifiedAnswer(
        answer_id, "customer_value", question, sql, "t", "2026-07-30T00:00:00", status=status
    )


def _fake_harness(generator):
    """Offline stand-in for assembly.eval_harness: fixed model, no context."""

    def assemble_for(_answer: CertifiedAnswer) -> AssembledInputs:
        return AssembledInputs(
            model=MODEL,
            prose=[],
            certified=None,
            certification="none",
            certified_sql=None,
            data_as_of=None,
            counts={},
        )

    return assemble_for, lambda _assembled: (generator, None)


def _suite(answers: list[CertifiedAnswer], gen_responses: list[str]):
    assemble_for, generators_for = _fake_harness(
        GroundedSQLGenerator(ScriptedLLM(gen_responses), model="m")
    )
    return run_certified_suite(
        connector=DuckDBConnector(settings.duckdb_jaffle_path),
        lens="customer_value",
        answers=answers,
        assemble_for=assemble_for,
        generators_for=generators_for,
        composer=AnswerComposer(ScriptedLLM(["scripted answer"]), model="m"),
        model_name="m",
    )


def test_equivalent_generation_passes_on_executed_results() -> None:
    """Different SQL text, same executed result — the comparison is of RESULTS,
    never SQL shape."""
    gen_sql = "SELECT count(*) AS repeat_count FROM customers WHERE number_of_orders > 1"
    out = _suite([_answer()], [gen_sql])
    r = out.results[0]
    assert r.passed and out.score == 1.0
    assert r.oracle_result == {"value": _jaffle_scalar(_SQL)}
    assert r.generated_sql != _SQL  # generation ran; nothing served the stored SQL


def test_divergence_is_generation_not_the_bypass() -> None:
    """The trivial-match trap: this question IS certified, so a serve-path
    lookup would return the stored SQL and pass. The suite must run generation
    instead — here scripted to a different metric — and fail honestly, naming
    both executed values."""
    wrong = "SELECT count(*) AS n FROM customers"
    out = _suite([_answer()], [wrong])
    r = out.results[0]
    assert not r.passed and out.score == 0.0
    assert r.generated_sql is not None and "number_of_orders" not in r.generated_sql
    assert r.oracle_result == {"value": _jaffle_scalar(_SQL)}
    assert r.generated_result == {"value": _jaffle_scalar("SELECT count(*) FROM customers")}
    assert r.reason == (
        f"generated {_jaffle_scalar('SELECT count(*) FROM customers')!r} "
        f"vs certified {_jaffle_scalar(_SQL)!r}"
    )


def test_diverging_case_reruns_once_before_counting() -> None:
    """Nondeterminism damping: first generation diverges, the re-run agrees —
    the case counts as passing."""
    out = _suite([_answer()], ["SELECT count(*) AS n FROM customers", _SQL])
    assert out.results[0].passed


def test_retired_answers_are_never_tested() -> None:
    out = _suite(
        [_answer(), _answer(status="retired", answer_id="a2", question="Old?")],
        [_SQL],
    )
    assert [r.answer_id for r in out.results] == ["a1"]


def test_broken_oracle_fails_its_case_without_generation() -> None:
    out = _suite([_answer(sql="SELECT ghost_column FROM customers")], [_SQL])
    r = out.results[0]
    assert not r.passed
    assert r.reason is not None and r.reason.startswith("certified SQL failed to execute")
    assert "error" in r.oracle_result and r.generated_result == {}


def test_column_order_is_insensitive_when_names_match() -> None:
    certified = "SELECT customer_id, customer_name FROM customers"
    generated = "SELECT customer_name, customer_id FROM customers"
    out = _suite([_answer(sql=certified, question="List customers?")], [generated])
    assert out.results[0].passed


def test_guard_rejected_generation_counts_as_divergence() -> None:
    """A generated query outside the lens's tables never executes — the case
    fails with the rejection as the generated side."""
    out = _suite([_answer()], ["SELECT count(*) FROM stripe_payments"])
    r = out.results[0]
    assert not r.passed
    assert "error" in r.generated_result


def test_format_result_shows_both_sides_compactly() -> None:
    assert format_result({"value": 19}) == "19"
    assert format_result({"error": "boom"}) == "error: boom"
    assert format_result({"columns": ["a"], "rows": [[1]]}) == '{"columns":["a"],"rows":[[1]]}'


def test_scalar_vs_rows_divergence_says_the_shape_plainly() -> None:
    """A count oracle against a row-shaped generation must say
    "certified SQL returns a count (K); generation returned N row(s)" — not the
    cryptic "0 missing / N extra row(s)". Same-column-count case (row diff)."""
    n_customers = _jaffle_scalar("SELECT count(*) FROM customers")
    repeats = _jaffle_scalar(_SQL)
    assert n_customers != repeats  # the fixture must distinguish the shapes
    rows_sql = "SELECT customer_id FROM customers"
    out = _suite([_answer()], [rows_sql, rows_sql])  # diverges twice (re-run idiom)
    r = out.results[0]
    assert not r.passed
    assert r.reason == (
        f"certified SQL returns a count ({repeats}); generation returned {n_customers} row(s)"
    )


def test_scalar_vs_multi_column_rows_says_the_shape_plainly() -> None:
    """The column-count-mismatch branch tells the same story."""
    n_customers = _jaffle_scalar("SELECT count(*) FROM customers")
    repeats = _jaffle_scalar(_SQL)
    rows_sql = "SELECT customer_id, customer_name FROM customers"
    out = _suite([_answer()], [rows_sql, rows_sql])
    r = out.results[0]
    assert not r.passed
    assert r.reason == (
        f"certified SQL returns a count ({repeats}); generation returned {n_customers} row(s)"
    )


def test_rows_vs_scalar_divergence_says_the_shape_plainly() -> None:
    """The mirror direction: a row-shaped oracle against a scalar generation."""
    n_customers = _jaffle_scalar("SELECT count(*) FROM customers")
    repeats = _jaffle_scalar(_SQL)
    certified = "SELECT customer_id FROM customers WHERE number_of_orders > 1"
    scalar_sql = "SELECT count(*) AS n FROM customers"
    out = _suite([_answer(sql=certified, question="Which repeats?")], [scalar_sql, scalar_sql])
    r = out.results[0]
    assert not r.passed
    assert r.reason == (
        f"certified SQL returns {repeats} row(s); generation returned a count ({n_customers})"
    )


# ── `dst test` (scratch DB) ──────────────────────────────────────────────

# The sweep is ORG-SCOPED, so every call below names its org — and so does every
# line of its output. Left unscoped, these would sweep whatever else the scratch
# database holds (and, on a checkout with a ./.env, whatever org that token owns).
_ORG = "CertSuiteT"


def _reachable(url: str) -> bool:
    from sqlalchemy import create_engine, text

    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


@pytest.fixture()
def org():
    from sqlalchemy import create_engine, text

    from services.auth.tokens import hash_token, new_admin_token
    from services.db.session import org_session
    from services.lenses import connection_store
    from services.lenses import store as lens_store

    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES (:n) RETURNING id"), {"n": _ORG}
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    with org_session(oid) as s:
        connection_store.create_connection(
            s, "jaffle", "duckdb", {"path": settings.duckdb_jaffle_path}, None
        )
        lens_store.create_lens(s, jaffle_customer_value_bundle())
        lens_store.publish(s, "customer_value")
        s.commit()
    yield oid
    with admin.begin() as c:
        for table in (
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


@needs_db
def test_cli_test_verb_full_corpus_exit_codes_and_restamp(org, monkeypatch, capsys) -> None:
    """`dst test <lens>`: honest per-answer output, exit 1 on divergence,
    and a GREEN answer re-stamps its bindings to current hashes (decision 5 —
    the re-verify flag clears through evidence, from the CLI too)."""
    from services.certify import store as certify_store
    from services.certify.bindings import certified_bindings
    from services.cli.main import _test
    from services.db.session import org_session
    from services.evals import store as eval_store

    oid = org
    bundle = jaffle_customer_value_bundle()
    with org_session(oid) as s:
        aid = certify_store.create(
            s,
            "customer_value",
            _Q,
            _SQL,
            None,
            "t",
            bindings={"entity/customers": "STALE-HASH"},
        )
        # A parked candidate must be VISIBLE as not-run — otherwise an unarmed
        # suite and a passing one print the same shape.
        eval_store.create_case(s, "customer_value", "how many at all?", "t", status="candidate")
        s.commit()

    # Green run: generation reproduces the certified result. The parity harness
    # would select the metric-layer IntentSQLGenerator; the scripted replies are
    # SQL-shaped, so pin the grounded path (same idiom as test_query_api).
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM([_SQL, "There are 19."]), "fake", "m"),
    )
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG))
    assert rc == 0
    out = capsys.readouterr().out
    assert f"PASS  {_ORG}/customer_value: {_Q}" in out
    assert f"1/1 passed (1 certified + 0 behavioral) in org {_ORG}" in out
    assert "1 candidate case(s) were NOT run" in out
    with org_session(oid) as s:
        stored = certify_store.get(s, aid)
        assert stored is not None
        assert stored.bindings == certified_bindings(_SQL, bundle.semantic_model)
        assert "STALE-HASH" not in (stored.bindings or {}).values()

    # Red run: generation diverges (scripted for both damping attempts) — exit 1
    # and the FAIL line names both executed values.
    wrong = "SELECT count(*) AS n FROM customers"
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM([wrong, "compose", wrong, "compose"]), "fake", "m"),
    )
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG))
    assert rc == 1
    out = capsys.readouterr().out
    assert f"FAIL  {_ORG}/customer_value" in out
    assert "certified SQL → " in out and "generated → " in out

    # Unknown lens: honest error, exit 1.
    rc = _test(argparse.Namespace(lens="no_such_lens", all=False, org=_ORG))
    assert rc == 1


@needs_db
def test_dst_test_records_its_run(org, monkeypatch, capsys) -> None:
    """Every result is recorded: a `dst test` sweep persists an eval_run
    (mode "test") with its per-case eval_result rows — the same tables the
    publish gate writes — so accuracy is a queryable trend whoever runs the
    sweep. Output bytes are unchanged; recording is a side effect only."""
    from sqlalchemy import text

    from services.certify import store as certify_store
    from services.cli.main import _test
    from services.db.session import org_session

    oid = org
    with org_session(oid) as s:
        certify_store.create(s, "customer_value", _Q, _SQL, None, "t")
        s.commit()
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM([_SQL, "There are 19."]), "fake", "m"),
    )
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG))
    assert rc == 0, capsys.readouterr().out
    with org_session(oid) as s:
        runs = s.execute(
            text(
                "SELECT id, mode, score, passed, failed FROM eval_run "
                "WHERE lens = 'customer_value' AND mode = 'test'"
            )
        ).all()
        assert len(runs) == 1, runs
        run_id, mode, score, passed, failed_n = runs[0]
        assert (mode, passed, failed_n) == ("test", 1, 0)
        assert score == 1.0
        results = s.execute(
            text("SELECT case_id, passed FROM eval_result WHERE run_id = :r"),
            {"r": str(run_id)},
        ).all()
        assert len(results) == 1
        assert results[0][0].startswith("certified:")
        assert results[0][1] is True


@needs_db
def test_cli_test_runs_behavioral_expectations(org, monkeypatch, capsys) -> None:
    """Approved expect-cases run alongside the corpus, as the scaffold promises
    — `dst test` must not skip them. The demo clarify pin passes
    deterministically; an expect: refuse on a question the pipeline answers
    fails and exits 1."""
    from services.cli.main import _test
    from services.db.session import org_session
    from services.evals import store as eval_store

    oid = org
    with org_session(oid) as s:
        eval_store.create_case(
            s,
            "customer_value",
            "what is the average value of a customer?",
            "authored",
            expect="clarify",
            term="value",
            status="approved",
        )
        s.commit()
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM(["GARBAGE ;;;"]), "fake", "m"),
    )
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG))
    assert rc == 0
    out = capsys.readouterr().out
    assert (
        f"PASS  {_ORG}/customer_value: what is the average value of a customer? "
        "[expect: clarify]" in out
    )
    # The denominator names its composition.
    assert f"1/1 passed (0 certified + 1 behavioral) in org {_ORG}" in out

    with org_session(oid) as s:
        eval_store.create_case(
            s,
            "customer_value",
            _Q,
            "authored",
            expect="refuse",
            status="approved",
        )
        s.commit()
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(
            ScriptedLLM([_SQL, "There are 19.", _SQL, "There are 19."]), "fake", "m"
        ),
    )
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG))
    assert rc == 1
    out = capsys.readouterr().out
    assert f"FAIL  {_ORG}/customer_value: {_Q} [expect: refuse]" in out


@needs_db
def test_cli_test_is_org_scoped_and_names_the_org(org, monkeypatch, capsys) -> None:
    """`dst test tox` swept FOUR different orgs' identically-named `tox`
    lenses — four copies of one corpus in a 37-minute run, and nothing in the
    output naming whose PASS was whose. A green suite that silently tested
    someone else's lens is worse than a red one.

    So: a second org holding a same-named lens with a certified answer that
    CANNOT pass, and a scoped sweep that stays green and never touches it."""
    from sqlalchemy import create_engine, text

    from services.certify import store as certify_store
    from services.cli.main import _test
    from services.db.session import org_session
    from services.lenses import connection_store
    from services.lenses import store as lens_store

    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        other = c.execute(
            text("INSERT INTO org (name) VALUES ('NeighbourT') RETURNING id")
        ).scalar_one()
    try:
        with org_session(other) as s:
            connection_store.create_connection(
                s, "jaffle", "duckdb", {"path": settings.duckdb_jaffle_path}, None
            )
            lens_store.create_lens(s, jaffle_customer_value_bundle())
            lens_store.publish(s, "customer_value")
            # Its oracle disagrees with the scripted generation, so this org's
            # copy fails the moment the sweep reaches it.
            certify_store.create(
                s, "customer_value", _Q, "SELECT 999 AS n FROM customers LIMIT 1", None, "t"
            )
            s.commit()
        with org_session(org) as s:
            certify_store.create(s, "customer_value", _Q, _SQL, None, "t")
            s.commit()

        monkeypatch.setattr(
            "services.runtime.assembly.IntentSQLGenerator",
            lambda llm, model, temperature=0.0: GroundedSQLGenerator(
                llm, model=model, temperature=temperature
            ),
        )
        monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
        monkeypatch.setattr(
            "services.llm.registry.resolve",
            lambda _ref: ResolvedModel(ScriptedLLM([_SQL, "There are 19."] * 4), "fake", "m"),
        )
        rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG))
        out = capsys.readouterr().out
        assert rc == 0, out
        assert f"PASS  {_ORG}/customer_value: {_Q}" in out
        assert "NeighbourT" not in out, "the sweep leaked into a neighbouring org"
        assert f"1/1 passed (1 certified + 0 behavioral) in org {_ORG}" in out

        # …and pointed AT the neighbour it fails, which is what proves the scope
        # was doing work rather than the second corpus being unreachable.
        rc = _test(argparse.Namespace(lens="customer_value", all=False, org="NeighbourT"))
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "FAIL  NeighbourT/customer_value" in out

        # An org that does not exist is an error, never a silent full sweep.
        assert _test(argparse.Namespace(lens="customer_value", all=False, org="NoSuchOrg")) == 1
    finally:
        with admin.begin() as c:
            for table in ("certified_answer", "lens_version", "lens", "connection"):
                c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": other})
            c.execute(text("DELETE FROM org WHERE id = :o"), {"o": other})
        admin.dispose()


@needs_db
def test_cli_test_skips_a_lens_it_cannot_serve_instead_of_dialling_another_vendor(
    org, monkeypatch, capsys
) -> None:
    """`dst test` used to fall back to the smart-tier client per lens
    (`registry.resolve(lens_ref) or pair`), so a lens pinned to a provider this
    install does not have was swept against a DIFFERENT vendor and reported
    PASS — a green result for a model the lens never serves on, which is the
    same lie the org scoping exists to prevent. It says SKIP, names what it
    tried, and counts it, so the exit code cannot claim verification."""
    from services.certify import store as certify_store
    from services.cli.main import _test
    from services.config import ProviderConfig
    from services.contracts.lens_config import ModelConfig
    from services.db.session import org_session
    from services.lenses import store as lens_store

    oid = org
    pinned = jaffle_customer_value_bundle()
    pinned.config.model = ModelConfig(provider="deepseek", model="deepseek-v4-pro")
    with org_session(oid) as s:
        lens_store.update_draft(s, "customer_value", pinned)
        lens_store.publish(s, "customer_value")
        certify_store.create(s, "customer_value", _Q, _SQL, None, "t")
        s.commit()

    # A real anthropic-only install: the tier resolves, the lens's pin does not.
    dialled: list[str] = []

    class _Tripwire:
        def __init__(self, *_a: object, **_kw: object) -> None: ...

        def complete(self, *, model: str, **_kw: object) -> object:
            dialled.append(model)
            raise AssertionError(f"dialled anthropic with model={model!r}")

    # Declared the way an install declares it, not by patching the object: `_test`
    # adopts its --dir project's configuration (services/config.py::adopt_project_env),
    # which rebuilds the settings singleton — so a provider that only exists as an
    # attribute assignment is one a real invocation would not have either.
    monkeypatch.setenv(
        "DST_PROVIDERS", json.dumps({"anthropic": {"type": "anthropic", "api_key": "sk-ant"}})
    )
    monkeypatch.setattr(
        settings, "providers", {"anthropic": ProviderConfig(type="anthropic", api_key="sk-ant")}
    )
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    monkeypatch.setattr("services.llm.anthropic_provider.AnthropicProvider", _Tripwire)
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG))
    assert dialled == [], "the sweep sent a request to a vendor the lens does not name"
    out = capsys.readouterr().out
    assert rc == 1, out
    assert f"SKIP  {_ORG}/customer_value: NOT verified" in out
    assert "deepseek/deepseek-v4-pro" in out and "configured providers: anthropic" in out
    assert "PASS" not in out


# ── templates in the suite ───────────────────────────────────────────────────


def _template(answer_id: str = "t1") -> CertifiedAnswer:
    return CertifiedAnswer(
        answer_id,
        "customer_value",
        "how many customers with more than {min_orders} orders?",
        "SELECT count(*) AS n FROM customers WHERE number_of_orders > {min_orders}",
        "maija",
        "2026-08-01T00:00:00",
        slots={"min_orders": {"type": "number"}},
        sample_bindings=[{"min_orders": "1"}],
    )


def test_template_case_tests_the_sample_binding() -> None:
    # Generation produces the same sample-bound SQL → results match the oracle.
    gen_json = (
        '{"sql": "SELECT count(*) AS n FROM customers WHERE number_of_orders > 1", '
        '"definition_used": null}'
    )
    outcome = _suite([_template()], [gen_json])
    (case,) = outcome.results
    assert case.passed, case.reason
    # The case runs CONCRETE: the sample-bound question, never placeholder text.
    assert case.question == "how many customers with more than 1 orders?"


def test_invalid_template_fails_its_case_loudly() -> None:
    broken = _template("t2")
    broken.sample_bindings = [{"min_orders": "not-a-number"}]
    outcome = _suite([broken], ["irrelevant"])
    (case,) = outcome.results
    assert not case.passed
    assert case.reason is not None and "re-certify" in case.reason


def test_compare_bridges_count_vs_row_cardinality_visibly() -> None:
    # Probe finding: haiku row-shapes "how many" questions; a 1×1 integer
    # oracle K >= 2 matched by generated row cardinality passes VISIBLY.
    from services.evals.certified_suite import _compare

    rows_19 = [[i] for i in range(1, 20)]
    ok, reason = _compare(["n"], [[19]], ["customer_id"], rows_19)
    assert ok and reason is not None and "shape-lenient" in reason
    # Cardinality mismatch still fails.
    ok, _reason = _compare(["n"], [[19]], ["customer_id"], rows_19[:-1])
    assert not ok
    # K == 1 stays strict — cardinality would bless ANY single-row result.
    ok, _reason = _compare(["n"], [[1]], ["customer_id"], [["wrong"]])
    assert not ok
    # K == 0 stays strict too.
    ok, _reason = _compare(["n"], [[0]], ["customer_id"], [])
    assert not ok


def test_template_sql_without_slots_never_executes_raw() -> None:
    # Probe run 4 (F2): a template-shaped row that arrives WITHOUT its slots
    # must fail NAMED — never reach the warehouse as raw `{placeholder}` SQL.
    ghost = CertifiedAnswer(
        "g1",
        "customer_value",
        "How many customers have more than {min_orders} orders?",
        "SELECT count(*) AS n FROM customers WHERE number_of_orders > {min_orders}",
        "probe",
        "2026-08-04T00:00:00",
    )
    out = _suite([ghost], ["irrelevant"])
    (case,) = out.results
    assert not case.passed
    assert case.reason is not None and "no slots" in case.reason
    assert case.oracle_result == {"error": "template SQL but no slots stored"}


@needs_db
def test_a_suite_that_verified_nothing_exits_4_not_0(org, monkeypatch, capsys) -> None:
    """`0/0 passed` must not be indistinguishable from a real pass.

    A suite run against a lens with nothing to verify reports success, so a
    metric can regress across an apply uncaught while `dst test` keeps exiting
    0.

    Exit 4 is the third outcome, in the spirit of `query`'s 0/3/1: nothing passed
    and nothing failed, because there was nothing to verify. A published lens with
    no certified answers and no eval cases is an UNGATED lens, and a script has to
    be able to see that without parsing prose.
    """
    from services.cli.main import _test

    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM([]), "fake", "m"),
    )
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG))
    captured = capsys.readouterr()
    assert rc == 4, captured.out + captured.err
    assert "0/0 passed" in captured.out
    assert "nothing was verified" in captured.err
