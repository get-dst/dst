"""Apply gates for certified SQL (+ the opt-in probe).

Two hard gates run per incoming answer — SQL must parse in the lens dialect and
must reference only tables the lens models; a gated answer is rejected BY NAME
and (blue/green) aborts the whole apply — nothing lands until every
answer passes. `dst apply --probe-certified` additionally runs each NEW
answer once (read-only, row-capped) through the lens's connector and records
verified_value; probe failure is a warning + stored anyway — verification is
advisory, the gates are not. Never probed by default.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.config import settings
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses import store as lens_store
from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
from services.lenses.repo import render_lens_repo
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
def org(monkeypatch):
    from services.contracts.fakes import HashEmbedder
    from services.project import apply as apply_engine

    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    # The certify self-test is UNCONDITIONAL now — pin the smart tier to
    # unresolvable so applies degrade to the loud skip instead of dialing an
    # ambient provider key.
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))  # claim cleanly with HashEmbedder
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('CertGateT') RETURNING id")
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
        c.execute(text("DELETE FROM embedding_meta"))
    admin.dispose()


_GOOD_Q = "How many repeat customers are there?"
_GOOD_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 1"


def _files(answers: list[dict[str, object]], eval_gate: str | None = None) -> dict[str, str]:
    entities, definitions = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions))
    for path, content in render_lens_repo(jaffle_customer_value_bundle()).items():
        files[f"lenses/customer_value/{path}"] = content
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        answers, sort_keys=False, allow_unicode=True
    )
    if eval_gate is not None:
        cfg = yaml.safe_load(files["lenses/customer_value/lens.yaml"])
        cfg["eval_gate"] = eval_gate
        files["lenses/customer_value/lens.yaml"] = yaml.safe_dump(cfg, sort_keys=False)
    return files


def _apply_out(
    headers: dict[str, str], files: dict[str, str], probe: bool = False
) -> list[dict[str, object]]:
    return client.post(
        "/mgmt/project/apply",
        headers=headers,
        params={"probe_certified": "true"} if probe else {},
        json={"files": files},
    ).json()


def _apply(
    headers: dict[str, str], files: dict[str, str], probe: bool = False
) -> dict[str, object]:
    out = _apply_out(headers, files, probe)
    return next(e for e in out if e.get("lens") == "customer_value")


def _aborted(out: list[dict[str, object]]) -> bool:
    return out[-1] == {
        "scope": "apply",
        "action": "aborted",
        "detail": "nothing deployed — fix the errors and re-apply",
    }


@needs_db
def test_parse_gate_rejects_by_name_and_aborts_the_apply(org) -> None:
    """Blue/green replaced the old 'skipped by name, the rest lands' contract:
    a gated answer is still named, but the apply is all-or-nothing now."""
    oid, headers = org
    out = _apply_out(
        headers,
        _files(
            [
                {"question": "Broken import?", "sql": "SELEC nope FROM"},
                {"question": _GOOD_Q, "sql": _GOOD_SQL},
            ]
        ),
    )
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any(
        "certified answer 'Broken import?' rejected: SQL does not parse as duckdb" in e
        for e in row["errors"]
    )
    assert _aborted(out)
    with org_session(oid) as s:  # neither the good answer nor the lens landed
        assert certify_store.list_for_lens(s, "customer_value") == []
        assert not lens_store.lens_exists(s, "customer_value")


@needs_db
def test_boundary_gate_names_the_foreign_table(org) -> None:
    oid, headers = org
    out = _apply_out(
        headers,
        _files(
            [
                {
                    "question": "Total Stripe fees?",
                    "sql": "SELECT sum(fee) FROM stripe_payments "
                    "JOIN customers USING (customer_id)",
                }
            ]
        ),
    )
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any(
        "certified answer 'Total Stripe fees?' rejected: references 'stripe_payments'" in e
        and "lens boundary" in e
        for e in row["errors"]
    )
    assert _aborted(out)
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value") == []

    # An edit to an EXISTING answer is gated the same way: the apply aborts,
    # the stored SQL stands.
    _apply(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}]))
    out = _apply_out(
        headers, _files([{"question": _GOOD_Q, "sql": "SELECT count(*) FROM stripe_payments"}])
    )
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any("'stripe_payments'" in e for e in row["errors"])
    assert _aborted(out)
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value")[0].sql == _GOOD_SQL


@needs_db
def test_probe_is_opt_in_and_records_verified_value(org) -> None:
    oid, headers = org
    # Default apply: never probed — verified_value stays empty.
    row = _apply(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}]))
    assert "certified answers: created 1, updated 0, unchanged 0" in row["applied"]
    assert not any("probe" in w for w in row["warnings"])
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value")[0].verified_value is None

    # --probe-certified: only the NEW answer executes (jaffle fixture: 19 repeats).
    row = _apply(
        headers,
        _files(
            [
                {"question": _GOOD_Q, "sql": _GOOD_SQL},
                {
                    "question": "How many customers in total?",
                    "sql": "SELECT count(*) FROM customers",
                },
            ]
        ),
        probe=True,
    )
    assert "certified answers: created 1, updated 0, unchanged 1" in row["applied"]
    with org_session(oid) as s:
        by_q = {a.question: a for a in certify_store.list_for_lens(s, "customer_value")}
        assert by_q[_GOOD_Q].verified_value is None  # not new — never re-probed
        probed = by_q["How many customers in total?"].verified_value
        assert probed is not None and set(probed) == {"value"}
        import duckdb

        expected = (
            duckdb.connect(settings.duckdb_jaffle_path, read_only=True)
            .execute("SELECT count(*) FROM customers")
            .fetchone()
        )
        assert probed["value"] == expected[0]  # type: ignore[index]


@needs_db
def test_probe_records_verified_value_keyless(org, monkeypatch) -> None:
    """--probe-certified must not silently no-op without an embedding provider:
    probing is orthogonal to embedding — keyless still executes the NEW answer
    and records its verified value (stored unembedded + the reindex warning)."""
    from services.project import apply as apply_engine

    oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", lambda: None)
    row = _apply(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}]), probe=True)
    assert any("stored unembedded" in w for w in row["warnings"])
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        assert stored.verified_value is not None
        assert stored.verified_value["value"] == 19  # jaffle fixture: 19 repeats


@needs_db
def test_bulk_import_warns_at_ten_new_answers(org) -> None:
    """One apply CREATING >=10 answers gets the honesty warning — counted
    on creates, not totals (9 then 10-more warns only the second time)."""
    _oid, headers = org
    nine = [{"question": f"Bulk question {i}?", "sql": _GOOD_SQL} for i in range(9)]
    row = _apply(headers, _files(nine))
    assert "certified answers: created 9, updated 0, unchanged 0" in row["applied"]
    assert not any("certified answers landed" in w for w in row["warnings"])
    # Any create is untested by the binding-scoped gate — the sweep gets named.
    assert any(
        w.startswith("9 certified answer(s) landed untested — run `dst test customer_value`")
        for w in row["warnings"]
    )

    ten_more = [{"question": f"Bulk question {i}?", "sql": _GOOD_SQL} for i in range(9, 19)]
    row = _apply(headers, _files(nine + ten_more))
    assert "certified answers: created 10, updated 0, unchanged 9" in row["applied"]
    assert any(
        w.startswith("10 certified answers landed — run `dst reviews`/evals") and "dst reindex" in w
        for w in row["warnings"]
    )


@needs_db
def test_probe_failure_warns_and_stores_anyway(org) -> None:
    oid, headers = org
    row = _apply(
        headers,
        _files([{"question": "Ghost column?", "sql": "SELECT no_such_column FROM customers"}]),
        probe=True,
    )
    assert any(
        "certified answer 'Ghost column?' probe failed" in w and "stored unverified" in w
        for w in row["warnings"]
    )
    assert "certified answers: created 1, updated 0, unchanged 0" in row["applied"]
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        assert stored.question == "Ghost column?"
        assert stored.verified_value is None


@needs_db
def test_shape_gate_rejects_dml_and_multi_statement(org) -> None:
    """Parsing alone is not enough: DML, multi-statement, and junk must never
    enter the certified store (they'd only fail at serve time). Both offenders
    are named in one response; the apply aborts — the clean answer
    does not land either."""
    oid, headers = org
    out = _apply_out(
        headers,
        _files(
            [
                {
                    "question": "Wipe a customer?",
                    "sql": "DELETE FROM customers WHERE customer_id = 1",
                },
                {
                    "question": "Sneaky drop?",
                    "sql": "SELECT 1 FROM customers; DROP TABLE customers",
                },
                {"question": _GOOD_Q, "sql": _GOOD_SQL},
            ]
        ),
    )
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert sum("rejected" in e for e in row["errors"]) == 2
    assert _aborted(out)
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value") == []


@needs_db
def test_probe_verified_value_survives_reapply(org) -> None:
    """A file that omits verified_value is not clearing it — a probe's stamped
    value must survive re-applies of unchanged files (and count as unchanged)."""
    oid, headers = org
    files = _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}])
    _apply(headers, files, probe=True)
    with org_session(oid) as s:
        first = certify_store.list_for_lens(s, "customer_value")[0]
        assert first.verified_value is not None
    row = _apply(headers, files)
    assert "certified answers: created 0, updated 0, unchanged 1" in row["applied"]
    with org_session(oid) as s:
        again = certify_store.list_for_lens(s, "customer_value")[0]
        assert again.verified_value == first.verified_value


@needs_db
def test_direct_create_endpoint_runs_the_same_gates(org, monkeypatch) -> None:
    """POST /mgmt/lenses/{lens}/certified must not be the ungated door the
    apply gates guard everywhere else: junk 422s, foreign
    tables 422 by name, and a clean create computes bindings (degrading
    embedder-less exactly like from-request)."""
    from services.api import mgmt_certify

    monkeypatch.setattr(mgmt_certify.registry, "resolve_embedder", lambda: None)
    oid, headers = org
    _apply(headers, _files([]))  # publish the lens
    r = client.post(
        "/mgmt/lenses/customer_value/certified",
        headers=headers,
        json={"question": "Junk?", "sql": "NOT SQL AT ALL ;;;"},
    )
    assert r.status_code == 422
    r = client.post(
        "/mgmt/lenses/customer_value/certified",
        headers=headers,
        json={"question": "Stripe?", "sql": "SELECT sum(amount) FROM stripe_payments"},
    )
    assert r.status_code == 422 and "stripe_payments" in r.json()["detail"]
    r = client.post(
        "/mgmt/lenses/customer_value/certified",
        headers=headers,
        json={"question": "Repeat customers?", "sql": _GOOD_SQL},
    )
    assert r.status_code == 201
    assert "warning" in r.json()  # unembedded until reindex — said, not silent
    with org_session(oid) as s:
        row = next(
            a
            for a in certify_store.list_for_lens(s, "customer_value")
            if a.question == "Repeat customers?"
        )
        assert row.bindings  # computed at create, not deferred to the next apply


@needs_db
def test_gate_failures_report_together(org) -> None:
    """SELECT * on a foreign table must name BOTH problems in one rejection —
    a shape error masking the boundary rejection sent a probe user down a
    fix-syntax-then-hit-the-wall path."""
    oid, headers = org
    out = _apply_out(
        headers,
        _files([{"question": "Stars abroad?", "sql": "SELECT * FROM payments_external"}]),
    )
    row = next(e for e in out if e.get("lens") == "customer_value")
    err = next(e for e in row["errors"] if "Stars abroad?" in e)
    assert "payments_external" in err and ("SELECT *" in err or "star" in err.lower())
    assert _aborted(out)


@needs_db
def test_review_certified_answer_survives_a_fileless_apply(org) -> None:
    """Acceptance #4 claimed a rule --certify'd (DB-only) answer vanished on a
    later apply. Under files-win deletion the survival rule
    is PROVENANCE: source review:* (what every review-plane endpoint stamps)
    marks server-origin — an apply whose certified_answers.yaml is [] must
    leave those untouched."""
    oid, headers = org
    _apply(headers, _files([]))  # publish the lens (empty certified file)
    with org_session(oid) as s:
        certify_store.create(
            s,
            "customer_value",
            "Review-born?",
            _GOOD_SQL,
            None,
            created_by="review",
            source="review:req_1",
        )
        s.commit()
    _apply(headers, _files([]))  # fileless (empty list) apply again
    with org_session(oid) as s:
        assert [a.question for a in certify_store.list_for_lens(s, "customer_value")] == [
            "Review-born?"
        ]


@needs_db
def test_deleting_the_file_warns_that_answers_keep_serving(org) -> None:
    """rm certified_answers.yaml is the natural 'remove them all' gesture and
    it is the one gesture files-win does not cover — absence leaves the surface
    unmanaged and the answers keep serving. Absence stays unmanaged BY DESIGN
    (absence-deletes would re-arm data loss through export's omitted empty
    files); the guarantee is loudness:
    the apply row warns with the remedy, plan notes the orphans, and both fire
    even though the lens skips the publish path as `unchanged`."""
    oid, headers = org
    _apply(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}], eval_gate="warn"))
    files = _files([], eval_gate="warn")
    del files["lenses/customer_value/certified_answers.yaml"]

    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert "file ABSENCE never deletes" in str(row.get("note") or "")

    row = _apply(headers, files)
    assert row["action"] == "unchanged"  # the deletion is not a diff — that's the trap
    assert any("file ABSENCE never deletes" in w for w in row.get("warnings") or [])
    assert any("certified_answers.yaml" in w for w in row.get("warnings") or [])
    with org_session(oid) as s:  # …and the answers indeed keep serving
        assert [a.question for a in certify_store.list_for_lens(s, "customer_value")] == [_GOOD_Q]


@needs_db
def test_review_promoted_answers_never_trigger_the_orphan_warning(org) -> None:
    # The carve-out stays carved out: server-origin (review-promoted) answers
    # are the NORMAL absent-from-files state, not an orphan.
    oid, headers = org
    _apply(headers, _files([], eval_gate="warn"))
    with org_session(oid) as s:
        certify_store.create(
            s, "customer_value", "Review-born?", "SELECT 1", None, "review", source="review:rev_1"
        )
        s.commit()
    files = _files([], eval_gate="warn")
    del files["lenses/customer_value/certified_answers.yaml"]
    row = _apply(headers, files)
    assert not any("ABSENCE" in w for w in row.get("warnings") or [])


@needs_db
def test_file_origin_answer_deletes_when_absent_from_the_file(org) -> None:
    """Files win on apply. A file-landed answer whose entry
    is removed from certified_answers.yaml deletes — loudly, in the applied
    count — instead of staying active forever while plan diffs the removal."""
    oid, headers = org
    # eval_gate: warn — under the block default, removing the LAST active answer
    # trips the starvation guard and aborts; that guard has its own tests. This
    # one is about file-origin deletion semantics.
    row = _apply(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}], eval_gate="warn"))
    assert "certified answers: created 1, updated 0, unchanged 0" in row["applied"]

    row = _apply(headers, _files([], eval_gate="warn"))  # the entry is gone; the file remains
    assert "certified answers: created 0, updated 0, deleted 1, unchanged 0" in row["applied"]
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value") == []

    # A tree WITHOUT the file leaves the surface unmanaged: nothing deletes —
    # the otherwise-identical tree now skips the publish path entirely
    # entirely, which satisfies the same guarantee.
    row2 = _apply(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}], eval_gate="warn"))
    files = _files([], eval_gate="warn")
    del files["lenses/customer_value/certified_answers.yaml"]
    row2 = _apply(headers, files)
    assert not any("deleted" in a for a in row2.get("applied") or [])
    with org_session(oid) as s:
        assert [a.question for a in certify_store.list_for_lens(s, "customer_value")] == [_GOOD_Q]


# ── is the apply really one transaction? ─────────────────────────────────────
# The live report was "the failed 502 applies had ALREADY committed their
# certified upserts — upserts land before the gate runs, in an earlier
# transaction". Certified answers DO land before the gate reads them (they are
# gate inputs), but "land" means staged in the request's session, and there is
# exactly one. These two pin that, so the CLI is allowed to state it.


@needs_db
def test_lens_gate_block_rolls_the_certified_upserts_back(org, monkeypatch) -> None:
    """The certified answers pass their own gates and are staged; the LENS
    publish gate then blocks. Nothing of the push may survive."""
    from services.evals import service as eval_service

    oid, headers = org
    staged: list[str] = []

    def blocking_gate(*, session, **_kw):
        # Non-vacuity: the upsert really is in this session when the gate runs.
        staged.extend(a.question for a in certify_store.list_for_lens(session, "customer_value"))
        return eval_service.GateDecision(
            gated=True, blocked=True, regressed=True, score=0.1, prev_score=0.9, failing=["c1"]
        )

    monkeypatch.setattr(eval_service, "publish_gate", blocking_gate)
    out = _apply_out(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}]))
    assert staged == [_GOOD_Q]
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any("accuracy regressed" in e for e in row["errors"])
    assert _aborted(out)
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value") == []
        assert not lens_store.lens_exists(s, "customer_value")


@needs_db
def test_an_unhandled_500_mid_apply_commits_nothing(org, monkeypatch) -> None:
    """The reported 502: a ProviderError raised after the certified upserts were
    staged. The exception propagates through the org_session dependency, which
    rolls back — so 'apply failed' and 'half of it is live' cannot co-occur."""
    from services.project import apply as apply_engine

    oid, headers = org
    staged: list[str] = []

    def boom(session, *_a, **_kw):
        # Non-vacuity: the certified upsert is already in the session here.
        staged.extend(a.question for a in certify_store.list_for_lens(session, "customer_value"))
        raise RuntimeError("provider 404 mid-gate")

    monkeypatch.setattr(apply_engine, "_publish_bundle", boom)
    raw = TestClient(app, raise_server_exceptions=False).post(
        "/mgmt/project/apply",
        headers=headers,
        json={"files": _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}])},
    )
    assert raw.status_code == 500
    assert staged == [_GOOD_Q]
    with org_session(oid) as s:
        assert certify_store.list_for_lens(s, "customer_value") == []
        assert not lens_store.lens_exists(s, "customer_value")


@pytest.fixture
def _crypto_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet

    from services.security import crypto

    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


def _pg_twin(s) -> None:
    """A postgres connection pointed at the test cluster — the different-dialect
    warehouse of the swap scenario."""
    dsn = settings.database_admin_url
    connection_store.create_connection(
        s,
        "pgtwin",
        "postgres",
        {
            "host": "localhost",
            "port": 5432,
            "database": dsn.rsplit("/", 1)[-1],
            "user": dsn.split("//", 1)[1].split(":", 1)[0],
        },
        dsn.split(":", 2)[2].split("@", 1)[0],
    )


def _swap_lens_to(files: dict[str, str], connection: str) -> dict[str, str]:
    """Re-point the lens AND its entities at another connection — the deploy
    contract move (same files, different warehouse)."""
    out = dict(files)
    cfg = yaml.safe_load(out["lenses/customer_value/lens.yaml"])
    cfg["connections"] = [connection]
    out["lenses/customer_value/lens.yaml"] = yaml.safe_dump(cfg, sort_keys=False)
    for path, content in list(out.items()):
        if path.startswith("semantic/entities/"):
            out[path] = content.replace("connection: jaffle", f"connection: {connection}")
    return out


@needs_db
def test_dialect_pin_blocks_a_connection_swap_until_reverified(org, _crypto_key) -> None:
    """Certified SQL is dialect-bound text, and nothing used to pin
    it — a lens re-pointed at a different-dialect
    connection would serve duckdb-verified SQL through a snowflake round-trip
    silently. The pin: a probe stamps the dialect it verified on; an apply
    whose compiled dialect differs refuses, BY NAME, until a re-probe on the
    new connection re-verifies the answer."""
    oid, headers = org
    row = _apply(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}]), probe=True)
    assert any("certified answers: created 1" in a for a in row["applied"])
    with org_session(oid) as s:
        (stored,) = certify_store.list_for_lens(s, "customer_value")
        assert stored.verified_dialect == "duckdb"  # the probe stamped its warehouse
        _pg_twin(s)
        s.commit()

    swapped = _swap_lens_to(_files([{"question": _GOOD_Q, "sql": _GOOD_SQL}]), "pgtwin")
    out = _apply_out(headers, swapped)
    row = next(e for e in out if e.get("lens") == "customer_value")
    err = next(e for e in row["errors"] if _GOOD_Q in e)
    assert "duckdb" in err and "postgres" in err  # names both dialects
    assert "--probe-certified" in err  # and the path that clears it
    assert _aborted(out)
    with org_session(oid) as s:
        (stored,) = certify_store.list_for_lens(s, "customer_value")
        assert stored.verified_dialect == "duckdb"  # nothing landed, pin intact


@needs_db
def test_dialect_pin_clears_when_the_reprobe_passes_on_the_new_warehouse(org, _crypto_key) -> None:
    oid, headers = org
    _apply(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}]), probe=True)
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:  # the pg twin of the jaffle table, so the re-probe can pass
        c.execute(text("DROP TABLE IF EXISTS customers"))
        c.execute(text("CREATE TABLE customers (number_of_orders int)"))
        c.execute(text("INSERT INTO customers VALUES (1), (2), (3)"))
    try:
        with org_session(oid) as s:
            _pg_twin(s)
            s.commit()
        swapped = _swap_lens_to(_files([{"question": _GOOD_Q, "sql": _GOOD_SQL}]), "pgtwin")
        out = _apply_out(headers, swapped, probe=True)
        row = next(e for e in out if e.get("lens") == "customer_value")
        assert row.get("errors", []) == []
        with org_session(oid) as s:
            (stored,) = certify_store.list_for_lens(s, "customer_value")
            assert stored.verified_dialect == "postgres"  # re-verified on the new warehouse
            assert stored.verified_value == {"value": 2}  # and re-executed there
    finally:
        with admin.begin() as c:
            c.execute(text("DROP TABLE IF EXISTS customers"))
        admin.dispose()


@needs_db
def test_probe_with_zero_new_entries_says_so(org) -> None:
    """--probe-certified probes NEW entries only — when an earlier apply
    already landed them, the flag must say so instead of silently probing
    nothing."""
    _oid, headers = org
    _apply(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}]))
    row = _apply(headers, _files([{"question": _GOOD_Q, "sql": _GOOD_SQL}]), probe=True)
    note = next(w for w in row["warnings"] if "--probe-certified" in w)
    assert "probed 0 new certified answers — 1 pre-existing" in note
    assert "dst test" in note
