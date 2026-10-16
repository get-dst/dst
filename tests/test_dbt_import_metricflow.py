"""A MetricFlow import that `dst plan` accepts.

The shapes a real semantic layer carries and the import used to copy through
unchanged, each of which apply then rejected: a `case when` measure left
unqualified, a primary entity that is an expression written as a primary key, a
metric definition carrying an aggregate as its `sql:`, the same measure name on
two semantic models, and a non-additive measure imported as a plain sum.
"""

from __future__ import annotations

import json
from pathlib import Path

from services.contracts.lens_config import LensConfig
from services.contracts.shared_semantic import SelectSpec
from services.dbt import import_shared_assets, load_artifacts
from services.lenses.store import LensBundle
from services.project.compile import compile_lens_model
from services.semantic.files import parse_semantic_files, render_semantic_files
from services.validate.report import validate_bundle


def _model_node(name: str, columns: list[str]) -> dict[str, object]:
    return {
        "resource_type": "model",
        "name": name,
        "alias": name,
        "database": "wh",
        "schema": "marts",
        "relation_name": f'"wh"."marts"."{name}"',
        "columns": {c: {"name": c} for c in columns},
    }


def _manifest() -> dict[str, object]:
    return {
        "metadata": {"project_name": "clinic", "dbt_schema_version": "v12"},
        "nodes": {
            "model.clinic.fct_visits": _model_node(
                "fct_visits", ["visit_id", "clinic_code", "visit_date", "status", "minutes"]
            ),
            "model.clinic.fct_capacity_daily": _model_node(
                "fct_capacity_daily", ["clinic_code", "day", "slots_per_week", "minutes"]
            ),
            "model.clinic.dim_clinic": _model_node("dim_clinic", ["clinic_code", "city"]),
        },
    }


def _measure(name: str, agg: str, expr: str | None = None, **extra: object) -> dict[str, object]:
    return {"name": name, "agg": agg, "expr": expr, **extra}


def _semantic_manifest() -> dict[str, object]:
    return {
        "semantic_models": [
            {
                "name": "visits",
                "defaults": {"agg_time_dimension": "visit_day"},
                "node_relation": {"alias": "fct_visits", "schema_name": "marts"},
                "entities": [
                    {"name": "visit", "type": "primary", "expr": "visit_id"},
                    {"name": "clinic", "type": "foreign", "expr": "clinic_code"},
                ],
                "dimensions": [
                    {
                        "name": "visit_day",
                        "type": "time",
                        "expr": "visit_date",
                        "type_params": {"time_granularity": "day"},
                    },
                    {"name": "status", "type": "categorical"},
                ],
                "measures": [
                    _measure("visit_records", "count", "visit_id"),
                    _measure(
                        "completed_visits",
                        "sum",
                        "case when status in ('completed', 'billed') then 1 else 0 end",
                    ),
                    _measure("minutes", "sum"),
                ],
            },
            {
                "name": "capacity_daily",
                "defaults": {"agg_time_dimension": "capacity_day"},
                "node_relation": {"alias": "fct_capacity_daily", "schema_name": "marts"},
                "entities": [
                    {
                        "name": "capacity_row",
                        "type": "primary",
                        "expr": "clinic_code || '|' || cast(day as varchar)",
                    },
                    {"name": "clinic", "type": "foreign", "expr": "clinic_code"},
                ],
                "dimensions": [
                    {
                        "name": "capacity_day",
                        "type": "time",
                        "expr": "day",
                        "type_params": {"time_granularity": "day"},
                    }
                ],
                "measures": [
                    _measure(
                        "slots_per_week",
                        "sum",
                        non_additive_dimension={"name": "capacity_day", "window_choice": "max"},
                    ),
                    # the same name as a measure on `visits`: one name, one home
                    _measure("minutes", "sum"),
                ],
            },
            {
                "name": "clinics",
                "node_relation": {"alias": "dim_clinic", "schema_name": "marts"},
                "entities": [{"name": "clinic", "type": "primary", "expr": "clinic_code"}],
                "dimensions": [{"name": "city", "type": "categorical"}],
                "measures": [],
            },
        ],
        "metrics": [
            {
                "name": "visits_completed",
                "type": "simple",
                "description": "Visits with status completed or billed.",
                "type_params": {"measure": {"name": "completed_visits"}},
            },
            {
                "name": "planned_slots",
                "type": "simple",
                "description": "Weekly slot capacity (a level, copied per day).",
                "type_params": {"measure": {"name": "slots_per_week"}},
            },
        ],
    }


def _import(tmp_path: Path):  # type: ignore[no-untyped-def]
    target = tmp_path / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(_manifest()))
    (target / "semantic_manifest.json").write_text(json.dumps(_semantic_manifest()))
    return import_shared_assets(load_artifacts(target), connection="warehouse")


def test_case_when_measures_are_qualified_with_the_entity_alias(tmp_path: Path) -> None:
    by_name = {e.name: e for e in _import(tmp_path).entities}
    metrics = {m.name: m for m in by_name["visits"].metrics}
    assert metrics["completed_visits"].expr == (
        "CASE WHEN visits.status IN ('completed', 'billed') THEN 1 ELSE 0 END"
    )
    # the business-named alias of the measure carries the same qualified expression
    assert metrics["visits_completed"].expr == metrics["completed_visits"].expr
    # a time dimension over `expr: visit_date` names the COLUMN as the time field
    assert by_name["visits"].default_time_field == "visit_date"


def test_an_expression_primary_entity_is_not_a_primary_key(tmp_path: Path) -> None:
    res = _import(tmp_path)
    by_name = {e.name: e for e in res.entities}
    assert by_name["capacity_daily"].primary_key == []
    assert by_name["visits"].primary_key == ["visit_id"]
    note = next(s for s in res.skipped if s.name == "capacity_daily")
    assert "expression" in note.reason and "primary_key" in note.reason
    # the FK side still joins on a real column
    assert {(r.left, r.right) for r in res.relationships} == {
        ("visits", "clinics"),
        ("capacity_daily", "clinics"),
    }


def test_definitions_bind_to_the_metric_and_carry_no_aggregate_sql(tmp_path: Path) -> None:
    defs = {d.term: d for d in _import(tmp_path).definitions}
    assert defs["visits_completed"].about == "visits.visits_completed"
    assert defs["planned_slots"].about == "capacity_daily.planned_slots"
    assert all(d.sql_expr is None for d in defs.values())


def test_a_duplicate_measure_name_keeps_its_first_home_with_a_note(tmp_path: Path) -> None:
    res = _import(tmp_path)
    by_name = {e.name: e for e in res.entities}
    assert "minutes" in {m.name for m in by_name["visits"].metrics}
    assert "minutes" not in {m.name for m in by_name["capacity_daily"].metrics}
    note = next(s for s in res.skipped if s.kind == "measure" and s.name == "minutes")
    assert "'visits'" in note.reason and "first model wins" in note.reason


def test_a_non_additive_measure_imports_as_a_level(tmp_path: Path) -> None:
    by_name = {e.name: e for e in _import(tmp_path).entities}
    metrics = {m.name: m for m in by_name["capacity_daily"].metrics}
    assert metrics["slots_per_week"].additivity == "level"
    assert metrics["planned_slots"].additivity == "level"
    # the snapshot field is the entity's default time field (the dimension's column)
    assert by_name["capacity_daily"].default_time_field == "day"
    assert metrics["slots_per_week"].agg_time_field is None


def test_the_imported_layer_plans_clean(tmp_path: Path) -> None:
    """What `dst plan` runs on the written files: the authoring parse, the
    select-all compile, and the bundle validation — no error on any of them."""
    res = _import(tmp_path)
    files = render_semantic_files(res.entities, res.definitions, res.relationships)
    entities, definitions, relationships = parse_semantic_files(files)
    config = LensConfig(
        name="clinic",
        display_name="Clinic",
        connections=["warehouse"],
        select=SelectSpec.model_validate({"entities": ["*"], "definitions": ["*"]}),
    )
    model, _warnings = compile_lens_model(
        config=config,
        shared_entities=entities,
        shared_definitions=definitions,
        shared_relationships=relationships,
        local_definitions=[],
        use_when=[],
        sample_queries=[],
        dialect="duckdb",
    )
    report = validate_bundle(LensBundle(config=config, semantic_model=model), [], [])
    errors = [i.message for i in report.issues if i.severity == "error"]
    assert errors == []
