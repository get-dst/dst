"""A quoted value is bound by the warehouse's own type on every compiled query.

Values reach the compiler as text from every source: a caller's binding, a
profile dictionary (which records a BOOLEAN's values as 'true'/'false'), an
authored population filter. DuckDB and Postgres coerce `flag = 'true'`;
BigQuery rejects it with "no matching signature for operator =", so the same
question that served on one warehouse failed on another. The compiler binds
each quoted literal compared with a boolean or numeric column to that type,
read from the stored profile, on every dialect and in every clause.
"""

from __future__ import annotations

import duckdb
import pytest

from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.query_intent import IntentFilter, QueryIntent
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.runtime.compiler import compile_intent
from services.runtime.value_guard import entity_column_types

TYPES = {
    "parcels": {
        "parcel_id": "INTEGER",
        "is_fragile": "BOOLEAN",
        "is_test": "BOOLEAN",
        "attempts": "INTEGER",
        "depot": "STRING",
        "scanned_on": "DATE",
    }
}


def _model(dialect: str = "bigquery", *, level: bool = False) -> SemanticModel:
    return SemanticModel(
        lens="courier",
        dialect=dialect,
        entities=[
            Entity(
                name="parcels",
                source=EntitySource(connection="wh", table="parcels"),
                primary_key=["parcel_id"],
                default_time_field="scanned_on",
                # Authored the way the profile dictionary spells the value.
                population_filter="is_test = 'false'",
                fields=[
                    Field(name="parcel_id", type="integer"),
                    Field(name="is_fragile", type="boolean"),
                    Field(name="attempts", type="integer"),
                    Field(name="depot", type="string"),
                    Field(name="scanned_on", type="date"),
                ],
                metrics=[
                    Metric(name="parcels", agg="count", expr="parcel_id"),
                    Metric(
                        name="fragile_parcels",
                        agg="count",
                        expr="parcel_id",
                        filters=["is_fragile = 'true'"],
                    ),
                    Metric(
                        name="parcels_on_hand",
                        agg="count",
                        expr="parcel_id",
                        additivity="level" if level else "flow",
                    ),
                ],
            )
        ],
    )


def _intent(metric: str = "parcels") -> QueryIntent:
    return QueryIntent(
        entity="parcels",
        metrics=[metric],
        filters=[
            IntentFilter(field="is_fragile", op="=", value="true"),
            IntentFilter(field="attempts", op="in", value=["2", "3"]),
            IntentFilter(field="depot", op="=", value="true"),
        ],
    )


def test_bindings_and_the_population_filter_bind_as_booleans_on_bigquery() -> None:
    sql = compile_intent(_intent(), _model(), column_types=TYPES)
    assert "parcels.is_fragile = TRUE" in sql
    assert "is_test = FALSE" in sql
    assert "parcels.attempts IN (2, 3)" in sql
    # A STRING column keeps its quoted value, whatever the value says.
    assert "parcels.depot = 'true'" in sql
    assert "'false'" not in sql and "is_fragile = 'true'" not in sql


def test_a_metric_filter_and_a_snapshot_subquery_bind_too() -> None:
    sql = compile_intent(
        QueryIntent(entity="parcels", metrics=["fragile_parcels"]), _model(), column_types=TYPES
    )
    assert "is_fragile = TRUE" in sql and "'true'" not in sql
    snap = compile_intent(
        QueryIntent(entity="parcels", metrics=["parcels_on_hand"]),
        _model(level=True),
        column_types=TYPES,
    )
    assert "parcels_latest.is_test = FALSE" in snap and "'false'" not in snap


@pytest.mark.parametrize("dialect", ["bigquery", "duckdb", "postgres", "snowflake"])
def test_every_dialect_binds_the_same(dialect: str) -> None:
    sql = compile_intent(_intent(), _model(dialect), column_types=TYPES)
    assert "'false'" not in sql and "is_fragile = 'true'" not in sql


def test_without_a_profiled_type_the_value_stays_as_written() -> None:
    sql = compile_intent(_intent(), _model())
    assert "is_fragile = 'true'" in sql


def test_a_value_the_type_cannot_hold_is_left_for_the_warehouse_to_refuse() -> None:
    intent = QueryIntent(
        entity="parcels",
        metrics=["parcels"],
        filters=[IntentFilter(field="is_fragile", op="=", value="sometimes")],
    )
    assert "is_fragile = 'sometimes'" in compile_intent(intent, _model(), column_types=TYPES)


def test_the_bound_sql_runs_live_on_duckdb() -> None:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE parcels (parcel_id INTEGER, is_fragile BOOLEAN, is_test BOOLEAN, "
        "attempts INTEGER, depot VARCHAR, scanned_on DATE)"
    )
    con.execute(
        "INSERT INTO parcels VALUES "
        "(1, TRUE, FALSE, 2, 'true', DATE '2026-09-01'),"
        "(2, TRUE, TRUE, 2, 'true', DATE '2026-09-01'),"  # a test parcel: outside the population
        "(3, FALSE, FALSE, 3, 'true', DATE '2026-09-02'),"
        "(4, TRUE, FALSE, 3, 'north', DATE '2026-09-02'),"
        "(5, TRUE, FALSE, 1, 'true', DATE '2026-09-03')"
    )
    sql = compile_intent(_intent(), _model("duckdb"), column_types=TYPES)
    assert "'true'" in sql and "= TRUE" in sql  # only the STRING column stays quoted
    assert con.execute(sql).fetchall() == [(1,)]


def test_every_profiled_column_carries_its_type() -> None:
    """A population filter may read a column no field declares."""
    profile = TableProfile(
        connection="wh",
        table="parcels",
        columns=[
            ColumnProfile(name="is_fragile", type="BOOLEAN"),
            ColumnProfile(name="is_test", type="BOOLEAN"),
        ],
    )
    assert entity_column_types(_model(), [profile]) == {
        "parcels": {"is_fragile": "BOOLEAN", "is_test": "BOOLEAN"}
    }
