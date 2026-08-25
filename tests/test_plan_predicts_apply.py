"""`dst plan` must reject exactly what `dst apply` rejects.

The promise is that plan validates what apply validates, and exits 1. It used to
hold for the semantic-file parse seam and nowhere else: a cross-entity metric and
an unservable `model:` both planned **exit 0** and then aborted the apply. A dry
run that passes what the real run rejects is worse than no dry run, because
people stop reading it.

So the table below is the contract, one row per REJECTION CLASS apply can make
without dialling anything: each tree must come back ``invalid`` from plan AND
``aborted`` from apply, with the same sentence. When a new gate is added to
apply, it belongs in ``check_lens`` and gets a row here.

The gates a dry run genuinely CANNOT run — warehouse probes, an executed eval
oracle, the scored publish gate — are pinned separately: plan has to NAME them,
because a silent clean plan reads as a clean bill of health.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import ProviderConfig, settings
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses.demo import jaffle_customer_value_bundle, jaffle_shared_assets
from services.lenses.repo import render_lens_repo
from services.project.apply import PLAN_UNCHECKED
from services.semantic.files import render_semantic_files

client = TestClient(app)

# Providers CONFIGURED is what makes an unservable lens an ERROR rather than the
# wholly-unconfigured install's warning.
_DEEPSEEK_ONLY = {
    "deepseek": ProviderConfig(
        type="openai-compatible",
        api_key="sk-d",
        base_url="https://api.deepseek.com",
        smart_model="deepseek-v4-pro",
    )
}


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
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    # An install that dials NOTHING is this file's premise — pin the embedder the
    # way `resolve` above is pinned, never inherit it from the environment.
    # ``DST_PROVIDERS=""`` in conftest does NOT imply "no
    # embedder": with the `local-embed` extra in the venv, `resolve_embedder`
    # falls back to the implicit in-process tier (registry.py:384), whose dim 384
    # then trips the vector(1024) column guard (embedding_meta.py:64) — so
    # `_apply_certified_answers` SKIPS the answer (apply.py:1451) and the
    # certified corpus this file's starvation test retires from is never
    # populated.
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('PlanPred') RETURNING id")
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


def _project_files() -> dict[str, str]:
    entities, definitions = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions))
    for path, content in render_lens_repo(jaffle_customer_value_bundle()).items():
        files[f"lenses/customer_value/{path}"] = content
    return files


_LENS = "lenses/customer_value/lens.yaml"


def _with_metric(files: dict[str, str], expr: str) -> dict[str, str]:
    """Append a metric to the shared `orders` entity — the repro."""
    entity = yaml.safe_load(files["semantic/entities/orders.yaml"])
    entity["metrics"].append({"name": "reaching", "agg": "sum", "expr": expr, "type": "simple"})
    files["semantic/entities/orders.yaml"] = yaml.safe_dump(entity, sort_keys=False)
    return files


def _cross_entity(files: dict[str, str]) -> dict[str, str]:
    # orders -> customers is declared many_to_one, so one customer has MANY
    # orders: joining customers onto orders is safe, the other direction is not.
    # This metric lives on `orders`, reaching customers — safe — so break the
    # declaration instead: an undeclared relationship is read as unsafe.
    entity = yaml.safe_load(files["semantic/entities/orders.yaml"])
    for join in entity["joins"]:
        join.pop("relationship", None)
    files["semantic/entities/orders.yaml"] = yaml.safe_dump(entity, sort_keys=False)
    return _with_metric(files, "orders.amount * customers.customer_lifetime_value")


def _unservable_model(files: dict[str, str]) -> dict[str, str]:
    config = yaml.safe_load(files[_LENS])
    config["model"] = {"provider": "nosuchprovider", "model": "nosuch-model-v9"}
    files[_LENS] = yaml.safe_dump(config, sort_keys=False)
    return files


def _bad_certified_sql(files: dict[str, str]) -> dict[str, str]:
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        [{"question": "everything?", "sql": "SELECT * FROM customers"}]
    )
    return files


def _off_boundary_certified(files: dict[str, str]) -> dict[str, str]:
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        [{"question": "how many?", "sql": "SELECT COUNT(*) AS n FROM secret_payroll"}]
    )
    return files


def _bad_certified_status(files: dict[str, str]) -> dict[str, str]:
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        [{"question": "how many?", "sql": "SELECT COUNT(*) AS n FROM customers", "status": "on"}]
    )
    return files


def _bad_eval_case(files: dict[str, str]) -> dict[str, str]:
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [{"question": "how many?", "expect": "clarify", "expected_sql": "SELECT 1"}]
    )
    return files


def _unparseable_oracle(files: dict[str, str]) -> dict[str, str]:
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [{"question": "how many?", "expected_sql": "SELEKT COUNT(*) FROMM customers"}]
    )
    return files


def _wrong_lens_name(files: dict[str, str]) -> dict[str, str]:
    config = yaml.safe_load(files[_LENS])
    config["name"] = "somebody_else"
    files[_LENS] = yaml.safe_dump(config, sort_keys=False)
    return files


def _unknown_entity_selected(files: dict[str, str]) -> dict[str, str]:
    config = yaml.safe_load(files[_LENS])
    config["select"]["entities"].append({"name": "ghosts"})
    files[_LENS] = yaml.safe_dump(config, sort_keys=False)
    return files


def _sample_query_out_of_scope(files: dict[str, str]) -> dict[str, str]:
    files["lenses/customer_value/queries.yaml"] = yaml.safe_dump(
        {"sample_queries": [{"question": "payroll?", "sql": "SELECT x FROM secret_payroll"}]}
    )
    return files


# (mutator, a phrase the ONE message must carry) — the sentence plan prints has
# to be the sentence apply prints, or the dry run is teaching a different fix.
GATE_CLASSES = [
    pytest.param(_cross_entity, "customers.customer_lifetime_value", id="cross_entity_metric"),
    pytest.param(_bad_certified_sql, "certified answer", id="certified_sql_shape"),
    pytest.param(_off_boundary_certified, "secret_payroll", id="certified_off_boundary"),
    pytest.param(_bad_certified_status, "'active' or 'retired'", id="certified_bad_status"),
    pytest.param(_bad_eval_case, "mutually exclusive", id="eval_case_two_shapes"),
    pytest.param(_unparseable_oracle, "does not parse", id="eval_oracle_unparseable"),
    pytest.param(_wrong_lens_name, "but the tree is", id="lens_name_mismatch"),
    pytest.param(_unknown_entity_selected, "ghosts", id="unknown_entity"),
    pytest.param(_sample_query_out_of_scope, "scope check", id="sample_query_out_of_scope"),
]


def _assert_plan_and_apply_agree(headers, files: dict[str, str], phrase: str) -> None:
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] == "invalid", f"plan passed a tree apply rejects: {row}"
    assert any(phrase in e for e in row["errors"]), row["errors"]

    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] == "aborted", out
    applied_errors = [e for r in out for e in r.get("errors", [])]
    assert any(phrase in e for e in applied_errors), applied_errors


@needs_db
@pytest.mark.parametrize("mutate,phrase", GATE_CLASSES)
def test_every_tree_apply_rejects_is_a_tree_plan_rejects(org, mutate, phrase) -> None:
    """The contract, one gate class at a time. Plan says invalid with the
    message; apply then aborts with the same one. Neither may be quiet."""
    _oid, headers = org
    _assert_plan_and_apply_agree(headers, mutate(_project_files()), phrase)


@needs_db
def test_plan_predicts_the_unservable_model_gate(org, monkeypatch) -> None:
    """The second asymmetry, and its own test because it needs an
    install with providers CONFIGURED — that is what makes an unservable lens an
    error rather than the wholly-unconfigured install's warning."""
    monkeypatch.setattr(settings, "providers", _DEEPSEEK_ONLY)
    _oid, headers = org
    _assert_plan_and_apply_agree(headers, _unservable_model(_project_files()), "nosuch-model-v9")


@needs_db
def test_the_valid_tree_still_plans_and_applies_clean(org) -> None:
    """The other half of the contract: nothing above may be a false alarm."""
    _oid, headers = org
    files = _project_files()
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    assert not [e for e in plan if e.get("status") == "invalid"], plan
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] != "aborted", out


@needs_db
def test_plan_of_a_brand_new_project_reads_its_own_dst_yaml(org) -> None:
    """Apply lands connections BEFORE any lens compiles. A dry run that looked
    only at the DB would reject a brand-new project's very first plan for naming
    a connection that does not exist yet — a false alarm is the same disease."""
    _oid, headers = org
    files = _project_files()
    files["dst.yaml"] = yaml.safe_dump(
        {"connections": {"fresh": {"type": "duckdb", "config": {"path": ":memory:"}}}}
    )
    config = yaml.safe_load(files[_LENS])
    config["connections"] = ["fresh"]
    for path in ("semantic/entities/orders.yaml", "semantic/entities/customers.yaml"):
        entity = yaml.safe_load(files[path])
        entity["source"]["connection"] = "fresh"
        files[path] = yaml.safe_dump(entity, sort_keys=False)
    files[_LENS] = yaml.safe_dump(config, sort_keys=False)
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] != "invalid", row


@needs_db
def test_an_unparseable_dst_yaml_reports_one_error_not_a_cascade(org) -> None:
    """Apply stops on the dst.yaml rejection BEFORE a lens compiles, so it
    reports one error. Plan must too — running the lens gates without the
    connections that file was going to declare would bury the root cause under a
    'names no applied warehouse connection' per lens."""
    _oid, headers = org
    files = _project_files()
    files["dst.yaml"] = "connections: [this is not a mapping\n"
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    assert [e for e in plan if e.get("scope") == "project" and e.get("status") == "invalid"]
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] != "invalid", row

    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] == "aborted"
    assert len([e for r in out for e in r.get("errors", [])]) == 1, out


@needs_db
def test_plan_names_the_gates_it_could_not_run(org) -> None:
    """A clean plan is not a clean bill of health. Three gates need the warehouse
    on the wire or a scored eval run; plan has to say so rather than exit 0 into
    an apply that probes a dead credential."""
    _oid, headers = org
    plan = client.post(
        "/mgmt/project/plan", headers=headers, json={"files": _project_files()}
    ).json()
    row = next(e for e in plan if e.get("scope") == "unchecked")
    assert row["checks"] == list(PLAN_UNCHECKED)
    assert any("connection probes" in c for c in row["checks"])
    assert any("publish eval gate" in c for c in row["checks"])


@needs_db
def test_plan_tries_the_recompile_it_announces(org) -> None:
    """`recompile_stale` runs over lenses the push never mentions, and ONE failure
    there aborts the whole apply. Plan used to print 'will recompile on apply' and
    exit 0 — so a shared-entity edit was rejected through a lens the author had
    not opened, with no warning."""
    _oid, headers = org
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})

    # A semantic-only push: no lenses/ files at all, so customer_value's row can
    # only come from the recompile pass.
    broken = _cross_entity(_project_files())
    push = {p: c for p, c in broken.items() if p.startswith("semantic/")}
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": push}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] == "invalid", row
    assert any("customer_lifetime_value" in e for e in row["errors"]), row

    out = client.post("/mgmt/project/apply", headers=headers, json={"files": push}).json()
    assert out[-1]["action"] == "aborted", out
    assert any(r.get("action") == "rejected-recompile" for r in out), out


@needs_db
def test_plan_predicts_the_starved_eval_gate(org) -> None:
    """`eval_gate: block` + a push that retires the last active certified answer
    aborts the apply. Plan measures it the only way a dry run can — by replaying
    the upsert's own status rules — and must reach the same verdict."""
    _oid, headers = org
    files = _project_files()
    config = yaml.safe_load(files[_LENS])
    config["eval_gate"] = "block"
    files[_LENS] = yaml.safe_dump(config, sort_keys=False)
    answer = {"question": "how many?", "sql": "SELECT COUNT(*) AS n FROM customers"}
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump([answer])
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})

    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        [{**answer, "status": "retired"}]
    )
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] == "invalid", row
    assert any("eval gate starved" in e for e in row["errors"]), row

    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] == "aborted", out
