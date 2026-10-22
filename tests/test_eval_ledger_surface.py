"""The eval, test and ledger surface: what `dst test`, the slot lane, the
request ledger and the answer envelope say about themselves.

Pinned here: a certified SQL's attributed gold is the answer's metric, not
every column it reads (a projected ratio's numerator and denominator are its
inputs); dated rows on a declared grain match by bucket; the slot lane proves a
latest-snapshot subquery and an alias are spelling; per-slot calibration;
`eval_result.actual_value` on every data-lane and slot-lane row; the
cross-lens run history in one call; the request ledger export; `source` on
every answer; and test traffic tagged and left out of the serving KPIs.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from services.certify.store import CertifiedAnswer
from services.config import settings
from services.contracts.eval import EvalResult
from services.contracts.query_intent import QueryIntent
from services.contracts.resolution import Resolution, Slot
from services.contracts.response import CertifiedProvenance, QueryResponse
from services.contracts.semantic_model import Entity, EntitySource, Field, Metric, SemanticModel
from services.evals import calibration
from services.evals.calibration import GradedDecision
from services.evals.certified_suite import _compare, _grain, value_json
from services.evals.slot_lane import SlotCase, eval_results, gold_for, sql_match, summary
from services.runtime.compiler import compile_intent
from services.runtime.resolution import answer_measures, attribute


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


# ── gold is the answer's metric ──────────────────────────────────────────────


def _members_model() -> SemanticModel:
    """A membership bridge as BI marts ship it: opening members, their change,
    net retention over them, and a month-end level beside the plain sum that
    shares its column."""
    return SemanticModel(
        lens="members",
        dialect="bigquery",
        entities=[
            Entity(
                name="member_bridge",
                source=EntitySource(connection="wh", table="marts.member_bridge"),
                default_time_field="month",
                fields=[
                    Field(name="open_members", type="number"),
                    Field(name="net_change", type="number"),
                    Field(name="close_members", type="number"),
                    Field(name="members", type="number"),
                    Field(name="month", type="date"),
                    Field(name="is_month_end", type="boolean"),
                ],
                metrics=[
                    Metric(
                        name="branch_open",
                        agg="sum",
                        expr="member_bridge.open_members",
                    ),
                    Metric(
                        name="branch_net_change",
                        agg="sum",
                        expr="member_bridge.net_change",
                    ),
                    Metric(name="branch_close", agg="sum", expr="member_bridge.close_members"),
                    Metric(
                        name="branch_retention",
                        type="ratio",
                        numerator="branch_close",
                        denominator="branch_open",
                    ),
                    Metric(name="members", agg="sum", expr="member_bridge.members"),
                    Metric(
                        name="members_month_end",
                        agg="sum",
                        expr="member_bridge.members",
                        filters=["member_bridge.is_month_end = TRUE"],
                    ),
                ],
            )
        ],
    )


def _gold(sql: str) -> list[tuple[str, str]]:
    model = _members_model()
    res = answer_measures(attribute(sql, model), sql, model)
    return sorted((s.name, s.source) for s in res.slots if s.kind == "metric")


@pytest.mark.parametrize(
    "sql",
    [
        # The inputs projected beside the ratio over them.
        "SELECT SUM(open_members) AS branch_open, SUM(net_change) AS branch_net_change, "
        "(SUM(open_members) + SUM(net_change)) / NULLIF(SUM(open_members), 0) "
        "AS branch_retention "
        "FROM marts.member_bridge AS member_bridge",
        # The same through a CTE, the ratio over the CTE's columns.
        "WITH b AS (SELECT SUM(open_members) AS opened, SUM(net_change) AS delta "
        "FROM marts.member_bridge) SELECT opened AS branch_open, delta AS branch_net_change, "
        "(opened + delta) / opened AS branch_retention "
        "FROM b",
    ],
)
def test_ratio_gold_is_the_ratio_not_its_inputs(sql: str) -> None:
    model = _members_model()
    every = {s.name for s in attribute(sql, model).slots if s.kind == "metric"}
    assert {"branch_open", "branch_net_change"} <= every  # what the SQL reads
    assert _gold(sql) == [("branch_retention", "declared")]  # what the answer is


def test_level_gold_is_the_projected_measure_not_the_filters() -> None:
    # The latest month-end snapshot: members_month_end is the answer; the
    # subquery that finds the date reads nothing that is the answer.
    assert _gold(
        "SELECT SUM(members) AS members_month_end FROM marts.member_bridge "
        "WHERE month = (SELECT MAX(month) FROM marts.member_bridge WHERE is_month_end = TRUE)"
    ) == [("members_month_end", "declared")]
    # A plain sum named after the plain metric stays members.
    assert _gold("SELECT SUM(members) AS members FROM marts.member_bridge") == [
        ("members", "declared")
    ]
    # A metric read only inside a filter subquery is not the answer: the
    # projected aggregate (a tie no alias breaks) stays the inferred aggregate.
    gold = _gold(
        "SELECT month, SUM(members) AS total FROM marts.member_bridge WHERE month IN "
        "(SELECT month FROM marts.member_bridge GROUP BY 1 HAVING SUM(close_members) > 0) "
        "GROUP BY month"
    )
    assert ("branch_close", "declared") not in gold
    assert [source for _n, source in gold] == ["inferred"]


def test_a_derived_table_column_resolves_to_what_it_computes() -> None:
    sql = (
        "WITH x AS (SELECT SUM(close_members) AS closing FROM marts.member_bridge) "
        "SELECT closing FROM x"
    )
    assert _gold(sql) == [("branch_close", "declared")]


def test_slot_lane_gold_is_narrowed_too() -> None:
    sql = (
        "SELECT SUM(open_members) AS branch_open, SUM(net_change) AS branch_net_change, "
        "(SUM(open_members) + SUM(net_change)) / NULLIF(SUM(open_members), 0) "
        "AS branch_retention "
        "FROM marts.member_bridge AS member_bridge"
    )
    a = CertifiedAnswer(id="a1", lens="members", question="Net retention?", sql=sql, created_by="t")
    gold = gold_for(a, _members_model(), SimpleNamespace(_domains={}))
    assert gold is not None
    assert [s.name for s in gold.slots if s.kind == "metric"] == ["branch_retention"]


# ── rows on a time grain match by bucket ─────────────────────────────────────

_GEN = [["2026-04-01", 18452.5], ["2026-05-01", 18611.0], ["2026-06-01", 18790.25]]
# Month ends, the latest month labelled by its last snapshot day (a partial month).
_CERT = [["2026-04-30", 18452.5], ["2026-05-31", 18611.0], ["2026-06-20", 18790.25]]


def test_month_grain_rows_match_on_the_bucket() -> None:
    cols = ["month", "members"]
    ok, reason = _compare(cols, _CERT, cols, _GEN, "month")
    assert ok
    assert reason is not None and reason.startswith("bucket-match:") and "month" in reason


def test_without_a_grain_arbitrary_days_stay_days() -> None:
    cols = ["month", "members"]
    assert _compare(cols, _CERT, cols, _GEN) == (False, "3 missing / 3 extra row(s) vs certified")
    # A day grain is not a bucket: two days are two days.
    assert not _compare(["d", "n"], [["2026-02-01", 1]], ["d", "n"], [["2026-02-02", 1]], "day")[0]


def test_bucket_match_never_folds_two_rows_into_one() -> None:
    # Two certified days in April with one value are two facts; bucketed
    # they would be one row, so the rule stands down and the diff decides.
    cert = [["2026-04-10", 1.0], ["2026-04-20", 1.0]]
    gen = [["2026-04-01", 1.0], ["2026-05-01", 1.0]]
    assert not _compare(["d", "v"], cert, ["d", "v"], gen, "month")[0]


def test_the_grain_comes_from_the_gold_then_the_generation() -> None:
    month = Resolution(
        method="attributed",
        tag="declared",
        slots=[Slot(kind="grain", name="month", source="declared", value="MONTH")],
    )
    none = Resolution(method="attributed", tag="declared", slots=[])
    assert _grain(none, month) == "month"
    assert _grain(None, None) is None


# ── the compile lane: spelling is not a difference ───────────────────────────


def _snapshot_model() -> SemanticModel:
    return SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity(
                name="arr_snapshots",
                source=EntitySource(connection="wh", table="arr_snapshots"),
                default_time_field="snapshot_date",
                fields=[
                    Field(name="snapshot_date", type="date"),
                    Field(name="arr", type="number"),
                ],
                metrics=[
                    Metric(name="arr", agg="sum", expr="arr_snapshots.arr", additivity="level"),
                    Metric(name="bookings", agg="sum", expr="arr_snapshots.arr"),
                ],
            )
        ],
    )


@pytest.mark.parametrize(
    "certified",
    [
        "SELECT SUM(arr) AS arr FROM arr_snapshots "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM arr_snapshots)",
        "SELECT SUM(arr) AS total_arr FROM arr_snapshots "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM arr_snapshots)",
        "SELECT SUM(s.arr) AS arr FROM arr_snapshots s "
        "WHERE (SELECT MAX(snapshot_date) FROM arr_snapshots) = s.snapshot_date",
    ],
)
def test_latest_snapshot_scalar_subquery_is_equivalent_to_the_compiled_one(
    certified: str,
) -> None:
    model = _snapshot_model()
    compiled = compile_intent(QueryIntent(entity="arr_snapshots", metrics=["arr"]), model)
    assert "arr_snapshots_latest" in compiled  # the correlated form the compiler writes
    assert sql_match(compiled, certified, "duckdb") == "equivalent"


def test_metric_alias_vs_raw_column_projection_is_equivalent() -> None:
    model = _snapshot_model()
    compiled = compile_intent(QueryIntent(entity="arr_snapshots", metrics=["bookings"]), model)
    for certified in (
        "SELECT SUM(arr) FROM arr_snapshots",
        "SELECT SUM(arr) AS x FROM arr_snapshots",
    ):
        assert sql_match(compiled, certified, "duckdb") == "equivalent"
    # A different literal is a different query, alias or not.
    other = "SELECT SUM(arr) FROM arr_snapshots WHERE snapshot_date = DATE '2026-01-31'"
    assert sql_match(compiled, other, "duckdb") == "differs"


def _case(**kw: object) -> SlotCase:
    base: dict[str, object] = {
        "answer_id": "a1",
        "question": "arr?",
        "verdict": "passed",
        "evidence": "",
        "typed": True,
    }
    return SlotCase(**{**base, **kw})  # type: ignore[arg-type]


def test_differs_with_equal_data_is_reported_not_failed() -> None:
    case = _case(sql_match="differs", data_equal=True, intent='{"entity":"x"}')
    (row,) = eval_results([case])
    assert row.passed is True
    assert row.checks["sql_match"] == "differs" and row.checks["sql_data_equal"] is True
    lane = SimpleNamespace(
        cases=[case], latencies_s=[], cost_usd=0.0, elapsed_s=0.0, workers=1, repeat=1, skipped=[]
    )
    assert summary([("o/l", lane)], repeat=1)["sql_differs_data_equal"] == 1  # type: ignore[index, list-item]


# ── calibration per slot ─────────────────────────────────────────────────────


def test_calibration_by_slot_carries_n_accuracy_unanimous_and_ece() -> None:
    records = [
        GradedDecision(
            decider="vote:m:k=5",
            slot="metric",
            chosen="a",
            gold="a" if i % 2 else "b",
            p=1.0,
            question=f"q{i}",
        )
        for i in range(16)
    ] + [
        GradedDecision(
            decider="vote:m:k=5",
            slot="grain",
            chosen="month",
            gold="month",
            p=0.8,
            question=f"g{i}",
        )
        for i in range(25)
    ]
    by = calibration.by_slot(calibration.report(records))
    (metric,) = by["metric"]
    assert metric["decider"] == "vote:m:k=5"
    assert (metric["n"], metric["accuracy"]) == (16, 0.5)
    assert (metric["unanimous_n"], metric["unanimous_accuracy"]) == (16, 0.5)
    # Below the n floor ECE is not a number anyone should read: None, and why.
    assert metric["ece"] is None and metric["status"] == "n too small"
    (grain,) = by["grain"]
    assert grain["status"] == "measured" and grain["ece"] == pytest.approx(0.2)


# ── actual_value on every row ────────────────────────────────────────────────


def test_value_json_is_the_verified_value_shape() -> None:
    import datetime as dt
    import decimal

    assert json.loads(value_json({"value": decimal.Decimal("82.59")})) == {"value": 82.59}
    table = {"columns": ["month", "arr"], "rows": [[dt.date(2026, 4, 30), decimal.Decimal("1.5")]]}
    assert json.loads(value_json(table)) == {
        "columns": ["month", "arr"],
        "rows": [["2026-04-30", 1.5]],
    }
    assert json.loads(value_json({"error": "boom"})) == {"error": "boom"}


def test_slot_rows_carry_their_generated_value() -> None:
    typed = _case(intent='{"entity":"arr_snapshots"}', compiled_sql="SELECT 1")
    untyped = _case(answer_id="a2", verdict="failed", typed=False, evidence="did not type: x")
    rows = eval_results([typed, untyped])
    assert rows[0].actual_value == '{"entity":"arr_snapshots"}' and rows[0].actual_sql == "SELECT 1"
    assert json.loads(rows[1].actual_value or "") == {"typed": False, "evidence": "did not type: x"}


@pytest.fixture
def admin_org():
    from services.auth.tokens import hash_token, new_admin_token

    admin = create_engine(settings.database_admin_url)
    raw = new_admin_token()
    with admin.begin() as c:
        org = c.execute(
            text("INSERT INTO org (name) VALUES (:n) RETURNING id"), {"n": f"Ledger-{uuid.uuid4()}"}
        ).scalar_one()
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash, label) VALUES (:o,:h,'t')"),
            {"o": org, "h": hash_token(raw)},
        )
    yield org, raw
    with admin.begin() as c:
        for table in ("eval_result", "eval_run", "request_log", "lens", "admin_token"):
            c.execute(text(f"DELETE FROM {table} WHERE org_id = :o"), {"o": org})
        c.execute(text("DELETE FROM org WHERE id = :o"), {"o": org})


@needs_db
def test_actual_value_reads_back_from_eval_result(admin_org) -> None:
    from services.db.session import org_session
    from services.evals import store

    org, _raw = admin_org
    data_row = EvalResult(
        run_id="",
        case_id="certified:c1",
        question="arr?",
        passed=True,
        grade="pass",
        actual_value=value_json({"value": 19}),
    )
    (slot_row,) = eval_results([_case(answer_id="c1", intent='{"entity":"e"}')])
    with org_session(org) as s:
        run_id = store.create_run(s, "arr", "test", score=1.0, passed=2, failed=0, errored=0)
        for r in (data_row, slot_row):
            r.run_id = run_id
        store.record_results(s, run_id, [data_row, slot_row])
        got = {r.case_id: r.actual_value for r in store.list_results(s, run_id)}
    assert json.loads(got["certified:c1"] or "") == {"value": 19}
    assert got["slots:c1"] == '{"entity":"e"}'


# ── run history across lenses, in one call ───────────────────────────────────


@needs_db
def test_runs_for_every_lens_in_one_call(admin_org, monkeypatch, capsys) -> None:
    from services.app import app
    from services.db.session import org_session
    from services.evals import store

    org, raw = admin_org
    with org_session(org) as s:
        for i in range(3):
            store.create_run(
                s, "customer_value", "test", score=i / 2, passed=i, failed=0, errored=0
            )
        store.create_run(s, "other_lens", "test", score=1.0, passed=1, failed=0, errored=0)
    client = TestClient(app)
    r = client.get("/mgmt/observe/runs?last=2", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text
    by = {e["lens"]: e["runs"] for e in r.json()["runs"]}
    assert {n: len(rs) for n, rs in by.items()} == {"customer_value": 2, "other_lens": 1}
    assert by["customer_value"][0]["started_at"] >= by["customer_value"][1]["started_at"]

    # The CLI reads that endpoint once.
    import httpx

    calls: list[str] = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        req = httpx.Request("GET", url)
        if url.endswith("/mgmt/observe/runs"):
            return httpx.Response(
                200,
                json={"runs": [{"lens": "a", "runs": []}, {"lens": "b", "runs": []}]},
                request=req,
            )
        return httpx.Response(404, json={"detail": "no"}, request=req)

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("DST_URL", "http://localhost:8000")
    monkeypatch.setenv("DST_ADMIN_TOKEN", "dstadm_t")
    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", "runs", "--all", "--last", "3", "--json"])
    assert main() == 0
    assert calls == ["http://localhost:8000/mgmt/observe/runs"]
    assert json.loads(capsys.readouterr().out) == {
        "runs": [{"lens": "a", "runs": []}, {"lens": "b", "runs": []}]
    }


# ── the request ledger export + test traffic ─────────────────────────────────


def _seed(org: object, rows: list[dict[str, object]]) -> None:
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        for r in rows:
            c.execute(
                text(
                    "INSERT INTO request_log (org_id, request_id, lens, caller, agent, question, "
                    "status, certification, resolution_tag, generator_tier, created_at) VALUES "
                    "(:o, :rid, :lens, :caller, :agent, :q, :status, :cert, :tag, :tier, "
                    "CAST(:ts AS timestamptz))"
                ),
                {
                    "o": org,
                    "rid": r.get("rid", f"req_{uuid.uuid4().hex[:16]}"),
                    "lens": r.get("lens", "arr"),
                    "caller": r.get("caller", "ana"),
                    "agent": r.get("agent"),
                    "q": r.get("q", "arr?"),
                    "status": r.get("status", "ok"),
                    "cert": r.get("cert", "none"),
                    "tag": r.get("tag", "declared"),
                    "tier": r.get("tier"),
                    "ts": r.get("ts", "2026-10-14T10:00:00+00:00"),
                },
            )


_LEDGER = [
    {
        "rid": "req_a",
        "status": "ok",
        "cert": "certified",
        "tag": "certified",
        "ts": "2026-10-13T09:00:00+00:00",
    },
    {"rid": "req_b", "status": "ok", "ts": "2026-10-14T09:00:00+00:00"},
    {"rid": "req_c", "status": "clarification", "tag": None, "ts": "2026-10-15T09:00:00+00:00"},
    {
        "rid": "req_d",
        "status": "refused",
        "tag": None,
        "lens": "ops",
        "ts": "2026-10-16T09:00:00+00:00",
    },
    {
        "rid": "req_e",
        "status": "error",
        "tag": None,
        "caller": "bo",
        "ts": "2026-10-17T09:00:00+00:00",
    },
    {"rid": "req_t", "status": "ok", "agent": "dst-test", "ts": "2026-10-17T10:00:00+00:00"},
    {"rid": "req_old", "status": "ok", "ts": "2026-09-01T09:00:00+00:00"},
    {"rid": "req_probe", "status": "ok", "tier": "probe", "ts": "2026-10-15T10:00:00+00:00"},
]


@needs_db
def test_request_ledger_export(admin_org) -> None:
    from services.app import app

    org, raw = admin_org
    _seed(org, _LEDGER)
    client = TestClient(app)
    h = {"Authorization": f"Bearer {raw}"}
    r = client.get("/mgmt/requests?since=2026-10-12&until=2026-10-19", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [x["request_id"] for x in rows] == ["req_a", "req_b", "req_c", "req_d", "req_e", "req_t"]
    kinds = {x["request_id"]: (x["answer_kind"], x["covered"]) for x in rows}
    assert kinds == {
        "req_a": ("certified", True),
        "req_b": ("generated", True),
        "req_c": ("clarify", True),
        "req_d": ("refuse", False),
        "req_e": ("error", True),
        "req_t": ("generated", True),
    }
    assert next(x for x in rows if x["request_id"] == "req_t")["agent"] == "dst-test"
    assert set(rows[0]) == {
        "ts",
        "caller",
        "agent",
        "question",
        "lens",
        "covered",
        "answer_kind",
        "certification",
        "request_id",
    }
    only = client.get("/mgmt/requests?lens=ops&caller=ana", headers=h).json()
    assert [x["request_id"] for x in only] == ["req_d"]
    as_csv = client.get("/mgmt/requests?since=2026-10-17&format=csv", headers=h)
    assert as_csv.headers["content-type"].startswith("text/csv")
    parsed = list(csv.DictReader(io.StringIO(as_csv.text)))
    assert [p["request_id"] for p in parsed] == ["req_e", "req_t"]
    assert client.get("/mgmt/requests?since=last-week", headers=h).status_code == 400


def test_observe_export_verb_wraps_the_ledger(monkeypatch, capsys) -> None:
    import httpx

    seen: list[tuple[str, dict[str, object]]] = []

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.append((url, dict(params or {})))
        return httpx.Response(200, text="ts,caller\n", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("DST_URL", "http://localhost:8000")
    monkeypatch.setenv("DST_ADMIN_TOKEN", "dstadm_t")
    from services.cli.main import main

    monkeypatch.setattr(
        sys, "argv", ["dst", "observe", "export", "--since", "2026-10-12", "--format", "csv"]
    )
    assert main() == 0
    assert seen == [
        ("http://localhost:8000/mgmt/requests", {"since": "2026-10-12", "format": "csv"})
    ]
    assert capsys.readouterr().out == "ts,caller\n"


@needs_db
def test_test_traffic_is_left_out_of_kpis_and_basis_by_default(admin_org) -> None:
    from services.db.session import org_session
    from services.observability import observe

    org, _raw = admin_org
    _seed(
        org,
        [
            {"status": "ok", "tag": "declared", "ts": "now"},
            {"status": "ok", "tag": "inferred", "agent": "dst-test", "ts": "now"},
            {"status": "error", "tag": None, "agent": "dst-test", "ts": "now"},
            {"status": "ok", "tag": "inferred", "agent": "benchmark", "ts": "now"},
        ],
    )
    with org_session(org) as s:
        k = observe.kpis(s)
        k_all = observe.kpis(s, include_test=True)
        audit = observe.audit_statement(s, days=30)
        callers = observe.caller_report(s)
    assert k["queries"] == 1 and k["outcomes"]["error"] == 0  # type: ignore[index]
    assert k["test_requests"] == 3
    assert k["basis"]["n"] == 1 and k["basis"]["governed_share"] == 1.0  # type: ignore[index]
    assert k["basis"]["test_excluded"] == 2  # type: ignore[index]
    assert k_all["queries"] == 4 and k_all["basis"]["n"] == 3  # type: ignore[index]
    assert k_all["test_requests"] == 0
    assert audit["asked"] == 1 and audit["test_requests"] == 3
    assert [c["queries"] for c in callers] == [1]


# ── every answer names its source ────────────────────────────────────────────


def _response(**kw: object) -> QueryResponse:
    return QueryResponse(lens="arr", answer="x", request_id="req_1", **kw)  # type: ignore[arg-type]


def test_every_answer_names_its_source() -> None:
    certified = _response(
        certification="certified",
        certified_provenance=CertifiedProvenance(cert_id="c-9", certified_by="t", certified_at=""),
    )
    compiled = _response(resolution=Resolution(method="construction", tag="declared"))
    generated = _response(resolution=Resolution(method="attributed", tag="inferred"))
    refused = _response(status="refused")
    out = [r.model_dump(mode="json") for r in (certified, compiled, generated, refused)]
    assert [(o["source"], o["certified_answer_id"]) for o in out] == [
        ("certified", "c-9"),
        ("compiled", None),
        ("generated", None),
        (None, None),
    ]
    # The routed envelope nests the same model: the fields ride it unchanged.
    from services.api.route import RoutedAnswer

    assert (
        "source"
        in RoutedAnswer.model_json_schema(mode="serialization")["$defs"]["QueryResponse"][
            "properties"
        ]
    )
    # A dumped response validates back (the derived fields are not inputs).
    assert QueryResponse.model_validate(out[0]).source == "certified"


def test_a_raw_date_axis_and_its_bucket_grade_as_one_breakdown() -> None:
    """The certified SQL groups by month_end_date; the typed reading buckets
    month_end_date by month. The data lane matches those rows on the bucket, so
    the slot lane reads them as one axis too, and says so — it failed the case
    with expected and got printing the same names."""
    from services.runtime.resolution import grade

    gold = Resolution(
        method="attributed",
        tag="declared",
        slots=[
            Slot(kind="metric", name="members_month_end", source="declared"),
            Slot(kind="dimension", name="month_end_date", source="declared"),
        ],
    )
    got = Resolution(
        method="construction",
        tag="declared",
        slots=[
            Slot(kind="metric", name="members_month_end", source="declared"),
            Slot(kind="grain", name="month_end_date", source="declared", value="month"),
        ],
    )
    verdict, evidence = grade(gold, got)
    assert verdict == "passed" and evidence.startswith("bucket-match")
    # the other way round reads the same
    assert grade(got, gold)[0] == "passed"
    # a bucket on another date column is another axis
    other = got.model_copy(
        update={
            "slots": [
                got.slots[0],
                Slot(kind="grain", name="signed_on", source="declared", value="month"),
            ]
        }
    )
    assert grade(gold, other)[0] == "failed"


def test_data_equal_is_claimed_only_beside_the_right_metric() -> None:
    """A differs case whose rows lane matched the certified rows, but whose
    compiled SQL read another metric: the rows lane ran its own generation,
    so "data equal" says nothing about this SQL and is not claimed."""
    from services.evals.slot_lane import metric_slot_passed

    def graded(chosen: str) -> list[GradedDecision]:
        return [
            GradedDecision(decider="v", slot="metric", chosen=chosen, gold="lapsed_members", p=1.0)
        ]

    assert metric_slot_passed(_case(sql_match="differs", graded=graded("lapsed_members")))
    assert not metric_slot_passed(_case(sql_match="differs", graded=graded("new_members")))
    assert not metric_slot_passed(_case(sql_match="differs"))  # nothing typed, nothing claimed
