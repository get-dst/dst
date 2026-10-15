"""Recent-window sampling (``probe.recent_partitions``).

The catalog pass is unchanged (every table, columns, row counts, whole-table
freshness and date coverage); the SAMPLING pass narrows to the latest N
partitions of a time-partitioned table, or the last N days of the entity's
declared ``default_time_field``, else the whole table as before. Pinned: which
window a table gets, that the SQL carries it on every dialect (BigQuery's as
the partition filter, DuckDB measured live), that the artifact records it and
labels what it read as partial, and that a dictionary the stored artifact had
proved complete survives a windowed refresh as the dated union — the very first
probe reads whole so there is something complete to keep.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest

from services.connectors.duckdb import DuckDBConnector
from services.connectors.sampling import sample_table, window_predicate
from services.contracts.profile import (
    ColumnProfile,
    ColumnSampleSpec,
    PartitioningProfile,
    SampleWindow,
    TableProfile,
    TableSampleSpec,
)
from services.contracts.semantic_model import EntitySource, Field
from services.contracts.shared_semantic import SharedEntity
from services.contracts.warehouse import QueryResult
from services.lenses.profiler import build_sample_spec, sample_profiles
from services.lenses.profiler_catalog import catalog_profiles
from services.project import probe
from services.project.probe import ProbeArtifact, carry_complete_dictionaries, sample_windows

EVENTS = "ops.events"


def _entity(table: str = EVENTS, time_field: str | None = "event_date") -> SharedEntity:
    return SharedEntity(
        name="events",
        description="d",
        grain="g",
        source=EntitySource(connection="warehouse", table=table),
        default_time_field=time_field,
        fields=[Field(name="event_date", type="date"), Field(name="kind", type="string")],
    )


def _profile(partitioning: PartitioningProfile | None = None, **cols: str) -> TableProfile:
    columns = [
        ColumnProfile(name="event_date", type="DATE"),
        ColumnProfile(name="kind", type="VARCHAR"),
    ]
    columns += [ColumnProfile(name=n, type=t) for n, t in cols.items()]
    return TableProfile(
        connection="warehouse", table=EVENTS, partitioning=partitioning, columns=columns
    )


# ── which window a table gets ────────────────────────────────────────────────


def test_a_partitioned_table_gets_its_latest_partitions() -> None:
    profiles = [_profile(PartitioningProfile(column="event_date", kind="time"))]
    assert sample_windows(profiles, [], "warehouse", 7) == {
        EVENTS: SampleWindow(kind="partitions", n=7, column="event_date")
    }
    # A partition column the catalog does not type as temporal is not a window.
    hashed = [_profile(PartitioningProfile(column="kind", kind="hash"))]
    assert sample_windows(hashed, [], "warehouse", 7) == {}


def test_an_unpartitioned_table_windows_on_its_declared_time_field() -> None:
    assert sample_windows([_profile()], [_entity()], "warehouse", 7) == {
        EVENTS: SampleWindow(kind="days", n=7, column="event_date")
    }
    assert sample_windows([_profile()], [_entity(time_field=None)], "warehouse", 7) == {}
    assert sample_windows([_profile()], [_entity(table="ops.other")], "warehouse", 7) == {}
    assert sample_windows([_profile()], [_entity()], "warehouse", 0) == {}  # whole tables


def test_ingestion_time_partitioning_is_recorded_as_no_window() -> None:
    profiles = [_profile(PartitioningProfile(column=None, kind="ingestion"))]
    got = sample_windows(profiles, [_entity()], "warehouse", 7)[EVENTS]
    assert got.kind == "none" and got.reason and "ingestion-time" in got.reason


# ── the SQL carries it ───────────────────────────────────────────────────────


def _scripted(newest: date, log: list[str]):  # noqa: ANN202
    def run(sql: str) -> QueryResult:
        log.append(sql)
        if sql.startswith("SELECT MIN("):
            return QueryResult(columns=["mn", "mx"], rows=[[date(2024, 1, 1), newest]])
        if " AS v, COUNT(*) AS n FROM" in sql:
            return QueryResult(columns=["v", "n"], rows=[["click", 3], ["view", 2]])
        return QueryResult(columns=["n_rows", "nn_0", "dc_0"], rows=[[5, 5, 2]])

    return run


def test_bigquery_sampling_sql_carries_the_partition_filter() -> None:
    """The window is the partition filter, anchored on the data's newest
    partition (the latest N partitions of the DATA, not of the calendar);
    the freshness/coverage probe stays whole-table."""
    log: list[str] = []
    spec = TableSampleSpec(
        table="proj.ds.events",
        columns=[ColumnSampleSpec(name="kind", type="STRING")],
        row_count=10,
        freshness_column="event_date",
        window=SampleWindow(kind="partitions", n=7, column="event_date"),
        window_type="DATE",
    )
    got = sample_table(
        _scripted(date(2026, 3, 10), log),
        spec,
        connection="bq",
        dialect="bigquery",
        max_rows=100,
        max_distinct_for_enum=25,
    )
    assert got is not None and got.sample_window == spec.window
    freshness, stats, top = log[0], log[1], log[2]
    assert (
        freshness == "SELECT MIN(`event_date`) AS mn, MAX(`event_date`) AS mx FROM `proj.ds.events`"
    )
    assert "WHERE `event_date` >= DATE '2026-03-04'" in stats
    assert "WHERE `event_date` >= DATE '2026-03-04'" in top
    # Windowed facts are labelled partial: a lower-bound count, never a
    # complete dictionary, the slice's row count on record.
    kind = got.columns[0]
    assert kind.top_values == ["click", "view"] and kind.values_complete is False
    assert kind.distinct_is_exact is False and got.sampled_rows == 5
    assert got.last_updated_logical == datetime(2026, 3, 10, tzinfo=UTC)
    assert window_predicate("ts", "TIMESTAMP", "2026-03-04", dialect="bigquery") == (
        "`ts` >= TIMESTAMP '2026-03-04'"
    )


def test_a_window_with_nothing_to_anchor_on_reads_whole_and_says_why() -> None:
    def run(sql: str) -> QueryResult:
        if sql.startswith("SELECT MIN("):
            return QueryResult(columns=["mn", "mx"], rows=[[None, None]])
        return QueryResult(columns=["n_rows", "nn_0", "dc_0"], rows=[[0, 0, 0]])

    spec = TableSampleSpec(
        table="t",
        columns=[ColumnSampleSpec(name="kind", type="VARCHAR")],
        freshness_column="event_date",
        window=SampleWindow(kind="days", n=7, column="event_date"),
    )
    got = sample_table(
        run, spec, connection="c", dialect="duckdb", max_rows=100, max_distinct_for_enum=25
    )
    assert got is not None and got.sample_window is not None
    assert got.sample_window.kind == "none" and got.sample_window.reason


# ── DuckDB, live ─────────────────────────────────────────────────────────────


def _events(path: Path) -> DuckDBConnector:
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS ops")
    con.execute("CREATE TABLE ops.events (event_date DATE, kind VARCHAR)")
    # 100 days: 'legacy' only in the first 90, 'click'/'view' throughout.
    con.execute(
        "INSERT INTO ops.events SELECT DATE '2026-01-01' + INTERVAL (i) DAY, "
        "CASE WHEN i < 90 AND i % 3 = 0 THEN 'legacy' WHEN i % 2 = 0 THEN 'click' ELSE 'view' END "
        "FROM range(100) r(i)"
    )
    con.close()
    return DuckDBConnector(str(path))


def test_duckdb_narrows_to_the_window_and_the_artifact_records_it(tmp_path: Path) -> None:
    connector = _events(tmp_path / "wh.duckdb")
    profiles = catalog_profiles(connector, "warehouse")
    windows = sample_windows(profiles, [_entity()], "warehouse", 7)
    assert windows == {EVENTS: SampleWindow(kind="days", n=7, column="event_date")}

    whole = sample_profiles(connector, profiles)[EVENTS]
    kind = next(c for c in whole.columns if c.name == "kind")
    assert sorted(kind.top_values or []) == ["click", "legacy", "view"] and kind.values_complete
    assert kind.values_complete_at is not None and whole.sample_window is None

    narrowed = sample_profiles(connector, profiles, windows=windows)[EVENTS]
    assert narrowed.sample_window == windows[EVENTS]
    kind = next(c for c in narrowed.columns if c.name == "kind")
    assert sorted(kind.top_values or []) == ["click", "view"]  # 'legacy' is outside the window
    assert kind.values_complete is False and kind.distinct_is_exact is False
    assert narrowed.sampled_rows == 7
    # Whole-table facts stay whole-table.
    assert narrowed.time_coverage is not None
    assert narrowed.time_coverage.min == datetime(2026, 1, 1, tzinfo=UTC)
    assert narrowed.last_updated_logical == datetime(2026, 4, 10, tzinfo=UTC)
    spec = build_sample_spec(profiles[0], window=windows[EVENTS])
    assert spec is not None and spec.window_type == "DATE"


# ── a complete dictionary survives a windowed refresh ────────────────────────


def test_a_complete_dictionary_stays_complete_through_a_window() -> None:
    then = datetime(2026, 3, 1, tzinfo=UTC)
    prior = ProbeArtifact(
        connection="warehouse",
        probed_at=then,
        tables=[
            TableProfile(
                connection="warehouse",
                table=EVENTS,
                profiled_at=then,
                columns=[
                    ColumnProfile(
                        name="kind",
                        type="VARCHAR",
                        top_values=["click", "view", "legacy"],
                        values_complete=True,
                        distinct_count=3,
                        distinct_is_exact=True,
                        is_low_cardinality=True,
                    )
                ],
            )
        ],
    )
    windowed = TableProfile(
        connection="warehouse",
        table=EVENTS,
        sample_window=SampleWindow(kind="days", n=7, column="event_date"),
        columns=[
            ColumnProfile(
                name="kind", type="VARCHAR", top_values=["click", "scroll"], values_complete=False
            )
        ],
    )
    kept = carry_complete_dictionaries([windowed], prior)[0].columns[0]
    assert kept.top_values == ["click", "view", "legacy", "scroll"]  # the union, prior order first
    assert kept.values_complete is True and kept.values_complete_at == then
    assert kept.distinct_count == 4 and kept.distinct_is_exact is False  # a value the window added
    # A whole-table read that came back partial is partial — only a WINDOW keeps.
    whole = windowed.model_copy(update={"sample_window": None})
    assert carry_complete_dictionaries([whole], prior)[0].columns[0].values_complete is False
    assert carry_complete_dictionaries([windowed], None) == [windowed]


# ── the verb, end to end ─────────────────────────────────────────────────────


def _project(root: Path, recent_partitions: int) -> Path:
    (root / "semantic" / "entities").mkdir(parents=True, exist_ok=True)
    (root / "dst.yaml").write_text(
        "name: t\nconnections:\n  warehouse:\n    type: duckdb\n"
        f"    config: {{path: {root / 'wh.duckdb'}}}\n"
        f"probe:\n  recent_partitions: {recent_partitions}\n",
        encoding="utf-8",
    )
    (root / "semantic" / "entities" / "events.yaml").write_text(
        "name: events\ndescription: d\ngrain: g\nsource:\n  connection: warehouse\n"
        f"  table: {EVENTS}\ndefault_time_field: event_date\nfields:\n"
        "  - name: event_date\n    type: date\n  - name: kind\n    type: string\n",
        encoding="utf-8",
    )
    if not (root / "wh.duckdb").exists():
        _events(root / "wh.duckdb")
    return root


def _cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def test_the_first_probe_reads_whole_and_the_refresh_reads_the_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, recent_partitions=7)
    assert _cli(monkeypatch, ["probe", "--dir", str(root)]) == 0
    first = probe.read_probe(root, "warehouse")
    assert first is not None
    kind = next(c for c in first.tables[0].columns if c.name == "kind")
    assert kind.values_complete and sorted(kind.top_values or []) == ["click", "legacy", "view"]
    assert first.tables[0].sample_window is None  # no artifact yet: whole table
    capsys.readouterr()

    assert _cli(monkeypatch, ["probe", "--dir", str(root)]) == 0
    assert "sampled over the last 7 partitions/days" in capsys.readouterr().out
    second = probe.read_probe(root, "warehouse")
    assert second is not None
    table = second.tables[0]
    assert table.sample_window == SampleWindow(kind="days", n=7, column="event_date")
    kind = next(c for c in table.columns if c.name == "kind")
    # The window saw two values; the dictionary the first probe proved
    # complete is kept whole and dated to that read.
    assert kind.values_complete and sorted(kind.top_values or []) == ["click", "legacy", "view"]
    assert kind.values_complete_at is not None
    assert kind.values_complete_at.date() == datetime.now(UTC).date()
    assert table.sampled_rows == 7

    # recent_partitions: 0 reads whole tables again.
    _project(root, recent_partitions=0)
    assert _cli(monkeypatch, ["probe", "--dir", str(root)]) == 0
    third = probe.read_probe(root, "warehouse")
    assert third is not None and third.tables[0].sample_window is None
