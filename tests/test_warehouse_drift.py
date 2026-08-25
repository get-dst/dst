"""A definition must not outlive the schema change that invalidated it in silence.

The failure class: `discount` is authored as ``list_price - unit_price``,
correctly, because the warehouse has no discount column. The warehouse later
gains ``ops.orders.discount_amount`` and the layer keeps serving the hand
derivation — confidently wrong, for as long as nobody looks, because every
change-detection surface sits off the daily path: `introspect` prints a
snapshot and never a diff, `dst test` passes 1/1 (both its sides read TODAY's
warehouse, so a consistent wrongness is invisible to it), and profile drift was
REST-only.

What is pinned here: the cross-reference fires (a bare column list is what
`introspect` already printed and nobody read); an unchanged warehouse says
nothing on stdout, so the verb survives being run daily; the committed baseline
carries schema and never literals; and `plan`'s passive check
stays cheap, stays quiet, and never turns a warehouse this laptop cannot reach
into a failed plan.

No server and no DB: the drift path is file-first by design, and `plan`'s httpx
call is monkeypatched (the test_cli_apply_legibility pattern).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import httpx
import pytest

from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.semantic_model import Definition, EntitySource, Field
from services.contracts.shared_semantic import SharedEntity
from services.project import warehouse_drift as wd

# ── fixtures ─────────────────────────────────────────────────────────────────

ORDERS = "ops.orders"


def _profile(table: str, columns: list[tuple[str, str]], **kw: object) -> TableProfile:
    return TableProfile(
        connection="warehouse",
        table=table,
        columns=[ColumnProfile(name=n, type=t, **kw) for n, t in columns],  # type: ignore[arg-type]
    )


def _day1() -> list[TableProfile]:
    return [
        _profile(
            ORDERS,
            [("order_id", "INTEGER"), ("list_price", "DECIMAL"), ("unit_price", "DECIMAL")],
        )
    ]


def _day5() -> list[TableProfile]:
    return [
        _profile(
            ORDERS,
            [
                ("order_id", "INTEGER"),
                ("list_price", "DECIMAL"),
                ("unit_price", "DECIMAL"),
                ("discount_amount", "DECIMAL(12,2)"),
            ],
        )
    ]


def _orders_entity() -> dict[str, SharedEntity]:
    return {
        "semantic/entities/orders.yaml": SharedEntity(
            name="orders",
            source=EntitySource(connection="warehouse", table=ORDERS),
            fields=[
                Field(name="list_price", type="number"),
                Field(name="unit_price", type="number"),
            ],
        )
    }


def _discount_definition() -> dict[str, Definition]:
    return {
        "semantic/definitions/discounts.md": Definition(
            term="discount",
            body="The difference between list_price and the unit_price actually charged.",
            about="orders",
            sql_expr="(list_price - unit_price) * quantity",
            sources=[ORDERS],
        )
    }


def _findings(
    previous: list[TableProfile],
    current: list[TableProfile],
    entities: dict[str, SharedEntity] | None = None,
    defs: dict[str, Definition] | None = None,
) -> list[wd.Finding]:
    return wd.cross_reference(
        wd.baseline_drift(previous, current),
        _orders_entity() if entities is None else entities,
        _discount_definition() if defs is None else defs,
    )


# ── the finding ──────────────────────────────────────────────────────────────


def test_a_new_column_names_the_definition_that_derives_the_same_quantity() -> None:
    """THE regression. `ops.orders gained discount_amount` is a line nobody reads
    twice; naming the definition that derives that quantity by hand is the line
    that gets read."""
    findings = _findings(_day1(), _day5())
    assert [(f.table, f.kind, f.detail) for f in findings] == [
        (ORDERS, "column_added", "discount_amount")
    ]
    refs = {(r.kind, r.name) for r in findings[0].refs}
    assert ("definition", "discount") in refs
    assert ("entity", "orders") in refs

    line = "\n".join(wd.render(findings[0]))
    assert "discount_amount" in line
    assert "(list_price - unit_price) * quantity" in line
    assert "supersedes the derivation" in line
    assert "semantic/definitions/discounts.md" in line  # the file you open to fix it


def test_a_definition_is_found_by_about_when_it_lists_no_sources() -> None:
    """The four bindings an author can write are not interchangeable in practice —
    a definition bound only through `about` is as bound as one listing `sources`."""
    bare = {
        "semantic/definitions/discounts.md": Definition(
            term="discount", body="…", about="orders.discount", sql_expr="list_price - unit_price"
        )
    }
    refs = _findings(_day1(), _day5(), defs=bare)[0].refs
    assert ("definition", "discount") in {(r.kind, r.name) for r in refs}


def test_a_dropped_column_a_definition_names_reads_as_broken() -> None:
    """The other direction of the same failure: the warehouse removes what the
    layer still computes from. `review whether` is the wrong verb for that."""
    current = [_profile(ORDERS, [("order_id", "INTEGER"), ("list_price", "DECIMAL")])]
    findings = _findings(_day1(), current)
    dropped = [f for f in findings if f.kind == "column_dropped"]
    assert [f.detail for f in dropped] == ["unit_price"]
    line = "\n".join(wd.render(dropped[0]))
    assert "names `unit_price` in its sql_expr" in line
    assert "now broken" in line


def test_a_table_nothing_reads_says_so_and_sorts_last() -> None:
    """Burying the one line that matters under forty true, dull ones is how a new
    column gets missed in `introspect` output."""
    previous = [*_day1(), _profile("ops.audit_log", [("event_id", "INTEGER")])]
    current = [
        *_day5(),
        _profile("ops.audit_log", [("event_id", "INTEGER"), ("trace_id", "VARCHAR")]),
    ]
    findings = _findings(previous, current)
    assert [f.table for f in findings] == [ORDERS, "ops.audit_log"]
    assert not findings[-1].referenced
    assert "nothing in semantic/ reads this table" in "\n".join(wd.render(findings[-1]))


def test_an_unchanged_warehouse_produces_no_findings() -> None:
    """Silence is the daily case, and a verb that prints a wall on a quiet day is
    one people stop reading on a loud one."""
    assert _findings(_day1(), _day1()) == []


def test_row_counts_and_freshness_are_not_schema_changes() -> None:
    """The same window also grew orders 3000 -> 4200. Data moving is not the
    layer going stale, and reporting it would drown the delta that is."""
    grown = [p.model_copy(update={"row_count": 4200}) for p in _day1()]
    assert _findings(_day1(), grown) == []


# ── what the committed baseline carries ──────────────────────────────────────


def _customers(values: list[str]) -> list[TableProfile]:
    return [
        TableProfile(
            connection="warehouse",
            table="ops.customers",
            columns=[
                ColumnProfile(
                    name="email", type="VARCHAR", top_values=values, min=values[0], max=values[-1]
                ),
                ColumnProfile(name="country", type="VARCHAR", top_values=["FI", "DK"]),
            ],
        )
    ]


def test_the_baseline_file_carries_schema_and_no_literals(tmp_path: Path) -> None:
    """`_BASELINE_INCLUDE` is a whitelist, and this is what it buys: the file is
    committed, so a value that moves every pass would put a hundred-line diff
    under every `--accept` and bury the schema change that is the whole point.
    The probe artifact is where literals belong, on purpose and by itself."""
    wd.write_baseline(tmp_path, "warehouse", _customers(["ada@corp.example"]))
    written = wd.baseline_path(tmp_path, "warehouse").read_text(encoding="utf-8")
    assert "corp.example" not in written and '"FI"' not in written
    assert '"email"' in written and '"country"' in written  # names and types survive


# ── the CLI verb, end to end against a real warehouse ────────────────────────


def _project(root: Path) -> Path:
    (root / "semantic" / "entities").mkdir(parents=True)
    (root / "semantic" / "definitions").mkdir(parents=True)
    (root / "dst.yaml").write_text(
        "name: t\nconnections:\n  warehouse:\n    type: duckdb\n"
        f"    config: {{path: {root / 'wh.duckdb'}}}\n",
        encoding="utf-8",
    )
    (root / "semantic" / "entities" / "orders.yaml").write_text(
        "name: orders\ndescription: d\ngrain: g\nsource:\n  connection: warehouse\n"
        "  table: ops.orders\nfields:\n  - name: list_price\n    type: number\n",
        encoding="utf-8",
    )
    (root / "semantic" / "definitions" / "discounts.md").write_text(
        "---\nmetric: discount\nabout: orders\nsources: [ops.orders]\n"
        "sql: (list_price - unit_price) * quantity\n---\n\nThe money given away.\n",
        encoding="utf-8",
    )
    return root


def _warehouse(root: Path, *, day5: bool) -> None:
    con = duckdb.connect(str(root / "wh.duckdb"))
    con.execute("CREATE SCHEMA IF NOT EXISTS ops")
    con.execute("DROP TABLE IF EXISTS ops.orders")
    con.execute(
        "CREATE TABLE ops.orders (order_id INTEGER, list_price DECIMAL(10,2), "
        "unit_price DECIMAL(10,2))"
    )
    if day5:
        con.execute("ALTER TABLE ops.orders ADD COLUMN discount_amount DECIMAL(12,2)")
    con.close()


def _cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def test_drift_arms_via_accept_then_reports_the_day_5_column(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole path with no server and no prior apply: `dst drift` is step
    one of authoring's maintenance, exactly as `introspect` is step one of
    authoring — it must not require a control plane to exist yet. Arming is an
    ACT (`--accept` or `dst probe`), never a side effect of asking."""
    root = _project(tmp_path)
    _warehouse(root, day5=False)

    # Unarmed: exit 4, nothing written — a bare run must never self-baseline
    # the very warehouse state it was asked to judge.
    assert _cli(monkeypatch, ["drift", "--connection", "warehouse", "--dir", str(root)]) == 4
    assert not wd.baseline_path(root, "warehouse").exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not armed" in captured.err and "dst probe" in captured.err

    assert (
        _cli(monkeypatch, ["drift", "--connection", "warehouse", "--accept", "--dir", str(root)])
        == 0
    )
    assert wd.baseline_path(root, "warehouse").exists()
    assert capsys.readouterr().out == ""  # recording is not a finding

    # An unchanged warehouse stays off stdout, so a daily run costs no attention.
    assert _cli(monkeypatch, ["drift", "--connection", "warehouse", "--dir", str(root)]) == 0
    assert capsys.readouterr().out == ""

    _warehouse(root, day5=True)
    # A gained column is a change and not a broken reference: exit 2.
    assert _cli(monkeypatch, ["drift", "--connection", "warehouse", "--dir", str(root)]) == 2
    out = capsys.readouterr().out
    assert "ops.orders: gained column `discount_amount`" in out
    assert "definition `discount` (semantic/definitions/discounts.md)" in out
    assert "supersedes the derivation" in out


def test_drift_names_the_warehouse_it_could_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unlike plan's courtesy check, the verb whose whole job is the comparison
    must fail loudly when it cannot make one — a silent exit 0 here would read as
    "no drift"."""
    root = _project(tmp_path)  # dst.yaml points at a file that does not exist
    assert _cli(monkeypatch, ["drift", "--connection", "warehouse", "--dir", str(root)]) == 1
    assert "did not profile from here" in capsys.readouterr().err


# ── armed by default, exit codes a gate can read ─────────────────────────────


def test_probe_arms_drift_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One artifact family: `dst probe` — the documented first step — writes the
    drift baseline alongside the probe artifact, so a project that probed never
    sits in the silent unarmed state."""
    root = _project(tmp_path)
    _warehouse(root, day5=False)
    assert _cli(monkeypatch, ["probe", "--connection", "warehouse", "--dir", str(root)]) == 0
    capsys.readouterr()
    assert wd.baseline_path(root, "warehouse").exists()
    # …and drift reads that baseline as armed: unchanged warehouse, exit 0.
    assert _cli(monkeypatch, ["drift", "--connection", "warehouse", "--dir", str(root)]) == 0

    # A re-probe UPDATES the baseline: after it, the day-5 column is the new
    # recorded truth and drift reports clean against it.
    _warehouse(root, day5=True)
    assert _cli(monkeypatch, ["probe", "--connection", "warehouse", "--dir", str(root)]) == 0
    capsys.readouterr()
    assert _cli(monkeypatch, ["drift", "--connection", "warehouse", "--dir", str(root)]) == 0


def test_drift_exit_1_when_a_dropped_column_breaks_declared_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """0 clean · 2 changes-none-breaking · 1 breaking — and the blast radius
    names the certified answer whose approved SQL reads the dropped column."""
    root = _project(tmp_path)
    (root / "lenses" / "finance").mkdir(parents=True)
    (root / "lenses" / "finance" / "certified_answers.yaml").write_text(
        "- question: Average unit price this year?\n"
        "  sql: SELECT avg(unit_price) FROM ops.orders\n",
        encoding="utf-8",
    )
    _warehouse(root, day5=False)
    assert (
        _cli(monkeypatch, ["drift", "--connection", "warehouse", "--accept", "--dir", str(root)])
        == 0
    )
    capsys.readouterr()

    con = duckdb.connect(str(root / "wh.duckdb"))
    con.execute("ALTER TABLE ops.orders DROP COLUMN unit_price")
    con.close()

    assert _cli(monkeypatch, ["drift", "--connection", "warehouse", "--dir", str(root)]) == 1
    out = capsys.readouterr().out
    assert "DROPPED column `unit_price`" in out
    assert "definition `discount`" in out  # names `unit_price` in its sql_expr
    assert "certified `Average unit price this year?`" in out
    assert "BREAKING" in out


def test_exit_code_classification() -> None:
    """The kinds that can break a reference are the destructive ones — and only
    when something declared actually reads what changed."""
    ref = wd.LayerRef(kind="definition", name="discount", path="d.md", why="reads it")
    breaking = wd.Finding(table=ORDERS, kind="column_dropped", detail="unit_price", refs=[ref])
    added = wd.Finding(table=ORDERS, kind="column_added", detail="discount_amount", refs=[ref])
    unread = wd.Finding(table="ops.audit_log", kind="column_dropped", detail="x", refs=[])

    assert wd.exit_code([]) == 0
    assert wd.exit_code([added]) == 2  # a gained column breaks nothing
    assert wd.exit_code([unread]) == 2  # destructive, but nothing declared reads it
    assert wd.exit_code([breaking]) == 1
    assert wd.exit_code([added, unread, breaking]) == 1


def test_certified_refs_require_both_table_and_column_mention() -> None:
    """`amount` alone matches half a corpus — a false BREAKING turns exit 1
    into noise a nightly gate stops trusting."""
    certified = [
        wd.CertifiedRef(question="q1", sql="SELECT avg(unit_price) FROM ops.orders", path="p"),
        wd.CertifiedRef(question="q2", sql="SELECT unit_price FROM ops.refunds", path="p"),
        wd.CertifiedRef(question="q3", sql="SELECT total FROM ops.orders", path="p"),
    ]
    findings = wd.cross_reference(
        [wd.ProfileDrift(table=ORDERS, kind="column_dropped", detail="unit_price")],
        {},
        {},
        certified,
    )
    assert [r.name for f in findings for r in f.refs] == ["q1"]
    # …and a non-destructive delta implicates no certified answer at all.
    gained = wd.cross_reference(
        [wd.ProfileDrift(table=ORDERS, kind="column_added", detail="unit_price")], {}, {}, certified
    )
    assert [r for f in gained for r in f.refs] == []


# ── plan's passive check ─────────────────────────────────────────────────────


@pytest.fixture()
def planned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A project whose plan request is answered by a stub — this file tests the
    warehouse entry, not the server's plan."""
    monkeypatch.delenv("DST_URL", raising=False)
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, headers=None, json=None, timeout=None, params=None: httpx.Response(
            200, json=[], request=httpx.Request("POST", url)
        ),
    )
    return _project(tmp_path)


def _plan(monkeypatch: pytest.MonkeyPatch, root: Path, *extra: str) -> int:
    return _cli(monkeypatch, ["plan", "--dir", str(root), "--token", "dstadm_x", *extra])


def test_plan_names_a_warehouse_that_moved_under_the_project(
    monkeypatch: pytest.MonkeyPatch, planned: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The passive half: the engineer runs plan every morning and never visits a
    dashboard, so this is the line that has to exist."""
    _warehouse(planned, day5=False)
    assert (
        _cli(monkeypatch, ["drift", "--connection", "warehouse", "--accept", "--dir", str(planned)])
        == 0
    )
    capsys.readouterr()
    _warehouse(planned, day5=True)

    assert _plan(monkeypatch, planned) == 0
    out = capsys.readouterr().out
    assert "the warehouse has changed since profiling (1 schema delta(s))" in out
    assert "run `dst drift`" in out  # a pointer, not the analysis


def test_plan_is_silent_when_nothing_moved(
    monkeypatch: pytest.MonkeyPatch, planned: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _warehouse(planned, day5=False)
    assert (
        _cli(monkeypatch, ["drift", "--connection", "warehouse", "--accept", "--dir", str(planned)])
        == 0
    )
    capsys.readouterr()
    assert _plan(monkeypatch, planned) == 0
    assert "warehouse:" not in capsys.readouterr().out


def test_plan_says_unarmed_for_a_declared_warehouse_without_a_baseline(
    monkeypatch: pytest.MonkeyPatch, planned: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The standing line: the silent unarmed state was the failure —
    every declared warehouse connection gets `armed (baseline <date>)` or
    UNARMED with the fix named, on the daily verb."""
    import json

    _warehouse(planned, day5=False)
    assert _plan(monkeypatch, planned) == 0
    assert "drift: 'warehouse' UNARMED — run `dst probe`" in capsys.readouterr().out

    assert _plan(monkeypatch, planned, "--json") == 0
    entries = [e for e in json.loads(capsys.readouterr().out) if e.get("scope") == "warehouse"]
    assert [e["drift"] for e in entries] == ["unarmed"]


def test_plan_says_armed_with_the_baseline_date(
    monkeypatch: pytest.MonkeyPatch, planned: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    _warehouse(planned, day5=False)
    assert (
        _cli(monkeypatch, ["drift", "--connection", "warehouse", "--accept", "--dir", str(planned)])
        == 0
    )
    capsys.readouterr()
    assert _plan(monkeypatch, planned) == 0
    out = capsys.readouterr().out
    date = wd.read_baseline(planned, "warehouse").recorded_at.date().isoformat()  # type: ignore[union-attr]
    assert f"drift: 'warehouse' armed (baseline {date})" in out

    assert _plan(monkeypatch, planned, "--json") == 0
    entries = [e for e in json.loads(capsys.readouterr().out) if e.get("scope") == "warehouse"]
    assert [e["drift"] for e in entries] == ["armed"]
    assert entries[0]["baseline_recorded_at"].startswith(date)


def test_plan_makes_no_warehouse_round_trip_without_a_baseline(
    monkeypatch: pytest.MonkeyPatch, planned: Path
) -> None:
    """The first bound. A project that never ran `dst drift` pays nothing —
    no connector built, no metadata query, no line."""
    _warehouse(planned, day5=False)
    called = False

    def spy(*a: object, **k: object) -> list[TableProfile]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("services.lenses.profiler_catalog.catalog_profiles", spy)
    assert _plan(monkeypatch, planned) == 0
    assert called is False


def test_plan_degrades_to_silence_when_the_warehouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, planned: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A warehouse this laptop cannot reach says nothing about whether the project
    is valid, which is what plan answers. Never an error, never a stack trace,
    and readable under --json for a caller that needs to know."""
    _warehouse(planned, day5=False)
    assert (
        _cli(monkeypatch, ["drift", "--connection", "warehouse", "--accept", "--dir", str(planned)])
        == 0
    )
    capsys.readouterr()
    (planned / "wh.duckdb").unlink()

    assert _plan(monkeypatch, planned) == 0
    captured = capsys.readouterr()
    assert "warehouse:" not in captured.out
    # Not on stderr either: a `warning:` about a warehouse plan never needed to
    # read is the noise that trains people to ignore the line that matters.
    assert "did not profile" not in captured.err
    assert "duckdb" not in captured.err.lower()


def test_plan_json_carries_the_degradation_the_prose_omits(
    monkeypatch: pytest.MonkeyPatch, planned: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    _warehouse(planned, day5=False)
    assert (
        _cli(monkeypatch, ["drift", "--connection", "warehouse", "--accept", "--dir", str(planned)])
        == 0
    )
    capsys.readouterr()
    (planned / "wh.duckdb").unlink()

    assert _plan(monkeypatch, planned, "--json") == 0
    entries = [e for e in json.loads(capsys.readouterr().out) if e.get("scope") == "warehouse"]
    assert [e["status"] for e in entries] == ["unavailable"]
    assert "did not profile from here" in entries[0]["note"]


def test_plan_gives_up_on_a_warehouse_that_hangs(
    monkeypatch: pytest.MonkeyPatch, planned: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The second bound, and the one that decides whether this feature is safe to
    put on a daily verb: a warehouse that accepts the connection and then never
    answers costs PLAN_WAREHOUSE_TIMEOUT and not one second more."""
    import json
    import time

    _warehouse(planned, day5=False)
    assert (
        _cli(monkeypatch, ["drift", "--connection", "warehouse", "--accept", "--dir", str(planned)])
        == 0
    )
    capsys.readouterr()

    def hang(*a: object, **k: object) -> list[TableProfile]:
        time.sleep(30)
        return []

    monkeypatch.setattr("services.lenses.profiler_catalog.catalog_profiles", hang)
    monkeypatch.setattr("services.cli.main.PLAN_WAREHOUSE_TIMEOUT", 0.2)

    started = time.monotonic()
    assert _plan(monkeypatch, planned, "--json") == 0
    assert time.monotonic() - started < 10  # not 30

    entries = [e for e in json.loads(capsys.readouterr().out) if e.get("scope") == "warehouse"]
    assert [e["status"] for e in entries] == ["unavailable"]
    assert "no answer within" in entries[0]["note"]
