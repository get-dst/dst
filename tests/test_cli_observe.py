"""`dst observe` — the attribution surface, from a terminal.

Questions like "who has been using the reporting tool and what for" are
answered by `/mgmt/observe/*` and rendered by the dashboard — but callers and
agents work in a terminal, so the data has to be reachable without a browser.

Two things are pinned: the verb answers without a browser, and an empty result
must not read like a broken surface.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest

from services.cli import main as cli


class _Resp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> Any:
        return self._payload


def _args(**over: Any) -> argparse.Namespace:
    base = dict(
        action=None,
        request_id=None,
        lens=None,
        status=None,
        limit=50,
        json=False,
        timeout=60.0,
        dir=".",
        url="http://x",
        token="dstadm_x",
    )
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def _routes(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    routes: dict[str, Any] = {}

    def fake_get(url: str, **_: Any) -> _Resp:
        for suffix, payload in routes.items():
            if url.endswith(suffix) or suffix in url:
                return _Resp(payload)
        return _Resp({"detail": "not found"}, 404)

    monkeypatch.setattr(cli, "_client", lambda *a, **k: ("http://x", {}))
    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    return routes


def test_summary_answers_who_used_it_and_how_much(_routes, capsys) -> None:
    """The header speaks the outcome vocabulary: declines are not errors.

    A burst with 62 declines and 1 fault printed "63 errors" once and sent a
    morning after a pipeline problem that did not exist; the split is the fix.
    """
    _routes["/kpis"] = {
        "queries": 651,
        "ai_cost_usd": 2.4859,
        "errors": 1,
        "declined": 87,
        "outcomes": {"ok": 563, "refused": 60, "clarification": 26, "rejected": 1, "error": 1},
    }
    _routes["/callers"] = [
        {"caller": "peder-holm", "queries": 384, "cost_usd": 1.7257, "errors": 1, "declined": 76},
        {"caller": "marta-lindqvist", "queries": 1, "cost_usd": 0.0, "errors": 0, "declined": 0},
    ]
    assert cli._observe(_args()) == 0
    out = capsys.readouterr().out
    assert "651 queries" in out
    assert "60 refused" in out and "1 error" in out
    assert "88 errors" not in out  # the conflated number must be unprintable
    # Per-person attribution is the point: a role that barely used it must be as
    # visible as the one that hammered it.
    assert "peder-holm" in out and "marta-lindqvist" in out


def test_summary_against_a_pre_split_server_says_non_ok_not_errors(_routes, capsys) -> None:
    """An old server's `errors` field counts every non-ok outcome. The CLI must
    not relabel that mixed number as "errors" — that is the exact lie the split
    removed."""
    _routes["/kpis"] = {"queries": 651, "ai_cost_usd": 2.4859, "errors": 88}
    _routes["/callers"] = [
        {"caller": "peder-holm", "queries": 384, "cost_usd": 1.7257, "errors": 77},
    ]
    assert cli._observe(_args()) == 0
    out = capsys.readouterr().out
    assert "88 non-ok" in out and "88 errors" not in out


def test_no_activity_says_so_rather_than_printing_an_empty_table(_routes, capsys) -> None:
    """ "Nobody used it" and "the surface is broken" must not look identical.

    An empty router page was read as a bug for exactly this reason.
    """
    _routes["/kpis"] = {"queries": 0, "ai_cost_usd": 0, "errors": 0}
    _routes["/callers"] = []
    assert cli._observe(_args()) == 0
    assert "no caller activity recorded yet" in capsys.readouterr().out


def test_requests_can_isolate_failures(_routes, capsys) -> None:
    _routes["/requests"] = [
        {
            "created_at": "2026-08-08T16:14:39+00:00",
            "caller": "peder-holm",
            "lens": "commercial",
            "status": "error",
            "question": "what was our total discount amount in 2025?",
        }
    ]
    assert cli._observe(_args(action="requests", status="error")) == 0
    out = capsys.readouterr().out
    assert "peder-holm" in out and "discount" in out and "error" in out


def test_requests_columns_never_run_together(_routes, capsys) -> None:
    """Width-exact values must still end before the next column begins.

    Pad-only formatting glued columns whenever a value filled its field: a
    14-char lens name ('customer_value') against the 13-char status
    'clarification' printed as 'customer_valueclarification'."""
    _routes["/requests"] = [
        {
            "created_at": "2026-08-08T16:14:39+00:00",
            "caller": "a-sixteen-char-x",
            "lens": "customer_value",
            "status": "clarification",
            "question": "what is the average value of a customer?",
        }
    ]
    assert cli._observe(_args(action="requests")) == 0
    out = capsys.readouterr().out
    assert "customer_value  clarification" in out
    assert "customer_valueclarification" not in out
    assert "a-sixteen-char-x  customer_value" in out


def test_show_prints_the_sql_that_ran(_routes, capsys) -> None:
    _routes["/requests/req_1"] = {
        "request_id": "req_1",
        "caller": "admin",
        "lens": "commercial",
        "status": "error",
        "question": "How much did we discount in total?",
        "sql": "WITH refunds AS (SELECT SUM(refunds.amount) FROM refunds) SELECT 1",
    }
    assert cli._observe(_args(action="show", request_id="req_1")) == 0
    out = capsys.readouterr().out
    assert "req_1" in out and "WITH refunds" in out


def test_json_is_parseable_for_agents(_routes, capsys) -> None:
    _routes["/kpis"] = {"queries": 2, "ai_cost_usd": 0.1, "errors": 0}
    _routes["/callers"] = [{"caller": "a", "queries": 2, "cost_usd": 0.1, "errors": 0}]
    assert cli._observe(_args(json=True)) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["kpis"]["queries"] == 2
    assert parsed["callers"][0]["caller"] == "a"


def test_summary_prints_the_basis_over_every_read_with_raw_inside(_routes, capsys) -> None:
    """One `basis:` line as shares of every served caller read, the caller's
    own SQL (`raw`) inside the denominator, the governed share after it.
    Zero buckets stay off the line; no reads, no line."""
    _routes["/kpis"] = {
        "queries": 1045,
        "ai_cost_usd": 0.1,
        "errors": 0,
        "declined": 0,
        "outcomes": {"ok": 1045},
        "resolution_histogram": {"certified": 500, "declared": 388, "mixed": 48, "inferred": 109},
        "basis": {
            "certified": 500,
            "declared": 388,
            "mixed": 48,
            "inferred": 109,
            "raw": 167,
            "unknown": 0,
            "n": 1212,
            "governed_share": 0.7723,
        },
    }
    _routes["/callers"] = [{"caller": "a", "queries": 1045, "cost_usd": 0.1, "errors": 0}]
    assert cli._observe(_args()) == 0
    out = capsys.readouterr().out
    assert (
        "basis: certified 41% · declared 32% · mixed 4% · inferred 9% · raw 14%  "
        "(n = 1,212) · governed 77%"
    ) in out
    assert "unknown" not in out

    _routes["/kpis"]["basis"] = {k: 0 for k in _routes["/kpis"]["basis"]} | {"governed_share": None}
    _routes["/kpis"]["resolution_histogram"] = {}
    assert cli._observe(_args()) == 0
    assert "basis:" not in capsys.readouterr().out


def test_summary_against_a_pre_basis_server_prints_the_governed_histogram(_routes, capsys) -> None:
    """A server without the basis block: the governed-only ledger histogram as
    counts; `unknown` = recorded before the ledger, printed as itself. A
    pre-ledger server prints no line."""
    _routes["/kpis"] = {
        "queries": 10,
        "ai_cost_usd": 0.1,
        "errors": 0,
        "declined": 0,
        "outcomes": {"ok": 10},
        "resolution_histogram": {"declared": 6, "inferred": 3, "unknown": 1},
    }
    _routes["/callers"] = [{"caller": "a", "queries": 10, "cost_usd": 0.1, "errors": 0}]
    assert cli._observe(_args()) == 0
    out = capsys.readouterr().out
    assert "basis: 6 declared · 3 inferred · 1 unknown" in out

    _routes["/kpis"].pop("resolution_histogram")
    assert cli._observe(_args()) == 0
    assert "basis:" not in capsys.readouterr().out


def test_show_prints_the_purpose_when_the_row_carries_one(_routes, capsys) -> None:
    """The caller's stated purpose is a label on the row; a reader asking "what
    for" must see it without `--json`."""
    _routes["/requests/req_p"] = {
        "request_id": "req_p",
        "lens": "sales",
        "status": "ok",
        "question": "revenue by country?",
        "sql": "SELECT country, SUM(amount) FROM orders GROUP BY 1",
        "purpose": "building a forecasting app",
    }
    assert cli._observe(_args(action="show", request_id="req_p")) == 0
    out = capsys.readouterr().out
    assert "purpose:      building a forecasting app" in out


def test_show_prints_a_probe_row_sql_once(_routes, capsys) -> None:
    """A raw-SQL probe stores its statement as the question too: the text view
    prints it once, under `sql:`, not as a question and again as SQL."""
    sql = "SELECT count(*) FROM staging.customers"
    _routes["/requests/req_s"] = {
        "request_id": "req_s",
        "lens": "ops",
        "status": "rejected",
        "question": sql,
        "sql": sql,
        "kind": "sql_probe",
    }
    assert cli._observe(_args(action="show", request_id="req_s")) == 0
    out = capsys.readouterr().out
    assert out.count(sql) == 1 and "sql:\n" + sql in out


def test_show_prints_the_ledger_slots(_routes, capsys) -> None:
    _routes["/requests/req-1"] = {
        "request_id": "req-1",
        "lens": "sales",
        "status": "ok",
        "confidence": "verified",
        "resolution_tag": "mixed",
        "question": "revenue by country?",
        "sql": "SELECT country, SUM(amount * 2) FROM orders GROUP BY 1",
        "resolution": {
            "method": "attributed",
            "tag": "mixed",
            "slots": [
                {"kind": "metric", "name": "revenue", "source": "declared"},
                {"kind": "metric", "name": "SUM(amount * 2)", "source": "inferred"},
                {"kind": "dimension", "name": "country", "source": "declared"},
            ],
        },
    }
    assert cli._observe(_args(action="show", request_id="req-1")) == 0
    out = capsys.readouterr().out
    assert "resolution_tag:" in out and "mixed" in out
    assert "revenue  [declared]" in out
    assert "SUM(amount * 2)  [inferred]" in out


def test_summary_headline_carries_the_bytes_scanned(_routes, capsys) -> None:
    _routes["/kpis"] = {"queries": 3, "ai_cost_usd": 0.1, "errors": 0, "wh_bytes": 16_000_000}
    _routes["/callers"] = []
    assert cli._observe(_args()) == 0
    assert "16.00 MB scanned" in capsys.readouterr().out
