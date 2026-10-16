"""Semantic drift and the baseline's lifecycle.

A column whose COMMENT changed from "UTC" to "local time", and whose values
moved by hours with it, has the same name, type and nullability on both days:
a schema-only diff is clean, and every duration derived from the column serves
wrong, verified. The baseline therefore carries comments and the two measured
numbers drift compares (a time column's median offset from the entity clock,
a numeric mean), plus enum dictionaries; the diff crosses the newest probe
artifact against it, under the tolerance dst.yaml declares.

The second half is the evidence itself: `dst probe` used to rewrite the
baseline on every run, so the nightly recipe erased each change before anyone
read it. The baseline advances by `dst drift --accept` or by a deploy — never
by a probe after the first.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.semantic_model import EntitySource, Field
from services.contracts.shared_semantic import SharedEntity
from services.project import probe
from services.project import warehouse_drift as wd
from services.project.schema import DriftTolerance

ORDERS = "ops.orders"


def _column(name: str, **kw: object) -> ColumnProfile:
    return ColumnProfile(name=name, type="TIMESTAMP", **kw)  # type: ignore[arg-type]


def _table(*columns: ColumnProfile) -> list[TableProfile]:
    return [TableProfile(connection="warehouse", table=ORDERS, columns=list(columns))]


def _entities() -> dict[str, SharedEntity]:
    return {
        "semantic/entities/orders.yaml": SharedEntity(
            name="orders",
            source=EntitySource(connection="warehouse", table=ORDERS),
            default_time_field="ordered_at",
            fields=[
                Field(name="ordered_at", type="timestamp"),
                Field(name="shipped_at", type="timestamp"),
            ],
        )
    }


def _cross(deltas: list) -> list[wd.Finding]:  # type: ignore[type-arg]
    return wd.cross_reference(deltas, _entities(), {})


# ── the diff ─────────────────────────────────────────────────────────────────


def test_a_changed_column_comment_breaks_a_declared_reference() -> None:
    before = _table(_column("shipped_at", description="shipment time, UTC"))
    after = _table(_column("shipped_at", description="shipment time, local"))
    findings = _cross(wd.baseline_drift(before, after))
    assert [f.kind for f in findings] == ["column_recommented"]
    assert wd.is_breaking(findings[0]) and wd.exit_code(findings) == 1
    text = "\n".join(wd.render(findings[0]))
    assert "RECOMMENTED `shipped_at` 'shipment time, UTC' -> 'shipment time, local'" in text
    assert "entity `orders`" in text and "stated meaning changed" in text


def test_a_comment_appearing_on_an_undocumented_column_is_a_recommented_finding() -> None:
    # The warehouse now states a meaning the layer was authored without; the
    # author decides whether the layer agrees, so it is named, never silent.
    before = _table(_column("shipped_at"))
    after = _table(_column("shipped_at", description="now documented"))
    found = wd.baseline_drift(before, after)
    assert [d.kind for d in found] == ["column_recommented"]
    assert "'(none)' -> 'now documented'" in found[0].detail


def test_a_duration_shift_beyond_tolerance_breaks_within_it_is_silent() -> None:
    """Two hours of shipped_at - ordered_at moved by three: past a one-hour
    tolerance, the duration every answer derives is not the one verified."""
    tolerance = DriftTolerance(time_shift="1h")
    before = _table(_column("shipped_at", time_offset_seconds=7_200.0))
    moved = _table(_column("shipped_at", time_offset_seconds=7_200.0 + 3 * 3_600))
    findings = _cross(wd.profile_drift(before, moved, tolerance))
    assert [f.kind for f in findings] == ["column_shifted"]
    assert wd.is_breaking(findings[0])
    text = "\n".join(wd.render(findings[0]))
    assert "SHIFTED `shipped_at`" in text and "2h -> 5h (+3h, tolerance 1h)" in text
    assert "not what was verified" in text

    within = _table(_column("shipped_at", time_offset_seconds=7_200.0 + 1_800))
    assert wd.profile_drift(before, within, tolerance) == []
    # A fact measured on one side only is no delta.
    assert wd.profile_drift(before, _table(_column("shipped_at")), tolerance) == []


def test_a_numeric_mean_shift_is_relative_to_the_baseline() -> None:
    tolerance = DriftTolerance(numeric_shift=0.2)
    before = _table(ColumnProfile(name="amount", type="DECIMAL", mean=100.0))
    assert (
        wd.profile_drift(
            before, _table(ColumnProfile(name="amount", type="DECIMAL", mean=115.0)), tolerance
        )
        == []
    )
    [delta] = wd.profile_drift(
        before, _table(ColumnProfile(name="amount", type="DECIMAL", mean=130.0)), tolerance
    )
    assert delta.kind == "column_shifted" and "mean 100 -> 130 (+30%" in delta.detail


def test_a_dictionary_member_gone_breaks_a_member_gained_does_not() -> None:
    before = _table(
        ColumnProfile(
            name="status", type="VARCHAR", top_values=["open", "paid"], values_complete=True
        )
    )
    renamed = _table(
        ColumnProfile(
            name="status", type="VARCHAR", top_values=["open", "settled"], values_complete=True
        )
    )
    findings = _cross(wd.profile_drift(before, renamed, DriftTolerance()))
    kinds = {f.kind: f for f in findings}
    assert set(kinds) == {"dictionary_changed", "enum_value_added"}
    assert wd.is_breaking(kinds["dictionary_changed"])
    assert "values gone: paid" in kinds["dictionary_changed"].detail
    assert not wd.is_breaking(kinds["enum_value_added"])
    assert wd.exit_code(findings) == 1


# ── the baseline file ────────────────────────────────────────────────────────


def test_the_baseline_carries_comments_measures_and_enum_values_only(tmp_path: Path) -> None:
    """What the committed file holds: the comment, the two compared numbers,
    an enum-sized complete dictionary verbatim; a long complete dictionary
    as a digest only; a partial list and min/max never."""
    names = [f"team-{i}" for i in range(40)]
    wd.write_baseline(
        tmp_path,
        "warehouse",
        _table(
            _column("shipped_at", description="UTC", time_offset_seconds=7_200.0, min="2026-01-01"),
            ColumnProfile(name="amount", type="DECIMAL", mean=12.5),
            ColumnProfile(
                name="status", type="VARCHAR", top_values=["a", "b"], values_complete=True
            ),
            ColumnProfile(
                name="team",
                type="VARCHAR",
                top_values=names,
                values_complete=True,
                distinct_count=40,
            ),
            ColumnProfile(
                name="note", type="VARCHAR", top_values=["partial"], values_complete=False
            ),
        ),
    )
    written = json.loads(wd.baseline_path(tmp_path, "warehouse").read_text(encoding="utf-8"))
    by_name = {c["name"]: c for c in written["tables"][0]["columns"]}
    assert by_name["shipped_at"]["description"] == "UTC"
    assert by_name["shipped_at"]["time_offset_seconds"] == 7_200.0
    assert "min" not in by_name["shipped_at"]
    assert by_name["amount"]["mean"] == 12.5
    assert by_name["status"]["top_values"] == ["a", "b"]
    assert by_name["team"]["top_values"] is None and by_name["team"]["values_digest"]
    assert by_name["note"]["top_values"] is None and by_name["note"]["values_digest"] is None
    raw = wd.baseline_path(tmp_path, "warehouse").read_text(encoding="utf-8")
    assert "team-1" not in raw and "partial" not in raw


def test_baseline_changed_ignores_the_timestamp(tmp_path: Path) -> None:
    tables = _table(_column("shipped_at", description="UTC"))
    assert wd.baseline_changed(tmp_path, "warehouse", tables)  # none yet
    wd.write_baseline(tmp_path, "warehouse", tables)
    assert not wd.baseline_changed(tmp_path, "warehouse", tables)
    assert wd.baseline_changed(
        tmp_path, "warehouse", _table(_column("shipped_at", description="local"))
    )


# ── the CLI, end to end on DuckDB ────────────────────────────────────────────


def _project(root: Path, tolerance: str | None = None) -> Path:
    (root / "semantic" / "entities").mkdir(parents=True)
    probe_block = f"probe:\n  drift_tolerance:\n    time_shift: {tolerance}\n" if tolerance else ""
    (root / "dst.yaml").write_text(
        "name: t\nconnections:\n  warehouse:\n    type: duckdb\n"
        f"    config: {{path: {root / 'wh.duckdb'}}}\n{probe_block}",
        encoding="utf-8",
    )
    (root / "semantic" / "entities" / "orders.yaml").write_text(
        "name: orders\ndescription: d\ngrain: g\nsource:\n  connection: warehouse\n"
        f"  table: {ORDERS}\ndefault_time_field: ordered_at\nfields:\n"
        "  - name: ordered_at\n    type: timestamp\n  - name: shipped_at\n    type: timestamp\n"
        "  - name: status\n    type: string\n",
        encoding="utf-8",
    )
    con = duckdb.connect(str(root / "wh.duckdb"))
    con.execute("CREATE SCHEMA IF NOT EXISTS ops")
    con.execute(
        "CREATE TABLE ops.orders AS SELECT "
        "TIMESTAMP '2026-01-01 08:00:00' + INTERVAL (i) DAY AS ordered_at, "
        "TIMESTAMP '2026-01-01 10:00:00' + INTERVAL (i) DAY AS shipped_at, "
        "CASE WHEN i % 2 = 0 THEN 'open' ELSE 'paid' END AS status FROM range(30) t(i)"
    )
    con.execute("COMMENT ON COLUMN ops.orders.shipped_at IS 'shipment time, UTC'")
    con.close()
    return root


def _cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def _sql(root: Path, statement: str) -> None:
    con = duckdb.connect(str(root / "wh.duckdb"))
    con.execute(statement)
    con.close()


def test_probe_arms_once_and_never_overwrites_the_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    where = ["--connection", "warehouse", "--dir", str(root)]
    assert _cli(monkeypatch, ["probe", *where]) == 0
    assert "drift baseline recorded (first probe)" in capsys.readouterr().err
    baseline = wd.baseline_path(root, "warehouse")
    recorded = baseline.read_text(encoding="utf-8")
    assert _cli(monkeypatch, ["drift", *where]) == 0
    capsys.readouterr()

    _sql(root, "ALTER TABLE ops.orders ADD COLUMN carrier VARCHAR")
    # The nightly re-probe writes the artifact beside the baseline and leaves
    # the baseline alone — the evidence drift needs survives the probe.
    assert _cli(monkeypatch, ["probe", *where]) == 0
    assert baseline.read_text(encoding="utf-8") == recorded
    err = capsys.readouterr().err
    assert "first probe" not in err
    assert "sampling statement(s); bytes not reported by duckdb" in err
    assert _cli(monkeypatch, ["drift", *where]) == 2
    assert "gained column `carrier`" in capsys.readouterr().out

    # Accepting advances it; the next run is clean against the new record.
    assert _cli(monkeypatch, ["drift", "--accept", *where]) == 0
    assert baseline.read_text(encoding="utf-8") != recorded
    assert "carrier" in baseline.read_text(encoding="utf-8")
    capsys.readouterr()
    assert _cli(monkeypatch, ["drift", *where]) == 0


def test_a_comment_change_in_the_live_warehouse_is_breaking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path)
    where = ["--connection", "warehouse", "--dir", str(root)]
    assert _cli(monkeypatch, ["probe", *where]) == 0
    _sql(root, "COMMENT ON COLUMN ops.orders.shipped_at IS 'shipment time, local'")
    capsys.readouterr()
    assert _cli(monkeypatch, ["drift", *where]) == 1
    out = capsys.readouterr().out
    assert "RECOMMENTED `shipped_at` 'shipment time, UTC' -> 'shipment time, local'" in out
    assert "entity `orders`" in out and "BREAKING" in out


def test_a_shift_past_tolerance_in_the_newest_artifact_is_breaking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The planted failure, end to end: shipped_at moves three hours against
    ordered_at while the schema stays identical. The probe measures the
    offset, the baseline remembers the old one, drift names the entity."""
    root = _project(tmp_path, tolerance="1h")
    where = ["--connection", "warehouse", "--dir", str(root)]
    assert _cli(monkeypatch, ["probe", *where]) == 0
    artifact = probe.read_probe(root, "warehouse")
    assert artifact is not None
    shipped = next(c for c in artifact.tables[0].columns if c.name == "shipped_at")
    assert shipped.time_offset_seconds == 7_200.0  # two hours, from the entity clock
    assert (
        next(c for c in artifact.tables[0].columns if c.name == "ordered_at").time_offset_seconds
        is None
    )

    _sql(root, "UPDATE ops.orders SET shipped_at = shipped_at + INTERVAL 3 HOUR")
    assert _cli(monkeypatch, ["probe", *where]) == 0
    capsys.readouterr()
    assert _cli(monkeypatch, ["drift", *where]) == 1
    out = capsys.readouterr().out
    assert "SHIFTED `shipped_at`" in out and "2h -> 5h (+3h, tolerance 1h)" in out
    assert "entity `orders`" in out and "1 BREAKING" in out

    # Within tolerance stays silent: accept, nudge by half an hour, clean.
    assert _cli(monkeypatch, ["drift", "--accept", *where]) == 0
    _sql(root, "UPDATE ops.orders SET shipped_at = shipped_at + INTERVAL 30 MINUTE")
    assert _cli(monkeypatch, ["probe", *where]) == 0
    capsys.readouterr()
    assert _cli(monkeypatch, ["drift", *where]) == 0
    assert capsys.readouterr().out == ""


def test_a_deploy_advances_the_baseline_to_the_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.cli.main import _advance_baselines

    root = _project(tmp_path)
    where = ["--connection", "warehouse", "--dir", str(root)]
    assert _cli(monkeypatch, ["probe", *where]) == 0
    baseline = wd.baseline_path(root, "warehouse")
    recorded = baseline.read_text(encoding="utf-8")
    capsys.readouterr()
    _advance_baselines(root)
    assert baseline.read_text(encoding="utf-8") == recorded  # nothing moved: no rewrite
    assert capsys.readouterr().err == ""

    _sql(root, "ALTER TABLE ops.orders ADD COLUMN carrier VARCHAR")
    assert _cli(monkeypatch, ["probe", *where]) == 0
    capsys.readouterr()
    _advance_baselines(root)
    assert "drift baseline advanced for 'warehouse'" in capsys.readouterr().err
    assert "carrier" in baseline.read_text(encoding="utf-8")
    assert _cli(monkeypatch, ["drift", *where]) == 0


def test_drift_tolerance_is_declared_in_dst_yaml() -> None:
    from services.project.schema import parse_project_yaml

    project = parse_project_yaml(
        "name: t\nconnections: {}\nprobe:\n  drift_tolerance:\n    time_shift: 30m\n"
        "    numeric_shift: 0.05\n"
    )
    assert project.probe.drift_tolerance.time_shift_seconds == 1_800
    assert project.probe.drift_tolerance.numeric_shift == 0.05
    assert parse_project_yaml("name: t\nconnections: {}\n").probe.drift_tolerance.time_shift == "1h"
    with pytest.raises(ValueError, match="needs a unit"):
        parse_project_yaml(
            "name: t\nconnections: {}\nprobe:\n  drift_tolerance:\n    time_shift: 7\n"
        )


# ── a measured catalog still gets its distribution facts ─────────────────────


def test_a_comment_appearing_on_a_bare_column_is_a_recommented_finding() -> None:
    from services.contracts.profile import ColumnProfile, TableProfile
    from services.project.warehouse_drift import _comment_drift

    before = TableProfile(
        connection="wh",
        table="public.jobs",
        columns=[ColumnProfile(name="closed_at", type="timestamp")],
    )
    after = before.model_copy(
        update={
            "columns": [ColumnProfile(name="closed_at", type="timestamp", description="local time")]
        }
    )
    added = _comment_drift(before, after)
    assert [d.kind for d in added] == ["column_recommented"]
    assert "'(none)' -> 'local time'" in added[0].detail
    removed = _comment_drift(after, before)
    assert len(removed) == 1 and "(removed)" in removed[0].detail
    assert _comment_drift(before, before) == []


def test_postgres_stats_statement_carries_mean_and_median_offset() -> None:
    from services.connectors.sampling import stats_sql
    from services.contracts.profile import ColumnSampleSpec

    sql = stats_sql(
        "public.jobs",
        [
            ColumnSampleSpec(name="amount", type="numeric", distribution_only=True),
            ColumnSampleSpec(name="closed_at", type="timestamp", distribution_only=True),
        ],
        dialect="postgres",
        max_rows=1000,
        row_count=10,
        exact=True,
        time_field="opened_at",
    )
    assert sql is not None
    assert 'AVG("amount") AS av_0' in sql
    assert "percentile_cont(0.5) WITHIN GROUP" in sql and "EXTRACT(EPOCH FROM" in sql
    assert "AS md_1" in sql
    # Distribution-only columns read nothing else: the catalog's facts stand.
    assert "nn_0" not in sql and "dc_1" not in sql and "mn_0" not in sql


def test_a_fully_measured_table_still_samples_its_distribution() -> None:
    """Postgres' catalog pass measures every column's values, so nothing is a
    gap — and the baseline recorded no means or offsets, so semantic drift
    could never fire there."""
    from services.connectors.sampling import _fold_stats
    from services.contracts.profile import ColumnProfile, TableProfile
    from services.contracts.protocols import QueryResult
    from services.lenses.profiler import build_sample_spec

    measured = TableProfile(
        connection="wh",
        table="public.jobs",
        columns=[
            ColumnProfile(name="id", type="integer", null_rate=0.0, top_values=["1"]),
            ColumnProfile(name="amount", type="numeric", null_rate=0.0, top_values=["9"]),
            ColumnProfile(name="opened_at", type="timestamp", null_rate=0.0, top_values=[]),
            ColumnProfile(name="closed_at", type="timestamp", null_rate=0.1, top_values=[]),
            ColumnProfile(name="note", type="text", null_rate=0.5, top_values=["a"]),
        ],
    )
    spec = build_sample_spec(measured, time_field="opened_at")
    assert spec is not None
    wanted = {(c.name, c.distribution_only) for c in spec.columns}
    assert ("amount", True) in wanted and ("id", True) in wanted
    assert ("closed_at", True) in wanted
    assert not any(c.name in ("opened_at", "note") for c in spec.columns)  # the anchor, text
    # Once measured, nothing is asked again.
    again = measured.model_copy(
        update={
            "columns": [
                c.model_copy(update={"mean": 1.0, "time_offset_seconds": 0.0})
                for c in measured.columns
            ]
        }
    )
    later = build_sample_spec(again, time_field="opened_at")
    assert later is None or not any(c.distribution_only for c in later.columns)
    # The folded fragment carries only the distribution facts.
    cols = [c for c in spec.columns if c.name in ("amount", "closed_at")]
    folded = _fold_stats(
        cols,
        QueryResult(columns=["n_rows", "av_0", "md_1"], rows=[[10, 4.5, -3600.0]]),
        exact=True,
        max_distinct_for_enum=20,
    )
    assert [(c.name, c.mean, c.time_offset_seconds, c.null_rate) for c in folded] == [
        ("amount", 4.5, None, None),
        ("closed_at", None, -3600.0, None),
    ]


def test_accept_keeps_the_measured_facts_so_a_later_shift_fires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`drift --accept` after a schema change must carry the probe's measured
    offset into the new baseline: a baseline without it can never report
    `column_shifted`, whatever the next probe measures."""
    root = _project(tmp_path, tolerance="1h")
    where = ["--connection", "warehouse", "--dir", str(root)]
    assert _cli(monkeypatch, ["probe", *where]) == 0
    _sql(root, "ALTER TABLE ops.orders ADD COLUMN carrier VARCHAR")
    assert _cli(monkeypatch, ["drift", "--accept", *where]) == 0
    recorded = json.loads(wd.baseline_path(root, "warehouse").read_text(encoding="utf-8"))
    shipped = next(c for c in recorded["tables"][0]["columns"] if c["name"] == "shipped_at")
    assert shipped["time_offset_seconds"] == 7_200.0
    capsys.readouterr()

    _sql(root, "UPDATE ops.orders SET shipped_at = shipped_at + INTERVAL 3 HOUR")
    assert _cli(monkeypatch, ["probe", *where]) == 0
    capsys.readouterr()
    assert _cli(monkeypatch, ["drift", *where]) == 1
    out = capsys.readouterr().out
    assert "SHIFTED `shipped_at`" in out and "2h -> 5h" in out and "1 BREAKING" in out


def test_accepted_tables_merge_artifact_and_prior_facts_by_table_name() -> None:
    """The artifact's facts land on the live column however the table is
    qualified; a column the artifact did not measure keeps the facts the
    previous baseline held instead of losing them."""
    live = [
        TableProfile(
            connection="warehouse",
            table="ops.orders",
            columns=[
                ColumnProfile(name="shipped_at", type="TIMESTAMP", description="UTC"),
                ColumnProfile(name="amount", type="DECIMAL"),
                ColumnProfile(name="carrier", type="VARCHAR"),
            ],
        )
    ]
    probed = [
        TableProfile(
            connection="warehouse",
            table="orders",
            columns=[ColumnProfile(name="SHIPPED_AT", type="TIMESTAMP", time_offset_seconds=1.0)],
        )
    ]
    prior = [
        TableProfile(
            connection="warehouse",
            table="ops.orders",
            columns=[
                ColumnProfile(name="shipped_at", type="TIMESTAMP", time_offset_seconds=9.0),
                ColumnProfile(name="amount", type="DECIMAL", mean=12.5),
            ],
        )
    ]
    (merged,) = wd.accepted_tables(live, probed, prior)
    by_name = {c.name: c for c in merged.columns}
    assert by_name["shipped_at"].time_offset_seconds == 1.0  # the artifact wins
    assert by_name["shipped_at"].description == "UTC"  # the catalog's schema stays
    assert by_name["amount"].mean == 12.5  # unmeasured this time: the prior fact survives
    assert by_name["carrier"].mean is None
    assert wd.accepted_tables(live, None, None) == live
