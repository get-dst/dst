"""Offline pins for introspect scoping: the cap must announce itself,
`--tables` must resolve against the FULL catalog, views must not print fake row
counts, and dataset pins must accept all three config spellings.

A fake client stands in for BigQuery — the live contract test stays opt-in.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from services.connectors.bigquery import BigQueryConnector
from services.contracts.protocols import TargetedIntrospect
from services.lenses.connections import _bigquery_datasets, config_warnings


def _field(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, field_type="STRING", mode="NULLABLE", description=None)


class FakeClient:
    """Just enough of google.cloud.bigquery.Client for the scan paths."""

    def __init__(self, datasets: dict[str, dict[str, str]]) -> None:
        # {dataset_id: {table_id: table_type}}
        self._datasets = datasets

    def list_datasets(self, project: str, timeout: float | None = None) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                dataset_id=ds, reference=SimpleNamespace(project=project, dataset_id=ds)
            )
            for ds in sorted(self._datasets)
        ]

    def list_tables(self, dataset_ref: Any, timeout: float | None = None) -> list[SimpleNamespace]:
        tables = self._datasets[dataset_ref.dataset_id]
        return [
            SimpleNamespace(
                project=dataset_ref.project,
                dataset_id=dataset_ref.dataset_id,
                table_id=t,
                reference=SimpleNamespace(
                    project=dataset_ref.project, dataset_id=dataset_ref.dataset_id, table_id=t
                ),
            )
            for t in sorted(tables)
        ]

    def get_table(self, reference: Any, timeout: float | None = None) -> SimpleNamespace:
        table_type = self._datasets[reference.dataset_id][reference.table_id]
        return SimpleNamespace(
            project=reference.project,
            dataset_id=reference.dataset_id,
            table_id=reference.table_id,
            schema=[_field("id")],
            num_rows=0 if table_type == "VIEW" else 42,
            table_type=table_type,
            time_partitioning=None,
            clustering_fields=None,
            description=None,
        )


def _connector(datasets: dict[str, dict[str, str]], pins: list[str] | None = None) -> Any:
    conn = BigQueryConnector.__new__(BigQueryConnector)
    conn._project = "proj"
    conn._datasets = pins or []
    conn._dataset = (pins or [None])[0]
    conn._client = FakeClient(datasets)
    return conn


def _wide_warehouse() -> dict[str, dict[str, str]]:
    # One dataset that sorts first and alone overflows the cap, then the one
    # that holds what the author actually wants — the field shape exactly.
    filler = {f"t{i:04d}": "TABLE" for i in range(BigQueryConnector._MAX_INTROSPECT_TABLES + 5)}
    return {"AAA_CENSUS": filler, "finance_marts": {"finance_kpis": "TABLE"}}


def test_capped_listing_says_truncated() -> None:
    snap = _connector(_wide_warehouse()).introspect()
    assert len(snap.tables) == BigQueryConnector._MAX_INTROSPECT_TABLES
    assert snap.truncated is True
    assert not any(t.name.endswith("finance_kpis") for t in snap.tables)


def test_uncapped_listing_is_not_truncated() -> None:
    snap = _connector({"one": {"a": "TABLE"}}).introspect()
    assert snap.truncated is False


def test_tables_resolve_against_the_full_catalog() -> None:
    conn = _connector(_wide_warehouse())
    assert isinstance(conn, TargetedIntrospect)
    # Every qualification depth reaches past the cap.
    for spelling in (
        "finance_kpis",
        "finance_marts.finance_kpis",
        "proj.finance_marts.finance_kpis",
    ):
        snap = conn.introspect_tables([spelling])
        assert [t.name for t in snap.tables] == ["proj.finance_marts.finance_kpis"], spelling


def test_views_have_no_fake_row_count() -> None:
    snap = _connector({"d": {"dim_account": "VIEW", "orders": "TABLE"}}).introspect()
    by_name = {t.name.rsplit(".", 1)[-1]: t for t in snap.tables}
    assert by_name["dim_account"].is_view is True
    assert by_name["dim_account"].row_count is None  # a view stores no count — not "~0 rows"
    assert by_name["orders"].is_view is False
    assert by_name["orders"].row_count == 42


def test_dataset_pins_scope_the_scan() -> None:
    data = {"a": {"t1": "TABLE"}, "b": {"t2": "TABLE"}, "c": {"t3": "TABLE"}}
    snap = _connector(data, pins=["a", "b"]).introspect()
    assert sorted(snap.schemas_searched) == ["proj.a", "proj.b"]
    assert sorted(t.name.rsplit(".", 1)[-1] for t in snap.tables) == ["t1", "t2"]


def test_config_spellings_all_pin_datasets() -> None:
    assert _bigquery_datasets({"datasets": ["a", "b"]}) == ["a", "b"]
    assert _bigquery_datasets({"dataset": "a"}) == ["a"]
    assert _bigquery_datasets({"schema": "a"}) == ["a"]  # the documented generic key
    assert _bigquery_datasets({}) == []


def test_unknown_config_keys_warn_and_known_ones_dont() -> None:
    warned = config_warnings("bigquery", {"schema": "a", "shcema": "b"})
    assert len(warned) == 1 and "shcema" in warned[0]
    assert config_warnings("bigquery", {"datasets": ["a"]}) == []
    # Unregistered types never warn — a false positive would bury the channel.
    assert config_warnings("github", {"anything": 1}) == []
