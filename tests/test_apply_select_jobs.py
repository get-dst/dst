"""`dst plan/apply --select` and `--scope`, files as the source of truth, and
apply as a background job.

Selection: SQLMesh-style selectors resolve against the push's own graph (a
lens reads the entities and definitions it selects); only the selected nodes'
files are pushed, so an unselected lens is neither validated nor touched, and
a selected shared change that an unselected published lens reads is refused,
naming it. Files win: a case dropped from evals/cases.yaml retires, and values
the server stamps onto a row never show as a change, so a plan right after an
apply is clean. Jobs: `?background=true` answers 202 with an id at once; the
job records per-lens progress and the rows, and a second apply while one runs
is a 409 naming the job.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.app import app
from services.config import settings
from services.db.session import org_session
from services.evals import store as eval_store
from services.project.plan import resolve_selection
from tests.test_apply_lens_selection import (
    _RED_CASE,
    A,
    B,
    _apply,
    _files,
    _garbage_llm,
    needs_db,
    org,  # noqa: F401 — the fixture
)

client = TestClient(app)


def _rows(out: list[dict[str, object]]) -> dict[str, object]:
    return {str(e["lens"]): e.get("action") for e in out if e.get("lens")}


def _plan(headers: dict[str, str], files: dict[str, str], select=None, scope=None):  # type: ignore[no-untyped-def]
    params: list[tuple[str, str]] = [("select", s) for s in select or []]
    if scope:
        params.append(("scope", scope))
    return client.post("/mgmt/project/plan", headers=headers, params=params, json={"files": files})


def _apply_sel(headers, files, select=None, scope=None, **extra):  # type: ignore[no-untyped-def]
    params: list[tuple[str, str]] = [("select", s) for s in select or []]
    if scope:
        params.append(("scope", scope))
    params += [(k, str(v).lower()) for k, v in extra.items()]
    return client.post("/mgmt/project/apply", headers=headers, params=params, json={"files": files})


# ── resolution (pure) ────────────────────────────────────────────────────────


def _resolve(selectors: list[str], scope: str = "all", files=None):  # type: ignore[no-untyped-def]
    return resolve_selection(files or _files(), selectors, scope, db_semantic={}, db_trees={})


def test_a_bare_lens_selects_the_lens_alone() -> None:
    sel = _resolve([f"lens/{A}"])
    assert sel.lenses == [A] and sel.assets == []
    assert all(p.startswith(f"lenses/{A}/") for p in sel.files)


def test_plus_before_a_lens_takes_what_it_reads() -> None:
    sel = _resolve([f"+lens/{B}"])
    assert sel.lenses == [B]
    assert sel.assets == ["entity/orders"]  # B selects orders and no definition
    assert "semantic/entities/orders.yaml" in sel.files
    assert "semantic/entities/customers.yaml" not in sel.files


def test_plus_after_an_asset_takes_every_lens_that_reads_it() -> None:
    assert _resolve(["entity/orders+"]).lenses == [A, B]
    assert _resolve(["entity/customers+"]).lenses == [A]


def test_globs_match_names_and_a_selector_matching_nothing_is_an_error() -> None:
    assert _resolve(["lens/*"]).lenses == [A, B]
    assert _resolve(["definition/*"]).assets == [
        "definition/lifetime_value",
        "definition/repeat_customer",
        "definition/value",
    ]
    with pytest.raises(ValueError, match="matches nothing"):
        _resolve(["lens/finance_*"])
    with pytest.raises(ValueError, match="not kind/name"):
        _resolve(["model/x"])


def test_changed_is_the_plans_own_diff() -> None:
    files = _files()
    db_trees = {
        lens: {p.split("/", 2)[2]: c for p, c in files.items() if p.startswith(f"lenses/{lens}/")}
        for lens in (A, B)
    }
    from services.project.loader import split_semantic

    db_semantic = {p: c for p, c in split_semantic(files).items()}
    edited = _files(a_note="customer questions")
    sel = resolve_selection(edited, ["@changed"], "all", db_semantic=db_semantic, db_trees=db_trees)
    assert sel.lenses == [A] and sel.assets == []


def test_scope_limits_the_kinds_of_files() -> None:
    files = {**_files(), "dst.yaml": "connections: {}\n"}
    assert set(_resolve([], "semantic", files).files) == {
        p for p in files if p.startswith("semantic/")
    }
    assert set(_resolve([], "config", files).files) == {"dst.yaml"}


# ── selection against the server ────────────────────────────────────────────


@needs_db
def test_an_unselected_lens_is_neither_validated_nor_touched(org, monkeypatch) -> None:  # noqa: F811
    _oid, headers = org
    _garbage_llm(monkeypatch)
    assert not any(e.get("errors") for e in _apply(headers, _files()).json())
    files = _files(a_note="customer questions")
    files[f"lenses/{B}/lens.yaml"] = "name: [this is not a lens\n"

    plan = _plan(headers, files, select=[f"lens/{A}"]).json()
    assert plan[0]["scope"] == "selection" and plan[0]["lenses"] == [A]
    assert {e.get("lens") for e in plan if e.get("lens")} == {A}
    assert not any(e.get("status") == "invalid" for e in plan), plan

    _garbage_llm(monkeypatch)
    r = _apply_sel(headers, files, select=[f"lens/{A}"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out[0]["scope"] == "selection"
    assert _rows(out) == {A: "updated"}


@needs_db
def test_a_selected_shared_change_refuses_an_unselected_reader_unless_plus_takes_it(
    org,  # noqa: F811
    monkeypatch,
) -> None:
    _oid, headers = org
    _garbage_llm(monkeypatch)
    assert not any(e.get("errors") for e in _apply(headers, _files()).json())
    files = _files(orders_tweak="One row per placed order.")

    plan = _plan(headers, files, select=["entity/orders"]).json()
    refused = {e["lens"]: e for e in plan if e.get("status") == "invalid"}
    assert set(refused) == {A, B} and "entity/orders+" in str(refused[A]["error"])

    r = _apply_sel(headers, files, select=["entity/orders"])
    assert r.status_code == 422, r.text
    assert A in r.json()["detail"] and B in r.json()["detail"]
    assert "entity/orders+" in r.json()["detail"]

    _garbage_llm(monkeypatch)
    r = _apply_sel(headers, files, select=["entity/orders+"])
    assert r.status_code == 200, r.text
    assert _rows(r.json()) == {A: "updated", B: "updated"}


@needs_db
def test_scope_cases_lands_cases_and_runs_no_gate(org, monkeypatch) -> None:  # noqa: F811
    oid, headers = org
    _garbage_llm(monkeypatch)
    assert not any(e.get("errors") for e in _apply(headers, _files()).json())
    with org_session(oid) as s:
        before = s.execute(text("SELECT count(*) FROM lens_version")).scalar()

    # A red case under a whole-tree apply aborts (the gate runs) …
    _garbage_llm(monkeypatch)
    out = _apply(headers, _files(b_cases=_RED_CASE)).json()
    assert out[-1]["action"] == "aborted"

    # … under --scope cases it lands, nothing republishes, no gate runs.
    _garbage_llm(monkeypatch)
    r = _apply_sel(headers, _files(b_cases=_RED_CASE), select=[f"lens/{B}"], scope="cases")
    assert r.status_code == 200, r.text
    out = r.json()
    assert not any(e.get("action") == "aborted" for e in out), out
    row = next(e for e in out if e.get("lens") == B)
    assert row["action"] == "unchanged" and row["gate"] is None
    assert "eval cases: created 1, updated 0, unchanged 0" in row["applied"]
    with org_session(oid) as s:
        assert s.execute(text("SELECT count(*) FROM lens_version")).scalar() == before
        assert [c.question for c in eval_store.list_cases(s, B)] == [_RED_CASE[0]["question"]]


# ── files are the source of truth ───────────────────────────────────────────


@needs_db
def test_plan_right_after_apply_reports_zero_changes(org, monkeypatch) -> None:  # noqa: F811
    """A project with certified answers and eval cases: apply, let the server
    stamp the values it records (verified value and prose), plan — zero
    changes. Then drop a case from the file: apply retires it and says so,
    and the next plan is clean again."""
    from services.contracts.fakes import HashEmbedder
    from services.project import apply as apply_engine

    oid, headers = org
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text("DELETE FROM embedding_meta"))
    monkeypatch.setattr(apply_engine, "_embedder", lambda: HashEmbedder())
    monkeypatch.setattr("services.llm.registry.resolve", lambda _ref: None)
    cases = [
        {"question": "How many orders were placed?", "expect": "answer"},
        {"question": "what is the value?", "expect": "clarify", "term": "value"},
    ]
    files = _files(b_cases=cases)
    files[f"lenses/{A}/certified_answers.yaml"] = yaml.safe_dump(
        [
            {
                "question": "How many repeat customers are there?",
                "sql": "SELECT COUNT(*) FROM customers WHERE number_of_orders > 1",
                "verified_by": "finance",
            }
        ],
        sort_keys=False,
    )
    out = _apply(headers, files).json()
    assert not any(e.get("errors") for e in out), out
    with admin.begin() as c:
        c.execute(
            text(
                "UPDATE certified_answer SET verified_value = '{\"value\": 7}', "
                "verified_prose = 'There are 7.' WHERE org_id = :o"
            ),
            {"o": oid},
        )

    def changes(plan: list[dict[str, object]]) -> list[object]:
        return [
            (e.get("lens") or e.get("path"), e.get("status"))
            for e in plan
            if e.get("status") not in (None, "unchanged", "hint")
        ]

    assert changes(_plan(headers, files).json()) == []

    files[f"lenses/{B}/evals/cases.yaml"] = yaml.safe_dump(cases[:1], sort_keys=False)
    assert changes(_plan(headers, files).json()) == [(B, "update")]
    out = _apply(headers, files).json()
    row = next(e for e in out if e.get("lens") == B)
    assert any("retired 1" in a for a in row["applied"]), row
    with org_session(oid) as s:
        status = {c.question: c.status for c in eval_store.list_cases(s, B)}
    assert status == {cases[0]["question"]: "approved", cases[1]["question"]: "retired"}
    assert changes(_plan(headers, files).json()) == []
    admin.dispose()


# ── apply as a background job ────────────────────────────────────────────────


@needs_db
def test_a_background_apply_returns_a_job_that_records_progress_and_rows(org, monkeypatch) -> None:  # noqa: F811
    _oid, headers = org
    _garbage_llm(monkeypatch)
    r = _apply_sel(headers, _files(), background=True)
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert r.json()["attach"] == f"dst apply status {job_id}"
    job = client.get(f"/mgmt/project/apply/jobs/{job_id}", headers=headers).json()
    assert job["state"] == "done"
    assert [p["lens"] for p in job["progress"]] == [A, B]
    assert _rows(job["result"]) == {A: "created", B: "created"}
    assert client.get("/mgmt/project/apply/jobs/nope", headers=headers).status_code == 404


@needs_db
def test_a_second_apply_while_one_runs_is_a_409_naming_the_job(org) -> None:  # noqa: F811
    from services.api.mgmt_project import _apply_lock_key, _job_write

    oid, headers = org
    _job_write(oid, "abc123", {"state": "running", "progress": []})
    holder = create_engine(settings.database_url).connect()
    try:
        tx = holder.begin()
        holder.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _apply_lock_key(oid)})
        for background in (True, False):
            r = _apply_sel(headers, _files(), background=background)
            assert r.status_code == 409, r.text
            assert "abc123" in r.json()["detail"]
            assert "dst apply status abc123" in r.json()["detail"]
        tx.rollback()
    finally:
        holder.close()
    # The lock is free and the job still says running: its process died. The
    # next background apply marks it failed and runs.
    r = _apply_sel(headers, _files(), background=True)
    assert r.status_code == 202, r.text
    old = client.get("/mgmt/project/apply/jobs/abc123", headers=headers).json()
    assert old["state"] == "failed" and "stopped" in old["detail"]


# ── the CLI side of jobs ─────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int, body: object) -> None:
        self.status_code, self._body, self.text = status, body, ""

    def json(self) -> object:
        return self._body

    def raise_for_status(self) -> _Resp:
        return self


def _cli(monkeypatch, tmp_path, argv: list[str], *, post=None, get=None) -> int:  # type: ignore[no-untyped-def]
    import sys

    import httpx

    from services.cli import main as cli

    (tmp_path / "dst.yaml").write_text("connections: {}\n")
    if post:
        monkeypatch.setattr(httpx, "post", post)
    if get:
        monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(sys, "argv", ["dst", *argv, "--dir", str(tmp_path), "--token", "dstadm_t"])
    return int(cli.main())


def test_cli_apply_polls_the_job_and_no_wait_prints_its_id(monkeypatch, tmp_path, capsys) -> None:
    sent: list[dict[str, object]] = []

    def post(url, **kw):  # type: ignore[no-untyped-def]
        sent.append(kw["params"])
        return _Resp(202, {"job_id": "j1", "state": "queued"})

    polls = iter(
        [
            {"state": "running", "progress": [{"lens": "a", "action": "updated"}]},
            {
                "state": "done",
                "progress": [{"lens": "a", "action": "updated"}],
                "result": [{"lens": "a", "action": "updated", "version": 2}],
            },
        ]
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    get = lambda url, **kw: _Resp(200, next(polls))  # noqa: E731
    argv = ["apply", "--select", "lens/a", "+entity/x", "--lens", "b", "--scope", "certified"]
    assert _cli(monkeypatch, tmp_path, argv, post=post, get=get) == 0
    assert sent[0]["select"] == ["lens/a", "+entity/x", "lens/b"]
    assert sent[0]["scope"] == "certified" and sent[0]["background"] == "true"
    assert "a: updated" in capsys.readouterr().err

    assert _cli(monkeypatch, tmp_path, ["apply", "--no-wait"], post=post) == 0
    assert "dst apply status j1" in capsys.readouterr().out


def test_cli_apply_status_prints_state_and_progress(monkeypatch, tmp_path, capsys) -> None:
    job = {"state": "running", "progress": [{"lens": "a", "action": "updated"}]}
    get = lambda url, **kw: _Resp(200, job)  # noqa: E731
    assert _cli(monkeypatch, tmp_path, ["apply", "status", "j1"], get=get) == 0
    assert "apply job j1: running — 1 lens(es): a updated" in capsys.readouterr().out

    failed = {"state": "failed", "status_code": 409, "detail": "another apply (job j0)"}
    get = lambda url, **kw: _Resp(200, failed)  # noqa: E731
    assert _cli(monkeypatch, tmp_path, ["apply", "status", "j1"], get=get) == 1
    assert "job j0" in capsys.readouterr().err


# ── the apply-time connection probe ─────────────────────────────────────────


class _Probe:
    kind = "duckdb"

    def __init__(self, fail: set[str] | None = None, hang: bool = False) -> None:
        self.sql: list[str] = []
        self.fail, self.hang = fail or set(), hang

    def introspect(self):  # type: ignore[no-untyped-def]
        raise AssertionError("declared tables must not introspect every dataset")

    def dry_run(self, sql: str):  # type: ignore[no-untyped-def]
        from services.contracts.warehouse import DryRunResult

        return DryRunResult()

    def execute(self, sql: str, *, read_only: bool = True, row_limit=None):  # type: ignore[no-untyped-def]
        import time

        from services.contracts.warehouse import QueryResult

        self.sql.append(sql)
        if self.hang and "LIMIT 0" in sql:
            time.sleep(2)
        if any(t in sql for t in self.fail):
            raise RuntimeError("permission denied")
        return QueryResult()


def test_the_probe_reads_each_declared_table_once_not_every_dataset() -> None:
    from services.project.apply import _probe_connection

    conn = _Probe(fail={"sales.refunds"})
    result, _report, warnings = _probe_connection("wh", conn, ["sales.orders", "sales.refunds"], 8)
    assert result.ok
    assert conn.sql == [
        "SELECT 1",
        "SELECT * FROM sales.orders LIMIT 0",
        "SELECT * FROM sales.refunds LIMIT 0",
    ]
    assert warnings == [
        "connection 'wh': cannot read declared table sales.refunds: permission denied"
    ]

    none_readable, _r, _w = _probe_connection("wh", _Probe(fail={"sales."}), ["sales.orders"], 8)
    assert not none_readable.ok and "no declared table is readable" in str(
        none_readable.failure.error if none_readable.failure else ""
    )


def test_a_step_timeout_names_the_setting_the_step_and_the_connection_size(monkeypatch) -> None:
    from services.project.apply import _probe_connection
    from services.runtime.bounded import ApplyStepTimeout

    monkeypatch.setattr(settings, "apply_step_timeout_s", 0.3)
    with pytest.raises(ApplyStepTimeout) as caught:
        _probe_connection("wh", _Probe(hang=True), ["sales.orders"], 8)
    message = str(caught.value)
    assert "DST_APPLY_STEP_TIMEOUT_S" in message
    assert "the read of table 'sales.orders' on connection 'wh'" in message
    assert "1 declared table(s), 8 dataset(s)" in message
