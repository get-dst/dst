"""`table/X` selectors: lenses downstream of a table, or of a dbt / SQLMesh model.

An entity's `source.table` is its table node. `table/X` picks the entities on
X; `table/X+` adds the definitions about them and the lenses reading either,
and with a lineage configured it first walks the transformation tool's own DAG
from X down to the tables entities read. The fixtures are one invented
project built twice: `dbt_toolshed` (its manifest is `dbt parse` output,
trimmed to the keys dst reads) and `sqlmesh_toolshed` (whose DAG, as SQLMesh
itself loads it, is pinned below), plus `dst_toolshed`, the dst project over
their marts.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.cli.main import _read_project
from services.project.lineage import (
    LineageError,
    LineageGraph,
    LineageNode,
    dbt_lineage,
    detect_lineage,
    load_dbt,
    load_sqlmesh,
)
from services.project.plan import resolve_selection
from services.project.schema import parse_project_yaml
from tests.test_apply_lens_selection import needs_db

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lineage"
DBT = FIXTURES / "dbt_toolshed"
SQLMESH = FIXTURES / "sqlmesh_toolshed"
PROJECT = FIXTURES / "dst_toolshed"


def _select(selectors: list[str], lineage: LineageGraph | None = None):  # type: ignore[no-untyped-def]
    return resolve_selection(
        _read_project(PROJECT), selectors, "all", db_semantic={}, db_trees={}, lineage=lineage
    )


# ── reading the DAGs ─────────────────────────────────────────────────────────


def test_the_dbt_graph_is_the_manifests_child_map() -> None:
    graph = load_dbt(DBT / "target")
    manifest = json.loads((DBT / "target" / "manifest.json").read_text())
    for node_id, node in graph.nodes.items():
        assert node.children == sorted(
            c for c in manifest["child_map"][node_id] if not c.startswith("test.")
        )
    assert graph.nodes["model.toolshed.fct_rentals"].relation == "toolshed.marts.fct_rentals"
    assert graph.nodes["model.toolshed.int_rental_days"].relation == ""  # ephemeral
    assert "raw.rentals" in graph.nodes["source.toolshed.raw.rentals"].names
    # A manifest without child_map (older or trimmed) yields the same edges
    # from each node's depends_on.
    del manifest["child_map"]
    assert dbt_lineage(manifest).nodes == graph.nodes


# What `sqlmesh` itself reports for the fixture (Context.models → depends_on),
# read once with SQLMesh installed; dst reads the same files with sqlglot.
SQLMESH_PARENTS = {
    "marts.dim_tools": {"staging.stg_tools"},
    "marts.fct_rentals": {"staging.stg_rentals", "staging.stg_tools"},
    "staging.stg_members": {"raw.members"},
    "staging.stg_rentals": {"raw.rentals"},
    "staging.stg_tools": {"raw.tools"},
}


def test_the_sqlmesh_graph_matches_sqlmesh_and_reports_the_python_model() -> None:
    graph = load_sqlmesh(SQLMESH)
    parents: dict[str, set[str]] = {}
    for node_id, node in graph.nodes.items():
        for child in node.children:
            parents.setdefault(child, set()).add(node_id)
    assert parents == SQLMESH_PARENTS
    # SQLMesh reads the Python model's inputs by running its own loader;
    # dst does not, and says so rather than dropping it.
    python = graph.nodes["marts.dim_members"]
    assert not python.walked and "Python model" in python.note
    assert graph.unwalked() == [
        "marts.dim_members (Python model models/marts/dim_members.py, inputs not read)"
    ]


def test_a_sqlmesh_query_that_does_not_parse_is_reported_not_skipped(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "config.yaml").write_text("model_defaults:\n  dialect: duckdb\n")
    (tmp_path / "models" / "broken.sql").write_text(
        "MODEL (name marts.broken, kind FULL);\nSELECT FROM WHERE (((\n"
    )
    (tmp_path / "models" / "half.py").write_text("@model('marts.half'\ndef broken(:\n")
    graph = load_sqlmesh(tmp_path)
    node = graph.nodes["marts.broken"]
    assert not node.walked and "does not parse" in node.note
    assert any("half.py does not parse" in line for line in graph.unwalked())


def test_lineage_detection_and_loud_failures(tmp_path: Path) -> None:
    assert detect_lineage(DBT).tool == "dbt"  # the project dir: target/ is read
    assert detect_lineage(DBT / "target").tool == "dbt"
    assert detect_lineage(SQLMESH).tool == "sqlmesh"
    (tmp_path / "dbt_project.yml").write_text("name: x\n")
    with pytest.raises(LineageError, match="dbt parse"):
        detect_lineage(tmp_path)  # a dbt project never parsed
    with pytest.raises(LineageError, match="neither"):
        detect_lineage(tmp_path / "nowhere")


def test_dst_yaml_names_exactly_one_lineage_tool() -> None:
    project = parse_project_yaml((PROJECT / "dst.yaml").read_text())
    assert project.lineage is not None and project.lineage.tool == ("dbt", "../dbt_toolshed/target")
    assert parse_project_yaml("lineage: {sqlmesh: ../sm}\n").lineage.tool == ("sqlmesh", "../sm")  # type: ignore[union-attr]
    for bad in ("lineage: {}\n", "lineage: {dbt: a, sqlmesh: b}\n"):
        with pytest.raises(ValueError, match="exactly one"):
            parse_project_yaml(bad)


# ── resolution ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("lineage", ["dbt", "sqlmesh"])
def test_a_staging_model_selects_exactly_the_lenses_on_the_marts_below_it(lineage: str) -> None:
    graph = detect_lineage(DBT if lineage == "dbt" else SQLMESH)
    rentals = _select(["table/stg_rentals+"], graph)
    assert rentals.lenses == ["rental_ops"]
    assert rentals.assets == ["definition/late_return", "entity/rentals"]
    assert rentals.paths == [
        {
            "selector": "table/stg_rentals+",
            "table": "marts.fct_rentals",
            "entity": "rentals",
            "lens": "rental_ops",
        },
        {
            "selector": "table/stg_rentals+",
            "table": "marts.fct_rentals",
            "entity": "rentals",
            "definition": "late_return",
            "lens": "rental_ops",
        },
    ]
    # Pushed: the two shared files and the one lens; the siblings stay out.
    assert {p.split("/")[1] for p in rentals.files if p.startswith("lenses/")} == {"rental_ops"}
    # stg_tools feeds both marts: both readers come along.
    assert _select(["table/stg_tools+"], graph).lenses == ["fleet", "rental_ops"]
    # A source (raw table) walks the same way.
    assert _select(["table/raw.rentals+"], graph).lenses == ["rental_ops"]


def test_dbt_walks_through_an_ephemeral_model() -> None:
    sel = _select(["table/stg_members+"], load_dbt(DBT / "target"))
    assert sel.lenses == ["membership"] and sel.notes == []


def test_a_python_model_in_the_way_is_said_and_the_unread_tables_named() -> None:
    sel = _select(["table/stg_members+"], load_sqlmesh(SQLMESH))
    assert sel.lenses == [] and sel.files == {}
    assert any("not walked" in n and "marts.dim_members" in n for n in sel.notes)
    assert sel.notes[-1] == "the selection reaches no lens; no entity reads staging.stg_members"


def test_table_names_match_on_trailing_parts_case_insensitively_and_by_glob() -> None:
    assert _select(["table/MARTS.FCT_RENTALS"]).assets == ["entity/rentals"]
    assert _select(["table/fct_rentals"]).assets == ["entity/rentals"]
    assert _select(["table/dim_*+"]).lenses == ["fleet", "membership", "rental_ops"]
    # Without +, no definitions or lenses: the entity alone, and no lens reached.
    bare = _select(["table/fct_rentals"])
    assert bare.lenses == [] and bare.notes == ["the selection reaches no lens"]
    assert bare.paths == [
        {"selector": "table/fct_rentals", "table": "marts.fct_rentals", "entity": "rentals"}
    ]


def test_a_short_name_on_two_schemas_warns_and_takes_both() -> None:
    graph = LineageGraph(
        tool="sqlmesh",
        nodes={
            "staging.tools": LineageNode(relation="staging.tools", children=["marts.dim_tools"]),
            "archive.tools": LineageNode(relation="archive.tools"),
            "marts.dim_tools": LineageNode(relation="marts.dim_tools"),
        },
    )
    sel = _select(["table/tools+"], graph)
    assert sel.lenses == ["fleet", "rental_ops"]
    assert sel.notes == [
        "table/tools+: 'tools' names 2 tables (archive.tools, staging.tools) — taking all of them"
    ]


def test_a_name_matching_nothing_warns_by_name_and_pushes_nothing() -> None:
    sel = _select(["table/stg_nothing+"], load_dbt(DBT / "target"))
    assert sel.lenses == [] and sel.assets == [] and sel.files == {}
    assert sel.notes == [
        "table/stg_nothing+: matches no table an entity reads, and no model in the dbt lineage",
        "the selection reaches no lens",
    ]
    # Without a lineage, a model name is not known, and the note says why.
    assert "no lineage is configured" in _select(["table/stg_rentals+"]).notes[0]


def test_table_selectors_mix_with_the_other_kinds() -> None:
    sel = _select(["table/stg_members+", "lens/fleet"], load_dbt(DBT / "target"))
    assert sel.lenses == ["fleet", "membership"]
    assert _select(["lens/fleet"]).paths == []


# ── the CLI ──────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int, body: object) -> None:
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self) -> object:
        return self._body

    def raise_for_status(self) -> _Resp:
        return self


def _run(monkeypatch, argv: list[str], stdin: str = "") -> int:  # type: ignore[no-untyped-def]
    from services.cli import main as cli

    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return int(cli.main())


def test_tables_from_selects_each_name_and_sends_the_lineage(monkeypatch, capsys) -> None:
    import httpx

    sent: list[dict[str, object]] = []

    def post(url, **kw):  # type: ignore[no-untyped-def]
        sent.append(kw)
        return _Resp(200, [])

    monkeypatch.setattr(httpx, "post", post)
    # `dbt ls` without -q prints its log lines to stdout too: skipped, counted.
    piped = "\x1b[0m11:30:12  Running with dbt=1.12.6\nstg_rentals\n\n# a comment\nstg_tools\n"
    argv = ["plan", "--dir", str(PROJECT), "--token", "dstadm_t", "--tables-from", "-", "--json"]
    assert _run(monkeypatch, argv, piped) == 0
    assert sent[0]["params"]["select"] == ["table/stg_rentals+", "table/stg_tools+"]  # type: ignore[index]
    lineage = LineageGraph.model_validate(sent[0]["json"]["lineage"])  # type: ignore[index]
    assert lineage.tool == "dbt" and "model.toolshed.stg_rentals" in lineage.nodes
    assert "skipped 1 line(s)" in capsys.readouterr().err

    # --lineage overrides dst.yaml's key.
    sent.clear()
    argv = ["plan", "--dir", str(PROJECT), "--token", "dstadm_t", "--lineage", str(SQLMESH)]
    assert _run(monkeypatch, [*argv, "--select", "table/stg_tools+"]) == 0
    assert sent[0]["json"]["lineage"]["tool"] == "sqlmesh"  # type: ignore[index]

    # No table selector: no lineage is read or sent.
    sent.clear()
    assert _run(monkeypatch, [*argv[:5], "--select", "lens/fleet"]) == 0
    assert "lineage" not in sent[0]["json"]  # type: ignore[operator]


def test_an_empty_tables_from_selects_nothing_and_exits_0(monkeypatch, capsys) -> None:
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no request"))
    for verb in ("plan", "apply"):
        argv = [verb, "--dir", str(PROJECT), "--token", "dstadm_t", "--tables-from", "-"]
        assert _run(monkeypatch, argv, "\n") == 0
        assert "names no table or model — nothing to" in capsys.readouterr().out


def test_a_configured_lineage_that_cannot_be_read_fails(monkeypatch, tmp_path, capsys) -> None:
    shutil.copytree(PROJECT, tmp_path / "p")  # ../dbt_toolshed does not exist next to it
    argv = ["plan", "--dir", str(tmp_path / "p"), "--token", "dstadm_t"]
    with pytest.raises(SystemExit) as exited:
        _run(monkeypatch, [*argv, "--select", "table/stg_rentals+"])
    assert exited.value.code == 1
    assert "error: lineage: manifest.json not found" in capsys.readouterr().err


def test_dst_test_select_runs_only_the_lenses_reached(monkeypatch, capsys) -> None:
    import argparse

    from services.cli.main import _test_selection

    args = argparse.Namespace(
        select=["table/stg_tools+"], tables_from=None, lineage=None, dir=str(PROJECT)
    )
    assert _test_selection(args) == {"fleet", "rental_ops"}
    out = capsys.readouterr().out
    assert "marts.dim_tools → tools → fleet" in out

    args = argparse.Namespace(
        select=None, tables_from="-", lineage=str(SQLMESH), dir=str(PROJECT), as_json=True
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("stg_members\n"))
    assert _test_selection(args) == set()  # the caller exits 0, nothing ran
    report = json.loads(capsys.readouterr().out)
    assert report["total"] == 0 and report["selection"]["lenses"] == []

    args = argparse.Namespace(select=["@changed"], tables_from=None, lineage=None, dir=".")
    with pytest.raises(SystemExit):
        _test_selection(args)


def test_star_selects_everything_on_every_command(monkeypatch, capsys) -> None:
    """`*` is a selector type of its own: the whole project, exactly as no
    selector, so a CI step that always passes a selector can run everything."""
    import argparse

    import httpx

    from services.cli.main import _test_selection

    every = _select([])
    assert _select(["*"]).lenses == every.lenses
    assert _select(["*", "lens/fleet"]).lenses == every.lenses

    sent: list[dict[str, object]] = []

    def post(url, **kw):  # type: ignore[no-untyped-def]
        sent.append(kw)
        return _Resp(200, [])

    monkeypatch.setattr(httpx, "post", post)
    for verb in ("plan", "apply"):
        sent.clear()
        argv = [verb, "--dir", str(PROJECT), "--token", "dstadm_t", "--select", "*"]
        assert _run(monkeypatch, [*argv, "--tables-from", "-"], "\n") == 0
        assert sent, f"{verb} --select '*' sends the whole push"
        assert "select" not in (sent[0].get("params") or {})

    args = argparse.Namespace(select=["*"], tables_from=None, lineage=None, dir=str(PROJECT))
    assert _test_selection(args) is None  # None = every published lens
    capsys.readouterr()


# ── end to end: plan and apply against the server ────────────────────────────


@pytest.fixture()
def toolshed(tmp_path: Path):  # type: ignore[no-untyped-def]
    """The fixture project with a real DuckDB warehouse behind its marts, an
    org and admin token, and the dbt fixture beside it (dst.yaml's lineage)."""
    import duckdb

    from services.auth.tokens import hash_token, new_admin_token
    from services.config import settings

    shutil.copytree(PROJECT, tmp_path / "dst_toolshed")
    shutil.copytree(DBT, tmp_path / "dbt_toolshed")
    warehouse = tmp_path / "toolshed.duckdb"
    with duckdb.connect(str(warehouse)) as con:
        con.execute("CREATE SCHEMA marts")
        con.execute(
            "CREATE TABLE marts.fct_rentals (rental_id INTEGER, tool_id INTEGER, "
            "member_id INTEGER, rented_on DATE, returned_on DATE, due_on DATE, days INTEGER, "
            "rental_fee DOUBLE)"
        )
        con.execute(
            "INSERT INTO marts.fct_rentals VALUES (1, 1, 1, '2026-03-02', "
            "'2026-03-05', '2026-03-04', 3, 36.0)"
        )
        con.execute(
            "CREATE TABLE marts.dim_tools (tool_id INTEGER, tool_name VARCHAR, "
            "category VARCHAR, day_rate DOUBLE)"
        )
        con.execute("INSERT INTO marts.dim_tools VALUES (1, 'tile saw', 'cutting', 12.0)")
        con.execute(
            "CREATE TABLE marts.dim_members (member_id INTEGER, joined_on DATE, plan VARCHAR)"
        )
        con.execute("INSERT INTO marts.dim_members VALUES (1, '2026-01-10', 'basic')")
    dst_yaml = tmp_path / "dst_toolshed" / "dst.yaml"
    config = yaml.safe_load(dst_yaml.read_text())
    config["connections"]["toolshed"]["config"]["path"] = str(warehouse)
    dst_yaml.write_text(yaml.safe_dump(config, sort_keys=False))

    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        oid = c.execute(
            text("INSERT INTO org (name) VALUES ('TableSel') RETURNING id")
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o, :h, 't')"),
            {"o": oid, "h": hash_token(raw)},
        )
    yield tmp_path / "dst_toolshed", raw
    with admin.begin() as c:
        for table in (
            "eval_run",
            "eval_case",
            "certified_answer",
            "lens_version",
            "lens",
            "semantic_asset",
            "connection",
            "admin_token",
        ):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": oid})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": oid})
    admin.dispose()


def _through_the_app(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Route the CLI's httpx calls into the app in-process."""
    import httpx

    from services.app import app

    client = TestClient(app)

    def call(method: str):  # type: ignore[no-untyped-def]
        def go(url: str, **kw):  # type: ignore[no-untyped-def]
            path = "/" + url.split("://", 1)[-1].split("/", 1)[1]
            kw.pop("timeout", None)
            return getattr(client, method)(path, **kw)

        return go

    monkeypatch.setattr(httpx, "post", call("post"))
    monkeypatch.setattr(httpx, "get", call("get"))
    monkeypatch.setattr("time.sleep", lambda _s: None)


@needs_db
def test_plan_and_apply_a_dbt_selection_end_to_end(toolshed, monkeypatch, capsys) -> None:
    root, token = toolshed
    _through_the_app(monkeypatch)
    base = ["--dir", str(root), "--token", token, "--url", "http://dst.test"]
    assert _run(monkeypatch, ["apply", *base]) == 0, capsys.readouterr()
    capsys.readouterr()

    # The model changed upstream; its readers, and only they, change too.
    entity = root / "semantic" / "entities" / "rentals.yaml"
    entity.write_text(entity.read_text().replace("per tool rental", "per rental of a tool"))
    assert _run(monkeypatch, ["plan", *base, "--select", "table/stg_rentals+", "--json"]) == 0
    selection = json.loads(capsys.readouterr().out)[0]
    assert selection["scope"] == "selection" and selection["lenses"] == ["rental_ops"]
    assert {(p["table"], p["entity"], p["lens"]) for p in selection["paths"]} == {
        ("marts.fct_rentals", "rentals", "rental_ops")
    }

    argv = ["apply", *base, "--tables-from", "-", "--json"]
    assert _run(monkeypatch, argv, "stg_rentals\n") == 0
    rows = json.loads(capsys.readouterr().out)
    lenses = {r["lens"]: r.get("action") for r in rows if r.get("lens")}
    assert lenses == {"rental_ops": "updated"}, rows

    # A changed model no entity reads: said, nothing pushed, exit 0.
    assert _run(monkeypatch, ["apply", *base, "--select", "table/int_member_plans"]) == 0
    out = capsys.readouterr().out
    assert "the selection reaches no lens" in out
