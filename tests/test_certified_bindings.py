"""Derived bindings + certified-answer staleness.

certified_bindings is pure sqlglot table extraction against the lens's compiled
model, reading hashes from the model's own shared_provenance (certified and lens
staleness can never disagree). Bindings recompute on apply create/update; plan
grows a per-lens re-verify line when a stored binding hash no longer matches.
Flagging is the whole product — nothing auto-disables or recompiles an answer.
"""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify import store as certify_store
from services.certify.bindings import certified_bindings, source_tables
from services.config import settings
from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    SemanticModel,
    SharedProvenance,
)
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
from services.lenses.repo import render_lens_repo
from services.project.plan import stale_certified
from services.semantic import store as semantic_store
from services.semantic.files import render_semantic_files

client = TestClient(app)


def _model(provenance: dict[str, str] | None = None) -> SemanticModel:
    assets = (
        provenance
        if provenance is not None
        else {"entity/orders": "h_orders", "entity/customers": "h_customers"}
    )
    return SemanticModel(
        lens="board",
        dialect="duckdb",
        entities=[
            Entity(
                name="orders",
                source=EntitySource(connection="wh", table="orders"),
                fields=[Field(name="amount", type="number")],
            ),
            Entity(
                name="customers",
                source=EntitySource(connection="wh", table="jaffle.customers"),
                fields=[Field(name="customer_id", type="integer")],
            ),
        ],
        shared_provenance=SharedProvenance(compiled_at="2026-07-27T00:00:00Z", assets=assets),
    )


# ── extraction matrix (pure) ─────────────────────────────────────────────────


def test_plain_select_binds_its_table() -> None:
    assert certified_bindings("SELECT count(*) FROM orders", _model()) == {
        "entity/orders": "h_orders"
    }


def test_join_with_aliases_binds_both_tables() -> None:
    sql = (
        "SELECT o.amount FROM orders AS o "
        "JOIN jaffle.customers AS c ON o.customer_id = c.customer_id"
    )
    assert certified_bindings(sql, _model()) == {
        "entity/orders": "h_orders",
        "entity/customers": "h_customers",
    }


def test_cte_names_are_not_source_tables() -> None:
    sql = "WITH recent AS (SELECT amount FROM orders) SELECT count(*) FROM recent"
    assert source_tables(sql, "duckdb") == {"orders"}
    assert certified_bindings(sql, _model()) == {"entity/orders": "h_orders"}


def test_cte_shadowing_a_model_table_does_not_bind_it() -> None:
    """A CTE named like a real table hides it: under-binding (a flag that doesn't
    fire) is the accepted failure mode, never over-trusting."""
    sql = "WITH customers AS (SELECT amount FROM orders) SELECT count(*) FROM customers"
    assert certified_bindings(sql, _model()) == {"entity/orders": "h_orders"}


def test_quoted_identifiers_bind() -> None:
    assert certified_bindings('SELECT count(*) FROM "orders"', _model()) == {
        "entity/orders": "h_orders"
    }


def test_bare_name_matches_qualified_model_table() -> None:
    # the model's table is jaffle.customers; BI SQL often writes the bare name
    assert certified_bindings("SELECT count(*) FROM customers", _model()) == {
        "entity/customers": "h_customers"
    }


def test_table_outside_the_model_binds_nothing() -> None:
    assert certified_bindings("SELECT 1 FROM stripe_payments", _model()) == {}


def test_definition_binds_by_sql_expr_containment() -> None:
    """An answer that IMPLEMENTS a governed definition binds it — the
    containment is canonical (qualifier-stripped), so a bare-column answer still
    matches the definition's qualified expr. An answer that merely reads the
    same table does not bind the definition."""
    from services.contracts.semantic_model import Definition

    model = _model({"entity/orders": "h_orders", "definition/big_order": "h_def"})
    model.definitions = [
        Definition(term="big_order", body="An order over 100.", sql_expr="orders.amount > 100")
    ]
    assert certified_bindings("SELECT count(*) FROM orders WHERE amount > 100", model) == {
        "entity/orders": "h_orders",
        "definition/big_order": "h_def",
    }
    assert certified_bindings("SELECT count(*) FROM orders", model) == {"entity/orders": "h_orders"}


def test_restamp_keeps_membership_when_definition_moved_away() -> None:
    """The severance bug: a definition whose expr no longer
    canonically embeds in the certified SQL must NOT drop from a green-test
    re-stamp — membership recomputes only on a SQL edit. The key keeps its
    place and takes the asset's CURRENT hash (so it reads freshly verified,
    and the next definition change flags it again)."""
    from services.certify.bindings import restamp_bindings
    from services.contracts.semantic_model import Definition

    model = _model({"entity/orders": "h_orders", "definition/big_order": "h_def_v2"})
    model.definitions = [
        Definition(term="big_order", body="An order over 900.", sql_expr="orders.amount > 900")
    ]
    sql = "SELECT count(*) FROM orders WHERE amount > 100"  # implements the OLD expr
    stored = {"entity/orders": "h_orders_old", "definition/big_order": "h_def_v1"}
    assert certified_bindings(sql, model) == {"entity/orders": "h_orders"}  # would sever
    assert restamp_bindings(sql, stored, model) == {
        "entity/orders": "h_orders",
        "definition/big_order": "h_def_v2",
    }


def test_restamp_keeps_stored_hash_for_a_deleted_asset() -> None:
    """An asset that left the provenance keeps its stored hash — the answer
    reads permanently stale until a human re-certifies or retires."""
    from services.certify.bindings import restamp_bindings

    model = _model({"entity/orders": "h_orders"})
    stored = {"entity/orders": "h_orders", "definition/big_order": "h_def_v1"}
    assert restamp_bindings("SELECT count(*) FROM orders", stored, model) == stored


def test_restamp_heals_an_under_captured_first_stamp() -> None:
    """Membership may GROW through evidence: a first stamp that intermittently
    missed the definition picks it up on the next green re-stamp — added
    sensitivity is always safe."""
    from services.certify.bindings import restamp_bindings
    from services.contracts.semantic_model import Definition

    model = _model({"entity/orders": "h_orders", "definition/big_order": "h_def"})
    model.definitions = [
        Definition(term="big_order", body="An order over 100.", sql_expr="orders.amount > 100")
    ]
    sql = "SELECT count(*) FROM orders WHERE amount > 100"
    assert restamp_bindings(sql, {"entity/orders": "h_stale"}, model) == {
        "entity/orders": "h_orders",
        "definition/big_order": "h_def",
    }


def test_restamp_backfills_empty_bindings_via_full_compute() -> None:
    from services.certify.bindings import restamp_bindings

    sql = "SELECT count(*) FROM orders"
    assert restamp_bindings(sql, None, _model()) == {"entity/orders": "h_orders"}
    assert restamp_bindings(sql, {}, _model()) == {"entity/orders": "h_orders"}


def test_no_provenance_or_unparseable_sql_binds_nothing() -> None:
    model = _model()
    model.shared_provenance = None
    assert certified_bindings("SELECT count(*) FROM orders", model) == {}
    assert certified_bindings("SELEC nope FROM FROM", _model()) == {}


def test_stale_certified_flags_only_changed_bindings() -> None:
    answers = [
        ("q stale", {"entity/orders": "OLD"}),
        ("q fresh", {"entity/orders": "h1", "entity/customers": "h2"}),
        ("q unbound", None),
    ]
    effective = {"entity/orders": "h1", "entity/customers": "h2"}
    assert stale_certified(answers, effective) == (["q stale"], ["entity/orders"])
    assert stale_certified(answers[1:], effective) == ([], [])


# ── apply + plan against the scratch DB ──────────────────────────────────────


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
    # The certify self-test is UNCONDITIONAL now — pin the smart tier to
    # unresolvable so applies degrade to the loud skip instead of dialing an
    # ambient provider key.
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))  # claim cleanly with HashEmbedder
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('CertBindT') RETURNING id")
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


_QUESTION = "How many repeat customers are there?"
_SQL = "SELECT count(*) FROM customers WHERE number_of_orders > 1"


def _project_files(sql: str = _SQL) -> dict[str, str]:
    entities, definitions = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions))
    for path, content in render_lens_repo(jaffle_customer_value_bundle()).items():
        files[f"lenses/customer_value/{path}"] = content
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        [{"question": _QUESTION, "sql": sql}], sort_keys=False, allow_unicode=True
    )
    return files


def _edit_customers(files: dict[str, str]) -> dict[str, str]:
    out = dict(files)
    out["semantic/entities/customers.yaml"] = out["semantic/entities/customers.yaml"].replace(
        "One row per customer.", "One row per customer, always."
    )
    return out


@needs_db
def test_bindings_computed_on_apply_and_staleness_lifecycle(org, monkeypatch) -> None:
    """The acceptance loop: apply stores bindings → a shared edit flags re-verify
    → the recompile does NOT clear the flag → a human sql edit does."""
    from services.contracts.fakes import HashEmbedder
    from services.project import apply as apply_engine

    oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        # The SQL embeds repeat_customer's sql_expr, so the answer also
        # binds the definition it implements — a definition change re-tests it.
        assert stored.bindings == {
            "entity/customers": semantic_store.asset_hashes(s)["entity/customers"],
            "definition/repeat_customer": semantic_store.asset_hashes(s)[
                "definition/repeat_customer"
            ],
        }

    # Unedited plan: no re-verify line anywhere.
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    assert not any(e.get("certified") for e in plan)

    # A shared edit the answer's SQL touches → the lens row carries the line + questions.
    edited = _edit_customers(files)
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": edited}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["certified"]["note"] == (
        "certified: 1 answers touch changed assets (entity/customers) — re-verify "
        "by updating each answer's verified_by (or sql) in certified_answers.yaml"
    )
    assert row["certified"]["questions"] == [_QUESTION]

    # Apply the shared edit: the lens recompiles, but the untouched answer keeps
    # its as-verified hashes — the flag survives the recompile.
    client.post("/mgmt/project/apply", headers=headers, json={"files": edited})
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": edited}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert "stale" not in row  # the lens itself recompiled fresh
    assert row["certified"]["questions"] == [_QUESTION]

    # A push that never mentions the lens still surfaces it: standalone row.
    semantic_only = {p: c for p, c in edited.items() if p.startswith("semantic/")}
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": semantic_only}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] == "certified-stale"
    assert row["certified"]["questions"] == [_QUESTION]

    # The human act: edit the answer's SQL → bindings recompute → flag clears.
    reverified = _edit_customers(_project_files(_SQL + " AND number_of_orders < 100"))
    client.post("/mgmt/project/apply", headers=headers, json={"files": reverified})
    with org_session(oid) as s:
        stored = certify_store.list_for_lens(s, "customer_value")[0]
        assert stored.bindings == {
            "entity/customers": semantic_store.asset_hashes(s)["entity/customers"],
            "definition/repeat_customer": semantic_store.asset_hashes(s)[
                "definition/repeat_customer"
            ],
        }
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": reverified}).json()
    assert not any(e.get("certified") for e in plan)


@needs_db
def test_stale_note_names_the_export_step_for_a_review_promoted_answer(org, monkeypatch) -> None:
    """Lap-1 friction 2: the note said 'update certified_answers.yaml' for an
    answer that file has never contained — a review-promoted one. The note must
    name `dst export` exactly then, and stay quiet for file-origin answers
    (the lifecycle test above pins the clause-free wording verbatim)."""
    from services.contracts.fakes import HashEmbedder
    from services.project import apply as apply_engine

    oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    files = _project_files()
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump([])
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    with org_session(oid) as s:
        certify_store.create(
            s,
            "customer_value",
            _QUESTION,
            _SQL,
            None,
            "review",
            source="review:req_e2e",
            bindings={"entity/customers": semantic_store.asset_hashes(s)["entity/customers"]},
        )
        s.commit()

    plan = client.post(
        "/mgmt/project/plan", headers=headers, json={"files": _edit_customers(files)}
    ).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["certified"]["note"].endswith(
        "(review-promoted answers are not in the file yet — "
        "`dst export --lens customer_value` renders them there first)"
    )


def _definition_path(files: dict[str, str]) -> str:
    return next(p for p in files if p.startswith("semantic/definitions/") and "repeat" in p)


def _migration(name: str):
    """Load a migration module by path — the restamp below is tested by RUNNING
    it, not by re-describing it in the test."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stamp_legacy_digest(session, key: str, digest: str) -> None:
    """Put the DB back into its PRE-UPGRADE state for one asset: the asset's own
    record, the bundle compiled from it, and the answers verified against it all
    carry the digest the old code computed."""
    session.execute(
        text("UPDATE semantic_asset SET content_hash = :h WHERE kind = :k AND name = :n"),
        {"h": digest, "k": key.split("/")[0], "n": key.split("/")[1]},
    )
    for col in ("draft_json", "published_json"):
        for row_id, bundle in session.execute(
            text(f"SELECT id, {col} FROM lens WHERE {col} IS NOT NULL")
        ).all():
            bundle["semantic_model"]["shared_provenance"]["assets"][key] = digest
            session.execute(
                text(f"UPDATE lens SET {col} = CAST(:b AS jsonb) WHERE id = :i"),
                {"b": json.dumps(bundle), "i": row_id},
            )
    for answer_id, bindings in session.execute(
        text("SELECT id, bindings FROM certified_answer WHERE bindings ? :k"), {"k": key}
    ).all():
        bindings[key] = digest
        session.execute(
            text("UPDATE certified_answer SET bindings = CAST(:b AS jsonb) WHERE id = :i"),
            {"b": json.dumps(bindings), "i": answer_id},
        )


@needs_db
def test_a_schema_addition_never_demands_re_verification(org, monkeypatch) -> None:
    """The upgrade regression (HANDS-ON-FINDINGS #3). Definition gained
    summary/grain/sources, every definition's digest moved, and the first plan
    after the upgrade told every project with certified answers to re-verify
    them — two lines under calling the very same definition files `unchanged`.
    Nothing had changed but dst's own hash function, and the only way to
    silence the demand was to falsely re-attest.

    Reproduced the way an upgrade produces it: the recorded digests are the ones
    the OLD code computed (its ``model_dump`` had no summary/grain/sources), the
    files are untouched, and migration 0039 is the upgrade. Both directions are
    pinned — the second half is the one that must never be traded for the first.
    """
    from services.contracts.fakes import HashEmbedder
    from services.contracts.shared_semantic import asset_hash
    from services.project import apply as apply_engine

    oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})

    with org_session(oid) as s:
        body = semantic_store.get_asset(s, "definition", "repeat_customer").body
        legacy_body = {k: v for k, v in body.items() if k not in ("summary", "grain", "sources")}
        assert set(body) - set(legacy_body) == {"summary", "grain", "sources"}  # the schema grew
        # What the pre-bump code recorded: sha over its own model_dump, which had
        # exactly these keys.
        _stamp_legacy_digest(s, "definition/repeat_customer", asset_hash(legacy_body))
        s.commit()

    # The disease: nothing in the project changed, and the plan says otherwise.
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["stale"] == ["definition/repeat_customer"]
    assert row["certified"]["questions"] == [_QUESTION]

    # The upgrade. A recorded digest that was in sync with the asset's own is
    # carried across the re-base; the meaning it was verified against is intact.
    with org_session(oid) as s:
        _migration("0039_asset_hash_authored_body").restamp_all(s.connection())
        s.commit()

    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] == "unchanged"
    assert "stale" not in row  # the definition did not change …
    assert "certified" not in row  # … so no answer verified against it is stale

    # The other direction, which matters more: authoring one of those keys IS a
    # meaning change (they render into the generation prompt), so it must still
    # invalidate every answer bound to the term.
    path = _definition_path(files)
    changed = dict(files)
    changed[path] = files[path].replace("---\n", "---\nsummary: two or more paid orders\n", 1)
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": changed}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["stale"] == ["definition/repeat_customer"]
    assert row["certified"]["questions"] == [_QUESTION]


@needs_db
def test_the_restamp_leaves_a_genuinely_drifted_answer_flagged(org, monkeypatch) -> None:
    """The upgrade must not launder a real re-verify demand into silence. An
    answer whose bindings had ALREADY diverged from the asset it was verified
    against was stale before the upgrade and stays stale after it — the restamp
    moves only digests that still matched the asset's own record."""
    from services.contracts.fakes import HashEmbedder
    from services.project import apply as apply_engine

    oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})

    with org_session(oid) as s:
        s.execute(
            text(
                "UPDATE certified_answer SET bindings = "
                "jsonb_set(bindings, '{definition/repeat_customer}', '\"VERIFIED-AGAINST-OLDER\"')"
            )
        )
        _migration("0039_asset_hash_authored_body").restamp_all(s.connection())
        s.commit()

    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["certified"]["questions"] == [_QUESTION]


@needs_db
def test_provenance_edit_is_the_reverify_act(org, monkeypatch) -> None:
    """Acceptance #3 item 6a: the documented way to clear a re-verify flag
    WITHOUT touching sql — explicitly updating verified_by (or source) counts
    as the re-verify act (_provenance_edited), bindings recompute against the
    current model, and the plan line clears. The note itself names the act."""
    from services.contracts.fakes import HashEmbedder
    from services.project import apply as apply_engine

    _oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    edited = _edit_customers(files)
    client.post("/mgmt/project/apply", headers=headers, json={"files": edited})
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": edited}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["certified"]["questions"] == [_QUESTION]
    assert "verified_by" in row["certified"]["note"]  # the note says how to clear it

    reverified = dict(edited)
    reverified["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        [{"question": _QUESTION, "sql": _SQL, "verified_by": "alex re-checked 2026-07"}],
        sort_keys=False,
        allow_unicode=True,
    )
    client.post("/mgmt/project/apply", headers=headers, json={"files": reverified})
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": reverified}).json()
    assert not any(e.get("certified") for e in plan)


@needs_db
def test_certified_stale_never_leaks_across_lenses(org, monkeypatch) -> None:
    """Acceptance #3 item 6b: a customers edit flags ONLY lenses whose answers
    bind customers. A sibling lens whose one answer reads orders alone must
    stay clean on the same plan — the computation is per-lens over per-answer
    bindings; the probe's 'phantom' on another lens can only be that lens's own
    answers genuinely binding the changed shared asset."""
    from services.contracts.fakes import HashEmbedder
    from services.lenses.demo import jaffle_customer_value_config
    from services.project import apply as apply_engine

    _oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    files = _project_files()
    orders_config = jaffle_customer_value_config().model_copy(
        update={"name": "orders_ops", "display_name": "Orders Ops", "description": ""}
    )
    orders_config.select.entities = [e for e in orders_config.select.entities if e.name == "orders"]
    orders_config.select.definitions = []
    files["lenses/orders_ops/lens.yaml"] = yaml.safe_dump(
        orders_config.model_dump(mode="json", exclude_none=True), sort_keys=False
    )
    files["lenses/orders_ops/certified_answers.yaml"] = yaml.safe_dump(
        [{"question": "How many orders are there?", "sql": "SELECT count(*) FROM orders"}],
        sort_keys=False,
        allow_unicode=True,
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert not any(e.get("errors") for e in out)

    edited = _edit_customers(files)
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": edited}).json()
    row_cv = next(e for e in plan if e.get("lens") == "customer_value")
    assert row_cv["certified"]["questions"] == [_QUESTION]
    row_orders = next(e for e in plan if e.get("lens") == "orders_ops")
    assert "certified" not in row_orders  # no cross-lens leak

    # ...and the orders lens DOES flag when ITS bound entity changes.
    orders_edited = dict(files)
    orders_edited["semantic/entities/orders.yaml"] = orders_edited[
        "semantic/entities/orders.yaml"
    ].replace("One row per order.", "One row per order, always.")
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": orders_edited}).json()
    row_orders = next(e for e in plan if e.get("lens") == "orders_ops")
    assert row_orders["certified"]["questions"] == ["How many orders are there?"]


def test_qualified_reference_defeats_cte_shadowing() -> None:
    """The review's smuggle repro: a CTE shadows the bare name while the SQL
    reads the physical table schema-qualified — a CTE can never be qualified,
    so the qualified reference must count for the boundary."""
    from services.certify.bindings import foreign_tables

    sql = (
        "WITH raw_payments AS (SELECT 1 AS amount) SELECT sum(p.amount) FROM main.raw_payments AS p"
    )
    assert "main.raw_payments" in source_tables(sql, "duckdb")
    assert foreign_tables(sql, _model()) == ["main.raw_payments"]
    # an unqualified reference to the shadowing CTE stays a CTE, not a table
    bare = "WITH raw_payments AS (SELECT 1 AS amount) SELECT sum(amount) FROM raw_payments"
    assert source_tables(bare, "duckdb") == set()


def test_differing_qualifications_never_match() -> None:
    """hr.customers is not jaffle.customers — a qualified reference passes only
    on its own qualification (bare-to-qualified matching stays)."""
    from services.certify.bindings import foreign_tables

    assert foreign_tables("SELECT customer_id FROM hr.customers", _model()) == ["hr.customers"]
    assert foreign_tables("SELECT customer_id FROM jaffle.customers", _model()) == []
    assert foreign_tables("SELECT customer_id FROM customers", _model()) == []
