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
    sweep. The run is also said out loud with the database it went to: `dst runs`
    reads through a server, and a server on another database lists nothing —
    the `recorded:` line is what resolves that."""
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
    out = capsys.readouterr().out
    assert rc == 0, out
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
        where = next(line for line in out.splitlines() if line.startswith("recorded: run "))
        assert where.startswith(
            f"recorded: run {str(run_id)[:8]} ({_ORG}/customer_value) in postgresql"
        )
        assert "***@" in where  # the url with its password hidden, never the password
        assert "dst runs <lens>" in out
        results = s.execute(
            text("SELECT case_id, passed, actual_value, checks FROM eval_result WHERE run_id = :r"),
            {"r": str(run_id)},
        ).all()
        assert len(results) == 1
        assert results[0][0].startswith("certified:")
        assert results[0][1] is True
        # The executed values ride the stored row, not only the verdict and SQL:
        # a copy of eval_result elsewhere must hold the numbers.
        assert results[0][2] == "19"
        assert results[0][3]["certified"] == "19" and results[0][3]["generated"] == "19"
    # --json carries the same two facts structurally.
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG, as_json=True))
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database"].startswith("postgresql") and "***@" in report["database"]
    assert [(r["org"], r["lens"]) for r in report["recorded"]] == [(_ORG, "customer_value")]
    assert len(report["recorded"][0]["run_id"]) == 36


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


def test_a_stored_attribution_is_recomputed_a_stored_construction_stands() -> None:
    """A certified answer stores the resolution it had when certified. An
    ATTRIBUTED one is the attributor's output then — stale once the attributor
    reads the SQL better — so the suite attributes again; a CONSTRUCTION is the
    typed reading a human approved and stands as gold."""
    from services.contracts.resolution import Resolution, Slot
    from services.evals.certified_suite import expected_resolution

    def res(method: str, metric: str) -> Resolution:
        return Resolution(
            method=method,  # type: ignore[arg-type]
            slots=[Slot(kind="metric", name=metric, source="declared")],
            tag="declared",
        )

    fresh = res("attributed", "radiant_win_rate")
    stale = res("attributed", "radiant_wins").model_dump()
    approved = res("construction", "radiant_wins").model_dump()
    assert expected_resolution(stale, lambda: fresh) == fresh
    assert expected_resolution(approved, lambda: fresh).slots[0].name == "radiant_wins"
    assert expected_resolution(None, lambda: fresh) == fresh


def test_only_an_exact_pass_stores_gold() -> None:
    """A shape-lenient pass can carry an extra metric; stored as gold, that slot
    would fail every later exact reading. Only an exact, typed construction is
    gold-worthy."""
    from services.contracts.resolution import Resolution, Slot
    from services.evals.certified_suite import gold_worthy

    typed = Resolution(
        method="construction",
        slots=[Slot(kind="metric", name="radiant_win_rate", source="declared")],
        tag="declared",
        typed=True,
    )
    from services.evals.certified_suite import _compare

    ok, reason = _compare(["r"], [[0.55]], ["r"], [[0.55]])
    assert ok and reason is None  # exact
    ok, reason = _compare(["r"], [[0.55]], ["wins", "r"], [[3, 0.55]])
    assert ok and reason is not None  # lenient: the value beside another metric
    assert gold_worthy(True, typed)
    assert not gold_worthy(False, typed)  # lenient or failed
    assert not gold_worthy(True, typed.model_copy(update={"typed": False}))


def test_compare_accepts_the_certified_value_beside_label_columns_visibly() -> None:
    """'What is the current patch?' certified as 7.41, answered as ('7.41', True):
    the value is there, labelled. Exactly one matching cell passes, visibly."""
    from services.evals.certified_suite import _compare

    ok, reason = _compare(["patch"], [["7.41"]], ["patch_name", "is_current"], [["7.41", True]])
    assert ok and reason is not None and "shape-lenient" in reason and "patch_name" in reason
    ok, _ = _compare(["r"], [[0.5553]], ["patch", "rate"], [["7.41", 0.55530000001]])
    assert ok
    # the value absent, or present twice (a coincidence it cannot tell apart): fail
    ok, _ = _compare(["patch"], [["7.41"]], ["patch_name", "x"], [["7.40", 1]])
    assert not ok
    ok, _ = _compare(["n"], [[3]], ["a", "b"], [[3, 3]])
    assert not ok
    # a bool never counts as the number 1
    ok, _ = _compare(["n"], [[1]], ["a", "b"], [[True, "x"]])
    assert not ok


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


def test_compare_tolerates_float_summation_drift_in_multirow_results() -> None:
    # Probe finding (spider-probe env, 2026-08-29): two equivalent SQL plans
    # summed the same transactions in different orders and drifted in the last
    # float bits (372.42995999999994 vs 372.42996); exact set-matching reported
    # "1 missing / 1 extra" on a numerically identical result. Multi-row floats
    # get the scalar tolerance, per cell.
    from services.evals.certified_suite import _compare

    ok, reason = _compare(
        ["month", "total"],
        [["2020-02-01", 103512.29000000004], ["2020-03-01", 158264.15]],
        ["month", "total"],
        [["2020-02-01", 103512.29000000001], ["2020-03-01", 158264.15]],
    )
    assert ok and reason is None
    # A real numeric difference still fails.
    ok, _reason = _compare(
        ["month", "total"],
        [["2020-02-01", 103512.29], ["2020-03-01", 158264.15]],
        ["month", "total"],
        [["2020-02-01", 103599.99], ["2020-03-01", 158264.15]],
    )
    assert not ok
    # Non-float cells stay exact: a different month is a different row.
    ok, _reason = _compare(
        ["month", "total"],
        [["2020-02-01", 103512.29]],
        ["month", "total"],
        [["2020-02-02", 103512.29]],
    )
    assert not ok
    # Cardinality mismatches never pair up.
    ok, _reason = _compare(
        ["month", "total"],
        [["2020-02-01", 103512.29], ["2020-03-01", 158264.15]],
        ["month", "total"],
        [["2020-02-01", 103512.29]],
    )
    assert not ok


# ── the resolution lane: meaning is graded beside values ─────────────────────

_COUNT_Q = "How many orders are there?"
_COUNT_ORACLE = "SELECT count(orders.order_id) AS order_count FROM orders"


def test_right_rows_by_an_ungoverned_path_passes_and_says_so() -> None:
    """The rows match the oracle, but generation counted a column that can be
    null instead of computing the declared `order_count` metric: the same number
    here, a different count. Rows are the gate — the case PASSES — and the
    resolution stage records the miss so the ledger can say 'right number,
    ungoverned path' instead of a clean green."""
    out = _suite(
        [_answer(sql=_COUNT_ORACLE, question=_COUNT_Q)],
        ["SELECT count(orders.status) FROM orders"],
    )
    r = out.results[0]
    assert r.passed and r.wrong_at is None
    assert r.stages["rows"] == "passed" and r.stages["resolution"] == "failed"
    assert r.ungoverned_pass
    assert r.resolution_expected == "declared" and r.resolution_got == "inferred"
    assert "order_count" in r.stage_evidence and "COUNT(orders.status)" in r.stage_evidence


def test_counting_every_row_computes_the_count_of_the_declared_key() -> None:
    """`order_id` is the declared key, so it is never null: COUNT(*) and
    COUNT(orders.order_id) are one count, and a generation writing COUNT(*)
    computed the declared `order_count` (the rule a ratio's operands follow)."""
    out = _suite([_answer(sql=_COUNT_ORACLE, question=_COUNT_Q)], ["SELECT count(*) FROM orders"])
    r = out.results[0]
    assert r.passed and r.stages["resolution"] == "passed", r.stage_evidence
    assert not r.ungoverned_pass


def test_wrong_rows_from_the_wrong_metric_attribute_to_resolution_first() -> None:
    """Wrong meaning ⇒ wrong values: the first failed stage is resolution, so
    the summary points at the layer/generation seam, not at 'rows'. The wrong
    meaning is read from the table the question names: SQL that reads a
    different table altogether never serves — it clarifies."""
    wrong = "SELECT count(DISTINCT customer_id) AS order_count FROM orders"
    out = _suite([_answer(sql=_COUNT_ORACLE, question=_COUNT_Q)], [wrong, wrong])
    r = out.results[0]
    assert not r.passed
    assert r.stages["resolution"] == "failed" and r.stages["rows"] == "failed"
    assert r.wrong_at == "resolution"


def test_generation_more_governed_than_the_oracle_passes_and_says_so() -> None:
    """The certified SQL counts a raw column; generation used the declared
    metric. Not a miss — a note for the certifier, and the case passes the
    resolution stage."""
    raw_oracle = "SELECT status, count(status) AS n FROM orders GROUP BY 1"
    out = _suite(
        [_answer(sql=raw_oracle, question=_COUNT_Q)],
        ["SELECT status, count(o.order_id) AS order_count FROM orders AS o GROUP BY 1"],
    )
    r = out.results[0]
    assert r.passed and r.stages["resolution"] == "passed" and not r.ungoverned_pass
    assert "computes COUNT(status) raw" in r.stage_evidence, r.stage_evidence
    assert "order_count" in r.stage_evidence


def test_governed_generation_passes_the_resolution_stage() -> None:
    out = _suite(
        [_answer(sql=_COUNT_ORACLE, question=_COUNT_Q)],
        ["SELECT count(o.order_id) AS order_count FROM orders AS o"],
    )
    r = out.results[0]
    assert r.passed and r.stages["resolution"] == "passed" and not r.ungoverned_pass


def test_an_oracle_naming_nothing_governed_skips_the_lane_out_loud() -> None:
    """A listing oracle declares no metric: the lane grades nothing and says
    `skipped` with the reason — never a clean pass for a stage that ran nothing."""
    listing = "SELECT customer_name FROM customers ORDER BY customer_name LIMIT 3"
    out = _suite([_answer(sql=listing, question="First three customers?")], [listing])
    r = out.results[0]
    assert r.passed and r.stages["resolution"] == "skipped"
    assert "names no declared" in r.stage_evidence


def test_a_green_typed_case_carries_its_ledger_as_gold_an_untyped_one_does_not() -> None:
    """The result carries the typed ledger only when the answer typed by
    construction: raw-SQL generation (attributed) carries none, so nothing
    attributed ever becomes typed gold."""
    from services.contracts.protocols import GeneratedQuery
    from services.contracts.query_intent import QueryIntent

    answers = [
        CertifiedAnswer(
            "c1",
            "customer_value",
            "how many customers",
            "SELECT count(*) AS n FROM customers",
            "t",
            "2026-01-01",
        )
    ]
    out = _suite(answers, ['{"sql": "SELECT count(*) AS n FROM customers", "rationale": "r"}'])
    (r,) = out.results
    assert r.passed and r.resolution is None  # attributed, not typed

    class _Typed:
        """A typed generator: the same SQL, carried with its intent."""

        model = "typed"

        def generate(self, **kw):  # type: ignore[no-untyped-def]
            intent = QueryIntent(entity="customers", metrics=["customer_count"])
            return GeneratedQuery(
                sql="SELECT count(*) AS n FROM customers", intent=intent, typed=True
            )

    assemble_for, _g = _fake_harness(_Typed())
    typed_out = run_certified_suite(
        connector=DuckDBConnector(settings.duckdb_jaffle_path),
        lens="customer_value",
        answers=answers,
        assemble_for=assemble_for,
        generators_for=lambda _a: (_Typed(), None),
        composer=AnswerComposer(ScriptedLLM(["scripted answer"]), model="m"),
        model_name="m",
    )
    (t,) = typed_out.results
    assert t.passed, t.reason
    assert t.resolution is not None and t.resolution["typed"] is True
    assert t.resolution["method"] == "construction"


def test_compare_month_grain_rows_match_on_bucket_and_say_so() -> None:
    """A month-grain trend certified on month-end dates against generated
    month-start labels: identical values, labels differing by day. The exact
    set diff reads it as 3 missing / 3 extra; the comparator reads the months,
    passes, and says it was lenient. Two rows in one month never fold."""
    import datetime as dt

    from services.evals.certified_suite import _compare

    cert = [[dt.date(2026, 4, 30), 1.0], [dt.date(2026, 5, 31), 2.0], [dt.date(2026, 6, 30), 3.0]]
    gen = [[dt.date(2026, 4, 1), 1.0], [dt.date(2026, 5, 1), 2.0], [dt.date(2026, 6, 1), 3.0]]
    ok, reason = _compare(["month", "arr"], cert, ["month", "arr"], gen)
    assert ok and reason and reason.startswith("month-lenient:")
    assert "2026-04-01" in reason and "2026-04-30" in reason
    # ISO strings (a connector that returns text) read the same way.
    ok, reason = _compare(["m", "v"], [["2026-04-30", 5]], ["m", "v"], [["2026-04-01", 5]])
    assert ok and reason and reason.startswith("month-lenient:")
    # A value that differs is still a failure — the rule only rewrites labels.
    ok, reason = _compare(["m", "v"], [["2026-04-30", 5]], ["m", "v"], [["2026-04-01", 6]])
    assert not ok and "1 missing / 1 extra" in (reason or "")
    # Two certified rows inside one month are two facts: no folding, no pass.
    ok, _ = _compare(
        ["d", "v"],
        [["2026-04-15", 1], ["2026-04-30", 2]],
        ["d", "v"],
        [["2026-04-01", 1], ["2026-04-02", 2]],
    )
    assert not ok


# ── the certified projection and value equivalence ───────────────────────────


def test_compare_runs_on_the_certified_projection() -> None:
    """Every certified column present by name, generation carrying more: the
    extras are ignored and the pass says which."""
    from services.evals.certified_suite import _compare

    ok, reason = _compare(
        ["month", "revenue"],
        [["2026-01", 10.0], ["2026-02", 12.0]],
        ["revenue", "month", "orders"],
        [[12.0, "2026-02", 4], [10.0, "2026-01", 3]],
    )
    assert ok and reason is not None and "projection" in reason and "orders" in reason
    # A certified column generation does not carry is still a divergence.
    ok, reason = _compare(["month", "revenue"], [["2026-01", 10.0]], ["month"], [["2026-01"]])
    assert not ok


def test_compare_treats_a_percentage_and_its_fraction_as_one_value() -> None:
    from services.evals.certified_suite import _compare

    ok, reason = _compare(["pct"], [[82.59]], ["pct"], [[0.8259]])
    assert ok and reason is not None and "value-equivalent" in reason
    ok, _ = _compare(["pct"], [[0.8259]], ["share"], [[82.59]])
    assert ok
    ok, _ = _compare(["pct"], [[82.59]], ["pct"], [[0.8359]])
    assert not ok


def test_compare_rounds_at_the_certified_values_printed_precision() -> None:
    from services.evals.certified_suite import _compare

    ok, reason = _compare(["r"], [[82.59]], ["r"], [[82.5912]])
    assert ok and reason is not None and "rounds to" in reason
    ok, _ = _compare(["r"], [[82.59]], ["r"], [[82.61]])
    assert not ok
    ok, _ = _compare(["n"], [[19]], ["n"], [[19.4]])
    assert ok  # an integer certified value: generation's 19.4 prints as 19
    ok, _ = _compare(["n"], [[19]], ["n"], [[19.6]])
    assert not ok
    # Row sets: the same physics per cell.
    ok, _ = _compare(
        ["seg", "pct"],
        [["a", 82.59], ["b", 17.41]],
        ["seg", "pct"],
        [["a", 0.825912], ["b", 0.174088]],
    )
    assert ok


@needs_db
def test_dst_test_exits_one_on_a_divergence_under_json_too(org, monkeypatch, capsys) -> None:
    """The exit code is the gate: a run with any failing case exits 1 whether
    it prints prose or the --json object, and the object's tally agrees."""
    from services.certify import store as certify_store
    from services.cli.main import _test
    from services.db.session import org_session

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
    wrong = "SELECT count(*) AS n FROM customers"
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM([wrong, "compose", wrong, "compose"]), "fake", "m"),
    )
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG, as_json=True))
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["total"] == 1 and report["passed"] == 0
    assert [r["verdict"] for r in report["results"]] == ["fail"]


def test_a_decimal_percentage_beside_a_label_column_is_value_equivalent() -> None:
    """Warehouse drivers hand back NUMERIC as Decimal; the projected
    comparison read it as a non-number and the pct-vs-fraction pair failed."""
    from decimal import Decimal

    from services.evals.certified_suite import _compare

    ok, reason = _compare(
        ["pct"], [[Decimal("58.71")]], ["hero", "pct"], [["x", Decimal("0.587123")]]
    )
    assert ok and reason is not None and "fraction/percentage" in reason
    ok, reason = _compare(
        ["pct"], [[Decimal("58.71")]], ["hero", "share"], [["x", Decimal("0.5871")]]
    )
    assert ok and reason is not None and "shape-lenient" in reason
    ok, _reason = _compare(["pct"], [[Decimal("58.71")]], ["hero", "pct"], [["x", Decimal("0.42")]])
    assert not ok


def test_a_row_set_pass_by_value_equivalence_carries_its_reason() -> None:
    """Label + pct rows where generation returned the fraction: the pass is
    by the value rules, so the reason says so — the scalar path already did,
    the row-set path returned a silent None."""
    from decimal import Decimal

    from services.evals.certified_suite import _compare

    ok, reason = _compare(
        ["label", "pct"],
        [["a", Decimal("82.59")], ["b", Decimal("10.10")]],
        ["label", "pct"],
        [["a", 0.8259], ["b", 0.101]],
    )
    assert ok and reason is not None and "value-equivalent" in reason
    # Last-bits wobble alone stays a plain pass.
    ok, reason = _compare(["l", "v"], [["a", 1.0000000001]], ["l", "v"], [["a", 1.0]])
    assert ok and reason is None


def test_an_extra_generated_column_beside_decimal_rows_is_projected_away() -> None:
    """(label, pct) certified as NUMERIC against (label, pct, count) generated:
    the extra column drops by name and the Decimal cells still compare as
    numbers — keyed as text they failed the fraction rule after projection."""
    from decimal import Decimal

    from services.evals.certified_suite import _compare

    ok, reason = _compare(
        ["label", "pct"],
        [["a", Decimal("82.59")], ["b", Decimal("10.10")]],
        ["label", "pct", "count"],
        [["a", 0.8259, 3], ["b", 0.101, 4]],
    )
    assert ok, reason
    assert reason is not None and "projection" in reason and "value-equivalent" in reason
    # A genuinely extra row is still a failure.
    ok, _ = _compare(
        ["label", "pct"],
        [["a", Decimal("82.59")]],
        ["label", "pct", "count"],
        [["a", 82.59, 3], ["c", 1.0, 5]],
    )
    assert not ok


@needs_db
def test_dst_test_prints_and_reports_the_lenient_reason(org, monkeypatch, capsys) -> None:
    """A value-equivalent pass names its reason on the PASS line and in the
    --json row; neither may read as an exact match."""
    from services.certify import store as certify_store
    from services.cli.main import _test
    from services.db.session import org_session

    certified = "SELECT 'all' AS label, 19.0 AS pct"
    generated = "SELECT 'all' AS label, 0.19 AS pct"
    with org_session(org) as s:
        certify_store.create(
            s, "customer_value", "share of repeat customers?", certified, None, "t"
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
        lambda _ref: ResolvedModel(ScriptedLLM([generated, "19 percent."]), "fake", "m"),
    )
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG))
    out = capsys.readouterr().out
    assert rc == 0, out
    line = next(li for li in out.splitlines() if li.startswith("PASS"))
    assert "value-equivalent" in line, line
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM([generated, "19 percent."]), "fake", "m"),
    )
    rc = _test(argparse.Namespace(lens="customer_value", all=False, org=_ORG, as_json=True))
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    (row,) = report["results"]
    assert row["verdict"] == "pass" and "value-equivalent" in str(row["reason"])
