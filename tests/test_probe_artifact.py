"""The probe artifact carries the warehouse's VALUES into the project — deliberately.

The failure this pins: asked for sales in a country whose `country` column
holds 'FI', generation writes ``WHERE country = 'Finland'`` and a confident zero
grades verified. The drift baseline whitelists schema fields so `--accept`
history stays quiet; the probe artifact exists because the literals ARE the
business knowledge. Pinned here: the verbose fields actually reach the file (the
baseline's whitelist must NOT apply), the entity cross-reference annotates mapped
tables, the two file shapes stay disjoint (neither reader accepts the other's
file), and the CLI verb works end to end against a real DuckDB with no server.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from services.contracts.profile import ColumnProfile, PartitioningProfile, TableProfile
from services.contracts.semantic_model import EntitySource, Field
from services.contracts.shared_semantic import SharedEntity
from services.project import probe
from services.project import warehouse_drift as wd

# ── fixtures ─────────────────────────────────────────────────────────────────

CUSTOMERS = "ops.customers"


def _customers() -> list[TableProfile]:
    return [
        TableProfile(
            connection="warehouse",
            table=CUSTOMERS,
            row_count=2,
            partitioning=PartitioningProfile(column="signup_date", kind="time"),
            columns=[
                ColumnProfile(
                    name="email",
                    type="VARCHAR",
                    top_values=["ada@corp.example", "bo@corp.example"],
                    values_complete=True,
                ),
                ColumnProfile(
                    name="country", type="VARCHAR", top_values=["FI", "DK"], values_complete=True
                ),
            ],
        )
    ]


def _entities() -> list[SharedEntity]:
    return [
        SharedEntity(
            name="customers",
            source=EntitySource(connection="warehouse", table=CUSTOMERS),
            fields=[
                Field(name="email", type="string"),
                Field(name="country", type="string"),
            ],
        )
    ]


# ── the artifact file ────────────────────────────────────────────────────────


def test_verbose_fields_reach_the_file_and_round_trip(tmp_path: Path) -> None:
    """The whole reason this file exists next to the whitelisted baseline: the
    value dictionary, the partition map and the row count must survive the
    write. If this ever starts filtering like `_BASELINE_INCLUDE`, serving is
    back to guessing 'Finland'."""
    path, artifact = probe.write_probe(tmp_path, "warehouse", _customers(), _entities())
    text = path.read_text(encoding="utf-8")
    assert "'FI'" not in text and '"FI"' in text  # the literal, JSON-encoded
    assert '"signup_date"' in text and '"row_count": 2' in text

    read = probe.read_probe(tmp_path, "warehouse")
    assert read is not None
    country = next(c for c in read.tables[0].columns if c.name == "country")
    assert country.top_values == ["FI", "DK"] and country.values_complete is True
    assert read.tables[0].partitioning is not None
    assert read.tables[0].partitioning.column == "signup_date"


def test_the_artifact_holds_exactly_what_was_sampled(tmp_path: Path) -> None:
    """The write is not a filter. Whatever the sampling pass collected lands in
    the file verbatim — which is why the decision about what may be read at all
    belongs at collection (`exclude_columns`), where it is one decision instead
    of one per consumer."""
    path, artifact = probe.write_probe(tmp_path, "warehouse", _customers(), _entities())
    written = path.read_text(encoding="utf-8")
    for column in _customers()[0].columns:
        stored = next(c for c in artifact.tables[0].columns if c.name == column.name)
        assert stored.top_values == column.top_values
        for value in column.top_values or []:
            assert value in written


def test_entity_map_annotates_mapped_tables_only(tmp_path: Path) -> None:
    """The artifact is the warehouse UNDER THE LAYER: a mapped table names its
    entities (unqualified references match too), an unmapped one gets no key —
    that absence is authoring signal, not noise to fill."""
    mystery = TableProfile(connection="warehouse", table="ops.mystery", columns=[])
    _path, artifact = probe.write_probe(
        tmp_path, "warehouse", [*_customers(), mystery], _entities()
    )
    assert artifact.entities == {CUSTOMERS: ["customers"]}


def test_the_two_profile_files_never_read_as_each_other(tmp_path: Path) -> None:
    """`profiles/` holds both files. The baseline reader must not swallow the
    probe artifact (its `--accept` history would drown in nightly rewrites) and
    the probe reader must not swallow a baseline (schema-only, it would blank
    every value dictionary). Disjoint shapes, pinned."""
    probe.write_probe(tmp_path, "warehouse", _customers(), _entities())
    wd.write_baseline(tmp_path, "warehouse", _customers())
    assert wd.baseline_connections(tmp_path) == ["warehouse"]  # probe file skipped
    baseline = wd.read_baseline(tmp_path, "warehouse")
    assert baseline is not None and baseline.tables[0].columns[0].top_values is None
    read = probe.read_probe(tmp_path, "warehouse")
    assert read is not None and read.entities  # the probe file, not the baseline

    # A corrupt artifact costs a re-probe, never a command.
    probe.probe_path(tmp_path, "warehouse").write_text("{not json", encoding="utf-8")
    assert probe.read_probe(tmp_path, "warehouse") is None


def test_parse_probe_names_the_file_that_failed() -> None:
    with pytest.raises(ValueError, match="profiles/x.probe.json"):
        probe.parse_probe("{}", "profiles/x.probe.json")
    assert probe.is_probe_path("profiles/warehouse.probe.json")
    assert not probe.is_probe_path("profiles/warehouse.json")
    assert not probe.is_probe_path("semantic/entities/warehouse.probe.json")


# ── the CLI verb, end to end against a real warehouse ────────────────────────


def _project(root: Path) -> Path:
    (root / "semantic" / "entities").mkdir(parents=True)
    (root / "dst.yaml").write_text(
        "name: t\nconnections:\n  warehouse:\n    type: duckdb\n"
        f"    config: {{path: {root / 'wh.duckdb'}}}\n",
        encoding="utf-8",
    )
    (root / "semantic" / "entities" / "customers.yaml").write_text(
        "name: customers\ndescription: d\ngrain: g\nsource:\n  connection: warehouse\n"
        f"  table: {CUSTOMERS}\nfields:\n  - name: email\n    type: string\n"
        "  - name: country\n    type: string\n",
        encoding="utf-8",
    )
    con = duckdb.connect(str(root / "wh.duckdb"))
    con.execute("CREATE SCHEMA IF NOT EXISTS ops")
    con.execute("CREATE TABLE ops.customers (email VARCHAR, country VARCHAR)")
    con.execute(
        "INSERT INTO ops.customers VALUES ('ada@corp.example', 'FI'), ('bo@corp.example', 'DK')"
    )
    con.close()
    return root


def _cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def test_probe_records_the_warehouse_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No server, no prior apply, no --connection: every declared warehouse
    probes, the sampled literals land in the file, and the summary counts what
    was actually written."""
    root = _project(tmp_path)
    assert _cli(monkeypatch, ["probe", "--dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert "probed 'warehouse'" in out and "value dictionaries" in out

    text = probe.probe_path(root, "warehouse").read_text(encoding="utf-8")
    assert '"FI"' in text
    read = probe.read_probe(root, "warehouse")
    assert read is not None and read.entities == {CUSTOMERS: ["customers"]}

    # Re-probing an unchanged warehouse succeeds and stays one file.
    assert _cli(monkeypatch, ["probe", "--dir", str(root)]) == 0


def test_sampling_scopes_to_the_layer_and_says_what_it_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The large-warehouse rule: catalog everywhere, sampling only where the
    layer reads — an unmapped table still lands in the artifact (schema, counts)
    but pays no sampling read, and the skip is printed, never silent.
    --sample-all buys the wide form back."""
    root = _project(tmp_path)
    con = duckdb.connect(str(root / "wh.duckdb"))
    con.execute("CREATE TABLE ops.noise (status VARCHAR)")
    con.execute("INSERT INTO ops.noise VALUES ('A'), ('B')")
    con.close()

    assert _cli(monkeypatch, ["probe", "--dir", str(root)]) == 0
    assert "sampled 1 of 2 table(s)" in capsys.readouterr().err
    read = probe.read_probe(root, "warehouse")
    assert read is not None
    noise = next(t for t in read.tables if t.table == "ops.noise")
    assert all(c.top_values is None for c in noise.columns)  # cataloged, not sampled
    mapped = next(t for t in read.tables if t.table == CUSTOMERS)
    assert any(c.top_values for c in mapped.columns)

    assert _cli(monkeypatch, ["probe", "--dir", str(root), "--sample-all"]) == 0
    assert "sampled" not in capsys.readouterr().err  # nothing skipped, nothing to confess
    read = probe.read_probe(root, "warehouse")
    assert read is not None
    noise = next(t for t in read.tables if t.table == "ops.noise")
    assert next(c for c in noise.columns if c.name == "status").top_values == ["A", "B"]


def test_an_empty_layer_samples_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bootstrap mode: the first probe runs BEFORE authoring, when no entity
    exists to scope by — scoping then would write a dictionary-free artifact and
    the authoring agent would have nothing to author from."""
    root = _project(tmp_path)
    (root / "semantic" / "entities" / "customers.yaml").unlink()
    assert _cli(monkeypatch, ["probe", "--dir", str(root)]) == 0
    read = probe.read_probe(root, "warehouse")
    assert read is not None
    country = next(c for c in read.tables[0].columns if c.name == "country")
    assert sorted(country.top_values or []) == ["DK", "FI"]


def test_probe_fails_loudly_on_an_empty_warehouse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty output is never success: an artifact recording zero
    tables would read as a probed warehouse to every later consumer."""
    root = _project(tmp_path)
    con = duckdb.connect(str(root / "wh.duckdb"))
    con.execute("DROP TABLE ops.customers")
    con.close()
    assert _cli(monkeypatch, ["probe", "--dir", str(root)]) == 1
    assert "listed no tables" in capsys.readouterr().err
    assert not probe.probe_path(root, "warehouse").exists()
