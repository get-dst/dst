"""The publish gate sources its cases from the certified corpus.

On apply, ONLY active answers whose bindings the push's changed asset hashes
invalidate run through the certified suite against the CANDIDATE bundle (the
staleness system as test selection). A divergence is a verbatim error naming
both executed results and the changed asset — blue/green aborts the apply and
rolls the staged run back. A GREEN run re-stamps each passing answer's bindings
to current hashes (the plan's re-verify flag clears through evidence).

The spec's acceptance flow, end to end: certify → break the definition → apply
aborts naming the divergence with both results → retiring the LAST active
answer under block aborts too (gate starvation) → same retire under
eval_gate: warn publishes with the loud skip.

Product decisions 1+2 (release-runway): certifying self-tests itself in the
same apply (divergence is an ALERT naming both results, never a block —
certify-to-override is a primary use case; provenance-/status-only edits are
not re-tested; generation unavailable keeps the untested nudge), and the
≥1→0 active-answer retirement under eval_gate: block hard-aborts naming the
escape hatch (fresh lenses keep doing their first applies).
"""

from __future__ import annotations

from typing import Literal

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.config import settings
from services.contracts.fakes import ScriptedLLM
from services.contracts.semantic_model import Definition
from services.db.session import org_session
from services.evals import store as eval_store
from services.evals.certified_suite import CertifiedCaseResult
from services.evals.service import _divergence_error
from services.lenses import connection_store
from services.lenses.demo import (
    jaffle_customer_value_bundle,
    jaffle_customer_value_config,
    jaffle_shared_assets,
)
from services.lenses.repo import render_lens_repo
from services.llm.registry import ResolvedModel
from services.runtime.generator import GroundedSQLGenerator
from services.semantic import store as semantic_store
from services.semantic.files import render_semantic_files

client = TestClient(app)


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


@pytest.fixture()
def org():
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('CertGateRW') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    with org_session(oid) as s:
        connection_store.create_connection(
            s, "jaffle", "duckdb", {"path": settings.duckdb_jaffle_path}, None
        )
        s.commit()
    yield oid, {"Authorization": f"Bearer {raw}"}
    with admin.begin() as c:
        for table in (
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "semantic_asset",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


_Q = "How many repeat customers are there?"
_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 1"
_BROKEN_EXPR = "customers.number_of_orders > 2"
_BROKEN_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 2"


def _jaffle_scalar(sql: str) -> object:
    row = duckdb.connect(settings.duckdb_jaffle_path, read_only=True).execute(sql).fetchone()
    return row[0]  # type: ignore[index]


def _definitions(broken: bool = False) -> list[Definition]:
    _entities, definitions = jaffle_shared_assets()
    if not broken:
        return definitions
    return [
        d.model_copy(
            update={
                "sql_expr": _BROKEN_EXPR,
                "body": "A repeat customer has number_of_orders > 2.",
            }
        )
        if d.term == "repeat_customer"
        else d
        for d in definitions
    ]


def _files(
    answers: list[dict[str, object]],
    *,
    broken_definition: bool = False,
    entity_tweak: str | None = None,
    cases: list[dict[str, object]] | None = None,
    resolve_ambiguity: bool = False,
    gate: Literal["off", "warn", "block"] = "block",
) -> dict[str, str]:
    """The jaffle project with eval_gate: block and NO sample queries (a sample
    embedding the old definition logic would trip the stale-sample gate first —
    the same-push doctrine would demand updating it, which is not under test)."""
    entities, _ = jaffle_shared_assets()
    definitions = _definitions(broken=broken_definition)
    if resolve_ambiguity:
        # "value" made guessable — the push the must-clarify pin exists to catch.
        definitions = [
            d.model_copy(
                update={
                    "status": "active",
                    "possible_mappings": [],
                    "sql_expr": "customers.customer_lifetime_value",
                    "body": "Value means customer lifetime value.",
                }
            )
            if d.status == "ambiguous"
            else d
            for d in definitions
        ]
    files = dict(render_semantic_files(entities, definitions))
    if entity_tweak:
        customers = files["semantic/entities/customers.yaml"]
        files["semantic/entities/customers.yaml"] = customers.replace(
            "One row per customer.", entity_tweak
        )
    for path, content in render_lens_repo(jaffle_customer_value_bundle()).items():
        files[f"lenses/customer_value/{path}"] = content
    config = jaffle_customer_value_config()
    config.eval_gate = gate
    files["lenses/customer_value/lens.yaml"] = yaml.safe_dump(
        config.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
    )
    files["lenses/customer_value/queries.yaml"] = yaml.safe_dump(
        {"use_when": [], "sample_queries": []}, sort_keys=False, allow_unicode=True
    )
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        answers, sort_keys=False, allow_unicode=True
    )
    if cases is not None:
        files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
            cases, sort_keys=False, allow_unicode=True
        )
    return files


def _apply(headers: dict[str, str], files: dict[str, str]) -> list[dict[str, object]]:
    return client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()


def _gate_llm(monkeypatch, responses: list[str]) -> None:
    """The gate's smart-tier LLM, fresh-scripted per resolve call. Generator and
    composer share the instance, so responses interleave [gen, compose, …].
    The embedder is pinned to None — an ambient voyage key would turn the
    fresh-answer upsert into a live HTTP call (and a 502 abort on a bad key);
    unembedded-with-warning is the deterministic path these tests rely on."""
    monkeypatch.setattr(
        "services.llm.registry.resolve",
        lambda _ref: ResolvedModel(ScriptedLLM(list(responses)), "fake", "m"),
    )
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    # The parity harness selects the metric-layer IntentSQLGenerator for
    # this model; the scripted replies are SQL-shaped, so pin the grounded path.
    monkeypatch.setattr(
        "services.runtime.assembly.IntentSQLGenerator",
        lambda llm, model, temperature=0.0: GroundedSQLGenerator(
            llm, model=model, temperature=temperature
        ),
    )


def test_divergence_message_verbatim_shape() -> None:
    """Spec decision 3, pinned character for character."""
    r = CertifiedCaseResult(
        answer_id="a1",
        question="Q",
        oracle_result={"value": 19},
        generated_result={"value": 3},
        passed=False,
    )
    assert _divergence_error("Q", r, ["definition/repeat_customer"]) == (
        "certified 'Q' diverged: certified SQL → 19, generated → 3 "
        "(definition repeat_customer changed this push) — "
        "fix the definition, or re-certify/retire the answer"
    )


@needs_db
def test_unrelated_change_tests_nothing(org, monkeypatch) -> None:
    """Binding-scoped selection: the answer binds customers (+ its definition);
    an ORDERS edit must not run it — the scripted garbage LLM would abort the
    apply if the suite consumed it. No run is recorded either (nothing scored,
    the baseline machinery stays untouched)."""
    oid, headers = org
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert not any(e.get("errors") for e in out)

    edited = _files([{"question": _Q, "sql": _SQL}])
    edited["semantic/entities/orders.yaml"] = edited["semantic/entities/orders.yaml"].replace(
        "One row per order.", "One row per order, always."
    )
    out = _apply(headers, edited)
    assert not any(e.get("errors") for e in out)
    assert not any(e.get("action") == "aborted" for e in out)
    with org_session(oid) as s:
        assert eval_store.list_runs(s, "customer_value") == []


@needs_db
def test_acceptance_flow_certify_break_abort_retire_green(org, monkeypatch) -> None:
    """The end-to-end gate path: certify → break the definition → the apply
    aborts with the verbatim divergence naming BOTH executed results and the
    changed definition (blue/green: nothing lands, the staged run rolls back)
    → retiring the LAST active answer under block aborts too (gate starvation)
    → the escape hatch, eval_gate: warn in the same push, publishes with the
    loud gate-SKIPPED warning."""
    oid, headers = org
    n_old = _jaffle_scalar(_SQL)
    n_new = _jaffle_scalar(_BROKEN_SQL)
    assert n_old != n_new  # the fixture must actually distinguish the definitions

    # 1) Certify. The answer lands in the same push, stamped against current
    #    hashes — nothing is stale, the gate has nothing to re-test. (The
    #    decision-1 self-test consumes the garbage and ALERTS, never aborts.)
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])  # the GATE would abort if it consumed this
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert not any(e.get("errors") for e in out)
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        assert stored.bindings is not None
        assert "definition/repeat_customer" in stored.bindings  # it implements the term

    # 2) Break the definition. Generation now follows the new meaning (scripted
    #    to the > 2 SQL, twice — the suite re-runs a divergence once), the
    #    certified SQL still says the old one: forced human choice.
    _gate_llm(monkeypatch, [_BROKEN_SQL, "compose", _BROKEN_SQL, "compose"])
    broken = _files([{"question": _Q, "sql": _SQL}], broken_definition=True)
    out = _apply(headers, broken)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected"
    assert (
        f"certified '{_Q}' diverged: certified SQL → {n_old}, generated → {n_new} "
        "(definition repeat_customer changed this push) — "
        "fix the definition, or re-certify/retire the answer"
    ) in row["errors"]
    assert out[-1]["scope"] == "apply" and out[-1]["action"] == "aborted"
    with org_session(oid) as s:
        # Blue/green: the broken definition never landed, the suite's staged
        # run rolled back with it — the baseline cannot be lowered by a failure.
        asset = semantic_store.get_asset(s, "definition", "repeat_customer")
        assert asset is not None and asset.body["sql_expr"] == "customers.number_of_orders > 1"
        assert eval_store.list_runs(s, "customer_value") == []

    # 3) Retiring the answer in the same push WAS the green path — but it is
    #    the lens's LAST active answer and the gate is block: publishing would
    #    starve the gate (decision 2). The apply aborts naming the escape
    #    hatch; blue/green rolls the retirement AND the definition back.
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])  # a status-only edit is never self-tested
    retired = _files([{"question": _Q, "sql": _SQL, "status": "retired"}], broken_definition=True)
    out = _apply(headers, retired)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected"
    assert (
        "eval gate starved: this apply retires the last active certified "
        "answer while eval_gate: block — keep at least one active answer, "
        "or set `eval_gate: warn`/`off` in lens.yaml and re-apply"
    ) in row["errors"]
    assert out[-1]["scope"] == "apply" and out[-1]["action"] == "aborted"
    with org_session(oid) as s:
        asset = semantic_store.get_asset(s, "definition", "repeat_customer")
        assert asset is not None and asset.body["sql_expr"] == "customers.number_of_orders > 1"
        # NOT retired is what starvation cares about; these applies run keyless
        # (see _gate_llm), so the un-retired state is pending_embedding.
        assert certify_store.list_for_lens(s, "customer_value")[0].status == "pending_embedding"

    # 4) The named escape hatch: same retire under eval_gate: warn publishes —
    #    with the gate's loud SKIPPED warning, not silence.
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])  # nothing active remains to test
    retired_warn = _files(
        [{"question": _Q, "sql": _SQL, "status": "retired"}],
        broken_definition=True,
        gate="warn",
    )
    out = _apply(headers, retired_warn)
    assert not any(e.get("errors") for e in out)
    assert not any(e.get("action") == "aborted" for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any("gate SKIPPED" in w for w in row["warnings"])
    with org_session(oid) as s:
        asset = semantic_store.get_asset(s, "definition", "repeat_customer")
        assert asset is not None and asset.body["sql_expr"] == _BROKEN_EXPR
        assert certify_store.list_for_lens(s, "customer_value")[0].status == "retired"


_CLARIFY_CASE = [
    {"question": "what is the average value of a customer?", "expect": "clarify", "term": "value"}
]
_AVG_SQL = "SELECT AVG(customer_lifetime_value) AS avg_value FROM customers"


@needs_db
def test_behavioral_pin_gates_the_push_that_makes_value_guessable(org, monkeypatch) -> None:
    """The demo promise end to end: the must-clarify pin passes while 'value'
    is ambiguous (the deterministic pre-check fires — the scripted garbage is
    never consumed), setting the 1.0 baseline; the push that resolves the
    ambiguity makes generation ANSWER, the pin fails, the score regresses,
    blue/green aborts."""
    oid, headers = org
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}], cases=_CLARIFY_CASE))
    assert not any(e.get("errors") for e in out)
    with org_session(oid) as s:
        assert [r.score for r in eval_store.list_runs(s, "customer_value")] == [1.0]

    _gate_llm(monkeypatch, [_AVG_SQL, "About $17.61.", _AVG_SQL, "About $17.61."])
    out = _apply(
        headers,
        _files([{"question": _Q, "sql": _SQL}], cases=_CLARIFY_CASE, resolve_ambiguity=True),
    )
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected"
    assert any("regressed" in e for e in row["errors"])
    assert out[-1]["scope"] == "apply" and out[-1]["action"] == "aborted"
    with org_session(oid) as s:
        # the failed run rolled back with the abort — the baseline stands
        assert [r.score for r in eval_store.list_runs(s, "customer_value")] == [1.0]


@needs_db
def test_false_pass_never_severs_the_definition_binding(org, monkeypatch) -> None:
    """The headline bug: the definition breaks but generation
    false-passes (stale prose steered it to the certified SQL). The gate can't
    detect that — but the re-stamp must NOT recompute membership by containment,
    or the definition key drops (the broken expr no longer embeds in the frozen
    certified SQL) and staleness goes dark for exactly the diverged pair. Pin:
    after a false pass, the binding keeps definition/repeat_customer at the
    CURRENT hash — so the NEXT definition change still selects the answer."""
    oid, headers = org
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])
    _apply(headers, _files([{"question": _Q, "sql": _SQL}]))

    # Break the definition; generation "agrees with the oracle" anyway.
    _gate_llm(monkeypatch, [_SQL, "compose"])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}], broken_definition=True))
    assert not any(e.get("errors") for e in out)  # the false pass publishes — by design
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        hashes = semantic_store.asset_hashes(s)
        assert stored.bindings == {
            "entity/customers": hashes["entity/customers"],
            "definition/repeat_customer": hashes["definition/repeat_customer"],
        }  # membership survived; values re-stamped to the broken definition's hash

    # The next definition change (the revert) must still select the answer:
    # scripted to DIVERGE now — the abort proves the safety net is still wired.
    n_old = _jaffle_scalar(_SQL)
    n_new = _jaffle_scalar(_BROKEN_SQL)
    _gate_llm(monkeypatch, [_BROKEN_SQL, "compose", _BROKEN_SQL, "compose"])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected"
    assert any(f"certified SQL → {n_old}, generated → {n_new}" in e for e in row["errors"])


@needs_db
def test_retiring_alongside_a_definition_change_never_severs_bindings(org, monkeypatch) -> None:
    """The confirmation run's FAIL: retiring an answer in the same push as the
    definition it depends on diverging lands (retired answers are never
    tested; under decision 2 the LAST-active retirement needs the eval_gate:
    warn hatch) — but the update path recomputed membership by containment,
    so the definition key dropped and a later reactivation came back blind to
    its own governing definition. A status edit verifies NOTHING: bindings
    freeze. Reactivating while the definition is still broken must therefore
    still read STALE — the gate re-tests it rather than trusting it."""
    oid, headers = org
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])
    _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    with org_session(oid) as s:
        before = certify_store.list_for_lens(s, "customer_value")[0].bindings
        assert before is not None and "definition/repeat_customer" in before

    # Retire + break in one push, via the warn hatch (block would abort on the
    # ≥1→0 starvation transition): nothing active to test, bindings frozen.
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])  # the gate would abort if anything ran
    retired = _files(
        [{"question": _Q, "sql": _SQL, "status": "retired"}],
        broken_definition=True,
        gate="warn",
    )
    out = _apply(headers, retired)
    assert not any(e.get("errors") for e in out)
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        assert stored.status == "retired"
        assert stored.bindings == before  # frozen, not recomputed

    # Reactivate (gate back to block) while the definition is STILL broken:
    # the frozen bindings disagree with current hashes, so the answer is
    # selected and diverges — a status-only edit is never self-tested, so the
    # script is consumed by the GATE alone.
    n_old = _jaffle_scalar(_SQL)
    n_new = _jaffle_scalar(_BROKEN_SQL)
    _gate_llm(monkeypatch, [_BROKEN_SQL, "compose", _BROKEN_SQL, "compose"])
    reactivated = _files(
        [{"question": _Q, "sql": _SQL, "status": "active"}], broken_definition=True
    )
    out = _apply(headers, reactivated)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected"
    assert any(f"certified SQL → {n_old}, generated → {n_new}" in e for e in row["errors"])


@needs_db
def test_green_run_restamps_bindings_and_clears_the_plan_flag(org, monkeypatch) -> None:
    """Decision 5: a passing test IS the re-verification. A customers edit
    selects the answer; generation reproduces the certified result; the apply
    publishes, the answer's bindings re-stamp to current hashes, the plan's
    re-verify flag is gone — and the suite's run rode the committed session."""
    oid, headers = org
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])
    _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    with org_session(oid) as s:
        before = certify_store.list_for_lens(s, "customer_value")[0].bindings

    edited = _files(
        [{"question": _Q, "sql": _SQL}], entity_tweak="One row per customer, deduplicated."
    )
    _gate_llm(monkeypatch, [_SQL, "compose"])  # generation agrees with the oracle
    out = _apply(headers, edited)
    assert not any(e.get("errors") for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "updated"

    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        hashes = semantic_store.asset_hashes(s)
        assert stored.bindings != before  # re-stamped…
        assert stored.bindings == {
            "entity/customers": hashes["entity/customers"],
            "definition/repeat_customer": hashes["definition/repeat_customer"],
        }  # …to current hashes
        runs = eval_store.list_runs(s, "customer_value")
        assert [r.score for r in runs] == [1.0]  # the suite's run persisted with the publish
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": edited}).json()
    assert not any(e.get("certified") for e in plan)  # the re-verify flag cleared


@needs_db
def test_certify_self_test_divergence_is_an_alert_never_a_block(org, monkeypatch) -> None:
    """Decision 1: the apply that LANDS a certified answer runs it through the
    certified suite in the same apply. Generation disagrees here (scripted to
    the >2 SQL) — the apply still publishes, because certify-to-override is a
    primary use case, with a LOUD warning naming BOTH executed results and the
    override reading. The answer it tested gets no untested nudge."""
    oid, headers = org
    n_old = _jaffle_scalar(_SQL)
    n_new = _jaffle_scalar(_BROKEN_SQL)
    _gate_llm(monkeypatch, [_BROKEN_SQL, "compose", _BROKEN_SQL, "compose"])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert not any(e.get("errors") for e in out)
    assert not any(e.get("action") == "aborted" for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created" and row["version"] is not None
    alert = next(w for w in row["warnings"] if "diverged at certification" in w)
    assert f"certified SQL → {n_old}, generated → {n_new}" in alert
    assert (
        "divergence at certification time can be the point — the certified answer "
        "overrides generation; re-check the oracle if that's not what you meant"
    ) in alert
    assert not any("landed untested" in w for w in row["warnings"])
    with org_session(oid) as s:  # override semantics: the certified answer stands
        assert certify_store.list_for_lens(s, "customer_value")[0].sql == _SQL


@needs_db
def test_provenance_only_edit_is_not_self_tested_but_a_sql_reauthor_is(org, monkeypatch) -> None:
    """Decision 1 scope, the binding-lifecycle edit classes: a provenance-only
    edit (verified_by) re-verifies an unmoved SQL — no re-test, the scripted
    garbage would alert if consumed. Re-authoring the SQL in the next push IS
    a re-test: the alert appears, and the apply still publishes."""
    oid, headers = org
    _gate_llm(monkeypatch, [_SQL, "compose"])  # the create's self-test: green, no alert
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert not any(e.get("errors") for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert not any("diverged at certification" in w for w in row["warnings"])
    assert not any("landed untested" in w for w in row["warnings"])

    _gate_llm(monkeypatch, ["GARBAGE ;;;"])  # would alert if any self-test ran
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL, "verified_by": "alex"}]))
    assert not any(e.get("errors") for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "certified answers: created 0, updated 1, unchanged 0" in row["applied"]
    assert not any("diverged at certification" in w for w in row["warnings"])
    assert not any("landed untested" in w for w in row["warnings"])
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value")[0].verified_by == "alex"

    # Re-author the SQL (certify-to-override the >2 meaning): generation still
    # says the >1 result — alert naming both, publish anyway.
    n_now = _jaffle_scalar(_BROKEN_SQL)
    n_gen = _jaffle_scalar(_SQL)
    _gate_llm(monkeypatch, [_SQL, "compose", _SQL, "compose"])
    out = _apply(
        headers,
        _files([{"question": _Q, "sql": _BROKEN_SQL, "verified_by": "alex"}]),
    )
    assert not any(e.get("errors") for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    alert = next(w for w in row["warnings"] if "diverged at certification" in w)
    assert f"certified SQL → {n_now}, generated → {n_gen}" in alert
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value")[0].sql == _BROKEN_SQL


@needs_db
def test_generation_unavailable_keeps_the_untested_nudge(org, monkeypatch) -> None:
    """Decision 1's fallback mirrors the gate's unresolvable-model degradation:
    the lens's model does not resolve → the self-test SKIPS with a loud warning
    naming what it tried, and the landed answer keeps the `dst test` nudge.

    It must never fall back to another provider's client: that is how a lens
    pinned to `deepseek/deepseek-v4-pro` came to send a real request to
    Anthropic naming a DeepSeek model (see `services/project/apply.py`)."""
    _oid, headers = org
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert not any(e.get("errors") for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any(
        "certify self-test SKIPPED: this lens's model cannot be served here" in w
        for w in row["warnings"]
    )
    assert any(
        w.startswith("1 certified answer(s) landed untested — run `dst test customer_value`")
        for w in row["warnings"]
    )


@needs_db
def test_a_lens_pinned_to_an_unconfigured_provider_never_dials_the_configured_one(
    org, monkeypatch
) -> None:
    """Bug class: applying a lens pinned to `deepseek/deepseek-v4-pro` on an
    ANTHROPIC-ONLY server sends a real request TO ANTHROPIC naming
    `deepseek-v4-pro` — `certify self-test failed to run: anthropic (HTTP 404)
    … 'message': 'model: deepseek-v4-pro'`. The publish gate refuses the lens;
    the self-test dials anyway if it falls back to
    `registry.resolve(registry.tier("smart"))` for the CLIENT while the
    generators still take the NAME from the lens config.

    So this test runs the real registry against real (Anthropic-only) provider
    config — no `registry.resolve` stub, or the fallback under test would be
    stubbed out with it — and puts a tripwire where Anthropic would be."""
    from services.config import ProviderConfig
    from services.contracts.lens_config import ModelConfig

    _oid, headers = org
    dialled: list[str] = []

    class _Tripwire:
        def complete(self, *, model: str, **_kw: object) -> object:
            dialled.append(model)
            raise AssertionError(f"dialled the wrong vendor with model={model!r}")

    monkeypatch.setattr(
        settings, "providers", {"anthropic": ProviderConfig(type="anthropic", api_key="sk-ant")}
    )
    monkeypatch.setattr(
        "services.llm.anthropic_provider.AnthropicProvider", lambda _k, **_: _Tripwire()
    )
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)

    files = _files([{"question": _Q, "sql": _SQL}], gate="off")
    config = jaffle_customer_value_config()
    config.eval_gate = "off"  # isolate the self-test from the publish gate
    config.model = ModelConfig(provider="deepseek", model="deepseek-v4-pro")
    files["lenses/customer_value/lens.yaml"] = yaml.safe_dump(
        config.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
    )
    out = _apply(headers, files)
    row = next(e for e in out if e.get("lens") == "customer_value")

    assert dialled == [], "the operator's question went to a vendor they did not choose"
    # The publish gate refuses the lens (8ab4099) …
    assert any("cannot answer here" in e for e in row["errors"])
    # … and the self-test says WHY it did not run: the model tried, the
    # configured providers, and what this install would have served instead.
    skip = next(w for w in row["warnings"] if "certify self-test SKIPPED" in w)
    assert "deepseek/deepseek-v4-pro" in skip
    assert "configured providers: anthropic" in skip
    assert "smart=anthropic/claude-sonnet-4-6" in skip


@needs_db
def test_certify_self_test_is_unconditional_under_eval_gate_off(org, monkeypatch) -> None:
    """Applying a certification unit-tests it and alerts,
    PERIOD — eval_gate: off (the default) governs the publish gate, never the
    self-test. The default-lens stranger must see the divergence alert, with
    the scorer's reason folded in, not just the untested nudge."""
    oid, headers = org
    n_old = _jaffle_scalar(_SQL)
    n_new = _jaffle_scalar(_BROKEN_SQL)
    _gate_llm(monkeypatch, [_BROKEN_SQL, "compose", _BROKEN_SQL, "compose"])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}], gate="off"))
    assert not any(e.get("errors") for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created" and row["version"] is not None
    alert = next(w for w in row["warnings"] if "diverged at certification" in w)
    assert f"certified SQL → {n_old}, generated → {n_new}" in alert
    assert f"(generated {n_new} vs certified {n_old})" in alert  # the scorer's reason
    assert "divergence at certification time can be the point" in alert
    assert not any("landed untested" in w for w in row["warnings"])
    with org_session(oid) as s:  # override semantics: the certified answer stands
        assert certify_store.list_for_lens(s, "customer_value")[0].sql == _SQL


class _StoppedClock:
    """A `time` stand-in for the suite: the first case reads before the deadline,
    the second past it. No sleeping, no timing race — the budget is exercised
    through the clock the suite actually reads."""

    def __init__(self) -> None:
        self._reads = iter([float("-inf"), float("inf")])

    def monotonic(self) -> float:
        return next(self._reads)


@needs_db
def test_an_apply_carrying_certified_answers_finishes_on_a_budget(org, monkeypatch) -> None:
    """The apply that looked like a deadlock. The self-test is unconditional and
    every case is a full generation, so a push of seven answers ran ~350s of
    model calls inside the request — past `dst apply --timeout` (300s) —
    while apply's backend sat `idle in transaction` at 0% CPU holding the org's
    advisory lock, and every later apply got 409.

    So the sweep is bounded (DST_CERTIFY_SELFTEST_BUDGET_S). The apply still
    COMPLETES and publishes; the answers past the budget land, untested, with
    both the reason (the budget) and the action (`dst test`) named."""
    oid, headers = org
    monkeypatch.setattr("services.evals.certified_suite.time", _StoppedClock())
    _gate_llm(monkeypatch, [_SQL, "compose"])
    out = _apply(
        headers,
        _files(
            [
                {"question": _Q, "sql": _SQL},
                {
                    "question": "How many customers are there?",
                    "sql": "SELECT count(*) FROM customers",
                },
            ],
            gate="off",
        ),
    )
    assert not any(e.get("errors") for e in out)
    assert not any(e.get("action") == "aborted" for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created" and row["version"] is not None
    assert "certified answers: created 2, updated 0, unchanged 0" in row["applied"]
    assert any(
        "certify self-test stopped after 1 of 2 answer(s): the 120s self-test budget "
        "(DST_CERTIFY_SELFTEST_BUDGET_S) ran out" in w
        for w in row["warnings"]
    )
    assert any(
        w.startswith("1 certified answer(s) landed untested — run `dst test customer_value`")
        for w in row["warnings"]
    )
    # …and the answer it DID reach is not claimed as verified. The self-test
    # samples a stochastic generator once: a case it agreed on at 01:32 diverged
    # in `dst test` at 02:31 (0.666… vs 66.66…, fraction vs percentage).
    # Apply says what one sample establishes, which is less than it sounds.
    assert any(
        "certify self-test: 1 of 2 landed answer(s) sampled ONCE against generation" in a
        and "not proof that it always will" in a
        for a in row["applied"]
    ), row["applied"]
    with org_session(oid) as s:  # bounded, not abandoned: both answers landed
        assert len(certify_store.list_for_lens(s, "customer_value")) == 2


@needs_db
def test_retiring_last_active_under_warn_warns_even_when_cases_keep_scoring(
    org, monkeypatch
) -> None:
    """Retiring the last active answer under warn yields ZERO gate output when
    approved cases keep the publish gate scoreable, so the could-not-score
    SKIPPED fallback never fires. The ≥1→0 transition itself must warn, in the
    apply row the CLI prints."""
    oid, headers = org
    _gate_llm(monkeypatch, [_SQL, "compose"])  # self-test green on the landing apply
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}], cases=_CLARIFY_CASE, gate="warn"))
    assert not any(e.get("errors") for e in out)

    _gate_llm(monkeypatch, ["GARBAGE ;;;"])  # status-only edit: no self-test; pin is deterministic
    out = _apply(
        headers,
        _files(
            [{"question": _Q, "sql": _SQL, "status": "retired"}],
            cases=_CLARIFY_CASE,
            gate="warn",
        ),
    )
    assert not any(e.get("errors") for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "updated" and row["version"] is not None
    starved = next(w for w in row["warnings"] if "eval gate starved" in w)
    assert "eval_gate: warn" in starved and "certify a replacement" in starved
    # the probe's exact hole: the behavioral pin kept scoring, so the SKIPPED
    # fallback (rightly) never fired — the transition warning is the signal
    assert not any("gate SKIPPED" in w and "configured but" in w for w in row["warnings"])
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value")[0].status == "retired"


@needs_db
def test_deleting_last_active_from_the_file_hits_the_starvation_block(org, monkeypatch) -> None:
    """Files win, so REMOVING the last
    active answer's entry from certified_answers.yaml is the same ≥1→0
    transition as retiring it — block aborts (blue/green keeps the answer),
    warn publishes the deletion with the starvation warning and the loud
    deleted count."""
    oid, headers = org
    _gate_llm(monkeypatch, [_SQL, "compose"])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert not any(e.get("errors") for e in out)

    _gate_llm(monkeypatch, ["GARBAGE ;;;"])  # nothing lands → nothing self-tests
    out = _apply(headers, _files([]))  # file present, entry gone — under block
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected"
    assert any("eval gate starved" in e and "eval_gate: block" in e for e in row["errors"])
    assert out[-1]["scope"] == "apply" and out[-1]["action"] == "aborted"
    with org_session(oid) as s:  # blue/green: the deletion rolled back
        # Keyless apply (see _gate_llm) ⇒ pending_embedding, still not retired.
        assert [a.status for a in certify_store.list_for_lens(s, "customer_value")] == [
            "pending_embedding"
        ]

    _gate_llm(monkeypatch, ["GARBAGE ;;;"])
    out = _apply(headers, _files([], gate="warn"))  # the named escape hatch
    assert not any(e.get("errors") for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "certified answers: created 0, updated 0, deleted 1, unchanged 0" in row["applied"]
    assert any("eval gate starved" in w and "eval_gate: warn" in w for w in row["warnings"])
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value") == []


@needs_db
def test_fresh_lens_first_applies_under_block_still_work(org, monkeypatch) -> None:
    """Decision 2 scope-pin: the starvation abort is ONLY the ≥1→0 retirement
    transition. A fresh lens under eval_gate: block — no certified answers at
    all, then a push landing only a retired-on-arrival answer (0→0) — keeps
    doing its first applies with the loud skip, never the abort."""
    _oid, headers = org
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])
    out = _apply(headers, _files([]))
    assert not any(e.get("errors") for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created" and row["version"] is not None
    assert any("gate SKIPPED" in w for w in row["warnings"])

    _gate_llm(monkeypatch, ["GARBAGE ;;;"])  # retired-on-arrival: never tested, never nagged
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL, "status": "retired"}]))
    assert not any(e.get("errors") for e in out)
    assert not any(e.get("action") == "aborted" for e in out)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "certified answers: created 1, updated 0, unchanged 0" in row["applied"]
    assert not any("landed untested" in w for w in row["warnings"])


def test_gate_blocks_failing_cases_without_a_baseline() -> None:
    """No prior score is not the same as no regression — the first gated apply
    is exactly when the gate is most needed. Failing cases block under `block`
    regardless of history."""
    from services.evals.service import gate_decision

    # First run, one failure: blocked.
    assert gate_decision(gate="block", score=0.5, prev_score=None, failing=["c1"]).blocked
    # First run, fully green: publishes.
    assert not gate_decision(gate="block", score=1.0, prev_score=None, failing=[]).blocked
    # Flat-but-failing: a red baseline must not license staying red.
    assert gate_decision(gate="block", score=0.5, prev_score=0.5, failing=["c1"]).blocked
    # Regression from green still blocks.
    assert gate_decision(gate="block", score=0.5, prev_score=1.0, failing=["c1"]).blocked
    # warn never blocks — it warns (the publish path surfaces the failing count).
    assert not gate_decision(gate="warn", score=0.5, prev_score=None, failing=["c1"]).blocked


def test_gate_label_and_dict_expose_the_decision(org=None) -> None:
    """A prose-only label on the apply row makes --json a strict subset of what
    the gate knew. One label function (apply footer and `dst evals gate` share
    it) and one dict shape (rows and the gate endpoint share it)."""
    from services.evals.service import gate_decision, gate_decision_dict, gate_label

    red = gate_decision(gate="block", score=0.5, prev_score=1.0, failing=["c1"])
    assert gate_label(red) == "blocked"
    d = gate_decision_dict(red)
    assert d is not None
    assert d["score"] == 0.5 and d["prev_score"] == 1.0 and d["failing"] == ["c1"]

    warn_red = gate_decision(gate="warn", score=0.5, prev_score=None, failing=["c1"])
    assert gate_label(warn_red) == "failing (1 case(s), score 0.5)"

    green = gate_decision(gate="block", score=1.0, prev_score=1.0, failing=[])
    assert gate_label(green) == "passed"

    assert gate_label(None) is None
    assert gate_decision_dict(None) is None


@needs_db
def test_gate_dry_run_reports_the_decision_and_stages_nothing(org, monkeypatch) -> None:
    """Without a dry run, the only way to learn a gate verdict is a full apply.
    POST /mgmt/lenses/{lens}/evals/gate runs the apply
    gate's own code path on the stored bundle and reports the decision — and
    its staged eval run rolls back, so a red dry run can never become the
    baseline the next real apply is judged against."""
    oid, headers = org
    _gate_llm(monkeypatch, [_SQL, "There are that many."])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert not any(e.get("errors") for e in out), out

    # The APPLY ROW carries the structured decision too — the gate label alone
    # would make --json a strict subset of what the gate knew.
    lens_row = next(e for e in out if e.get("lens") == "customer_value")
    detail = lens_row.get("gate_detail")
    assert detail is not None, "apply --json rows must carry gate_detail"
    assert "score" in detail and "failing" in detail and "skip_reason" in detail

    with org_session(oid) as s:
        runs_before = len(eval_store.list_runs(s, "customer_value"))

    _gate_llm(monkeypatch, [_SQL, "There are that many."])
    r = client.post("/mgmt/lenses/customer_value/evals/gate", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lens"] == "customer_value"
    assert body["gate"] == "passed" or body["gate"].startswith("skipped"), body
    assert body["gate_detail"] is not None and "score" in body["gate_detail"]

    # The dry run staged NO eval run — atomicity stays the baseline filter.
    with org_session(oid) as s:
        assert len(eval_store.list_runs(s, "customer_value")) == runs_before

    # And an unknown lens is a 404, not a stack trace.
    assert client.post("/mgmt/lenses/no_such/evals/gate", headers=headers).status_code == 404


@needs_db
def test_the_dry_run_sees_an_unpublished_shared_asset_change(org, monkeypatch) -> None:
    """Gating only the stored bundle false-greens every shared-asset change —
    1.0/passed where the apply scores 0.0 with failing cases, certifying
    exactly the pushes apply then rejects. With the candidate tree in the
    request, the dry run runs the REAL apply path (semantic assets landed,
    lens compiled, gate scored) inside a savepoint."""
    oid, headers = org
    # Baseline: the must-clarify pin passes while 'value' is ambiguous (1.0).
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}], cases=_CLARIFY_CASE))
    assert not any(e.get("errors") for e in out)

    # The candidate tree resolves the ambiguity in a SHARED definition only.
    # Stored-bundle mode (no files) is blind to it and must SAY so…
    candidate = _files([{"question": _Q, "sql": _SQL}], cases=_CLARIFY_CASE, resolve_ambiguity=True)
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])
    stored = client.post("/mgmt/lenses/customer_value/evals/gate", headers=headers).json()
    assert stored["gate"] == "passed"
    assert "STORED bundle" in stored.get("scope_note", "")

    # …while candidate mode sees what the apply will see: the pin fails.
    _gate_llm(monkeypatch, [_AVG_SQL, "About $17.61.", _AVG_SQL, "About $17.61."])
    dry = client.post(
        "/mgmt/lenses/customer_value/evals/gate",
        headers=headers,
        json={"files": candidate},
    ).json()
    assert dry["gate"] in ("blocked", "regressed"), dry
    assert dry.get("gate_detail", {}).get("failing"), dry

    # Nothing leaked: the shared definition is still ambiguous in the store,
    # so the REAL apply of the same tree still blocks identically.
    _gate_llm(monkeypatch, [_AVG_SQL, "About $17.61.", _AVG_SQL, "About $17.61."])
    out = _apply(headers, candidate)
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected"
    with org_session(oid) as s:
        # the dry run's staged eval runs rolled back with everything else
        assert [r.score for r in eval_store.list_runs(s, "customer_value")] == [1.0]


def _apply_override(headers: dict[str, str], files: dict[str, str], reason: str | None) -> object:
    params: dict[str, str] = {"allow_failing_cases": "true"}
    if reason is not None:
        params["override_reason"] = reason
    return client.post("/mgmt/project/apply", headers=headers, params=params, json={"files": files})


@needs_db
def test_the_audited_override_publishes_past_a_failing_gate(org, monkeypatch) -> None:
    """Without an override, the only escape from a block is committing
    eval_gate: warn — a governance downgrade someone must remember to
    restore. The override
    publishes ONCE past failing cases with the reason loud on the row and
    durable in the version history; the file keeps eval_gate: block."""
    oid, headers = org
    # Same shape as the behavioral-pin block test: baseline 1.0, then the push
    # that resolves the ambiguity makes the pin fail and the gate block.
    _gate_llm(monkeypatch, ["GARBAGE ;;;"])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}], cases=_CLARIFY_CASE))
    assert not any(e.get("errors") for e in out)

    # Without the override: blocked, and the error names the audited escape.
    _gate_llm(monkeypatch, [_AVG_SQL, "About $17.61.", _AVG_SQL, "About $17.61."])
    out = _apply(
        headers,
        _files([{"question": _Q, "sql": _SQL}], cases=_CLARIFY_CASE, resolve_ambiguity=True),
    )
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected"
    assert any("--allow-failing-cases" in e for e in row["errors"])

    # With the override + reason: published, gate labelled overridden, the
    # reason on the row and in the version summary.
    _gate_llm(monkeypatch, [_AVG_SQL, "About $17.61.", _AVG_SQL, "About $17.61."])
    r = _apply_override(
        headers,
        _files([{"question": _Q, "sql": _SQL}], cases=_CLARIFY_CASE, resolve_ambiguity=True),
        "intended behaviour change: value resolved to CLV",
    )
    out = r.json()  # type: ignore[union-attr]
    assert not any(e.get("action") == "aborted" for e in out), out
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["version"] is not None
    assert str(row["gate"]).startswith("overridden")
    assert any(
        "PUBLISHED ANYWAY" in w and "intended behaviour change" in w for w in row["warnings"]
    )
    with org_session(oid) as s:
        summaries = [
            r0[0]
            for r0 in s.execute(
                text("SELECT summary FROM lens_version WHERE lens = 'customer_value'")
            ).all()
        ]
    assert any("gate override: intended behaviour change" in (smy or "") for smy in summaries)


@needs_db
def test_the_override_without_a_reason_is_refused(org) -> None:
    _oid, headers = org
    r = _apply_override(headers, _files([]), None)
    assert r.status_code == 422  # type: ignore[union-attr]
    assert "override_reason" in r.json()["detail"]  # type: ignore[union-attr]


@needs_db
def test_the_override_never_covers_a_certified_divergence(org, monkeypatch) -> None:
    """A certified divergence is a served-answer contradiction — the override
    must not launder it; re-certify in the same push instead."""
    oid, headers = org
    _gate_llm(monkeypatch, [_SQL, "There are that many."])
    out = _apply(headers, _files([{"question": _Q, "sql": _SQL}]))
    assert not any(e.get("errors") for e in out), out

    # Break the definition the certified answer binds — divergence blocks…
    _gate_llm(monkeypatch, [_BROKEN_SQL, "There are fewer."])
    r = _apply_override(
        headers,
        _files([{"question": _Q, "sql": _SQL}], broken_definition=True),
        "trying to force it through",
    )
    out = r.json()  # type: ignore[union-attr]
    # …override or not: the apply aborts on the divergence.
    assert any(e.get("action") == "aborted" for e in out) or any(
        e.get("errors") for e in out if e.get("lens") == "customer_value"
    ), out
