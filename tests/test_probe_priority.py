"""The catalog pass never starves the semantic layer.

A wide warehouse's 250-slot listing cap went entirely to an
unrelated sync-log dataset that sorted first — zero coverage of every table
the lenses read — and the next apply told the author their correct table
names were 'not found'. The orchestrator now guarantees priority tables into
the profile via the connector's uncapped introspect_tables path, and the
apply warning states the honest epistemic status ('not in the profiling
pass'), never the stronger claim ('not found').
"""

from __future__ import annotations

import pytest

from services.contracts.profile import TableProfile
from services.contracts.warehouse import ColumnSchema, SchemaSnapshot, TableSchema
from services.lenses.profiler_catalog import catalog_profiles


class _CappedConnector:
    """A connector whose catalog pass returns only the irrelevant dataset
    (the cap ate everything else), but which can resolve wanted tables
    against the full catalog — the TargetedIntrospect capability."""

    def __init__(self) -> None:
        self.resolved: list[str] = []

    def profile_catalog(self) -> list[TableProfile]:
        return [
            TableProfile(connection="raw", table=f"proj.CENSUS.SYNC_LOG_{i}", columns=[])
            for i in range(5)
        ]

    def introspect_tables(self, wanted: list[str]) -> SchemaSnapshot:
        self.resolved.extend(wanted)
        return SchemaSnapshot(
            connection="raw",
            dialect="bigquery",
            tables=[
                TableSchema(
                    name=t,
                    columns=[ColumnSchema(name="id", type="STRING", nullable=True)],
                )
                for t in wanted
            ],
        )

    # Protocol duck-typing minimums so isinstance(CatalogProfiler) holds.
    def catalog_join_candidates(self) -> list:  # pragma: no cover — protocol member
        return []

    def introspect(self) -> SchemaSnapshot:  # pragma: no cover — not used here
        return SchemaSnapshot(connection="raw", dialect="bigquery", tables=[])


def test_priority_tables_are_backfilled_past_the_cap() -> None:
    conn = _CappedConnector()
    profiles = catalog_profiles(conn, "bq", priority_tables={"proj.store_marts.store_kpis"})
    tables = {p.table for p in profiles}
    assert "proj.store_marts.store_kpis" in tables
    assert conn.resolved == ["proj.store_marts.store_kpis"]
    # The capped set survives too — backfill adds, never replaces.
    assert any("CENSUS" in t for t in tables)


def test_a_priority_table_already_in_the_pass_is_not_re_resolved() -> None:
    conn = _CappedConnector()
    catalog_profiles(conn, "bq", priority_tables={"proj.CENSUS.SYNC_LOG_0"})
    assert conn.resolved == []


def test_no_priority_set_keeps_the_old_shape() -> None:
    conn = _CappedConnector()
    profiles = catalog_profiles(conn, "bq")
    assert len(profiles) == 5 and conn.resolved == []


def test_the_apply_warning_never_claims_not_found() -> None:
    """'not found — check the name' blamed authors for tables a
    truncated pass never looked at. The warning now states what is actually
    known and names the probe that settles it."""
    import inspect

    from services.project import apply as apply_engine

    src = inspect.getsource(apply_engine._missing_warehouse_tables)
    assert "not found in connection" not in src  # the old strong claim
    assert "profiling pass" in src
    assert "--tables" in src


def test_tables_named_as_the_entity_names_them_are_sampled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--tables raw_shopify.abandoned_checkout`, as the entity spells it, sampled
    nothing on BigQuery: the catalog lists `project.dataset.table` and the
    scope compared exact strings. A name may leave out leading qualifiers, as
    a table reference may; it may not add one, and a name that matches no
    listed table says so."""
    import argparse

    from services.cli.main import _sample_scope

    listed = [
        TableProfile(connection="bq", table=t, columns=[])
        for t in (
            "lumo-analytics.raw_shopify.abandoned_checkout",
            "lumo-analytics.raw_shopify.orders",
            "lumo-analytics.staging.abandoned_checkout",
        )
    ]

    def scope(tables: str) -> set[str]:
        return _sample_scope(argparse.Namespace(tables=tables), listed, [], "bq")

    assert scope("raw_shopify.abandoned_checkout") == {
        "lumo-analytics.raw_shopify.abandoned_checkout"
    }
    assert scope("lumo-analytics.raw_shopify.abandoned_checkout") == {
        "lumo-analytics.raw_shopify.abandoned_checkout"
    }
    assert scope("RAW_SHOPIFY.Orders") == {"lumo-analytics.raw_shopify.orders"}
    assert capsys.readouterr().err == ""
    assert scope("other.raw_shopify.orders, raw_shopify.missing") == set()
    err = capsys.readouterr().err
    assert "'other.raw_shopify.orders', which matches no table 'bq' lists" in err
    assert "'raw_shopify.missing'" in err
