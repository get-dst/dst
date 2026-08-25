"""The project pipeline compiles lenses from the shared layer.

Pure halves (render ↔ load, plan engines) run DB-free; the apply/export/plan
endpoints run against the scratch Postgres (skipped when unreachable), driving
the exact flow `dst export/plan/apply` uses: push semantic/** + lenses/**,
recompile stale published lenses — including ones a push never mentions.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.certify.store import CertifiedAnswer
from services.config import settings
from services.contracts.semantic_model import Definition
from services.db.session import org_session
from services.lenses import connection_store
from services.lenses import store as lens_store
from services.lenses.demo import (
    jaffle_customer_value_bundle,
    jaffle_customer_value_config,
    jaffle_shared_assets,
)
from services.lenses.repo import render_lens_repo
from services.llm.registry import ResolvedModel
from services.project.loader import is_managed, load_lens_source, split_by_lens, split_semantic
from services.project.plan import plan_lenses, plan_semantic, stale_lenses
from services.semantic import store as semantic_store
from services.semantic.files import render_semantic_files

client = TestClient(app)

_ANSWER = CertifiedAnswer(
    id="ca_1",
    lens="customer_value",
    question="How many repeat customers are there?",
    sql="SELECT COUNT(*) FROM customers WHERE number_of_orders > 1",
    created_by="alex",
    created_at="2026-07-01T00:00:00Z",
    verified_value={"value": 42},
)


def _render() -> dict[str, str]:
    return render_lens_repo(jaffle_customer_value_bundle(), certified_answers=[_ANSWER])


# ── render ↔ load (pure) ─────────────────────────────────────────────────────


def test_lens_tree_shape() -> None:
    files = _render()
    assert files["lens.yaml"].startswith("name: customer_value\n")
    assert "select:" in files["lens.yaml"]  # config carries the shared-layer selection
    assert "semantic_model.yaml" not in files
    assert "How many orders were placed in total?" in files["queries.yaml"]
    assert "How many repeat customers" in files["certified_answers.yaml"]
    assert "embedding" not in files["certified_answers.yaml"]
    # the demo's definitions are shared — no local pages, but compiled.yaml shows them
    assert not any(p.startswith("definitions/") for p in files)
    compiled = yaml.safe_load(files["compiled.yaml"])
    assert {d["term"] for d in compiled["definitions"]} == {
        "lifetime_value",
        "repeat_customer",
        "value",
    }
    assert compiled["shared_provenance"]["assets"]


def test_load_recovers_config_queries_and_pairs() -> None:
    source = load_lens_source(_render())
    original = jaffle_customer_value_bundle()
    assert source.config == original.config
    assert source.local_definitions == []  # shared terms never round-trip as local
    assert source.use_when == original.semantic_model.use_when
    assert source.sample_queries == original.semantic_model.sample_queries
    assert source.certified_answers[0]["question"] == _ANSWER.question
    assert source.certified_answers[0]["sql"] == _ANSWER.sql


def test_local_definitions_round_trip() -> None:
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.definitions.append(
        Definition(term="board_margin", body="Local nuance.", sql_expr="margin > 0")
    )
    files = render_lens_repo(bundle)
    assert "definitions/board-margin.md" in files
    source = load_lens_source(files)
    assert [d.term for d in source.local_definitions] == ["board_margin"]
    assert source.local_definitions[0].sql_expr == "margin > 0"
    assert source.local_definitions[0].source == "authored"


def test_render_load_render_is_a_fixed_point() -> None:
    first = _render()
    source = load_lens_source(first)
    # load gives declarable parts, not a bundle; re-render from a bundle carrying them
    bundle = jaffle_customer_value_bundle()
    assert source.config == bundle.config
    second = render_lens_repo(bundle, certified_answers=[_ANSWER])
    managed_first = {p: c for p, c in first.items() if is_managed(p)}
    managed_second = {p: c for p, c in second.items() if is_managed(p)}
    assert managed_first == managed_second


def test_split_by_lens_strips_prefixes_and_ignores_project_root() -> None:
    trees = split_by_lens(
        {
            "dst.yaml": "name: x",
            "lenses/a/lens.yaml": "1",
            "lenses/a/definitions/foo.md": "2",
            "lenses/b/lens.yaml": "3",
        }
    )
    assert set(trees) == {"a", "b"}
    assert trees["a"] == {"lens.yaml": "1", "definitions/foo.md": "2"}


def test_split_semantic_selects_project_level_shared_files() -> None:
    files = {
        "semantic/entities/orders.yaml": "e",
        "semantic/definitions/ltv.md": "d",
        "semantic/README.md": "ignored",
        "lenses/a/definitions/foo.md": "lens-local",
        "dst.yaml": "root",
    }
    assert split_semantic(files) == {
        "semantic/entities/orders.yaml": "e",
        "semantic/definitions/ltv.md": "d",
    }


# ── plan engines (pure) ──────────────────────────────────────────────────────


def test_plan_statuses_and_diffs() -> None:
    db_tree = _render()
    incoming = dict(db_tree)
    assert plan_lenses({"jcv": db_tree}, {"jcv": incoming})[0].status == "unchanged"
    incoming["queries.yaml"] = incoming["queries.yaml"].replace("count(*)", "COUNT(*)")
    plan = plan_lenses({"jcv": db_tree}, {"jcv": incoming})[0]
    assert plan.status == "update"
    assert [d.path for d in plan.diffs] == ["queries.yaml"]
    assert "+" in plan.diffs[0].diff and "COUNT(*)" in plan.diffs[0].diff
    # README/compiled drift alone is ignored — runtime output, not managed.
    incoming2 = dict(db_tree)
    incoming2["README.md"] = "totally different"
    incoming2["compiled.yaml"] = "also different"
    assert plan_lenses({"jcv": db_tree}, {"jcv": incoming2})[0].status == "unchanged"
    assert plan_lenses({}, {"new": db_tree})[0].status == "create"


def test_plan_semantic_statuses_and_diffs() -> None:
    entities, definitions = jaffle_shared_assets()
    db_files = render_semantic_files(entities, definitions)
    incoming = dict(db_files)
    plans = plan_semantic(db_files, incoming)
    assert {p.status for p in plans} == {"unchanged"}
    incoming["semantic/entities/customers.yaml"] = incoming[
        "semantic/entities/customers.yaml"
    ].replace("One row per customer.", "One row per customer, always.")
    incoming["semantic/entities/suppliers.yaml"] = "name: suppliers\n"
    del incoming["semantic/entities/orders.yaml"]
    by_path = {p.path: p for p in plan_semantic(db_files, incoming)}
    assert by_path["semantic/entities/customers.yaml"].status == "update"
    assert "+" in by_path["semantic/entities/customers.yaml"].diff
    assert by_path["semantic/entities/suppliers.yaml"].status == "create"
    # a DB asset absent from the push is untouched — it never enters the plan
    assert "semantic/entities/orders.yaml" not in by_path


def test_server_only_is_a_names_diff() -> None:
    from services.project.plan import server_only

    assert server_only(["b", "a", "c"], ["b"]) == ["a", "c"]
    assert server_only([], ["pushed-but-unknown"]) == []


def test_stale_lenses_compares_provenance_to_effective_hashes() -> None:
    provenances = {
        "board": {"entity/orders": "h1", "definition/ltv": "h2"},
        "ops": {"entity/orders": "h1"},
        "fresh": {"entity/customers": "h3"},
    }
    effective = {"entity/orders": "CHANGED", "definition/ltv": "h2", "entity/customers": "h3"}
    assert stale_lenses(provenances, effective) == {
        "board": ["entity/orders"],
        "ops": ["entity/orders"],
    }


# ── the full loop against the scratch DB ─────────────────────────────────────


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
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('ProjSyncT') RETURNING id")
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
        c.execute(text("DELETE FROM embedding_meta"))  # global claim row — leave none behind
    admin.dispose()


def _project_files() -> dict[str, str]:
    entities, definitions = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions))
    lens_tree = render_lens_repo(jaffle_customer_value_bundle())
    for path, content in lens_tree.items():
        files[f"lenses/customer_value/{path}"] = content
    return files


def _second_lens_yaml() -> str:
    config = jaffle_customer_value_config().model_copy(
        update={"name": "repeat_buyers", "display_name": "Repeat Buyers", "description": ""}
    )
    config.select.entities = [e for e in config.select.entities if e.name == "customers"]
    config.select.definitions = ["repeat_customer"]
    return yaml.safe_dump(config.model_dump(mode="json", exclude_none=True), sort_keys=False)


@needs_db
def test_apply_export_plan_fixed_point(org) -> None:
    _oid, headers = org
    files = _project_files()
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    semantic = next(e for e in out if e.get("scope") == "semantic")
    assert sorted(semantic["applied"]) == [
        "created definition/lifetime_value",
        "created definition/repeat_customer",
        "created definition/value",
        "created entity/customers",
        "created entity/orders",
    ]
    lens = next(e for e in out if e.get("lens") == "customer_value")
    assert lens["action"] == "created" and lens["version"] == 1
    assert not any(e.get("action") == "rejected-recompile" for e in out)

    exported = client.get("/mgmt/project/export", headers=headers).json()["files"]
    assert {p: c for p, c in exported.items() if p.startswith("semantic/")} == {
        p: c for p, c in files.items() if p.startswith("semantic/")
    }
    for path, content in files.items():
        if path.startswith("lenses/") and is_managed(path.split("/", 2)[2]):
            assert exported[path] == content, path

    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": exported}).json()
    assert {e["status"] for e in plan if e.get("scope") == "semantic"} == {"unchanged"}
    lens_rows = [e for e in plan if "lens" in e]
    assert [(e["lens"], e["status"]) for e in lens_rows] == [("customer_value", "unchanged")]
    assert "stale" not in lens_rows[0]


@needs_db
def test_apply_plan_converges_across_yaml_styles(org) -> None:
    """The same data in a different YAML emitter style (2-space
    sequences, narrow wrap) planned every lens as `update` forever, and an
    absent evals/cases.yaml diffed against the DB's rendered `[]`. Style must
    never drive the change decision, and a clean apply must converge: the next
    plan reports unchanged and the next apply skips the publish path."""
    _oid, headers = org
    files = _project_files()
    qpath = "lenses/customer_value/queries.yaml"
    files[qpath] = yaml.safe_dump(yaml.safe_load(files[qpath]), sort_keys=False, indent=4, width=30)
    files.pop("lenses/customer_value/evals/cases.yaml", None)  # absent ≡ [] — never a diff
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert not any(e.get("action") == "rejected" for e in out)

    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    lens_rows = [(e["lens"], e["status"]) for e in plan if "lens" in e]
    assert lens_rows == [("customer_value", "unchanged")]
    assert {e["status"] for e in plan if e.get("scope") == "semantic"} == {"unchanged"}

    # The no-op apply must not pay a real one's cost: no republish, no version
    # bump — the lens row reports `unchanged`.
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    actions = {e["lens"]: e["action"] for e in out if "lens" in e}
    assert actions == {"customer_value": "unchanged"}
    with org_session(_oid) as s:
        assert len(lens_store.list_versions(s, "customer_value")) == 1


@needs_db
def test_shared_edit_marks_stale_and_apply_recompiles_absent_lenses(org) -> None:
    _oid, headers = org
    files = _project_files()
    files["lenses/repeat_buyers/lens.yaml"] = _second_lens_yaml()
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert {e.get("action") for e in out if "lens" in e} == {"created"}

    # Edit ONE shared entity file; push it alone — no lenses/ in the push at all.
    edited = {
        "semantic/entities/customers.yaml": files["semantic/entities/customers.yaml"].replace(
            "One row per customer.", "One row per customer (deduplicated)."
        )
    }
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": edited}).json()
    by_lens = {e["lens"]: e for e in plan if "lens" in e}
    assert by_lens["customer_value"]["status"] == "stale"
    assert by_lens["customer_value"]["stale"] == ["entity/customers"]
    assert by_lens["repeat_buyers"]["stale"] == ["entity/customers"]
    assert by_lens["customer_value"]["note"] == "will recompile on apply"

    out = client.post("/mgmt/project/apply", headers=headers, json={"files": edited}).json()
    actions = {e["lens"]: e for e in out if "lens" in e}
    assert actions["customer_value"]["action"] == "recompiled"
    assert actions["repeat_buyers"]["action"] == "recompiled"
    assert actions["customer_value"]["version"] == 2
    with org_session(_oid) as s:
        published = lens_store.resolve_published(s, "customer_value")
        assert published is not None
        customers = next(e for e in published.semantic_model.entities if e.name == "customers")
        assert customers.description == "One row per customer (deduplicated)."
        assert lens_store.list_versions(s, "customer_value")[0].summary == (
            "recompile (shared assets changed)"
        )

    # Fixed point: a second identical apply recompiles nothing.
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": edited}).json()
    assert not any("lens" in e for e in out)


@needs_db
def test_partial_push_compiles_against_db_assets(org) -> None:
    _oid, headers = org
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    # A later push with ONLY a lens tree compiles against the stored shared layer.
    push = {"lenses/repeat_buyers/lens.yaml": _second_lens_yaml()}
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": push}).json()
    row = next(e for e in out if e.get("lens") == "repeat_buyers")
    assert row["action"] == "created", row
    with org_session(_oid) as s:
        published = lens_store.resolve_published(s, "repeat_buyers")
        assert published is not None
        assert [e.name for e in published.semantic_model.entities] == ["customers"]
        assert [d.source for d in published.semantic_model.definitions] == ["shared"]


@needs_db
def test_rejected_recompile_aborts_the_apply_nothing_lands(org) -> None:
    """A recompile failure no longer half-applies — under the old
    contract the broken shared edit LANDED while the lens kept its prior
    bundle; now the whole apply rolls back, shared asset included."""
    _oid, headers = org
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    # Break the shared entity: a connection the lens doesn't list → validate error.
    broken = {
        "semantic/entities/customers.yaml": files["semantic/entities/customers.yaml"].replace(
            "connection: jaffle", "connection: nowhere"
        )
    }
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": broken}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected-recompile"
    assert any("nowhere" in err for err in row["errors"])
    assert out[-1] == {
        "scope": "apply",
        "action": "aborted",
        "detail": "nothing deployed — fix the errors and re-apply",
    }
    with org_session(_oid) as s:
        published = lens_store.resolve_published(s, "customer_value")
        assert published is not None  # the prior bundle still serves
        customers = next(e for e in published.semantic_model.entities if e.name == "customers")
        assert customers.source.connection == "jaffle"
        assert len(lens_store.list_versions(s, "customer_value")) == 1  # no new version
        # the broken shared edit itself rolled back — blue/green, not half-applied
        asset = semantic_store.get_asset(s, "entity", "customers")
        assert asset is not None and "nowhere" not in str(asset.body)


@needs_db
def test_malformed_semantic_file_rejects_before_lenses(org) -> None:
    _oid, headers = org
    files = _project_files()
    files["semantic/entities/customers.yaml"] = "name: [unclosed"
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] == "aborted" and out[-1]["scope"] == "apply"
    assert out[-2]["action"] == "rejected"
    assert "semantic/entities/customers.yaml" in out[-2]["errors"][0]
    assert not any("lens" in e for e in out)  # nothing lens-side mutated


@needs_db
def test_a_misspelled_key_is_rejected_by_plan_and_apply(org) -> None:
    """The whole point, end to end: a typo must never apply clean. Plan marks
    the lens invalid with the same message apply rejects on (plan predicts
    apply), and the message names file, key path and the fix."""
    _oid, headers = org
    files = _project_files()
    files["lenses/customer_value/lens.yaml"] += "\ndescriptions: oops\n"

    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] == "invalid"
    assert "did you mean `description`" in row["error"]

    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    rejected = next(e for e in out if e.get("action") == "rejected")
    assert "lens.yaml:" in rejected["errors"][0]
    assert "did you mean `description`" in rejected["errors"][0]


@needs_db
def test_a_key_that_parses_but_is_never_read_applies_with_a_warning(org) -> None:
    """The retired `context:` block is authored by projects in the wild (earlier
    scaffolds emitted it) — rejecting would break them, staying
    silent is what let the class survive."""
    _oid, headers = org
    files = _project_files()
    files["lenses/customer_value/lens.yaml"] += "\ncontext:\n  uploads: [notes.md]\n"
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] in ("created", "updated")
    assert any("`context`" in w and "retired" in w for w in row["warnings"])


@needs_db
def test_malformed_lens_list_file_is_rejected_never_a_500(org) -> None:
    """An entry appended after the old scaffold's `[]` placeholder made the file
    malformed — and both plan and apply answered a raw 500 internal_error with
    the ParserError buried in serve.log (both list files). The
    contract now: plan marks the lens invalid, apply rejects the row and aborts,
    and the error names the file and the parse position."""
    _oid, headers = org
    for path in ("certified_answers.yaml", "evals/cases.yaml"):
        files = _project_files()
        files[f"lenses/customer_value/{path}"] = "[]\n- question: appended\n  sql: SELECT 1\n"

        r = client.post("/mgmt/project/plan", headers=headers, json={"files": files})
        assert r.status_code == 200
        row = next(e for e in r.json() if e.get("lens") == "customer_value")
        assert row["status"] == "invalid"
        assert path in row["error"] and "line 2" in row["error"]

        r = client.post("/mgmt/project/apply", headers=headers, json={"files": files})
        assert r.status_code == 200
        out = r.json()
        rejected = next(e for e in out if e.get("action") == "rejected")
        assert path in rejected["errors"][0] and "line 2" in rejected["errors"][0]
        assert out[-1]["action"] == "aborted"


_BAD_ENTITY = """name: {name}
source: {{connection: jaffle, table: {name}}}
fields:
  - {{name: id, type: BIGINT}}
  - {{name: label, type: VARCHAR}}
"""


@needs_db
def test_plan_validates_every_semantic_file_the_way_apply_does(org) -> None:
    """Entity files carrying warehouse types in
    `fields[].type` rendered a clean create-diff in plan; apply then rejected all
    of them. Plan's entire job is to predict apply — every bad file, on its own
    row, with the error apply would give."""
    _oid, headers = org
    files = _project_files()
    files["semantic/entities/player.yaml"] = _BAD_ENTITY.format(name="player")
    files["semantic/entities/match.yaml"] = _BAD_ENTITY.format(name="match")

    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    bad = {e["path"]: e for e in plan if e.get("scope") == "semantic" and e.get("error")}
    # BOTH files, not just the first one parse_semantic_files trips over
    assert set(bad) == {"semantic/entities/player.yaml", "semantic/entities/match.yaml"}
    for row in bad.values():
        assert row["status"] == "invalid" and not row["diff"]
        assert "'BIGINT' is not one of" in row["error"]
    # the valid files still plan normally alongside them
    assert any(
        e.get("path") == "semantic/entities/orders.yaml" and e.get("status") == "create"
        for e in plan
    )

    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] == "aborted"  # exactly what the plan predicted


# ── a semantics change: what an UNCHANGED file means ─────────────────────────


def _migration(name: str):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_a_lens_whose_temperature_actually_moved_is_noticed() -> None:
    """`ModelConfig.temperature` had zero readers, so every lens scaffolded
    before it gained one changed sampling silently on upgrade — and `plan` said
    `unchanged`, correctly, because the file had not.
    The release stamps the lenses it moved; this is the rule it stamps by, and
    it must stay narrow: only a pin the answer_mode does not already supply."""
    notice = _migration("0040_lens_upgrade_notice").temperature_notice
    assert notice({}) is None  # unset: always followed answer_mode
    assert notice({"temperature": None}) is None
    assert notice({"temperature": 0.2}) is None  # balanced already supplied 0.2
    assert notice({"temperature": 0.0, "answer_mode": "strict"}) is None
    # a lens scaffolded before the change: temperature 0.0 under a balanced default.
    assert "generates at 0.0 now and generated at 0.2" in notice({"temperature": 0.0})
    # And the direction that costs more: a strict lens becoming LESS deterministic.
    line = notice({"temperature": 0.2, "answer_mode": "strict"})
    assert "generates at 0.2 now and generated at 0.0" in line
    assert "remove `model.temperature` from lens.yaml to go back to 0.0" in line


def test_balanced_determinism_change_is_noticed_on_exactly_the_moved_lenses() -> None:
    """Same doctrine: balanced now generates at 0.0 — only a
    lens that actually FOLLOWED balanced's old 0.2 gets the stamp."""
    notice = _migration("0057_balanced_generates_deterministic").balanced_default_notice
    assert notice({}) is not None  # the default default: balanced, unpinned — moved
    assert "generates at 0.0 now and generated at 0.2" in notice({"answer_mode": "balanced"})
    assert notice({"temperature": 0.2}) is None  # a pin always won; it still does
    assert notice({"temperature": 0.0}) is None
    assert notice({"answer_mode": "strict"}) is None  # already 0.0
    assert notice({"answer_mode": "exploratory"}) is None  # keeps 0.5


@needs_db
def test_an_upgrade_notice_rides_the_plan_and_clears_on_apply(org) -> None:
    """The notice has to reach the one command people run to see what changed,
    on a project where NOTHING changed — and then stop. Applying the lens is
    the act that clears it: its owner has now re-published under the new code."""
    oid, headers = org
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    line = "generation temperature: this lens generates at 0.0 now and generated at 0.2 …"
    with org_session(oid) as s:
        s.execute(
            text("UPDATE lens SET upgrade_notice = :n WHERE name = 'customer_value'"), {"n": line}
        )
        s.commit()

    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] == "unchanged"  # the file diff is still the truth
    assert row["semantics"] == [line]  # …and it is not the whole truth

    # A push that never mentions the lens still gets told: the notice is about
    # what the SERVER is doing, not about a file.
    semantic_only = {p: c for p, c in files.items() if p.startswith("semantic/")}
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": semantic_only}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["semantics"] == [line]

    # The apply that clears it says it once — someone who never runs plan must
    # not be the only person the notice misses.
    applied = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert line in next(r for r in applied if r.get("lens") == "customer_value")["warnings"]
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    assert not any(e.get("semantics") for e in plan)


@needs_db
def test_plan_hints_unselected_shared_definitions(org) -> None:
    _oid, headers = org
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    # Push a definition page no lens selects.
    push = {
        "semantic/definitions/gross-margin.md": "---\nmetric: gross_margin\n---\n\nRevenue minus.\n"
    }
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": push}).json()
    hint = next(e for e in plan if e.get("status") == "hint")
    assert "gross_margin" in hint["hint"]


@needs_db
def test_apply_lints_dormant_shared_definitions(org) -> None:
    """A shared definition NO lens selects never reaches validate_bundle,
    so its double truth (sql: + about: a non-metric) used to apply green —
    governance the author believed active, dormant. The apply must warn, once;
    selecting the term moves the same warning to the lens row, still once."""
    _oid, headers = org
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})

    def _double_truth(out: list[dict[str, object]]) -> list[str]:
        return [
            w
            for e in out
            for w in (e.get("warnings") or [])  # type: ignore[union-attr]
            if "'order_size'" in w and "sql_expr and about" in w
        ]

    push = {
        # sql: + about: a plain FIELD, selected by no lens → the dormant double truth
        "semantic/definitions/order-size.md": (
            "---\nterm: order_size\nabout: orders.amount\nsql: orders.amount > 100\n---\n\nBig.\n"
        ),
        # about: a METRIC stays exempt on the shared path too (the metric owns the SQL)
        "semantic/definitions/gross-revenue.md": (
            "---\nterm: gross_revenue\nabout: orders.revenue\nsql: SUM(orders.amount)\n---\n\nR.\n"
        ),
    }
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": push}).json()
    assert not any(e.get("action") == "aborted" for e in out)  # a warning, never a gate
    lint = next(e for e in out if e.get("scope") == "semantic" and e.get("action") == "lint")
    assert not [w for w in lint["warnings"] if "gross_revenue" in w]
    doubled = _double_truth(out)
    assert len(doubled) == 1 and "no lens selects" in doubled[0]

    # Select the term → the compiled path warns on the lens row; the shared
    # lint stands down (nothing warns twice, no lint row at all).
    config = jaffle_customer_value_config().model_copy(
        update={"name": "sizing", "display_name": "Sizing", "description": ""}
    )
    config.select.definitions = ["order_size"]
    push = {
        "lenses/sizing/lens.yaml": yaml.safe_dump(
            config.model_dump(mode="json", exclude_none=True), sort_keys=False
        )
    }
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": push}).json()
    assert next(e for e in out if e.get("lens") == "sizing")["action"] == "created"
    doubled = _double_truth(out)
    assert len(doubled) == 1 and "no lens selects" not in doubled[0]
    assert not any(e.get("action") == "lint" for e in out)


# ── eval gate on apply, upserts, orphans, apply lock ─────────────────────────


def _gated_files(gate: str, *, connection: str | None = None) -> dict[str, str]:
    files = _project_files()
    if connection is not None:  # rename the demo's connection, entity sources included
        files = {
            path: content.replace("connection: jaffle", f"connection: {connection}")
            for path, content in files.items()
        }
    cfg = yaml.safe_load(files["lenses/customer_value/lens.yaml"])
    cfg["eval_gate"] = gate
    if connection is not None:
        cfg["connections"] = [connection]
    files["lenses/customer_value/lens.yaml"] = yaml.safe_dump(cfg, sort_keys=False)
    return files


def _regressed_outcome():
    from services.contracts.eval import EvalResult, EvalRun
    from services.evals.runner import RunOutcome

    return RunOutcome(
        run=EvalRun(
            id="r2",
            lens="customer_value",
            started_at="2026-07-22T00:00:00Z",
            mode="regression",
            score=0.5,
            passed=1,
            failed=1,
        ),
        results=[EvalResult(run_id="r2", case_id="c1", passed=False)],
    )


@needs_db
def test_apply_enforces_eval_gate_block_and_warn(org, monkeypatch) -> None:
    from services.evals import service as eval_service
    from services.evals import store as eval_store
    from services.llm import registry

    _oid, headers = org
    files = _gated_files("block")
    monkeypatch.setattr(registry, "resolve", lambda ref: ResolvedModel(object(), "fake", "m"))  # type: ignore[arg-type]
    # Eval-less degrade: no approved cases → gate stands down, publish proceeds
    # (build_and_run returning None is exactly "nothing to score").
    monkeypatch.setattr(eval_service, "build_and_run", lambda **kw: None)
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert next(e for e in out if e.get("lens") == "customer_value")["action"] == "created"

    # A prior regression run scored 0.9; every gate run now scores 0.5 → regression.
    with org_session(_oid) as s:
        eval_store.create_run(
            s, "customer_value", "regression", score=0.9, passed=9, failed=1, errored=0
        )
        s.commit()
    monkeypatch.setattr(eval_service, "build_and_run", lambda **kw: _regressed_outcome())

    # block on the direct apply path: a real edit republishes, the gate scores
    # 0.5 < 0.9 → rejected → the whole apply aborts. (An IDENTICAL re-apply
    # would skip the publish path as `unchanged`, so the gate needs an actual
    # change to fire here.)
    cfg = yaml.safe_load(files["lenses/customer_value/lens.yaml"])
    cfg["description"] = "edited so the gate has a publish to block"
    files["lenses/customer_value/lens.yaml"] = yaml.safe_dump(cfg, sort_keys=False)
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected"
    assert any("eval gate blocked publish" in e and "c1" in e for e in row["errors"])
    assert out[-1]["action"] == "aborted" and out[-1]["scope"] == "apply"
    with org_session(_oid) as s:
        assert len(lens_store.list_versions(s, "customer_value")) == 1  # no new version

    # block on the recompile pass: a shared edit pushed alone recompiles the lens,
    # the gate rejects it → abort: the prior bundle keeps serving AND the shared
    # edit itself never lands (old contract half-applied it).
    edited = {
        "semantic/entities/customers.yaml": files["semantic/entities/customers.yaml"].replace(
            "One row per customer.", "One row per customer (deduplicated)."
        )
    }
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": edited}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "rejected-recompile"
    assert any("eval gate blocked publish" in e for e in row["errors"])
    assert out[-1]["action"] == "aborted" and out[-1]["scope"] == "apply"
    with org_session(_oid) as s:
        published = lens_store.resolve_published(s, "customer_value")
        assert published is not None
        customers = next(e for e in published.semantic_model.entities if e.name == "customers")
        assert customers.description == "One row per customer."  # prior bundle stands
        assert len(lens_store.list_versions(s, "customer_value")) == 1
        asset = semantic_store.get_asset(s, "entity", "customers")
        assert asset is not None and "deduplicated" not in str(asset.body)

    # warn publishes anyway, with the regression surfaced as a warning.
    out = client.post(
        "/mgmt/project/apply", headers=headers, json={"files": _gated_files("warn")}
    ).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "updated"
    assert any(
        "eval gate: accuracy regressed" in w and "eval_gate: warn" in w for w in row["warnings"]
    )
    with org_session(_oid) as s:
        assert len(lens_store.list_versions(s, "customer_value")) == 2


# ── blue/green apply — everything deploys or nothing ─────────────────────────


@needs_db
def test_multi_lens_apply_with_one_rejection_lands_nothing(org, monkeypatch) -> None:
    """One rejected lens aborts the WHOLE apply: sibling lenses, semantic
    assets, and connections staged in the same push all roll back (the old
    contract landed the siblings and exited 0)."""
    from services.project import apply as apply_engine

    _oid, headers = org
    monkeypatch.setattr(apply_engine, "_embedder", lambda: None)  # keyless: store unembedded
    files = _project_files()
    files["lenses/customer_value/certified_answers.yaml"] = yaml.safe_dump(
        [{"question": _ANSWER.question, "sql": _ANSWER.sql}], sort_keys=False
    )
    # names 'repeat_buyers' but the tree is lenses/broken/ → apply_lens rejects
    files["lenses/broken/lens.yaml"] = _second_lens_yaml()
    files["dst.yaml"] = (
        "connections:\n  fresh:\n    type: duckdb\n"
        f"    config: {{path: {settings.duckdb_jaffle_path}}}\n"
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    # Staged-then-aborted rows must not read as deploys: action rewritten to
    # rolled-back, version nulled, and every staged `applied` count says
    # "rolled back:" itself (a bare "created 1" inside a rolled-back
    # block read as a partial apply) — a scripter reading rows without the
    # banner must never believe the sibling landed.
    connections = next(e for e in out if e.get("scope") == "connections")
    assert connections["applied"] == ["rolled back: created 'fresh' (duckdb)"]
    assert connections["action"] == "rolled-back"
    sibling = next(e for e in out if e.get("lens") == "customer_value")
    assert sibling["action"] == "rolled-back" and sibling["version"] is None
    assert sibling["applied"] == [
        "rolled back: certified answers: created 1, updated 0, unchanged 0"
    ]
    broken = next(e for e in out if e.get("lens") == "broken")
    assert broken["action"] == "rejected"  # error rows keep their diagnosis
    assert any("lens.yaml names 'repeat_buyers'" in e for e in broken["errors"])
    assert out[-1] == {
        "scope": "apply",
        "action": "aborted",
        "detail": "nothing deployed — fix the errors and re-apply",
    }
    with org_session(_oid) as s:  # …and NONE of it landed
        assert lens_store.lens_names(s) == []
        assert lens_store.list_versions(s, "customer_value") == []
        assert semantic_store.list_assets(s) == []
        assert connection_store.get_connection(s, "fresh") is None

    # Drop the broken tree → all green: everything lands, no abort row.
    del files["lenses/broken/lens.yaml"]
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert not any(e.get("action") == "aborted" for e in out)
    assert not any(e.get("errors") for e in out)
    with org_session(_oid) as s:
        assert lens_store.lens_names(s) == ["customer_value"]
        assert len(lens_store.list_versions(s, "customer_value")) == 1
        assert len(semantic_store.list_assets(s)) == 5
        assert connection_store.get_connection(s, "fresh") is not None


@needs_db
def test_require_gates_fails_closed_on_a_skipped_gate(org) -> None:
    """By default a skipped gate publishes with a warning and
    exit 0, so a provider outage silently converts a gated apply into an
    ungated one. ?require_gates=true turns any configured-but-skipped gate into
    an abort, and the gate outcome rides the lens row either way."""
    _oid, headers = org
    files = _gated_files("block")
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created"
    # The org fixture pins registry.resolve → None: an infra skip, named.
    assert row["gate"] == "skipped (model unservable)"

    # Fail-closed: the same push (edited so the publish path runs) aborts.
    cfg = yaml.safe_load(files["lenses/customer_value/lens.yaml"])
    cfg["description"] = "edited so the gate has a publish to guard"
    files["lenses/customer_value/lens.yaml"] = yaml.safe_dump(cfg, sort_keys=False)
    out = client.post(
        "/mgmt/project/apply?require_gates=true", headers=headers, json={"files": files}
    ).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any("--require-gates" in e for e in row["errors"])
    assert out[-1]["action"] == "aborted" and out[-1]["scope"] == "apply"
    with org_session(_oid) as s:
        assert len(lens_store.list_versions(s, "customer_value")) == 1  # nothing landed


@needs_db
def test_gate_blocked_apply_aborts_and_never_lowers_the_baseline(org, monkeypatch) -> None:
    """The one-shot tripwire: a gate-BLOCKED publish used to persist its
    failing eval run, and recompile_stale in the SAME apply re-gated the lens
    against that just-lowered baseline — republishing the broken definition as
    'recompiled' while the output also showed the rejection. Blue/green: the
    abort rolls the run back, the prior answer keeps serving, and an identical
    re-apply aborts AGAIN on the same score comparison."""
    from services.evals import service as eval_service
    from services.evals import store as eval_store
    from services.llm import registry

    _oid, headers = org
    files = _gated_files("block")
    monkeypatch.setattr(registry, "resolve", lambda ref: ResolvedModel(object(), "fake", "m"))  # type: ignore[arg-type]
    monkeypatch.setattr(eval_service, "build_and_run", lambda **kw: None)
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert next(e for e in out if e.get("lens") == "customer_value")["action"] == "created"
    with org_session(_oid) as s:  # the published baseline: a 0.9 regression run
        eval_store.create_run(
            s, "customer_value", "regression", score=0.9, passed=9, failed=1, errored=0
        )
        s.commit()

    def _scoring_run(**kw):  # noqa: ANN003 — the real build_and_run persists its run; mimic that
        eval_store.create_run(
            kw["session"], "customer_value", "regression", score=0.5, passed=1, failed=1, errored=0
        )
        return _regressed_outcome()

    monkeypatch.setattr(eval_service, "build_and_run", _scoring_run)
    # The breaking push: a shared edit + the gated lens tree in one apply.
    broken = dict(files)
    broken["semantic/entities/customers.yaml"] = broken["semantic/entities/customers.yaml"].replace(
        "One row per customer.", "One row per customer (deduplicated)."
    )
    for attempt in (1, 2):  # identical re-apply must hit the SAME comparison
        out = client.post("/mgmt/project/apply", headers=headers, json={"files": broken}).json()
        row = next(e for e in out if e.get("lens") == "customer_value")
        assert row["action"] == "rejected", f"attempt {attempt}"
        assert any("score 0.5 < prev 0.9" in e for e in row["errors"]), f"attempt {attempt}"
        # the blocked lens must NOT resurrect through the recompile pass
        assert not any(e.get("action") == "recompiled" for e in out), f"attempt {attempt}"
        assert out[-1]["action"] == "aborted" and out[-1]["scope"] == "apply"
        with org_session(_oid) as s:
            runs = eval_store.list_runs(s, "customer_value")
            assert [r.score for r in runs] == [0.9]  # the failing 0.5 run rolled back
            published = lens_store.resolve_published(s, "customer_value")
            assert published is not None  # the prior answer keeps serving
            customers = next(e for e in published.semantic_model.entities if e.name == "customers")
            assert customers.description == "One row per customer."
            assert len(lens_store.list_versions(s, "customer_value")) == 1
            asset = semantic_store.get_asset(s, "entity", "customers")
            assert asset is not None and "deduplicated" not in str(asset.body)


@needs_db
def test_apply_gate_stands_down_keyless_but_loudly(org, monkeypatch) -> None:
    """No smart-tier model configured → apply publishes ungated (the interactive
    endpoint's exact degradation) — but the skip is SAID: eval_gate: block with
    no model resolving was a silently-inert seatbelt."""
    from services.evals import service as eval_service
    from services.llm import registry

    _oid, headers = org

    def _boom(**kw):  # noqa: ANN003
        raise AssertionError("gate must not run keyless")

    monkeypatch.setattr(registry, "resolve", lambda ref: None)
    monkeypatch.setattr(eval_service, "build_and_run", _boom)
    files = _gated_files("block")
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created"
    assert any(
        "this lens's model cannot be served here" in w and "gate SKIPPED" in w
        for w in row["warnings"]
    )


@needs_db
def test_apply_gates_on_cases_landing_in_the_same_push(org, monkeypatch) -> None:
    """Root cause: cases used to land AFTER the gate ran, so the
    apply that introduces evals/cases.yaml (typically the one that also sets
    eval_gate) never scored them and no baseline run was ever recorded — the
    next, breaking, apply had nothing to regress against. The gate must see the
    push's own cases."""
    from services.evals import service as eval_service
    from services.evals import store as eval_store
    from services.llm import registry

    _oid, headers = org
    seen: dict[str, int] = {}

    def _spy(**kw):  # noqa: ANN003
        cases = eval_store.list_cases(kw["session"], "customer_value", status="approved")
        seen["approved"] = len(cases)
        return None

    monkeypatch.setattr(registry, "resolve", lambda ref: ResolvedModel(object(), "fake", "m"))  # type: ignore[arg-type]
    monkeypatch.setattr(eval_service, "build_and_run", _spy)
    files = _gated_files("block")
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [
            {
                "question": "How many customers are repeat customers?",
                "expected_sql": "SELECT 19",
                "status": "approved",
            }
        ]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created"
    assert seen["approved"] == 1  # the pushed case was visible to the gate


_CLARIFY_CASE = [
    {
        "question": "what is the average value of a customer?",
        "expect": "clarify",
        "term": "value",
        "status": "approved",
    }
]


@needs_db
def test_first_apply_resolves_a_connection_landing_in_the_same_push(org, monkeypatch) -> None:
    """The FIRST apply of a new project pushes dst.yaml's connection, the
    lens and evals/cases.yaml together. The behavioral gate resolved its
    connector on its OWN session, which cannot see a connection staged in the
    in-flight apply transaction — `unknown connection 'bank'` came back as an
    opaque 500, and the only workaround was applying twice (once without the
    cases). The gate resolves on the CALLER's session now, so the staged
    connection is visible and the pin actually scores (1.0: 'value' is still
    ambiguous, so the deterministic pre-check decides it without consuming the
    scripted garbage). The connection is deliberately NOT named 'jaffle': the
    dev/seed built-in fallback would resolve that one without a DB row at all."""
    from services.contracts.fakes import ScriptedLLM
    from services.evals import store as eval_store
    from services.llm import registry

    _oid, headers = org
    with org_session(_oid) as s:  # a brand-new project has no connection row yet
        connection_store.delete_connection(s, "jaffle")
        s.commit()
    monkeypatch.setattr(
        registry, "resolve", lambda _ref: ResolvedModel(ScriptedLLM(["GARBAGE ;;;"]), "fake", "m")
    )
    monkeypatch.setattr(registry, "resolve_embedder", lambda: None)
    files = _gated_files("block", connection="bank")
    files["dst.yaml"] = (
        "connections:\n  bank:\n    type: duckdb\n"
        f"    config: {{path: {settings.duckdb_jaffle_path}}}\n"
    )
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(_CLARIFY_CASE)
    response = client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    assert response.status_code == 200, response.text
    out = response.json()
    assert not any(e.get("errors") for e in out), out
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created" and row["version"] == 1
    with org_session(_oid) as s:
        assert [r.score for r in eval_store.list_runs(s, "customer_value")] == [1.0]


@needs_db
def test_gate_skips_loudly_when_the_connection_cannot_build_a_connector(org, monkeypatch) -> None:
    """The other half of the guarantee: a connection that exists but won't build
    a connector (dead credential, unreachable host) is a gate the operator must
    be TOLD stood down — never an opaque `internal_error`. The apply publishes
    with the skip named, and nothing is scored."""
    from services.contracts.fakes import ScriptedLLM
    from services.evals import service as eval_service
    from services.evals import store as eval_store
    from services.llm import registry

    def _dead(*_a, **_kw):  # noqa: ANN002,ANN003
        raise RuntimeError("password authentication failed for user 'dst'")

    _oid, headers = org
    monkeypatch.setattr(
        registry, "resolve", lambda _ref: ResolvedModel(ScriptedLLM(["GARBAGE ;;;"]), "fake", "m")
    )
    monkeypatch.setattr(registry, "resolve_embedder", lambda: None)
    monkeypatch.setattr(eval_service, "resolve_connector", _dead)
    files = _gated_files("block")
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(_CLARIFY_CASE)
    response = client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    assert response.status_code == 200, response.text
    out = response.json()
    assert not any(e.get("errors") for e in out), out
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created"
    assert any(
        "gate SKIPPED" in w and "connection 'jaffle' is unavailable" in w and "password" in w
        for w in row["warnings"]
    ), row["warnings"]
    with org_session(_oid) as s:
        assert eval_store.list_runs(s, "customer_value") == []


@needs_db
def test_apply_upserts_certified_answers_and_eval_cases(org, monkeypatch) -> None:
    from services.certify import store as certify_store
    from services.contracts.fakes import HashEmbedder
    from services.evals import store as eval_store
    from services.project import apply as apply_engine

    _oid, headers = org
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))  # claim cleanly with HashEmbedder
    admin.dispose()
    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())

    files = _project_files()
    for path, content in render_lens_repo(
        jaffle_customer_value_bundle(), certified_answers=[_ANSWER]
    ).items():
        files[f"lenses/customer_value/{path}"] = content
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [
            {
                "question": "How many orders shipped?",
                "expected_sql": "SELECT 1",
                "status": "approved",
                "expected": "1",  # a typo'd key must warn, never vanish silently
            }
        ]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "certified answers: created 1, updated 0, unchanged 0" in row["applied"]
    assert "eval cases: created 1, updated 0, unchanged 0" in row["applied"]
    assert any(
        "eval case 'How many orders shipped?': unknown key 'expected'" in w
        and (
            "known keys: question, expected_sql, expected_answer, expect, term, "
            "status, source, tags" in w
        )
        for w in row["warnings"]
    )

    # Edit the certified SQL + the eval expectation in the files → updates, not no-ops.
    files["lenses/customer_value/certified_answers.yaml"] = files[
        "lenses/customer_value/certified_answers.yaml"
    ].replace("COUNT(*)", "COUNT(1)")
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [{"question": "How many orders shipped?", "expected_sql": "SELECT 2", "status": "approved"}]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "certified answers: created 0, updated 1, unchanged 0" in row["applied"]
    assert "eval cases: created 0, updated 1, unchanged 0" in row["applied"]
    with org_session(_oid) as s:
        answers = certify_store.list_for_lens(s, "customer_value")
        assert len(answers) == 1 and "COUNT(1)" in answers[0].sql
        cases = eval_store.list_cases(s, "customer_value")
        assert len(cases) == 1 and cases[0].expected_sql == "SELECT 2"

    # Identical re-apply: counted as unchanged, honestly.
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "certified answers: created 0, updated 0, unchanged 1" in row["applied"]
    assert "eval cases: created 0, updated 0, unchanged 1" in row["applied"]

    # A keyless environment still lands an edit: the embedding is of the (unchanged)
    # question, so updates need no embedder — only new questions warn + skip.
    monkeypatch.setattr(apply_engine, "_embedder", lambda: None)
    files["lenses/customer_value/certified_answers.yaml"] = files[
        "lenses/customer_value/certified_answers.yaml"
    ].replace("COUNT(1)", "COUNT(*)")
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "certified answers: created 0, updated 1, unchanged 0" in row["applied"]
    with org_session(_oid) as s:
        assert "COUNT(*)" in certify_store.list_for_lens(s, "customer_value")[0].sql


@needs_db
def test_eval_case_status_changes_land_on_apply(org) -> None:
    """The file offered four editable fields and three worked —
    promoting candidate -> approved in cases.yaml was a silent no-op, so the
    eval gate could never be armed from the files. Status rides the change
    tuple now; promotion is a reviewable git diff."""
    from services.evals import store as eval_store

    _oid, headers = org
    files = _project_files()
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [{"question": "Promote me?", "expect": "answer", "status": "candidate"}]
    )
    client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    with org_session(_oid) as s:
        assert eval_store.list_cases(s, "customer_value")[0].status == "candidate"

    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [{"question": "Promote me?", "expect": "answer", "status": "approved"}]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "eval cases: created 0, updated 1, unchanged 0" in row["applied"]
    with org_session(_oid) as s:
        assert eval_store.list_cases(s, "customer_value")[0].status == "approved"

    # …and retirement works the same way.
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [{"question": "Promote me?", "expect": "answer", "status": "retired"}]
    )
    client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    with org_session(_oid) as s:
        assert eval_store.list_cases(s, "customer_value")[0].status == "retired"


@needs_db
def test_eval_case_tags_round_trip(org) -> None:
    """`tags` is a real key — it lands, a tags-only edit
    counts as `updated` (not the silent no-op class), export renders it back,
    and an untagged case renders no tags key at all."""
    from services.evals import store as eval_store

    _oid, headers = org
    files = _project_files()
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [
            {
                "question": "How are we tracking to the ARR goal in EMEA?",
                "expect": "answer",
                "status": "approved",
                "tags": ["persona:cro", "intent:discriminator"],
            },
            {"question": "Untagged?", "expect": "refuse", "status": "approved"},
        ]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert not any("unknown key 'tags'" in w for w in row.get("warnings") or [])
    assert "eval cases: created 2, updated 0, unchanged 0" in row["applied"]
    with org_session(_oid) as s:
        by_q = {c.question: c for c in eval_store.list_cases(s, "customer_value")}
        assert by_q["How are we tracking to the ARR goal in EMEA?"].tags == [
            "persona:cro",
            "intent:discriminator",
        ]
        assert by_q["Untagged?"].tags == []

    # A tags-only edit is an update, never a silent no-op…
    files["lenses/customer_value/evals/cases.yaml"] = files[
        "lenses/customer_value/evals/cases.yaml"
    ].replace("intent:discriminator", "intent:headline")
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "eval cases: created 0, updated 1, unchanged 1" in row["applied"]

    # …and the export round-trips: tags render back, untagged renders no key.
    export = client.get("/mgmt/project/export", headers=headers).json()["files"]
    cases = yaml.safe_load(export["lenses/customer_value/evals/cases.yaml"])
    by_question = {c["question"]: c for c in cases}
    tagged = by_question["How are we tracking to the ARR goal in EMEA?"]
    assert tagged["tags"] == ["persona:cro", "intent:headline"]
    assert "tags" not in by_question["Untagged?"]

    # Convergence: an identical re-apply reports the whole lens unchanged.
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "unchanged"


@needs_db
def test_apply_behavioral_cases_and_the_value_deprecation_nudge(org) -> None:
    """Behavioral expectations ({question, expect, term?}) are
    a real case type. expect/expected_sql are mutually exclusive (an ERROR —
    blue/green aborts the apply); a landed behavioral case persists in the
    expect/term columns (migration 0031) and exports back as expect/term keys;
    a value-shaped case still lands (inert — it no longer gates anywhere) but
    warns toward `dst evals migrate`."""
    from services.evals import store as eval_store

    _oid, headers = org
    files = _project_files()
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [{"question": "Both shapes?", "expected_sql": "SELECT 1", "expect": "clarify"}]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any("Both shapes?" in e and "mutually exclusive" in e for e in row["errors"])
    assert any(e.get("action") == "aborted" for e in out)

    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [{"question": "Bad shape?", "expect": "explode"}]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any("expect must be 'clarify', 'refuse', or 'answer'" in e for e in row["errors"])

    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [
            {
                "question": "what is the average value of a customer?",
                "expect": "clarify",
                "term": "value",
                "status": "approved",
            },
            {
                # The must-answer pin must pass the front door — the
                # apply must not reject what the scorer already runs.
                "question": "How many repeat customers do we have?",
                "expect": "answer",
                "status": "approved",
            },
            {
                "question": "How many customers are repeat customers?",
                "expected_sql": (
                    "SELECT count(*) AS n FROM customers WHERE customers.number_of_orders > 1"
                ),
                "status": "approved",
            },
        ]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created" and not row["errors"]
    assert "eval cases: created 3, updated 0, unchanged 0" in row["applied"]
    assert any(
        "1 value case(s)" in w and "no longer gate" in w and "dst evals migrate" in w
        for w in row["warnings"]
    )
    with org_session(_oid) as s:
        stored = {c.question: c for c in eval_store.list_cases(s, "customer_value")}
    behavioral = stored["what is the average value of a customer?"]
    assert (behavioral.expect, behavioral.term) == ("clarify", "value")  # real columns
    assert behavioral.expected_answer is None  # no marker: prose oracle field, left empty
    assert behavioral.expected_sql is None
    must_answer = stored["How many repeat customers do we have?"]
    assert (must_answer.expect, must_answer.term) == ("answer", None)

    # Identical re-apply: the column comparison is stable — unchanged, not updated.
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert "eval cases: created 0, updated 0, unchanged 3" in row["applied"]

    # Export decodes the marker back into the authored expect/term keys.
    export = client.get("/mgmt/project/export", headers=headers).json()["files"]
    cases = yaml.safe_load(export["lenses/customer_value/evals/cases.yaml"])
    exported = next(c for c in cases if c["question"].startswith("what is the average"))
    assert exported["expect"] == "clarify" and exported["term"] == "value"
    assert "expected_answer" not in exported


@needs_db
def test_apply_gates_broken_eval_expected_sql_per_case(org) -> None:
    """A mis-shaped expected_sql used to sail through apply and
    crash the publish gate with a raw duckdb.BinderException 500. Now it is
    parse-checked + execute-probed at upsert — and under blue/green a broken
    oracle is an ERROR: the whole apply aborts naming the case; nothing lands
    until the case is fixed."""
    from services.evals import store as eval_store

    _oid, headers = org
    files = _project_files()
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [
            {
                "question": "Broken case?",
                "expected_sql": "SELECT count(*) FROM customers WHERE customers.nope > 1",
                "status": "approved",
            },
            {
                "question": "Good case?",
                # unaliased + column-qualified — the exact shape that used to crash
                "expected_sql": (
                    "SELECT count(*) AS n FROM customers WHERE customers.number_of_orders > 1"
                ),
                "status": "approved",
            },
        ]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any("Broken case?" in e and "rejected" in e for e in row["errors"])
    assert any(e.get("action") == "aborted" for e in out)  # blue/green: nothing lands
    with org_session(_oid) as s:
        assert eval_store.list_cases(s, "customer_value") == []

    # Fix the broken case → everything (incl. the good case) lands together.
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [
            {
                "question": "Good case?",
                "expected_sql": (
                    "SELECT count(*) AS n FROM customers WHERE customers.number_of_orders > 1"
                ),
                "status": "approved",
            }
        ]
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert row["action"] == "created" and not row["errors"]
    with org_session(_oid) as s:
        assert [c.question for c in eval_store.list_cases(s, "customer_value")] == ["Good case?"]


@needs_db
def test_plan_lists_orphans_only_for_semantic_pushes(org) -> None:
    _oid, headers = org
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})

    # A push carrying one semantic file → every other DB asset is an advisory orphan.
    push = {"semantic/entities/customers.yaml": files["semantic/entities/customers.yaml"]}
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": push}).json()
    orphans = [e["note"] for e in plan if e.get("status") == "orphan"]
    assert (
        "entity/orders: in DB but no file — remove with dst semantic rm, "
        "or export to recover the file"
    ) in orphans
    assert len(orphans) == 4  # entity/orders + the three shared definitions
    assert not any(n.startswith("entity/customers:") for n in orphans)

    # A lens-only push must not spam orphans.
    push = {"lenses/repeat_buyers/lens.yaml": _second_lens_yaml()}
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": push}).json()
    assert not any(e.get("status") == "orphan" for e in plan)

    # The full tree accounts for every asset → no orphans.
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    assert not any(e.get("status") == "orphan" for e in plan)


def _api_born_bundle(name: str) -> lens_store.LensBundle:
    """A lens created straight through the API/wizard — never applied from files."""
    bundle = jaffle_customer_value_bundle()
    bundle.config.name = name
    bundle.config.display_name = name
    return bundle


@needs_db
def test_plan_lists_server_only_lenses_and_connections(org) -> None:
    _oid, headers = org
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    # Born on the server, absent from every file: an API lens + a connection.
    with org_session(_oid) as s:
        lens_store.create_lens(s, _api_born_bundle("wizard_born"))
        connection_store.create_connection(s, "extra", "duckdb", {"path": "/tmp/x.duckdb"}, None)
        s.commit()

    # lenses/ in the push → the server-only LENS surfaces; no connection spam.
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    rows = [e for e in plan if e.get("scope") == "server_only"]
    assert [(e["kind"], e["name"]) for e in rows] == [("lens", "wizard_born")]
    note = rows[0]["note"]
    assert "dst export --lens wizard_born" in note  # the adopt command
    assert "leave it for its owner" in note  # deletion is never the default
    assert "dst lens rm wizard_born" in note  # the explicit removal verb

    # dst.yaml in the push → the connections section joins; declared ones
    # are accounted for, secrets stay a server-side note.
    with_yaml = dict(files)
    with_yaml["dst.yaml"] = (
        "connections:\n  jaffle:\n    type: duckdb\n"
        f"    config: {{path: {settings.duckdb_jaffle_path}}}\n"
    )
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": with_yaml}).json()
    rows = [e for e in plan if e.get("scope") == "server_only"]
    assert [(e["kind"], e["name"]) for e in rows] == [
        ("lens", "wizard_born"),
        ("connection", "extra"),
    ]
    conn_note = rows[1]["note"]
    assert "secret_env" in conn_note and "secrets stay server-side" in conn_note
    assert "leave it for its owner" in conn_note

    # A semantic-only push carries neither lenses/ nor dst.yaml → silence.
    push = {"semantic/entities/customers.yaml": files["semantic/entities/customers.yaml"]}
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": push}).json()
    assert not any(e.get("scope") == "server_only" for e in plan)


@needs_db
def test_lens_api_exposes_from_files_signal(org) -> None:
    """from_files=False ⇔ no lens_version of the files-apply summary — wizard/API/
    cloud-born lenses carry the 'not in files' tag until adopted via export."""
    _oid, headers = org
    client.post("/mgmt/project/apply", headers=headers, json={"files": _project_files()})
    with org_session(_oid) as s:
        lens_store.create_lens(s, _api_born_bundle("wizard_born"))
        s.commit()
    by_name = {row["name"]: row for row in client.get("/mgmt/lenses", headers=headers).json()}
    assert by_name["customer_value"]["from_files"] is True
    assert by_name["wizard_born"]["from_files"] is False
    assert client.get("/mgmt/lenses/wizard_born", headers=headers).json()["from_files"] is False
    # A UI publish records a version too — but not a files-apply one: the signal
    # must not flip on publish alone.
    assert client.post("/mgmt/lenses/wizard_born/publish", headers=headers).status_code == 200
    assert client.get("/mgmt/lenses/wizard_born", headers=headers).json()["from_files"] is False


@needs_db
def test_export_scoped_to_lenses(org) -> None:
    """?lens= exports just those trees (published-or-draft — an API-born
    lens is usually a draft) plus their connection declarations; bare export
    stays full-project and now carries every connection declaration."""
    _oid, headers = org
    files = _project_files()
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})
    with org_session(_oid) as s:
        lens_store.create_lens(s, _api_born_bundle("wizard_born"))  # draft-only
        s.commit()

    out = client.get("/mgmt/project/export?lens=wizard_born", headers=headers).json()
    assert out["files"] and all(p.startswith("lenses/wizard_born/") for p in out["files"])
    assert "lenses/wizard_born/lens.yaml" in out["files"]
    # the lens declares jaffle → its declaration rides along; config, no secret
    assert out["connections"] == [
        {
            "name": "jaffle",
            "type": "duckdb",
            "config": {"path": settings.duckdb_jaffle_path},
            "has_secret": False,
        }
    ]

    assert client.get("/mgmt/project/export?lens=nope", headers=headers).status_code == 404

    bare = client.get("/mgmt/project/export", headers=headers).json()
    assert any(p.startswith("semantic/") for p in bare["files"])
    assert any(p.startswith("lenses/customer_value/") for p in bare["files"])
    assert {c["name"] for c in bare["connections"]} == {"jaffle"}


def test_cli_export_scoped_snippet_never_touches_dst_yaml(monkeypatch, capsys, tmp_path):
    import httpx

    payload = {
        "files": {"lenses/wizard_born/lens.yaml": "name: wizard_born\n"},
        "connections": [
            {
                "name": "warehouse-prod",
                "type": "bigquery",
                "config": {"project": "acme"},
                "has_secret": True,
            }
        ],
    }
    seen: dict[str, object] = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["params"] = params
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    authored = "# my comments live here\nconnections: {}\n"
    (tmp_path / "dst.yaml").write_text(authored, encoding="utf-8")
    argv = ["export", "--dir", str(tmp_path), "--lens", "wizard_born", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["params"] == [("lens", "wizard_born")]
    tree = tmp_path / "lenses" / "wizard_born" / "lens.yaml"
    assert tree.read_text(encoding="utf-8") == "name: wizard_born\n"
    # the authored dst.yaml is NEVER auto-edited — the snippet goes to stdout
    assert (tmp_path / "dst.yaml").read_text(encoding="utf-8") == authored
    out = capsys.readouterr().out
    assert "merge into dst.yaml yourself" in out
    assert "type: bigquery" in out and "config: {project: acme}" in out
    assert "secret_env: DST_API_KEY_WAREHOUSE_PROD" in out
    # export output ENDS with the adoption summary — absolute path, so scrollback
    # names the real destination
    summary = (
        f"wrote {tmp_path.resolve()}/lenses/wizard_born/ — commit it; future applies now govern it"
    )
    assert out.strip().endswith(summary)


_ENTITY_YAML = "name: customer_nodes\nsource:\n  connection: bank\n  table: spider.customer_nodes\n"


def _export_payload() -> dict[str, object]:
    """What the server renders back: every asset at its CANONICAL path — the
    slug's `customer-nodes.yaml`, the unfoldered `month-end-balance.md`."""
    return {
        "files": {
            "semantic/entities/customer-nodes.yaml": _ENTITY_YAML,
            "semantic/definitions/month-end-balance.md": (
                "---\nmetric: month_end_balance\n---\n\nBalance at month end, net.\n"
            ),
        }
    }


def _authored_project(tmp_path) -> None:
    """The same two assets as this project actually authors them: one named after
    the term (underscored), one foldered."""
    entities = tmp_path / "semantic" / "entities"
    entities.mkdir(parents=True)
    (entities / "customer_nodes.yaml").write_text(_ENTITY_YAML, encoding="utf-8")
    folder = tmp_path / "semantic" / "definitions" / "examples"
    folder.mkdir(parents=True)
    (folder / "month-end-balance.md").write_text(
        "---\nmetric: month_end_balance\n---\n\nBalance at month end.\n", encoding="utf-8"
    )


def test_cli_export_writes_into_the_files_this_project_already_authors(
    monkeypatch, capsys, tmp_path
) -> None:
    """`dst export` run inside its own project used to break it.
    Assets render at their canonical path (slug: `_` -> `-`), so exporting a
    project whose entities are underscored — or whose pages are foldered — landed
    a SECOND file per asset, and the next `dst plan` exited 1. Same shape as
    `patches approve` on the scaffold: a slug treated as an identity."""
    import httpx

    from services.project.loader import split_semantic
    from services.semantic.files import parse_semantic_files

    _authored_project(tmp_path)

    def fake_get(url, headers=None, params=None, timeout=None):
        return httpx.Response(200, json=_export_payload(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    argv = ["export", "--dir", str(tmp_path), "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0

    # the server's content landed — in the author's files, not beside them
    assert not (tmp_path / "semantic" / "entities" / "customer-nodes.yaml").exists()
    assert not (tmp_path / "semantic" / "definitions" / "month-end-balance.md").exists()
    nested = tmp_path / "semantic" / "definitions" / "examples" / "month-end-balance.md"
    assert "net." in nested.read_text(encoding="utf-8")
    assert "customer-nodes.yaml -> semantic/entities/customer_nodes.yaml" in capsys.readouterr().out

    # and the tree still plans — two pages for one asset is what plan exits 1 on
    files = {
        p.relative_to(tmp_path).as_posix(): p.read_text(encoding="utf-8")
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    parse_semantic_files(split_semantic(files))


def test_cli_export_names_the_comments_it_would_drop_before_dropping_them(
    monkeypatch, capsys, tmp_path
) -> None:
    """Export rewrites files from server state, and the server stores no comments
    — the provenance header of a certified_answers.yaml went silently. Silence is
    the defect: name them first and take y/N (`--yes` headless), the `lens rm`
    idiom. Refusing must leave the file untouched, not half-written."""
    import httpx

    _authored_project(tmp_path)
    authored = tmp_path / "semantic" / "entities" / "customer_nodes.yaml"
    authored.write_text(
        "# nodes are the region bridge - see LEARNINGS.md\n" + _ENTITY_YAML, encoding="utf-8"
    )

    def fake_get(url, headers=None, params=None, timeout=None):
        return httpx.Response(200, json=_export_payload(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    argv = ["export", "--dir", str(tmp_path), "--token", "dstadm_t"]
    # not a tty (pytest), no --yes: refuse, and write NOTHING
    assert _run_cli(monkeypatch, argv) == 1
    out = capsys.readouterr().out
    assert "the server stores no comments" in out
    assert "semantic/entities/customer_nodes.yaml: 1 comment line(s)" in out
    assert authored.read_text(encoding="utf-8").startswith("# nodes are the region bridge")

    assert _run_cli(monkeypatch, [*argv, "--yes"]) == 0
    assert authored.read_text(encoding="utf-8") == _ENTITY_YAML


def test_cli_export_asks_nothing_when_there_are_no_comments_to_lose(
    monkeypatch, capsys, tmp_path
) -> None:
    """A tree with nothing to lose must not grow a prompt — the gate is consent
    for a real loss, not a new step in every scripted export."""
    import httpx

    _authored_project(tmp_path)

    def fake_get(url, headers=None, params=None, timeout=None):
        return httpx.Response(200, json=_export_payload(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert _run_cli(monkeypatch, ["export", "--dir", str(tmp_path), "--token", "dstadm_t"]) == 0
    assert "the server stores no comments" not in capsys.readouterr().out


@needs_db
def test_lens_delete_cascades_and_fileless_apply_never_resurrects(org, monkeypatch) -> None:
    """Deleting a lens takes its name-keyed rows with it (no FK does),
    and applies that don't carry the lens never bring it back."""
    from services.contracts.fakes import HashEmbedder
    from services.project import apply as apply_engine

    _oid, headers = org
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))  # claim cleanly with HashEmbedder
    admin.dispose()
    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    files = _project_files()
    for path, content in render_lens_repo(
        jaffle_customer_value_bundle(), certified_answers=[_ANSWER]
    ).items():
        files[f"lenses/customer_value/{path}"] = content
    files["lenses/customer_value/evals/cases.yaml"] = yaml.safe_dump(
        [{"question": "How many orders shipped?", "expected_sql": "SELECT 1", "status": "approved"}]
    )
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})

    assert client.delete("/mgmt/lenses/customer_value", headers=headers).status_code == 204
    with org_session(_oid) as s:
        for table in ("lens_version", "certified_answer", "eval_case"):
            left = s.execute(
                text(f"SELECT count(*) FROM {table} WHERE lens = 'customer_value'")
            ).scalar()
            assert left == 0, f"{table} rows survived the delete"

    # A fileless apply, then a semantic-only push: neither resurrects the lens.
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": {}}).json()
    assert not any(e.get("lens") == "customer_value" for e in out)
    push = {"semantic/entities/customers.yaml": files["semantic/entities/customers.yaml"]}
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": push}).json()
    assert not any(e.get("lens") == "customer_value" for e in out)
    with org_session(_oid) as s:
        assert not lens_store.lens_exists(s, "customer_value")


def test_cli_lens_rm_prints_cascade_and_requires_confirmation(monkeypatch, capsys) -> None:
    import httpx

    listing = {
        "/versions": [{"version": 1}, {"version": 2}],
        "/certified": [{"id": "c1"}],
        "/evals/cases": [{"id": "e1"}, {"id": "e2"}, {"id": "e3"}],
    }
    deleted: list[str] = []

    def fake_get(url, headers=None, timeout=None):
        suffix = url.split("customer_value", 1)[1]
        return httpx.Response(200, json=listing[suffix], request=httpx.Request("GET", url))

    def fake_delete(url, headers=None, timeout=None):
        deleted.append(url)
        return httpx.Response(204, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "delete", fake_delete)

    # Headless without --yes: refuse, nothing deleted (pytest stdin is no tty).
    argv = ["lens", "rm", "customer_value", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 1
    assert deleted == []
    captured = capsys.readouterr()
    assert "--yes" in captured.err
    assert "lens versions: 2, certified answers: 1, eval cases: 3" in captured.out

    # --yes deletes, cascade printed first.
    assert _run_cli(monkeypatch, [*argv, "--yes"]) == 0
    assert deleted == ["http://localhost:8000/mgmt/lenses/customer_value"]
    out = capsys.readouterr().out
    assert "deleting lens 'customer_value' also deletes" in out
    assert "removed lens 'customer_value'" in out


def test_cli_lens_rm_interactive_abort(monkeypatch, capsys) -> None:
    import sys
    import types

    import httpx

    def fake_get(url, headers=None, timeout=None):
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    def fake_delete(url, headers=None, timeout=None):
        raise AssertionError("delete must not run on an aborted confirmation")

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "delete", fake_delete)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert _run_cli(monkeypatch, ["lens", "rm", "x", "--token", "dstadm_t"]) == 1
    assert "aborted — nothing deleted" in capsys.readouterr().out


@needs_db
def test_concurrent_apply_gets_409(org) -> None:
    from services.api.mgmt_project import _apply_lock_key

    oid, headers = org
    admin = create_engine(settings.database_admin_url)
    with admin.connect() as c, c.begin():
        c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _apply_lock_key(oid)})
        r = client.post("/mgmt/project/apply", headers=headers, json={"files": {}})
        assert r.status_code == 409
        assert r.json()["detail"] == "another apply is in progress for this org — retry shortly"
    admin.dispose()
    # The other transaction ended → the xact lock is gone and apply proceeds.
    r = client.post("/mgmt/project/apply", headers=headers, json={"files": {}})
    assert r.status_code == 200


# ── the semantic rm + apply CLI surfaces (httpx mocked — no server needed) ───


def _run_cli(monkeypatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def test_cli_semantic_rm(monkeypatch, capsys) -> None:
    import httpx

    seen: dict[str, str] = {}

    def fake_delete(url, headers=None, timeout=None):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        return httpx.Response(204, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(httpx, "delete", fake_delete)
    assert _run_cli(monkeypatch, ["semantic", "rm", "entity", "orders", "--token", "dstadm_t"]) == 0
    assert seen["url"] == "http://localhost:8000/mgmt/semantic/entity/orders"
    assert seen["auth"] == "Bearer dstadm_t"
    assert capsys.readouterr().out.strip() == "removed entity 'orders'"


def test_cli_semantic_rm_surfaces_dependent_lens_409(monkeypatch, capsys) -> None:
    import httpx

    detail = (
        "entity 'orders' is selected by published lens(es): board_pack"
        " — deselect it there (edit lens.yaml + apply) first"
    )

    def fake_delete(url, headers=None, timeout=None):
        return httpx.Response(409, json={"detail": detail}, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(httpx, "delete", fake_delete)
    assert _run_cli(monkeypatch, ["semantic", "rm", "entity", "orders", "--token", "dstadm_t"]) == 1
    assert detail in capsys.readouterr().err


def test_cli_plan_renders_server_only_section(monkeypatch, capsys, tmp_path) -> None:
    import httpx

    note = (
        "lens 'wizard_born': on the server but not in the push — adopt it "
        "(dst export --lens wizard_born) or leave it for its owner; "
        "delete only explicitly (dst lens rm wizard_born)"
    )
    rows = [{"scope": "server_only", "kind": "lens", "name": "wizard_born", "note": note}]

    def fake_post(url, headers=None, json=None, timeout=None):
        return httpx.Response(200, json=rows, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    # plan/apply refuse a directory with no project (tests/test_cli_no_project.py)
    (tmp_path / "dst.yaml").write_text("name: t\n", encoding="utf-8")
    assert _run_cli(monkeypatch, ["plan", "--dir", str(tmp_path), "--token", "dstadm_t"]) == 0
    out = capsys.readouterr().out
    assert f"server-only: {note}" in out  # verbatim — the note IS the adoption pointer
    assert "Plan:" in out  # the terraform-style counts line


def test_cli_apply_surfaces_lock_409(monkeypatch, capsys, tmp_path) -> None:
    import httpx

    detail = "another apply is in progress for this org — retry shortly"

    def fake_post(url, headers=None, params=None, json=None, timeout=None):
        return httpx.Response(409, json={"detail": detail}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    # plan/apply refuse a directory with no project (tests/test_cli_no_project.py)
    (tmp_path / "dst.yaml").write_text("name: t\n", encoding="utf-8")
    assert _run_cli(monkeypatch, ["apply", "--dir", str(tmp_path), "--token", "dstadm_t"]) == 1
    assert detail in capsys.readouterr().err


def test_cli_apply_aborted_exits_1_and_prints_the_abort(monkeypatch, capsys, tmp_path) -> None:
    """An aborted apply must exit 1 and shout the abort row + every
    error AFTER the JSON dump — an agent reading the tail sees the verdict."""
    import httpx

    rows = [
        {"scope": "semantic", "applied": [], "warnings": [], "errors": []},
        {
            "lens": "customer_value",
            "action": "rejected",
            "errors": [
                "eval gate blocked publish: accuracy regressed (score 0.5 < prev 0.9 — failing: c1)"
            ],
        },
        {
            "scope": "apply",
            "action": "aborted",
            "detail": "nothing deployed — fix the errors and re-apply",
        },
    ]

    def fake_post(url, headers=None, params=None, json=None, timeout=None):
        return httpx.Response(200, json=rows, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    # plan/apply refuse a directory with no project (tests/test_cli_no_project.py)
    (tmp_path / "dst.yaml").write_text("name: t\n", encoding="utf-8")
    assert _run_cli(monkeypatch, ["apply", "--dir", str(tmp_path), "--token", "dstadm_t"]) == 1
    captured = capsys.readouterr()
    assert "APPLY ABORTED — nothing deployed — fix the errors and re-apply" in captured.err
    assert "customer_value: eval gate blocked publish" in captured.err


def test_cli_apply_green_exits_0(monkeypatch, capsys, tmp_path) -> None:
    import httpx

    rows = [
        {
            "lens": "customer_value",
            "action": "created",
            "version": 1,
            "errors": [],
            "warnings": ["eval_gate: warn configured but no approved eval cases — gate SKIPPED"],
            "applied": [],
        }
    ]

    def fake_post(url, headers=None, params=None, json=None, timeout=None):
        return httpx.Response(200, json=rows, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    # plan/apply refuse a directory with no project (tests/test_cli_no_project.py)
    (tmp_path / "dst.yaml").write_text("name: t\n", encoding="utf-8")
    assert _run_cli(monkeypatch, ["apply", "--dir", str(tmp_path), "--token", "dstadm_t"]) == 0
    captured = capsys.readouterr()
    assert "ABORTED" not in captured.err  # warnings never abort


def test_cli_apply_prints_row_warnings_the_starvation_pin(monkeypatch, capsys, tmp_path) -> None:
    """The CLI-VISIBLE surface: a lens row's warnings —
    the warn-mode starvation warning here — must reach the terminal on
    `dst apply`. The server emitting it is not enough; this pins whatever
    renderer _apply uses to keep printing warnings."""
    import httpx

    starved = (
        "eval gate starved: this apply retires the last active certified "
        "answer while eval_gate: warn — published anyway, but the certified "
        "gate now has nothing to test (gate SKIPPED for certified coverage); "
        "certify a replacement to restore it"
    )
    rows = [
        {
            "lens": "customer_value",
            "action": "updated",
            "version": 7,
            "errors": [],
            "warnings": [starved],
            "applied": ["certified answers: created 0, updated 1, unchanged 0"],
        }
    ]

    def fake_post(url, headers=None, params=None, json=None, timeout=None):
        return httpx.Response(200, json=rows, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    # plan/apply refuse a directory with no project (tests/test_cli_no_project.py)
    (tmp_path / "dst.yaml").write_text("name: t\n", encoding="utf-8")
    assert _run_cli(monkeypatch, ["apply", "--dir", str(tmp_path), "--token", "dstadm_t"]) == 0
    assert starved in capsys.readouterr().out  # the user SEES the starvation warning


@needs_db
def test_apply_connections_probes_before_landing(org):
    """A dead credential/config in dst.yaml must never replace a working
    stored connection: the connectivity probe runs BEFORE the upsert, failures
    come back as errors, prior state stays."""
    from services.project.apply import apply_connections
    from services.project.schema import ProjectConfig

    oid, _ = org
    with org_session(oid) as s:
        bad = ProjectConfig.model_validate(
            {
                "connections": {
                    "jaffle": {"type": "duckdb", "config": {"path": "/nowhere/nope.duckdb"}},
                    "fresh": {"type": "duckdb", "config": {"path": "/nowhere/nope2.duckdb"}},
                }
            }
        )
        applied, _warnings, errors, _caps = apply_connections(s, bad)
        assert applied == []
        assert len(errors) == 2 and all("NOT applied" in e for e in errors)
        stored = connection_store.get_connection(s, "jaffle")
        assert stored is not None and stored.config["path"] == settings.duckdb_jaffle_path
        assert connection_store.get_connection(s, "fresh") is None

        good = ProjectConfig.model_validate(
            {
                "connections": {
                    "fresh": {"type": "duckdb", "config": {"path": settings.duckdb_jaffle_path}}
                }
            }
        )
        applied2, _warnings2, errors2, caps2 = apply_connections(s, good)
        assert applied2 == ["created 'fresh' (duckdb)"] and errors2 == []
        # The probed connection reports its capabilities —
        # duckdb: query works, no history catalog exists.
        assert caps2 == ["fresh: read ✓ · query ✓ · query history ✓"]


def test_plan_semantic_matches_foldered_files_by_identity():
    """Folders under semantic/ are organization only: a foldered file whose
    asset matches the DB's canonical flat render plans as unchanged, never as a
    phantom create."""
    from services.lenses.demo import jaffle_shared_assets
    from services.project.plan import plan_semantic
    from services.semantic.files import render_semantic_files

    entities, definitions = jaffle_shared_assets()
    db_files = render_semantic_files(entities, definitions)
    orders = db_files["semantic/entities/orders.yaml"]
    plans = plan_semantic(db_files, {"semantic/entities/sales/orders.yaml": orders})
    assert [(p.path, p.status) for p in plans] == [
        ("semantic/entities/sales/orders.yaml", "unchanged")
    ]


def test_plan_lens_yaml_with_scaffold_comments_is_unchanged():
    """The scaffolded lens.yaml carries an appended reference-comment block that
    never round-trips through the server — plan must canonicalize it away, or
    the first plan after the first apply shows a permanent phantom update."""
    import yaml as _yaml

    from services.lenses.demo import jaffle_customer_value_bundle
    from services.project.plan import plan_lenses

    config = jaffle_customer_value_bundle().config
    canonical = _yaml.safe_dump(
        config.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
    )
    scaffolded = canonical + "\n# -- Reference: every lens field ----\n# name: <required>\n"
    plans = plan_lenses(
        {"customer_value": {"lens.yaml": canonical}},
        {"customer_value": {"lens.yaml": scaffolded}},
    )
    assert [(p.lens, p.status) for p in plans] == [("customer_value", "unchanged")]


def test_plan_scaffolded_empty_evals_file_is_unchanged():
    """The scaffold ships evals/cases.yaml as a comment header + [] and the DB
    side now always renders the file, even empty (the certified_answers.yaml
    contract) — the first plan after the first apply must not phantom-diff."""
    from services.project.plan import plan_lenses

    scaffolded = "# Eval cases - inputs to the publish gate.\n# - question: ...\n[]\n"
    plans = plan_lenses(
        {"customer_value": {"evals/cases.yaml": "[]\n"}},
        {"customer_value": {"evals/cases.yaml": scaffolded}},
    )
    assert [(p.lens, p.status) for p in plans] == [("customer_value", "unchanged")]


def test_plan_never_diffs_server_stamped_certified_provenance():
    """The DB render stamps created_by/created_at onto certified pairs; an
    authored file never carries them — the plan must not present the server's
    stamps as the user's removals. A real edit still diffs, and its
    diff carries no stamp lines."""
    from services.project.plan import plan_lenses

    db_tree = {"certified_answers.yaml": _render()["certified_answers.yaml"]}
    authored = yaml.safe_dump(
        [{"question": _ANSWER.question, "sql": _ANSWER.sql, "verified_value": {"value": 42}}],
        sort_keys=False,
    )
    plans = plan_lenses(
        {"customer_value": db_tree}, {"customer_value": {"certified_answers.yaml": authored}}
    )
    assert [(p.lens, p.status) for p in plans] == [("customer_value", "unchanged")]
    edited = authored.replace("COUNT(*)", "COUNT(1)")
    plan = plan_lenses(
        {"customer_value": db_tree}, {"customer_value": {"certified_answers.yaml": edited}}
    )[0]
    assert plan.status == "update" and "COUNT(1)" in plan.diffs[0].diff
    assert "created_by" not in plan.diffs[0].diff and "created_at" not in plan.diffs[0].diff


def test_plan_certified_status_default_round_trips():
    """The scaffold teaches `status: active` explicitly; the DB render elides
    the default (status renders only when retired) — an applied file must not
    phantom-diff `+status: active` forever. A retire edit
    still diffs, and a retired pair round-trips byte-identically."""
    from dataclasses import replace

    from services.project.plan import plan_lenses

    db_tree = {"certified_answers.yaml": _render()["certified_answers.yaml"]}
    authored = yaml.safe_dump(
        [
            {
                "question": _ANSWER.question,
                "sql": _ANSWER.sql,
                "verified_value": {"value": 42},
                "status": "active",
            }
        ],
        sort_keys=False,
    )
    plans = plan_lenses(
        {"customer_value": db_tree}, {"customer_value": {"certified_answers.yaml": authored}}
    )
    assert [(p.lens, p.status) for p in plans] == [("customer_value", "unchanged")]
    retired = authored.replace("status: active", "status: retired")
    plan = plan_lenses(
        {"customer_value": db_tree}, {"customer_value": {"certified_answers.yaml": retired}}
    )[0]
    assert plan.status == "update" and "+  status: retired" in plan.diffs[0].diff
    retired_db = render_lens_repo(
        jaffle_customer_value_bundle(), certified_answers=[replace(_ANSWER, status="retired")]
    )
    plans = plan_lenses(
        {"customer_value": {"certified_answers.yaml": retired_db["certified_answers.yaml"]}},
        {"customer_value": {"certified_answers.yaml": retired}},
    )
    assert [(p.lens, p.status) for p in plans] == [("customer_value", "unchanged")]


def test_plan_never_diffs_unpushed_review_answers_as_removals():
    """Plan side: review-approved answers (source review:*)
    are server-origin — a pushed certified_answers.yaml that doesn't carry them
    is the normal idiom, not a removal (apply keeps them). File-originated
    entries absent from the push still diff: apply deletes them now."""
    from dataclasses import replace

    from services.project.plan import plan_lenses

    review_born = replace(
        _ANSWER, id="ca_2", question="Review-born?", source="review:req_9", verified_value=None
    )
    db_tree = render_lens_repo(
        jaffle_customer_value_bundle(), certified_answers=[_ANSWER, review_born]
    )
    authored = yaml.safe_dump(
        [{"question": _ANSWER.question, "sql": _ANSWER.sql, "verified_value": {"value": 42}}],
        sort_keys=False,
    )
    plans = plan_lenses(
        {"customer_value": {"certified_answers.yaml": db_tree["certified_answers.yaml"]}},
        {"customer_value": {"certified_answers.yaml": authored}},
    )
    assert [(p.lens, p.status) for p in plans] == [("customer_value", "unchanged")]

    # An empty push still shows the FILE-ORIGIN removal (a truthful promise —
    # apply deletes it) and never mentions the review-born answer.
    plan = plan_lenses(
        {"customer_value": {"certified_answers.yaml": db_tree["certified_answers.yaml"]}},
        {"customer_value": {"certified_answers.yaml": "[]\n"}},
    )[0]
    assert plan.status == "update"
    diff = plan.diffs[0].diff
    assert f"-- question: {_ANSWER.question}" in diff or _ANSWER.question in diff
    assert "Review-born?" not in diff


def test_plan_skips_certified_when_the_push_carries_no_file():
    """A tree without certified_answers.yaml leaves the surface unmanaged —
    apply deletes nothing, so a removal diff would be a plan/apply lie."""
    from services.project.plan import plan_lenses

    db_tree = _render()
    incoming = {p: c for p, c in db_tree.items() if p != "certified_answers.yaml"}
    plans = plan_lenses({"customer_value": db_tree}, {"customer_value": incoming})
    assert [(p.lens, p.status) for p in plans] == [("customer_value", "unchanged")]


def test_loader_distinguishes_absent_file_from_empty_file():
    """None = no certified_answers.yaml in the tree (apply must not delete);
    [] = present but empty (files win — file-origin rows delete on apply).
    Comment-only (the scaffold shape since the `[]` placeholder died) is
    present-but-empty too — the same [] plan's canonicalizer parses it to, so
    plan and apply keep converging."""
    files = _render()
    assert load_lens_source(files).certified_answers  # entries load
    files["certified_answers.yaml"] = "[]\n"
    assert load_lens_source(files).certified_answers == []
    files["certified_answers.yaml"] = "# a comment-only file - the scaffold shape\n"
    assert load_lens_source(files).certified_answers == []
    del files["certified_answers.yaml"]
    assert load_lens_source(files).certified_answers is None


def test_malformed_list_file_raises_the_file_and_position_not_a_parser_error():
    """The trap: an entry APPENDED after a live `[]`. yaml's
    ParserError is not a ValueError, so it sailed past the plan/apply catches
    and became a raw 500 ("check serve.log"). The parse seam (loader.parse_yaml)
    turns it into the actionable contract: file, position, problem."""
    import re

    for path in ("certified_answers.yaml", "evals/cases.yaml"):
        files = _render()
        files[path] = "[]\n- question: appended\n  sql: SELECT 1\n"
        with pytest.raises(ValueError, match=re.escape(path) + r": invalid YAML at line 2"):
            load_lens_source(files)


def test_malformed_lens_queries_and_project_yaml_name_their_file():
    """Same seam, the other managed YAML files."""
    files = _render()
    files["queries.yaml"] = "use_when: [unclosed"
    with pytest.raises(ValueError, match=r"queries\.yaml: invalid YAML"):
        load_lens_source(files)
    files = _render()
    files["lens.yaml"] = "name: [unclosed"
    with pytest.raises(ValueError, match=r"lens\.yaml: invalid YAML"):
        load_lens_source(files)

    from services.project.schema import parse_project_yaml

    with pytest.raises(ValueError, match=r"dst\.yaml: invalid YAML"):
        parse_project_yaml("connections: [unclosed")


def test_plan_list_entry_order_is_authoring_sugar():
    """The DB serialized certified answers newest-first while
    the file kept author order — a by-the-book retire left an order-only diff
    forever. Entries compare sorted by question; a real content edit still
    diffs."""
    from dataclasses import replace

    from services.project.plan import plan_lenses

    second = replace(
        _ANSWER, id="ca_2", question="A second question?", sql="SELECT 2", created_at="2026-07-02"
    )
    db_tree = render_lens_repo(jaffle_customer_value_bundle(), certified_answers=[second, _ANSWER])
    authored = yaml.safe_dump(
        [
            {"question": _ANSWER.question, "sql": _ANSWER.sql, "verified_value": {"value": 42}},
            {"question": second.question, "sql": second.sql, "verified_value": {"value": 42}},
        ],
        sort_keys=False,
    )
    plans = plan_lenses(
        {"customer_value": {"certified_answers.yaml": db_tree["certified_answers.yaml"]}},
        {"customer_value": {"certified_answers.yaml": authored}},
    )
    assert [(p.lens, p.status) for p in plans] == [("customer_value", "unchanged")]
    edited = authored.replace("SELECT 2", "SELECT 3")
    plan = plan_lenses(
        {"customer_value": {"certified_answers.yaml": db_tree["certified_answers.yaml"]}},
        {"customer_value": {"certified_answers.yaml": edited}},
    )[0]
    assert plan.status == "update" and "SELECT 3" in plan.diffs[0].diff


def test_plan_eval_case_source_authored_round_trips():
    """The server stamps `source: authored` onto hand-written
    eval cases, so landing ANY case left the plan dirty until the author copied
    the stamp in. The default elides from both sides; a non-default source
    (review:<id>) still compares."""
    from services.project.plan import plan_lenses

    db_cases = yaml.safe_dump(
        [{"question": "avg value?", "expect": "clarify", "source": "authored"}], sort_keys=False
    )
    authored = yaml.safe_dump([{"question": "avg value?", "expect": "clarify"}], sort_keys=False)
    plans = plan_lenses(
        {"customer_value": {"evals/cases.yaml": db_cases}},
        {"customer_value": {"evals/cases.yaml": authored}},
    )
    assert [(p.lens, p.status) for p in plans] == [("customer_value", "unchanged")]
    from_review = db_cases.replace("source: authored", "source: review:tk_1")
    plan = plan_lenses(
        {"customer_value": {"evals/cases.yaml": from_review}},
        {"customer_value": {"evals/cases.yaml": authored}},
    )[0]
    assert plan.status == "update"


@needs_db
def test_sample_embedding_stale_definition_aborts_the_apply(org) -> None:
    """The third shadowing vector: a definition's sql_expr changes but a sample
    query still embeds the OLD expression — the exemplar keeps steering both
    serving and the eval runner to retired logic. Under blue/green this is an
    error that aborts, naming both."""
    _oid, headers = org
    files = _project_files()
    # The scaffold sample is definition-free by design now — this test plants
    # one that embeds the definition's logic, the vector under test.
    files["lenses/customer_value/queries.yaml"] = yaml.safe_dump(
        {
            "use_when": [],
            "sample_queries": [
                {
                    "question": "How many customers are repeat customers?",
                    "sql": "SELECT count(*) AS n FROM customers WHERE number_of_orders > 1",
                }
            ],
        },
        sort_keys=False,
    )
    client.post("/mgmt/project/apply", headers=headers, json={"files": files})

    # Change the definition; the planted sample still says number_of_orders > 1.
    files["semantic/definitions/repeat-customer.md"] = files[
        "semantic/definitions/repeat-customer.md"
    ].replace("number_of_orders > 1", "number_of_orders > 90")
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert any(
        "embeds the PREVIOUS logic of definition 'repeat_customer'" in e for e in row["errors"]
    )
    assert any(e.get("action") == "aborted" for e in out)

    # Updating the sample in the same push lands everything together.
    files["lenses/customer_value/queries.yaml"] = files[
        "lenses/customer_value/queries.yaml"
    ].replace("number_of_orders > 1", "number_of_orders > 90")
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    row = next(e for e in out if e.get("lens") == "customer_value")
    assert not row["errors"] and not any(e.get("action") == "aborted" for e in out)


@needs_db
def test_apply_names_entities_whose_names_are_sql_keywords(org) -> None:
    """`order` is one of the commonest table names there is. Apply created
    `entity/order` without a word, and every answer through it then died on a
    warehouse parser error. dst quotes the name now — and says so, because the
    author's own hand-written SQL against that lens needs the same quoting."""
    _oid, headers = org
    files = _project_files()
    files["semantic/entities/order.yaml"] = yaml.safe_dump(
        {
            "name": "order",
            "source": {"connection": "jaffle", "table": "orders"},
            "fields": [{"name": "order_id", "type": "string"}],
        },
        sort_keys=False,
    )
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    semantic = next(e for e in out if e.get("scope") == "semantic" and "applied" in e)
    assert "created entity/order" in semantic["applied"]
    warning = next(w for w in semantic["warnings"] if "'order'" in w)
    assert "SQL keyword" in warning and "quotes it" in warning
    # …and it is a NOTE, not a rejection: the author cannot rename the table.
    assert not any(e.get("errors") for e in out)


def test_only_keyword_names_are_flagged() -> None:
    from services.project.apply import _reserved_name_warnings

    assert _reserved_name_warnings(["customers", "orders", "order_items"]) == []
    assert len(_reserved_name_warnings(["order", "select", "customers"])) == 2
