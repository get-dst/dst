"""A lens exposes tables that have no definitions — declared as a grant,
resolved at apply from the connection's profiles, ungoverned on every rail.

Pinned: the grant resolves from profiles (a pattern matching nothing and a
bare-name collision both fail apply, by name); `hide` withholds a column and
the guard refuses it as out of scope; `SELECT *` passes only on an
exposed-only statement with nothing hidden; both generation prompts list the
table with the no-definitions line; the typed resolver never offers it; the
router takes no anchor from it; an answer that reads it carries the EXPOSED
line and a tag that is never declared; `describe_lens` lists it apart from the
entities; plan prints the count and the untyped_fallback warning; and the drift
audit names the lens whose grant a dropped table breaks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.auth.tokens import hash_token, new_admin_token
from services.config import settings
from services.contracts.fakes import EchoQueryGenerator, ScriptedDecider
from services.contracts.lens_config import LensConfig
from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.protocols import Decision
from services.contracts.semantic_model import (
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.contracts.shared_semantic import SelectEntity, SelectSpec, SharedEntity
from services.db.session import org_session
from services.lenses import connection_store, profile_store
from services.lenses.demo import (
    jaffle_customer_value,
    jaffle_customer_value_bundle,
    jaffle_shared_assets,
)
from services.lenses.describe import describe_model
from services.lenses.repo import render_lens_repo
from services.lenses.store import LensBundle, load_published_lens
from services.project import warehouse_drift as wd
from services.project.compile import CompileError, compile_lens_model
from services.project.expose import UNTYPED_WARNING, expose_summary, resolve_expose
from services.project.loader import load_lens_source
from services.project.probe import ProbeArtifact
from services.router.profiles import coverage_profile
from services.runtime import resolution, sql_guard
from services.runtime.generator import serialize_model
from services.runtime.intent_generator import serialize_layer
from services.runtime.pipeline import run_query
from services.runtime.typed_resolver import TypedResolver
from services.semantic.files import render_semantic_files
from tests.test_pipeline import _conn

client = TestClient(app)

WH = "warehouse"
NOW = datetime(2026, 10, 15, tzinfo=UTC)


def _profile(
    table: str, columns: list[tuple[str, str]], description: str | None = None
) -> TableProfile:
    return TableProfile(
        connection=WH,
        table=table,
        description=description,
        profiled_at=NOW,
        columns=[ColumnProfile(name=n, type=t) for n, t in columns],
    )


def _profiles() -> dict[str, list[TableProfile]]:
    return {
        WH: [
            _profile(
                "staging.orders",
                [("order_id", "BIGINT"), ("amount", "NUMERIC"), ("placed_at", "TIMESTAMP")],
                description="raw orders as loaded",
            ),
            _profile(
                "staging.customers",
                [("customer_id", "BIGINT"), ("email", "VARCHAR"), ("ssn", "VARCHAR")],
            ),
            _profile("analytics.dim_region", [("region_id", "INTEGER"), ("name", "TEXT")]),
        ]
    }


def _shared() -> dict[str, SharedEntity]:
    return {
        "orders": SharedEntity(
            name="orders",
            source=EntitySource(connection=WH, table="marts.orders"),
            fields=[Field(name="order_id", type="integer"), Field(name="amount", type="number")],
            metrics=[Metric(name="revenue", agg="sum", expr="orders.amount")],
            common_questions=["what is revenue?"],
        )
    }


def _config(
    tables: list[str], hide: list[str] | None = None, select: list[str] | None = None, **kw: object
) -> LensConfig:
    return LensConfig(
        name="ops",
        connections=[WH],
        select=SelectSpec(
            entities=[SelectEntity(name=n) for n in (["orders"] if select is None else select)]
        ),
        expose=[{"connection": WH, "tables": tables, "hide": hide or []}],  # type: ignore[list-item]
        **kw,  # type: ignore[arg-type]
    )


def _compile(config: LensConfig) -> SemanticModel:
    model, _warnings = compile_lens_model(
        config=config,
        shared_entities=_shared(),
        shared_definitions={},
        shared_relationships={},
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
        profiles=_profiles(),
    )
    return model


def _model() -> SemanticModel:
    # `staging.customers` only: `staging.*` would also match `staging.orders`,
    # whose bare name collides with the selected `orders` (pinned below).
    return _compile(_config(["staging.customers"], hide=["*.ssn"]))


# ── resolution at apply ──────────────────────────────────────────────────────


def test_the_grant_resolves_from_profiles_into_a_flagged_entity() -> None:
    model = _model()
    assert sorted(e.name for e in model.entities) == ["customers", "orders"]
    customers = next(e for e in model.entities if e.name == "customers")
    assert customers.exposed and customers.source.table == "staging.customers"
    assert customers.metrics == [] and customers.dimensions == []
    assert customers.default_time_field is None
    orders = next(e for e in model.entities if e.name == "orders")
    assert not orders.exposed and orders.metrics  # the declared entity is untouched


def test_an_exposed_entity_carries_the_profile_description_and_semantic_types() -> None:
    model = _compile(_config(["staging.orders"], select=[]))
    (orders,) = model.entities
    assert orders.exposed and orders.exposed_whole
    assert orders.description == "raw orders as loaded"
    assert [(f.name, f.type) for f in orders.fields] == [
        ("order_id", "integer"),
        ("amount", "number"),
        ("placed_at", "timestamp"),
    ]


def test_a_pattern_matching_nothing_fails_compile_naming_the_pattern() -> None:
    with pytest.raises(CompileError) as exc:
        _compile(_config(["staging.customers", "nowhere.*"]))
    message = str(exc.value)
    assert "'nowhere.*'" in message and "dst probe" in message


def test_a_connection_without_profiles_says_so() -> None:
    entities, errors = resolve_expose(_config(["staging.*"]), {})
    assert not entities
    assert errors and "no stored profiles" in errors[0] and "dst probe" in errors[0]


def test_a_bare_name_collision_with_a_selected_entity_fails_compile() -> None:
    # staging.orders' bare name is `orders`, which the lens also selects.
    with pytest.raises(CompileError) as exc:
        _compile(_config(["staging.orders"]))
    assert "collides with a selected entity" in str(exc.value)


def test_two_exposed_tables_sharing_a_bare_name_fail_compile() -> None:
    profiles = {
        WH: [
            _profile("staging.customers", [("id", "INTEGER")]),
            _profile("legacy.customers", [("id", "INTEGER")]),
        ]
    }
    _entities, errors = resolve_expose(_config(["*.customers"]), profiles)
    assert errors and "share the bare name 'customers'" in errors[0]


def test_a_grant_on_a_connection_the_lens_does_not_list_is_an_error() -> None:
    config = _config(["staging.*"])
    config.connections = ["other"]
    _entities, errors = resolve_expose(config, _profiles())
    assert errors and "not in its connections" in errors[0]


def test_the_summary_counts_tables_and_columns() -> None:
    model = _model()
    assert expose_summary(model.entities) == "exposes 1 table (2 columns)"
    assert expose_summary([]) is None
    # With the profiles it resolved from, the columns `hide` withheld are counted.
    assert expose_summary(model.entities, _profiles()) == "exposes 1 table (2 columns, 1 hidden)"
    whole = _compile(_config(["analytics.dim_*"]))
    assert expose_summary(whole.entities, _profiles()) == "exposes 1 table (2 columns)"


def test_a_pattern_matching_only_a_dropped_table_names_the_table() -> None:
    # The stored profile is withheld by the caller; the error still says which
    # table the pattern used to match and why it no longer does.
    profiles = {WH: [_profile("staging.orders", [("order_id", "BIGINT")])]}
    config = _config(["staging.products"], select=[])
    _entities, errors = resolve_expose(config, profiles, dropped={WH: ["staging.products"]})
    assert len(errors) == 1
    assert "no profiled table matches" in errors[0]
    assert "staging.products was dropped upstream" in errors[0]
    assert "dst probe" in errors[0]
    # A pattern that still matches a profiled table is untouched by the list.
    _entities, errors = resolve_expose(
        _config(["staging.orders"], select=[]), profiles, dropped={WH: ["staging.products"]}
    )
    assert not errors


def test_the_loader_rejects_an_unknown_expose_key_by_name() -> None:
    with pytest.raises(ValueError) as exc:
        load_lens_source({"lens.yaml": "name: ops\nexpose:\n  - connection: wh\n    table: [a]\n"})
    assert "did you mean `tables`" in str(exc.value)


# ── the rails ────────────────────────────────────────────────────────────────


def test_hide_removes_the_column_and_the_guard_refuses_it() -> None:
    model = _model()
    customers = next(e for e in model.entities if e.name == "customers")
    assert [f.name for f in customers.fields] == ["customer_id", "email"]
    assert customers.exposed and not customers.exposed_whole
    refused = sql_guard.check("SELECT c.ssn FROM staging.customers c", model)
    assert not refused.ok and "out of lens scope" in str(refused.reason)
    admitted = sql_guard.check("SELECT c.email FROM staging.customers c", model)
    assert admitted.ok, admitted.reason


def test_a_hide_pattern_that_withholds_nothing_fails_by_name() -> None:
    # A column the author believes is hidden and is not would be served; the
    # miss fails like a table pattern matching nothing.
    _entities, errors = resolve_expose(_config(["staging.customers"], hide=["*.snn"]), _profiles())
    assert len(errors) == 1
    assert "hides '*.snn'" in errors[0] and "staging.customers" in errors[0]
    for hide in ("*.ssn", "customers.SSN", "staging.customers.ssn"):
        _entities, errors = resolve_expose(_config(["staging.customers"], hide=[hide]), _profiles())
        assert errors == [], hide
    # A table pattern that matched nothing is the error already reported.
    _entities, errors = resolve_expose(_config(["staging.nothing"], hide=["*.snn"]), _profiles())
    assert len(errors) == 1 and "no profiled table matches" in errors[0]


def test_star_passes_on_an_exposed_only_statement_with_nothing_hidden() -> None:
    model = _compile(_config(["analytics.dim_*"]))
    assert next(e for e in model.entities if e.exposed).exposed_whole
    ok = sql_guard.check("SELECT * FROM analytics.dim_region", model)
    assert ok.ok, ok.reason
    by_entity = sql_guard.check("SELECT * FROM dim_region LIMIT 5", model)
    assert by_entity.ok, by_entity.reason


def test_star_is_rewritten_into_the_allowed_columns_of_an_exposed_table() -> None:
    # The returned SQL names the columns, so what comes back is never more
    # than the lens lists — a whole table lists every profiled column.
    model = _compile(_config(["analytics.dim_*"]))
    ok = sql_guard.check("SELECT * FROM analytics.dim_region", model)
    assert ok.ok and ok.sql == "SELECT region_id, name FROM analytics.dim_region"
    qualified = sql_guard.check("SELECT r.* FROM analytics.dim_region r", model)
    assert qualified.ok and qualified.sql == (
        "SELECT r.region_id, r.name FROM analytics.dim_region AS r"
    )
    # ...and a lens with a hidden column SOMEWHERE no longer bans the star on a
    # table that hides nothing.
    model = _compile(_config(["analytics.dim_*", "staging.customers"], hide=["*.ssn"]))
    ok = sql_guard.check("SELECT * FROM analytics.dim_region", model)
    assert ok.ok and ok.sql == "SELECT region_id, name FROM analytics.dim_region"


JOINED = (
    "SELECT * FROM staging.customers c JOIN analytics.dim_region r ON r.region_id = c.customer_id"
)


def test_star_on_a_partly_hidden_table_never_carries_the_hidden_column() -> None:
    model = _compile(_config(["analytics.dim_*", "staging.customers"], hide=["*.ssn"]))
    for sql in (
        "SELECT * FROM staging.customers",
        "SELECT c.* FROM staging.customers c",
        "SELECT * FROM customers",
        # In a CTE too: the derived projection is made explicit where it is read,
        # and the outer star is expanded from it.
        "WITH s AS (SELECT * FROM staging.customers) SELECT * FROM s",
        "SELECT s.* FROM (SELECT * FROM staging.customers) s",
        JOINED,
    ):
        r = sql_guard.check(sql, model)
        assert r.ok, (sql, r.reason)
        assert r.sql is not None and "ssn" not in r.sql, r.sql
        assert "*" not in r.sql, r.sql
    # A star the rewrite cannot make explicit never reaches the caller: an
    # aggregate over a qualified star hands back every column.
    r = sql_guard.check("SELECT array_agg(c.*) FROM staging.customers c", model)
    assert not r.ok and "SELECT *" in str(r.reason)
    r = sql_guard.check("SELECT * FROM staging.customers", model)
    assert r.sql == "SELECT customer_id, email FROM staging.customers"
    r = sql_guard.check(JOINED, model)
    assert r.sql is not None and r.sql.startswith(
        "SELECT c.customer_id, c.email, r.region_id, r.name FROM"
    )


def test_star_is_refused_when_a_declared_entity_is_read() -> None:
    model = _compile(_config(["analytics.dim_*", "staging.customers"], hide=["*.ssn"]))
    for sql in (
        "SELECT * FROM analytics.dim_region r JOIN marts.orders o ON o.order_id = r.region_id",
        "SELECT r.* FROM analytics.dim_region r JOIN marts.orders o ON o.order_id = r.region_id",
        "SELECT o.* FROM analytics.dim_region r JOIN orders o ON o.order_id = r.region_id",
    ):
        r = sql_guard.check(sql, model)
        assert not r.ok and "SELECT *" in str(r.reason), sql


def test_both_generation_prompts_list_the_exposed_table_with_the_line() -> None:
    model = _model()
    raw = serialize_model(model)
    assert "staging.customers" in raw and "no governed definitions" in raw
    assert "customers.email" in raw and "ssn" not in raw
    layer = serialize_layer(model)
    assert "Entity 'customers'" in layer and "no governed definitions" in layer


def test_the_typed_resolver_never_offers_an_exposed_entity() -> None:
    model = _model()
    decider = ScriptedDecider(
        [Decision(chosen="listing", probs={"listing": 1.0}, provider="scripted")]
    )
    TypedResolver(decider, domains={}, today=NOW.date()).resolve("list customers", model)  # type: ignore[arg-type]
    offered = {name for call in decider.calls for name in call["options"]}  # type: ignore[union-attr]
    assert "customers" not in offered, offered
    # The one governed entity needs no entity decision at all.
    assert all(call["options"] != ["orders", "customers"] for call in decider.calls)


def test_a_lens_of_only_exposed_tables_declines_to_type() -> None:
    model = _model()
    model = model.model_copy(update={"entities": [e for e in model.entities if e.exposed]})
    decider = ScriptedDecider([Decision(chosen=None, probs={}, provider="scripted")])
    res = TypedResolver(decider, domains={}, today=NOW.date()).resolve("anything", model)  # type: ignore[arg-type]
    assert res.intent is None and res.decline and "exposed without definitions" in res.decline
    assert decider.calls == []


def test_router_anchors_exclude_the_exposed_table() -> None:
    model = _model()
    config = _config(["staging.customers"])
    profile = coverage_profile(LensBundle(config=config, semantic_model=model))
    assert "customers" not in profile.anchors
    assert "orders" in profile.anchors and "revenue" in profile.anchors
    # The table is still in scope: the lens does read it.
    assert "staging.customers" in profile.scope


def test_attribution_reads_an_exposed_column_as_inferred() -> None:
    model = _model()
    res = resolution.attribute(
        "SELECT c.email, count(*) AS n FROM staging.customers c GROUP BY c.email", model
    )
    assert res.tag == "inferred"
    assert all(s.source == "inferred" for s in res.slots), res.slots


def test_an_answer_reading_an_exposed_table_carries_exposed_and_is_not_declared() -> None:
    # The demo lens plus one exposed staging table of the same warehouse.
    model = jaffle_customer_value()
    exposed, errors = resolve_expose(
        LensConfig(
            name="customer_value",
            connections=["jaffle"],
            expose=[{"connection": "jaffle", "tables": ["stg_orders"]}],  # type: ignore[list-item]
        ),
        {
            "jaffle": [
                TableProfile(
                    connection="jaffle",
                    table="stg_orders",
                    columns=[
                        ColumnProfile(name="order_id", type="INTEGER"),
                        ColumnProfile(name="status", type="VARCHAR"),
                    ],
                )
            ]
        },
    )
    assert not errors
    model.entities.append(exposed[0])
    res = run_query(
        question="How many staged orders are there?",
        lens_name="customer_value",
        org_id="org-1",
        caller="analyst",
        semantic_model=model,
        connector=_conn(),
        generator=EchoQueryGenerator("SELECT count(*) AS n FROM stg_orders"),
        composer=None,
    )
    assert res.trace.status == "ok", res.trace.error
    lines = [n for n in res.response.degraded if n.startswith("EXPOSED: ")]
    assert lines == ["EXPOSED: reads stg_orders, a table with no governed definitions"], (
        res.response.degraded
    )
    assert res.response.resolution is not None
    assert res.response.resolution.tag not in ("declared", "certified")
    assert res.response.receipt is not None
    assert res.response.receipt.resolution_tag == res.response.resolution.tag


def test_describe_lens_lists_exposed_tables_apart_from_entities() -> None:
    model = _model()
    described = describe_model("ops", display_name="Ops", description="", model=model)
    assert [e.name for e in described.entities] == ["orders"]
    assert [t.table for t in described.exposed_tables] == ["staging.customers"]
    assert [c.name for c in described.exposed_tables[0].columns] == ["customer_id", "email"]
    assert described.note and "ungoverned" in described.note
    assert (
        "EXPOSED line" in described.note and "never a declared or certified tag" in described.note
    )
    plain = describe_model("plain", display_name="P", description="", model=jaffle_customer_value())
    assert plain.exposed_tables == [] and plain.note is None


def test_drift_names_the_lens_whose_grant_a_dropped_table_breaks() -> None:
    lens_yaml = yaml.safe_dump(
        {
            "name": "ops",
            "connections": [WH],
            "expose": [{"connection": WH, "tables": ["staging.*"]}],
        }
    )
    exposes = wd.parse_exposes({"lenses/ops/lens.yaml": lens_yaml})
    before = [_profile("staging.orders", [("order_id", "INTEGER")])]
    findings = wd.cross_reference(wd.baseline_drift(before, []), {}, {}, exposes=exposes)
    dropped = next(f for f in findings if f.kind == "table_dropped")
    assert dropped.referenced and wd.is_breaking(dropped)
    line = "\n".join(wd.render(dropped))
    assert "lens `ops`" in line and "pattern 'staging.*'" in line and "no longer resolves" in line


# ── apply and plan, end to end ───────────────────────────────────────────────


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

_LENS = "lenses/customer_value/lens.yaml"


@pytest.fixture()
def org(monkeypatch):
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: None)
    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('ExposeTest') RETURNING id")
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
            "table_profile",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


def _project_files(
    tables: list[str], hide: list[str] | None = None, **lens_keys: object
) -> dict[str, str]:
    entities, definitions, relationships = jaffle_shared_assets()
    files = dict(render_semantic_files(entities, definitions, relationships))
    for path, content in render_lens_repo(jaffle_customer_value_bundle()).items():
        files[f"lenses/customer_value/{path}"] = content
    config = yaml.safe_load(files[_LENS])
    config["expose"] = [{"connection": "jaffle", "tables": tables, "hide": hide or []}]
    config.update(lens_keys)
    files[_LENS] = yaml.safe_dump(config, sort_keys=False)
    return files


def _probe_artifact() -> str:
    return ProbeArtifact(
        connection="jaffle",
        probed_at=NOW,
        tables=[
            TableProfile(
                connection="jaffle",
                table="stg_orders",
                profiled_at=NOW,
                columns=[
                    ColumnProfile(name="order_id", type="INTEGER"),
                    ColumnProfile(name="customer_id", type="INTEGER"),
                    ColumnProfile(name="order_date", type="DATE"),
                    ColumnProfile(name="status", type="VARCHAR"),
                ],
            ),
            TableProfile(
                connection="jaffle",
                table="stg_customers",
                profiled_at=NOW,
                columns=[
                    ColumnProfile(name="customer_id", type="INTEGER"),
                    ColumnProfile(name="email", type="VARCHAR"),
                ],
            ),
            TableProfile(
                connection="jaffle",
                table="customers",
                profiled_at=NOW,
                columns=[ColumnProfile(name="customer_id", type="INTEGER")],
            ),
        ],
    ).model_dump_json()


@needs_db
def test_a_pattern_matching_nothing_fails_plan_and_apply(org) -> None:
    _oid, headers = org
    files = _project_files(["stg_*"])  # no profiles pushed, none stored
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] == "invalid" and any("'stg_*'" in e for e in row["errors"]), row
    assert any("dst probe" in e for e in row["errors"])
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] == "aborted", out
    assert any("'stg_*'" in e for r in out for e in r.get("errors", []))


@needs_db
def test_a_bare_name_collision_fails_plan_and_apply(org) -> None:
    _oid, headers = org
    files = _project_files(["customers"])
    files["profiles/jaffle.probe.json"] = _probe_artifact()
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] == "invalid" and any("collides" in e for e in row["errors"]), row
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] == "aborted", out


@needs_db
def test_plan_counts_the_grant_warns_on_untyped_fallback_and_apply_publishes_it(org) -> None:
    oid, headers = org
    files = _project_files(["stg_*"], hide=["stg_customers.email"])
    files["profiles/jaffle.probe.json"] = _probe_artifact()
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] != "invalid", row
    assert row["exposes"] == "exposes 2 tables (5 columns, 1 hidden)"
    assert row["warnings"] == [UNTYPED_WARNING]

    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] != "aborted", out
    bundle = load_published_lens(oid, "customer_value")
    assert bundle is not None
    exposed = {e.name: e for e in bundle.semantic_model.entities if e.exposed}
    assert set(exposed) == {"stg_orders", "stg_customers"}
    assert [f.name for f in exposed["stg_customers"].fields] == ["customer_id"]
    assert exposed["stg_orders"].exposed_whole and not exposed["stg_customers"].exposed_whole

    # With untyped answers accepted the warning goes away; the count stays.
    files = _project_files(["stg_*"], hide=["stg_customers.email"], untyped_fallback=True)
    files["profiles/jaffle.probe.json"] = _probe_artifact()
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["exposes"] == "exposes 2 tables (5 columns, 1 hidden)" and "warnings" not in row


@needs_db
def test_a_re_pushed_artifact_republishes_an_identical_lens_as_unchanged(org) -> None:
    # A re-probed artifact in the push is a touched input, so the lens takes
    # the publish path; its managed files match the DB, which is what the plan
    # reports — the apply row must say the same, not "updated".
    _oid, headers = org
    files = _project_files(["stg_*"])
    files["profiles/jaffle.probe.json"] = _probe_artifact()
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    first = next(r for r in out if r.get("lens") == "customer_value")
    assert first["action"] in ("created", "updated"), out
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    assert next(e for e in plan if e.get("lens") == "customer_value")["status"] == "unchanged"
    files["profiles/jaffle.probe.json"] = _probe_restamped(NOW + timedelta(days=1))
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert any(r.get("scope") == "profiles" for r in out), out  # the input was touched
    again = next(r for r in out if r.get("lens") == "customer_value")
    assert again["action"] == "unchanged", out
    # The same artifact pushed again is no change at all: nothing lands, and
    # the lens is not sent back through the publish path and its eval gate.
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert not any(r.get("scope") == "profiles" for r in out), out
    third = next(r for r in out if r.get("lens") == "customer_value")
    assert third == {"lens": "customer_value", "action": "unchanged"}, out


def _probe_restamped(probed_at: datetime) -> str:
    """The same artifact, re-probed later."""
    artifact = ProbeArtifact.model_validate_json(_probe_artifact())
    return ProbeArtifact(
        connection="jaffle",
        probed_at=probed_at,
        tables=[p.model_copy(update={"profiled_at": probed_at}) for p in artifact.tables],
    ).model_dump_json()


def _probe_without(table: str, probed_at: datetime) -> str:
    """The same artifact re-probed later, with one table gone from the catalog."""
    artifact = ProbeArtifact.model_validate_json(_probe_artifact())
    kept = [p.model_copy(update={"profiled_at": probed_at}) for p in artifact.tables]
    return ProbeArtifact(
        connection="jaffle",
        probed_at=probed_at,
        tables=[p for p in kept if p.table != table],
    ).model_dump_json()


@needs_db
def test_a_table_dropped_upstream_fails_the_grant_on_the_next_push(org) -> None:
    oid, headers = org
    files = _project_files(["stg_customers"])
    files["profiles/jaffle.probe.json"] = _probe_artifact()
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] != "aborted", out

    # The next probe no longer lists the table; the stored profile is still
    # there (absence never deletes), but the grant must not resolve from it.
    later = NOW + timedelta(days=1)
    files["profiles/jaffle.probe.json"] = _probe_without("stg_customers", later)
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] == "invalid", row
    assert any("stg_customers was dropped upstream" in e for e in row["errors"]), row
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] == "aborted", out
    assert any("dropped upstream" in e for r in out for e in r.get("errors", [])), out
    with org_session(oid) as s:
        assert profile_store.get_profile(s, "jaffle", "stg_customers") is not None

    # A catalog that lists it again is fine, and a push with no artifact at all
    # resolves from the store as before.
    files["profiles/jaffle.probe.json"] = _probe_without("customers", later)
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] != "invalid", row
    del files["profiles/jaffle.probe.json"]
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] != "invalid", row
    assert row["exposes"] == "exposes 1 table (2 columns)"


@needs_db
def test_an_older_artifact_never_drops_a_fresher_stored_profile(org) -> None:
    # The no-downgrade rule: a commit older than the server's profile cannot
    # declare the table gone.
    _oid, headers = org
    files = _project_files(["stg_customers"])
    files["profiles/jaffle.probe.json"] = _probe_artifact()
    out = client.post("/mgmt/project/apply", headers=headers, json={"files": files}).json()
    assert out[-1]["action"] != "aborted", out
    files["profiles/jaffle.probe.json"] = _probe_without("stg_customers", NOW - timedelta(days=1))
    plan = client.post("/mgmt/project/plan", headers=headers, json={"files": files}).json()
    row = next(e for e in plan if e.get("lens") == "customer_value")
    assert row["status"] != "invalid", row
