"""References resolve or the compile fails.

The shape throughout: entity `sessions` over source table `web_sessions` —
the name≠table class the demo entities dodge.
Unknown refs error naming the fragment, the ref, and the candidates;
table-qualified refs rewrite to entity form (compiled bundle only, warning);
unqualified columns resolve only in single-entity models; an entity name
colliding with another entity's source table is a compile error. validate_bundle
runs the same checks for wizard-path parity.
"""

from __future__ import annotations

import pytest

from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.contracts.shared_semantic import SelectSpec, SharedEntity
from services.project.compile import CompileError, compile_lens_model
from services.semantic.resolve import resolve_model


def _sessions(**over: object) -> SharedEntity:
    base: dict[str, object] = {
        "name": "sessions",
        "source": {"connection": "wh", "table": "web_sessions"},
        "primary_key": ["session_id"],
        "default_time_field": "started_at",
        "fields": [
            {"name": "session_id", "type": "string"},
            {"name": "converted", "type": "boolean"},
            {"name": "started_at", "type": "timestamp"},
        ],
        "metrics": [
            {"name": "session_count", "agg": "count", "expr": "sessions.session_id"},
            {
                "name": "converted_sessions",
                "agg": "count",
                "expr": "sessions.session_id",
                "filters": ["sessions.converted = true"],
            },
        ],
    }
    base.update(over)
    return SharedEntity.model_validate(base)


CUSTOMERS = SharedEntity.model_validate(
    {
        "name": "customers",
        "source": {"connection": "wh", "table": "crm.customers"},
        "fields": [{"name": "customer_id", "type": "string"}],
    }
)


def _compile(entities: list[SharedEntity], definitions: list[Definition] | None = None):
    config = LensConfig(
        name="probe",
        display_name="Probe",
        connections=["wh"],
        select=SelectSpec.model_validate(
            {
                "entities": [e.name for e in entities],
                "definitions": [d.term for d in definitions or []],
            }
        ),
    )
    return compile_lens_model(
        config=config,
        shared_entities={e.name: e for e in entities},
        shared_definitions={d.term: d for d in definitions or []},
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
    )


# ── the happy path: name ≠ table resolves clean ──────────────────────────────


def test_name_ne_table_entity_compiles_warning_free() -> None:
    model, warnings = _compile([_sessions()])
    assert warnings == []
    metric = model.entities[0].metrics[1]
    assert metric.expr == "sessions.session_id"
    assert metric.filters == ["sessions.converted = true"]


def test_demo_project_still_compiles_warning_free() -> None:
    # name == table pass-through unchanged: the jaffle demo carries no rewrites.
    from services.lenses.demo import jaffle_customer_value

    model = jaffle_customer_value()
    _resolved, errors, warnings = resolve_model(model)
    assert errors == [] and warnings == []


# ── the typo matrix: errors name the fragment, the ref, the candidates ───────


def test_unknown_entity_qualifier_errors_with_candidates() -> None:
    bad = _sessions(
        metrics=[{"name": "session_count", "agg": "count", "expr": "web_sesions.session_id"}]
    )
    with pytest.raises(CompileError) as err:
        _compile([bad, CUSTOMERS])
    assert str(err.value) == (
        "metric session_count: 'web_sesions.session_id' — no entity 'web_sesions'; "
        "entities in this lens: customers, sessions"
    )


def test_unknown_column_on_known_entity_errors_with_members() -> None:
    bad = _sessions(
        metrics=[{"name": "session_count", "agg": "count", "expr": "sessions.sesion_id"}]
    )
    with pytest.raises(CompileError) as err:
        _compile([bad])
    assert str(err.value) == (
        "metric session_count: 'sessions.sesion_id' — no member 'sesion_id' on entity "
        "'sessions'; members of sessions: converted, session_count, session_id, started_at"
    )


def test_unqualified_column_resolves_only_in_single_entity_models() -> None:
    bare = _sessions(metrics=[{"name": "session_count", "agg": "count", "expr": "session_id"}])
    model, warnings = _compile([bare])  # single entity: resolves, no rewrite
    assert warnings == []
    assert model.entities[0].metrics[0].expr == "session_id"
    with pytest.raises(CompileError) as err:
        _compile([bare, CUSTOMERS])
    assert str(err.value) == (
        "metric session_count: column 'session_id' is unqualified — qualify it; "
        "candidates: sessions.session_id"
    )


def test_unqualified_unknown_column_names_the_entities() -> None:
    bare = _sessions(metrics=[{"name": "n", "agg": "count", "expr": "sesion_id"}])
    with pytest.raises(CompileError) as err:
        _compile([bare, CUSTOMERS])
    assert str(err.value) == (
        "metric n: column 'sesion_id' is unqualified — qualify it as <entity>.sesion_id; "
        "entities in this lens: customers, sessions"
    )


def test_entity_name_colliding_with_another_source_table_errors() -> None:
    reports = SharedEntity.model_validate(
        {
            "name": "reports",
            "source": {"connection": "wh", "table": "sessions"},
            "fields": [{"name": "report_id", "type": "string"}],
        }
    )
    with pytest.raises(CompileError) as err:
        _compile([_sessions(), reports])
    assert (
        "entity 'sessions' collides with the source table of entity 'reports' "
        "('sessions') — entity names are the logical namespace; rename entity 'sessions'"
    ) in str(err.value)


def test_member_name_slots_are_checked() -> None:
    bad = _sessions(primary_key=["sesion_id"], default_time_field="started_atx")
    with pytest.raises(CompileError) as err:
        _compile([bad])
    msg = str(err.value)
    assert "entity sessions primary_key: 'sesion_id' — no member 'sesion_id'" in msg
    assert "entity sessions default_time_field: 'started_atx' — no member 'started_atx'" in msg


def test_agg_time_field_is_checked() -> None:
    bad = _sessions(
        metrics=[
            {
                "name": "session_count",
                "agg": "count",
                "expr": "sessions.session_id",
                "agg_time_field": "ended_at",
            }
        ]
    )
    with pytest.raises(CompileError, match="metric session_count agg_time_field: 'ended_at'"):
        _compile([bad])


def test_ratio_and_derived_refs_error_at_compile() -> None:
    ratio = _sessions(
        metrics=[
            {"name": "session_count", "agg": "count", "expr": "sessions.session_id"},
            {"name": "rate", "type": "ratio", "numerator": "converted", "denominator": "nope"},
        ]
    )
    with pytest.raises(CompileError) as err:
        _compile([ratio])
    msg = str(err.value)
    assert "metric rate: numerator 'converted' is not a metric on entity 'sessions'" in msg
    assert "metric rate: denominator 'nope' is not a metric on entity 'sessions'" in msg
    assert "metrics of sessions: rate, session_count" in msg
    derived = _sessions(
        metrics=[
            {"name": "session_count", "agg": "count", "expr": "sessions.session_id"},
            {"name": "delta", "type": "derived", "expr": "{sesion_count} - 1"},
        ]
    )
    with pytest.raises(CompileError, match=r"metric delta: '\{sesion_count\}' references no"):
        _compile([derived])


def test_join_on_and_definition_sql_expr_are_resolved() -> None:
    joined = _sessions(
        joins=[{"right": "customers", "on": "sessions.customer_idx = customers.customer_id"}]
    )
    with pytest.raises(CompileError, match="join sessions -> customers: 'sessions.customer_idx'"):
        _compile([joined, CUSTOMERS])
    bad_def = Definition(term="converted", body="b", sql_expr="sessions.conversion = true")
    with pytest.raises(CompileError, match="definition converted: 'sessions.conversion'"):
        _compile([_sessions()], [bad_def])


def test_dimension_expr_is_resolved() -> None:
    bad = _sessions(dimensions=[{"name": "day", "expr": "date_trunc('day', sessions.ended_at)"}])
    with pytest.raises(CompileError, match="dimension day: 'sessions.ended_at'"):
        _compile([bad])


# ── table-qualified input rewrites to entity form ────────────────────────────


def test_table_qualified_refs_rewrite_to_entity_form_with_warning() -> None:
    authored = _sessions(
        metrics=[
            {
                "name": "converted_sessions",
                "agg": "count",
                "expr": "web_sessions.session_id",
                "filters": ["web_sessions.converted = true"],
            }
        ]
    )
    model, warnings = _compile([authored])
    metric = model.entities[0].metrics[0]
    assert metric.expr == "sessions.session_id"
    assert metric.filters == ["sessions.converted = TRUE"]
    assert (
        "metric converted_sessions: 'web_sessions.session_id' rewritten to "
        "'sessions.session_id' (source table → entity name)"
    ) in warnings
    assert (
        "metric converted_sessions filter: 'web_sessions.converted' rewritten to "
        "'sessions.converted' (source table → entity name)"
    ) in warnings
    # the authored shared asset is untouched — the rewrite is bundle-only
    assert authored.metrics[0].expr == "web_sessions.session_id"


def test_qualified_source_table_ref_also_rewrites() -> None:
    # bindings idiom: a bare modeled table matches the ref's last segment and
    # vice versa — authors pasting fully-qualified BI SQL get the same kindness.
    joined = _sessions(
        joins=[{"right": "customers", "on": "sessions.session_id = crm.customers.customer_id"}]
    )
    model, warnings = _compile([joined, CUSTOMERS])
    assert model.joins[0].on == "sessions.session_id = customers.customer_id"
    assert any(
        "'crm.customers.customer_id' rewritten to 'customers.customer_id'" in w for w in warnings
    )


def test_definition_sql_expr_rewrites() -> None:
    d = Definition(term="converted", body="b", sql_expr="web_sessions.converted = true")
    model, warnings = _compile([_sessions()], [d])
    assert model.definitions[0].sql_expr == "sessions.converted = TRUE"
    assert any("definition converted:" in w and "rewritten" in w for w in warnings)


# ── wizard-path parity: validate_bundle reports the same matrix ──────────────


def _bundle(model: SemanticModel):
    from services.lenses.store import LensBundle

    return LensBundle(
        config=LensConfig(name="probe", display_name="P", connections=["wh"]),
        semantic_model=model,
    )


def _h1_model(metrics: list[Metric]) -> SemanticModel:
    return SemanticModel(
        lens="probe",
        dialect="duckdb",
        entities=[
            Entity(
                name="sessions",
                source=EntitySource(connection="wh", table="web_sessions"),
                fields=[
                    Field(name="session_id", type="string"),
                    Field(name="converted", type="boolean"),
                ],
                metrics=metrics,
            )
        ],
    )


def test_validate_bundle_reports_unresolved_reference() -> None:
    from services.validate.report import validate_bundle

    model = _h1_model([Metric(name="n", agg="count", expr="web_sesions.session_id")])
    report = validate_bundle(_bundle(model), [], [])
    assert not report.ok
    errors = [i for i in report.issues if i.code == "unresolved_reference"]
    assert len(errors) == 1 and errors[0].severity == "error"
    assert "no entity 'web_sesions'" in errors[0].message


def test_validate_bundle_warns_on_rewritable_ref() -> None:
    from services.validate.report import validate_bundle

    model = _h1_model([Metric(name="n", agg="count", expr="web_sessions.session_id")])
    report = validate_bundle(_bundle(model), [], [])
    assert report.ok  # a warning, never a block — validate can't rewrite
    warned = [i for i in report.issues if i.code == "reference_rewritten"]
    assert len(warned) == 1 and warned[0].severity == "warning"
    assert "rewritten to 'sessions.session_id'" in warned[0].message


def test_resolve_model_never_mutates_its_input() -> None:
    model = _h1_model([Metric(name="n", agg="count", expr="web_sessions.session_id")])
    resolved, errors, warnings = resolve_model(model)
    assert errors == [] and len(warnings) == 1
    assert model.entities[0].metrics[0].expr == "web_sessions.session_id"
    assert resolved.entities[0].metrics[0].expr == "sessions.session_id"


def test_zero_entity_model_has_nothing_to_resolve() -> None:
    model = SemanticModel(
        lens="probe",
        dialect="duckdb",
        definitions=[Definition(term="x", body="b", sql_expr="amount > 0")],
    )
    assert resolve_model(model) == (model, [], [])


# ── physical translation happens at the serving boundary, once ───────────────


def _seed_warehouse(path: str) -> None:
    import duckdb

    con = duckdb.connect(path)
    con.execute(
        "CREATE TABLE web_sessions (session_id VARCHAR, converted BOOLEAN, started_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO web_sessions VALUES "
        "('s1', true, '2026-07-01 10:00:00'), "
        "('s2', false, '2026-07-01 11:00:00'), "
        "('s3', true, '2026-07-02 09:00:00')"
    )
    con.close()


def test_serialize_model_aliases_tables_to_entity_names() -> None:
    from services.runtime.generator import serialize_model

    model, _ = _compile([_sessions()])
    prompt = serialize_model(model)
    assert "- web_sessions AS sessions" in prompt
    assert "    sessions.session_id string" in prompt  # member lines entity-qualified
    assert "aliased to their entity names" in prompt  # the added generation rule
    # alias omitted when the entity name IS the table name
    identical = SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity(
                name="projects",
                source=EntitySource(connection="wh", table="projects"),
                fields=[Field(name="id", type="string")],
            )
        ],
    )
    lines = serialize_model(identical).splitlines()
    assert "- projects" in lines and "- projects AS projects" not in lines


def test_guard_resolves_entity_alias_to_physical_table(tmp_path) -> None:
    from services.connectors.duckdb import DuckDBConnector
    from services.runtime import sql_guard

    model, _ = _compile([_sessions()])
    # The emission the guard used to reject: the entity name as the table.
    r = sql_guard.check("SELECT COUNT(sessions.session_id) AS n FROM sessions", model)
    assert r.ok, r.reason
    assert r.sql is not None and "web_sessions AS sessions" in r.sql
    path = str(tmp_path / "wh.duckdb")
    _seed_warehouse(path)
    result = DuckDBConnector(path).execute(r.sql)
    assert result.rows == [[3]]


def test_guard_keeps_raw_physical_tables_accepted() -> None:
    from services.runtime import sql_guard

    model, _ = _compile([_sessions()])
    r = sql_guard.check("SELECT sessions.session_id FROM web_sessions AS sessions", model)
    assert r.ok and r.sql is not None and "web_sessions AS sessions" in r.sql
    # a qualified reference never denotes an entity — entity names are bare aliases
    bad = sql_guard.check("SELECT x.session_id FROM crm.sessions AS x", model)
    assert not bad.ok and "out of lens scope" in (bad.reason or "")


def test_guard_checks_columns_through_the_entity_alias() -> None:
    from services.runtime import sql_guard

    model, _ = _compile([_sessions()])
    r = sql_guard.check("SELECT sessions.nope FROM sessions", model)
    assert not r.ok
    assert "column 'sessions.nope' is out of lens scope" in (r.reason or "")


def test_guard_trust_tables_never_translates() -> None:
    # Certified SQL is physical — a human blessed those exact tables; a table
    # that happens to share an entity's name must execute as written.
    from services.runtime import sql_guard

    model, _ = _compile([_sessions()])
    r = sql_guard.check("SELECT sessions.session_id FROM sessions", model, trust_tables=True)
    assert r.ok and r.sql is not None and "web_sessions" not in r.sql


def test_h1_generation_end_to_end(tmp_path) -> None:
    """A name≠table lens serves an entity-qualified
    generation — guard passes, the warehouse executes, the answer lands."""
    from services.connectors.duckdb import DuckDBConnector
    from services.contracts.fakes import ScriptedLLM
    from services.runtime.answer import AnswerComposer
    from services.runtime.generator import GroundedSQLGenerator
    from services.runtime.pipeline import run_query

    path = str(tmp_path / "wh.duckdb")
    _seed_warehouse(path)
    count_only = [{"name": "session_count", "agg": "count", "expr": "sessions.session_id"}]
    model, _ = _compile([_sessions(metrics=count_only)])
    script = (
        '{"sql": "SELECT COUNT(sessions.session_id) AS session_count '
        'FROM web_sessions AS sessions", "definition_used": null, "rationale": "r"}'
    )
    llm = ScriptedLLM([script, "There were 3 sessions."])
    result = run_query(
        question="how many sessions do we have?",
        lens_name="probe",
        org_id="org-1",
        caller="test",
        semantic_model=model,
        connector=DuckDBConnector(path),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert result.trace.status == "ok", result.trace.error
    assert result.response.data is not None and result.response.data.rows == [[3]]


def test_h2_twin_metric_question_serves_end_to_end(tmp_path) -> None:
    """Resolution at the pipeline level: with BOTH twins in the model (session_count
    unfiltered, converted_sessions filtered), the innocent
    'how many sessions' generation must never be rejected by the filter guard."""
    from services.connectors.duckdb import DuckDBConnector
    from services.contracts.fakes import ScriptedLLM
    from services.runtime.answer import AnswerComposer
    from services.runtime.generator import GroundedSQLGenerator
    from services.runtime.pipeline import run_query

    path = str(tmp_path / "wh.duckdb")
    _seed_warehouse(path)
    model, _ = _compile([_sessions()])  # both twins
    script = (
        '{"sql": "SELECT COUNT(sessions.session_id) AS session_count '
        'FROM web_sessions AS sessions", "definition_used": null, "rationale": "r"}'
    )
    llm = ScriptedLLM([script, "There were 3 sessions."])
    result = run_query(
        question="how many sessions do we have?",
        lens_name="probe",
        org_id="org-1",
        caller="test",
        semantic_model=model,
        connector=DuckDBConnector(path),
        generator=GroundedSQLGenerator(llm),
        composer=AnswerComposer(llm),
    )
    assert result.trace.status == "ok", result.trace.error
    assert result.response.data is not None and result.response.data.rows == [[3]]


def test_h1_intent_path_compiles_runnable_sql(tmp_path) -> None:
    from services.connectors.duckdb import DuckDBConnector
    from services.contracts.query_intent import QueryIntent
    from services.runtime.compiler import compile_intent

    model, _ = _compile([_sessions()])
    sql = compile_intent(QueryIntent(metrics=["session_count"]), model)
    assert "FROM web_sessions AS sessions" in sql
    path = str(tmp_path / "wh.duckdb")
    _seed_warehouse(path)
    assert DuckDBConnector(path).execute(sql).rows == [[3]]


# ── subquery scopes: the BigQuery correlation trap ───────────────────────────


def _snapshot_model(filter_sql: str) -> SemanticModel:
    entity = Entity(
        name="finance_kpis",
        source=EntitySource(connection="bq", table="p.d.finance_kpis"),
        fields=[Field(name="status_date", type="date"), Field(name="arr", type="number")],
        metrics=[
            Metric(name="current_arr", agg="sum", expr="finance_kpis.arr", filters=[filter_sql])
        ],
    )
    return SemanticModel(lens="l", name="m", dialect="bigquery", entities=[entity])


def test_unaliased_subquery_source_table_is_self_aliased() -> None:
    # The one form the resolver used to accept without an alias silently
    # degenerated on BigQuery to an always-true filter (a "current" metric
    # summed every daily snapshot). The repair makes the accepted form the
    # correct form, and says so.
    out, errors, warnings = resolve_model(
        _snapshot_model(
            "finance_kpis.status_date = "
            "(SELECT MAX(finance_kpis.status_date) FROM `p.d.finance_kpis`)"
        )
    )
    assert errors == []
    assert len(warnings) == 1 and "self-aliased" in warnings[0]
    assert (
        out.entities[0].metrics[0].filters[0] == "finance_kpis.status_date = "
        "(SELECT MAX(finance_kpis.status_date) FROM `p.d.finance_kpis` AS finance_kpis)"
    )


def test_already_self_aliased_subquery_passes_untouched() -> None:
    good = (
        "finance_kpis.status_date = "
        "(SELECT MAX(finance_kpis.status_date) FROM `p.d.finance_kpis` AS finance_kpis)"
    )
    out, errors, warnings = resolve_model(_snapshot_model(good))
    assert (errors, warnings) == ([], [])
    assert out.entities[0].metrics[0].filters[0] == good


def test_subquery_local_aliases_are_that_scopes_sql() -> None:
    # Custom inner aliases were rejected ("no entity 'snap'"), which forced
    # authors into the unaliased degenerate form. A name bound by the
    # subquery's own FROM is not an entity ref.
    out, errors, warnings = resolve_model(
        _snapshot_model(
            "finance_kpis.status_date = "
            "(SELECT MAX(snap.status_date) FROM `p.d.finance_kpis` AS snap)"
        )
    )
    assert (errors, warnings) == ([], [])


def test_bare_columns_inside_a_from_bearing_subquery_pass() -> None:
    out, errors, warnings = resolve_model(
        _snapshot_model(
            "finance_kpis.status_date = (SELECT MAX(status_date) FROM `p.d.finance_kpis` AS s)"
        )
    )
    assert (errors, warnings) == ([], [])


def test_entity_refs_outside_subqueries_still_resolve_strictly() -> None:
    _, errors, _ = resolve_model(_snapshot_model("finance_kpis.no_such_column = 1"))
    assert len(errors) == 1 and "no member 'no_such_column'" in errors[0]
