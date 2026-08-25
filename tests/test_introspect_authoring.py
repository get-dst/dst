"""The authoring path a newcomer touches first.

Four ways `dst introspect` becomes unusable, each pinned here: it prints a
blank line and exits 0 on a valid warehouse; it demands an `apply` before the
step that TELLS you what to author; the types it prints (BIGINT, VARCHAR) are
rejected by `fields[].type`; and the listing carries no profile fact on the
file-first path (which every scaffolded project takes) while the prose it does
print is unparseable by its own separator. The last section pins `--profile`,
`--json`, and the format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
from pydantic import ValidationError

from services.connectors.duckdb import DuckDBConnector
from services.contracts.profile import EXACT_PROFILE_MAX_ROWS, ColumnProfile, TableProfile
from services.contracts.semantic_model import (
    FIELD_TYPES,
    Dimension,
    Field,
    warehouse_field_type,
)
from services.contracts.warehouse import ColumnSchema, SchemaSnapshot, TableSchema
from services.semantic.introspect import (
    _column_facts,
    empty_listing_reason,
    schema_json,
    serialize_schema,
)


def _snapshot(*columns: tuple[str, str]) -> SchemaSnapshot:
    return SchemaSnapshot(
        connection="wh",
        dialect="duckdb",
        tables=[
            TableSchema(
                name="spider.player",
                columns=[ColumnSchema(name=n, type=t) for n, t in columns],
                row_count=7,
            )
        ],
    )


# ── `fields[].type` is a closed SEMANTIC enum ────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BIGINT", "integer"),
        ("INT64", "integer"),
        ("integer", "integer"),
        ("UBIGINT", "integer"),
        ("INTERVAL", "string"),  # not "INT"-prefixed by accident
        ("VARCHAR(50)", "string"),
        ("CHARACTER VARYING", "string"),
        ("TEXT", "string"),
        ("NUMERIC(10,2)", "number"),
        ("DOUBLE PRECISION", "number"),
        ("BOOLEAN", "boolean"),
        ("DATE", "date"),
        ("DATETIME", "timestamp"),  # checked before DATE, or a datetime reads as a date
        ("TIMESTAMP WITH TIME ZONE", "timestamp"),
        ("TIMESTAMP_NTZ", "timestamp"),
        ("STRUCT(a INTEGER)", "json"),
        ("VARIANT", "json"),
    ],
)
def test_warehouse_types_map_to_the_authoring_enum(raw: str, expected: str) -> None:
    assert warehouse_field_type(raw) == expected


def test_unknown_type_maps_to_nothing() -> None:
    """A typo must never be dressed up as a warehouse-type mapping hint."""
    assert warehouse_field_type("bananas") is None


def test_warehouse_type_is_rejected_and_the_error_lists_the_enum() -> None:
    with pytest.raises(ValidationError) as exc:
        Field(name="runs", type="BIGINT")  # type: ignore[arg-type]
    message = str(exc.value)
    for allowed in FIELD_TYPES:
        assert allowed in message
    assert "'BIGINT' is a warehouse type" in message and "'integer'" in message


def test_nonsense_type_lists_the_enum_without_a_bogus_hint() -> None:
    with pytest.raises(ValidationError) as exc:
        Field(name="runs", type="bananas")  # type: ignore[arg-type]
    message = str(exc.value)
    assert "string, number, integer" in message
    assert "warehouse type" not in message


def test_dimensions_carry_the_same_enum() -> None:
    with pytest.raises(ValidationError) as exc:
        Dimension(name="season", type="VARCHAR")  # type: ignore[arg-type]
    assert "'VARCHAR' is a warehouse type" in str(exc.value)
    assert Dimension(name="season").type == "string"  # unchanged default


def test_introspect_prints_authorable_types_not_warehouse_types() -> None:
    """The highest-value half: the natural source hands you a VALID value."""
    text = serialize_schema(_snapshot(("id", "BIGINT"), ("name", "VARCHAR")), [], None)
    assert "  - id: integer (BIGINT)" in text
    assert "  - name: string (VARCHAR)" in text
    # the header says which half of `integer (BIGINT)` goes in the entity file
    assert "fields[].type" in text and " | ".join(FIELD_TYPES) in text


def test_a_type_that_is_already_semantic_prints_once() -> None:
    text = serialize_schema(_snapshot(("ok", "boolean")), [], None)
    assert "  - ok: boolean" in text and "(boolean)" not in text


# ── empty output must never be exit 0 ────────────────────────────────────────


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def test_cli_reports_an_empty_warehouse_on_stderr_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx

    monkeypatch.setenv("DST_ADMIN_TOKEN", "dstadm_x")
    detail = "connection 'wh' has no tables in the schemas searched: main"
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **_: httpx.Response(
            404, json={"detail": detail}, request=httpx.Request("GET", url)
        ),
    )
    assert _run_cli(monkeypatch, ["introspect", "--connection", "wh"]) == 1
    captured = capsys.readouterr()
    assert detail in captured.err
    assert captured.out == ""  # nothing on stdout to be mistaken for a listing


# ── step 1 of authoring cannot require step 4 ────────────────────────────────


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with a declared warehouse and NOTHING applied — no server, no
    token, no DB. This is the state every new user is in at introspect time."""
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("DST_URL", raising=False)
    warehouse = str(tmp_path / "wh.duckdb")
    con = duckdb.connect(warehouse)
    con.execute("CREATE SCHEMA spider")
    con.execute("CREATE TABLE spider.player (player_id BIGINT, name VARCHAR)")
    con.close()
    (tmp_path / "dst.yaml").write_text(
        f"connections:\n  warehouse:\n    type: duckdb\n    config: {{path: {warehouse}}}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_introspect_works_from_dst_yaml_without_an_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, monkeypatch)
    argv = ["introspect", "--connection", "warehouse", "--dir", str(project)]
    assert _run_cli(monkeypatch, argv) == 0
    out = capsys.readouterr().out
    assert "spider.player" in out and "  - player_id: integer (BIGINT)" in out


def test_a_declared_schema_scopes_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, monkeypatch)
    warehouse = str(project / "wh.duckdb")
    (project / "dst.yaml").write_text(
        "connections:\n  warehouse:\n    type: duckdb\n"
        f"    config: {{path: {warehouse}, schema: main}}\n",
        encoding="utf-8",
    )
    argv = ["introspect", "--connection", "warehouse", "--dir", str(project)]
    assert _run_cli(monkeypatch, argv) == 1  # main is empty — and says so, loudly
    assert "searched: main" in capsys.readouterr().err


def test_an_undeclared_connection_names_the_file_to_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The error must not tell you to `dst apply` first — the one thing you
    cannot do before you know what to author."""
    project = _project(tmp_path, monkeypatch)
    argv = ["introspect", "--connection", "ghost", "--dir", str(project)]
    assert _run_cli(monkeypatch, argv) == 1
    err = capsys.readouterr().err
    assert "connections.ghost" in err and "dst.yaml" in err
    assert "before any apply" in err


def test_an_unreachable_declared_warehouse_falls_back_to_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A warehouse the server can reach and this shell cannot (a VPC) must still
    introspect — the declaration is tried first, never exclusively."""
    import httpx

    project = _project(tmp_path, monkeypatch)
    (project / "dst.yaml").write_text(
        "connections:\n  warehouse:\n    type: duckdb\n    config: {path: /nope/missing.duckdb}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DST_ADMIN_TOKEN", "dstadm_x")
    served = {"text": "- served.by.the.server: id integer"}
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **_: httpx.Response(200, json=served, request=httpx.Request("GET", url)),
    )
    argv = ["introspect", "--connection", "warehouse", "--dir", str(project)]
    assert _run_cli(monkeypatch, argv) == 0
    captured = capsys.readouterr()
    assert "served.by.the.server" in captured.out
    assert "did not introspect from here" in captured.err


def test_empty_listing_reason_names_the_schemas_searched() -> None:
    snapshot = SchemaSnapshot(
        connection="wh", dialect="duckdb", tables=[], schemas_searched=["main", "marts"]
    )
    reason = empty_listing_reason("wh", snapshot, None)
    assert reason is not None and "main, marts" in reason
    # a non-empty warehouse the filter missed is a DIFFERENT, equally loud answer
    filtered = empty_listing_reason("wh", _snapshot(("id", "BIGINT")), ["ghost"])
    assert filtered is not None and "spider.player" in filtered
    assert empty_listing_reason("wh", _snapshot(("id", "BIGINT")), None) is None
    # ...and a filter that DOES match is not an error at all
    assert empty_listing_reason("wh", _snapshot(("id", "BIGINT")), ["spider.player"]) is None


# ── the listing must carry facts, and be readable as written ─────────────────


def _facts_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A warehouse with every awkward shape: a low-cardinality code column
    (the codes ARE the business knowledge), a column name with a space, a type
    containing ", ", and a VIEW fronting the base table (every dbt staging model)."""
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("DST_URL", raising=False)
    warehouse = str(tmp_path / "wh.duckdb")
    con = duckdb.connect(warehouse)
    con.execute(
        'CREATE TABLE base ("customer id" BIGINT, status VARCHAR, '
        "payload STRUCT(a INTEGER, b VARCHAR))"
    )
    con.execute(
        "INSERT INTO base SELECT i, ['A', 'C', 'X'][(i % 3) + 1], {'a': i, 'b': 'x'} "
        "FROM range(120) t(i)"
    )
    con.execute("CREATE VIEW stg_base AS SELECT * FROM base")
    con.close()
    (tmp_path / "dst.yaml").write_text(
        f"connections:\n  warehouse:\n    type: duckdb\n    config: {{path: {warehouse}}}\n",
        encoding="utf-8",
    )
    return tmp_path


def _parse_listing(text: str) -> dict[str, list[tuple[str, str]]]:
    """Read the listing exactly as its header documents it — one column per line,
    name and type split on the first ": ". An authoring agent does this; before
    the format change no correct reading existed."""
    tables: dict[str, list[tuple[str, str]]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if line.startswith("- "):
            current = line[2:].split(" (~")[0]
            tables[current] = []
        elif line.startswith("  - "):
            name, _, rest = line[4:].partition(": ")
            tables[current].append((name, rest.split(" — ")[0].split(" [")[0]))
    return tables


def test_profile_surfaces_the_enum_values_a_code_column_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`serialize_schema(snapshot, [], tables)` means the file-first path can
    never print a single fact."""
    project = _facts_project(tmp_path, monkeypatch)
    argv = ["introspect", "--connection", "warehouse", "--dir", str(project), "--profile"]
    assert _run_cli(monkeypatch, argv) == 0
    out = capsys.readouterr().out
    assert "Values: 'A', 'C', 'X'" in out
    assert "NOT PROFILED" not in out


def test_without_profile_the_listing_says_it_is_schema_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing used to tell "not profiled" from "no facts exist", so three agents
    read a bare schema as a complete answer and left."""
    project = _facts_project(tmp_path, monkeypatch)
    argv = ["introspect", "--connection", "warehouse", "--dir", str(project)]
    assert _run_cli(monkeypatch, argv) == 0
    out = capsys.readouterr().out
    assert "NOT PROFILED" in out and "--profile" in out
    assert "Values: " not in out


def test_a_view_carries_profile_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """profile_catalog() read duckdb_tables() alone while introspect() unioned
    views — so a view-fronted warehouse could never carry a fact."""
    project = _facts_project(tmp_path, monkeypatch)
    argv = ["introspect", "--connection", "warehouse", "--dir", str(project), "--profile"]
    assert _run_cli(monkeypatch, argv) == 0
    listing = capsys.readouterr().out
    view = listing.split("- stg_base")[1]
    assert "Values: 'A', 'C', 'X'" in view
    profiled = {p.table for p in DuckDBConnector(str(project / "wh.duckdb")).profile_catalog()}
    assert profiled == {"base", "stg_base"}


def test_the_listing_round_trips_through_its_own_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A type containing ", " and a name containing " " both survive: the old
    listing separated columns with ", " and put no delimiter at all between a
    column's name and its type, so `customer id integer` authored `type: id`."""
    project = _facts_project(tmp_path, monkeypatch)
    argv = ["introspect", "--connection", "warehouse", "--dir", str(project), "--profile"]
    assert _run_cli(monkeypatch, argv) == 0
    parsed = _parse_listing(capsys.readouterr().out)
    assert parsed["base"] == [
        ("customer id", "integer (BIGINT)"),
        ("status", "string (VARCHAR)"),
        ("payload", "json (STRUCT(a INTEGER, b VARCHAR))"),
    ]


def test_json_is_a_structured_contract_not_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    project = _facts_project(tmp_path, monkeypatch)
    argv = [
        "introspect", "--connection", "warehouse", "--dir", str(project), "--profile", "--json",
    ]  # fmt: skip
    assert _run_cli(monkeypatch, argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profiled"] is True and payload["dialect"] == "duckdb"
    columns = {c["name"]: c for t in payload["tables"] if t["name"] == "base" for c in t["columns"]}
    assert columns["status"]["values"] == ["A", "C", "X"]
    assert columns["payload"]["type"] == "json"  # the fields[].type value to author
    assert columns["payload"]["warehouse_type"] == "STRUCT(a INTEGER, b VARCHAR)"
    assert columns["customer id"]["type"] == "integer"


def test_a_warehouse_that_cannot_be_sampled_still_prints_its_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--profile is an enrichment, never a gate: a sampling failure must not cost
    the author the schema they came for."""
    from services.lenses import profiler

    project = _facts_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        profiler, "sample_profiles", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    argv = ["introspect", "--connection", "warehouse", "--dir", str(project), "--profile"]
    assert _run_cli(monkeypatch, argv) == 0
    captured = capsys.readouterr()
    assert "  - status: string (VARCHAR)" in captured.out
    assert "--profile did not complete (boom)" in captured.err


def test_every_type_a_real_warehouse_reports_is_authorable(tmp_path: Path) -> None:
    """The loop that failed, end to end: introspect a live warehouse, copy the
    type it prints into an entity field, validate."""
    path = str(tmp_path / "wh.duckdb")
    con = duckdb.connect(path)
    con.execute(
        "CREATE TABLE t (a BIGINT, b VARCHAR, c DECIMAL(10,2), d BOOLEAN, "
        "e DATE, f TIMESTAMP, g DOUBLE, h JSON, i INTEGER[])"
    )
    con.close()
    snapshot = DuckDBConnector(path, profile=False).introspect()
    for column in snapshot.tables[0].columns:
        Field(name=column.name, type=warehouse_field_type(column.type))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# --profile prints facts, or says it is guessing — never a guess as a fact
#
# `--profile` shipped rendering `approx_count_distinct` over a random 10k-row
# sample as a bare integer. It claimed 520 distinct ids in a 500-row table,
# dropped 2 of `atom.element`'s 21 values with nothing marking the list partial,
# disagreed with itself between two runs on the same read-only file, and printed
# 19 values beside `"distinct_count": 18`. Plain `introspect` — which counts —
# was strictly more accurate than the flag that exists to be more thorough.
# ---------------------------------------------------------------------------

_RARE = "pb"  # the one-row enum member a 10k-of-12,333 sample loses


@pytest.fixture
def accuracy_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The reported shapes: a 500-row table, a 100-row `order`, a table over the
    old 10k sampling cap holding a one-row enum member, and a view."""
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("DST_URL", raising=False)
    warehouse = str(tmp_path / "acc.duckdb")
    con = duckdb.connect(warehouse)
    con.execute(
        "CREATE TABLE widget (id INTEGER, price DOUBLE, ok BOOLEAN, "
        "tags VARCHAR[], meta STRUCT(a INTEGER, b VARCHAR))"
    )
    con.execute(
        "INSERT INTO widget SELECT i, i * 1.5, (i % 2 = 0), ['t1','t2'], {'a': i, 'b': 'x'} "
        "FROM range(1, 501) t(i)"
    )
    con.execute('CREATE TABLE "order" (order_id INTEGER, note VARCHAR)')
    con.execute('INSERT INTO "order" SELECT i, NULL FROM range(1, 101) t(i)')
    # 12,333 rows, 3 elements — one of which occurs exactly once
    con.execute("CREATE TABLE atom (atom_id INTEGER, element VARCHAR)")
    con.execute(
        f"INSERT INTO atom SELECT i, CASE WHEN i = 1 THEN '{_RARE}' "
        "WHEN i % 2 = 0 THEN 'h' ELSE 'c' END FROM range(1, 12334) t(i)"
    )
    con.execute("CREATE VIEW widget_v AS SELECT * FROM widget")
    con.close()
    (tmp_path / "dst.yaml").write_text(
        f"connections:\n  warehouse:\n    type: duckdb\n    config: {{path: {warehouse}}}\n",
        encoding="utf-8",
    )
    return tmp_path


def _profile_json(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    import json

    argv = [
        "introspect", "--connection", "warehouse", "--dir", str(project), "--profile", "--json",
    ]  # fmt: skip
    assert _run_cli(monkeypatch, argv) == 0
    payload: dict[str, Any] = json.loads(capsys.readouterr().out)
    return payload


def _profile_text(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    argv = ["introspect", "--connection", "warehouse", "--dir", str(project), "--profile"]
    assert _run_cli(monkeypatch, argv) == 0
    return capsys.readouterr().out


def _table(payload: dict[str, Any], name: str) -> dict[str, Any]:
    table: dict[str, Any] = next(t for t in payload["tables"] if t["name"] == name)
    return table


def _all_columns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for t in payload["tables"] for c in t["columns"]]


def test_a_distinct_count_never_exceeds_the_rows_it_counted(
    accuracy_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`- widget (~500 rows)` with `id [distinct: 520]` under it. An author who
    cannot tell a key from a non-key cannot author the entity."""
    payload = _profile_json(accuracy_project, monkeypatch, capsys)
    for table in payload["tables"]:
        rows = table["row_count"]
        for column in table["columns"]:
            if "distinct_count" in column and rows is not None:
                assert column["distinct_count"] <= rows, (table["name"], column["name"])
    ids = next(c for c in _table(payload, "widget")["columns"] if c["name"] == "id")
    assert ids["distinct_count"] == 500 and ids["distinct_is_exact"]  # 500 of 500 = a key


def test_a_value_list_that_may_be_missing_members_says_so(
    accuracy_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`atom` is over the old 10k cap, so its rarest element used to vanish with
    nothing marking the list partial — and the value dictionary is where the
    business meaning lives."""
    payload = _profile_json(accuracy_project, monkeypatch, capsys)
    element = next(c for c in _table(payload, "atom")["columns"] if c["name"] == "element")
    assert _RARE in element["values"]  # the one-row member survives
    assert element["values_complete"] is True
    # nothing on an exactly-counted warehouse is partial…
    assert all(c["values_complete"] for c in _all_columns(payload) if c.get("values"))
    # …and a list that IS partial is never rendered as though it were whole
    partial = ColumnProfile(name="element", type="VARCHAR", top_values=["h", "c"])
    assert "Values (partial): 'h', 'c'" in _column_facts(partial)


def test_values_and_distinct_count_never_contradict_each_other(
    accuracy_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """19 entries in `values` beside `"distinct_count": 18` — the approximate
    aggregate and the top-k query disagreeing inside one object."""
    payload = _profile_json(accuracy_project, monkeypatch, capsys)
    for column in _all_columns(payload):
        values, distinct = column.get("values"), column.get("distinct_count")
        if values is None or distinct is None:
            continue
        assert len(values) <= distinct, column["name"]  # never more listed than exist
        if column["values_complete"]:
            assert len(values) == distinct, column["name"]


def test_two_runs_on_a_read_only_file_agree(
    accuracy_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same command, the same untouched file, two different answers."""
    assert _profile_json(accuracy_project, monkeypatch, capsys) == _profile_json(
        accuracy_project, monkeypatch, capsys
    )
    assert _profile_text(accuracy_project, monkeypatch, capsys) == _profile_text(
        accuracy_project, monkeypatch, capsys
    )


def test_profile_is_never_less_accurate_than_plain_introspect(
    accuracy_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`atom_id [distinct: 12333]` without the flag became `11076` with it, so
    the more thorough mode destroyed the one fact an author most needs."""
    argv = ["introspect", "--connection", "warehouse", "--dir", str(accuracy_project)]
    assert _run_cli(monkeypatch, argv) == 0
    bare = capsys.readouterr().out
    profiled = _profile_text(accuracy_project, monkeypatch, capsys)
    assert "distinct: 12333" in bare and "distinct: 12333" in profiled
    assert ">=" not in profiled  # nothing in this warehouse needed estimating


def test_the_sampling_boundary_is_on_screen_when_it_is_crossed(
    accuracy_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Behaviour changes qualitatively at a row count, and nothing used to say so:
    the same command told the truth about a 500-row table and guessed about a
    12,333-row one in identical formatting."""
    assert "SAMPLED" not in _profile_text(accuracy_project, monkeypatch, capsys)

    snapshot = SchemaSnapshot(
        connection="wh",
        dialect="duckdb",
        tables=[
            TableSchema(
                name="events",
                row_count=50_000_000,
                columns=[ColumnSchema(name="id", type="BIGINT")],
            )
        ],
    )
    sampled = TableProfile(
        connection="wh",
        table="events",
        row_count=50_000_000,
        sampled_rows=10_000,
        columns=[
            ColumnProfile(name="id", type="BIGINT", distinct_count=9_812, min="21", max="49999")
        ],
    )
    text = serialize_schema(snapshot, [sampled], None)
    assert f"over {EXACT_PROFILE_MAX_ROWS:,} rows" in text and "LOWER BOUND" in text
    assert "SAMPLED 10000 rows" in text
    assert "distinct: >=9812" in text  # the lower bound it is, not the count it is not
    assert "range in sample: 21..49999" in text  # a sub-range, never the extremes

    payload = schema_json(snapshot, [sampled], None)
    assert payload["exact_profile_max_rows"] == EXACT_PROFILE_MAX_ROWS
    table = payload["tables"][0]  # type: ignore[index]
    assert table["sampled_rows"] == 10_000
    assert table["columns"][0]["distinct_is_exact"] is False  # a parser never has to infer


def test_the_smaller_authoring_traps_in_the_same_listing(
    accuracy_project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Booleans rendered as Python reprs, a LIST typed `string`, a STRUCT given a
    MIN..MAX range, an all-null column with no null rate, and unmarked views."""
    text = _profile_text(accuracy_project, monkeypatch, capsys)
    payload = _profile_json(accuracy_project, monkeypatch, capsys)
    columns = {c["name"]: c for c in _all_columns(payload)}

    assert "'true'" in text and "'True'" not in text  # SQL literals, not repr()
    assert columns["tags"]["type"] == "json"  # typed `string`, `tags = 't1'` found nothing
    assert "- tags: json (VARCHAR[])" in text
    assert "min" not in columns["meta"] and "max" not in columns["meta"]  # STRUCT is not a range
    assert "100% null" in text  # `order.note` is entirely null and now says so
    assert "widget_v" in text and "VIEW" in text
    assert _table(payload, "widget_v")["is_view"] is True
    assert _table(payload, "widget")["is_view"] is False
