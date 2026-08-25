"""`dst query` + token-less verbs: the agent-DX seam.

In-project agents were sourcing .env and curling /v1 by hand (and grepping
shell history for tokens). The CLI now reads DST_ADMIN_TOKEN from the env
or ./.env, bootstrap saves it there, and `dst query` is the verify
one-liner.
"""

from __future__ import annotations

import json

import httpx
import pytest

from services.cli.main import EXIT_DECLINED


def _run_cli(monkeypatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def _fake_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://x"))


def test_query_prints_answer_sql_and_meta(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return _fake_response(
            {
                "lens": "sales",
                "answer": "Total bookings were 11.7M EUR.",
                "sql": "SELECT SUM(x) FROM deals",
                "confidence": "verified",
                "certification": "none",
                "definition_used": "bookings",
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    assert _run_cli(monkeypatch, ["query", "sales", "total bookings?", "--token", "dstadm_t"]) == 0
    out = capsys.readouterr().out
    assert "Total bookings were 11.7M EUR." in out
    assert "sql: SELECT SUM(x) FROM deals" in out
    assert "confidence: verified" in out and "definition: bookings" in out
    assert seen["url"].endswith("/v1/lenses/sales/query")
    assert seen["auth"] == "Bearer dstadm_t"


def test_query_renders_clarification(monkeypatch, capsys) -> None:
    """A clarification still renders — and exits DECLINED, because no SQL ran."""
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response(
            {
                "lens": "sales",
                "status": "clarification",
                "answer": "ambiguous",
                "clarification": {
                    "term": "earnings",
                    "question": "commission or total payout?",
                    "options": ["commission - payouts.commission"],
                },
            }
        ),
    )
    code = _run_cli(monkeypatch, ["query", "sales", "earnings?", "--token", "dstadm_t"])
    assert code == EXIT_DECLINED
    out = capsys.readouterr().out
    assert "clarify: commission or total payout?" in out
    assert "- commission - payouts.commission" in out


def test_token_resolves_from_project_env_file(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / ".env").write_text("DST_ADMIN_TOKEN=dstadm_from_env\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _fake_response({"lens": "l", "answer": "ok"})
    )
    assert _run_cli(monkeypatch, ["query", "l", "q?"]) == 0
    assert "ok" in capsys.readouterr().out


def test_missing_token_is_actionable(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("DST_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, ["query", "l", "q?"])
    err = capsys.readouterr().err
    assert "dst bootstrap" in err and "--key" in err


def test_query_json_dumps_the_full_response(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _fake_response({"lens": "l", "answer": "ok", "extra": 1})
    )
    assert _run_cli(monkeypatch, ["query", "l", "q?", "--json", "--token", "dstadm_t"]) == 0
    assert json.loads(capsys.readouterr().out)["extra"] == 1


# ─── a query that ran no SQL exits non-zero, never a silent empty ────────────


@pytest.mark.parametrize(
    ("status", "answer", "code"),
    [
        # Failures: something broke and nothing ran.
        ("rejected", "I couldn't form an in-scope query: parse error: …", 1),
        ("rejected", "I couldn't form an in-scope query: SELECT * is not allowed", 1),
        ("error", "The query could not be executed: Parser Error: syntax error", 1),
        (
            "error",
            "The query could not be executed: SQL generation did not return within the "
            "5s serving timeout (DST_SERVING_TIMEOUT_S)",
            1,
        ),
        # A decline is NOT a failure — it is the governed outcome, and it gets its
        # own code so a caller can tell "the lens cannot know this" from "the lens
        # is broken" without reading English.
        ("refused", "I can't answer this from this lens's data: no churn table", EXIT_DECLINED),
    ],
)
def test_query_without_sql_exits_non_zero(monkeypatch, capsys, status, answer, code) -> None:
    """Three independent triggers — a parser error, a `SELECT *` rejection and a
    provider timeout — all exited 0 with the reason as prose in `answer` and
    `verification`/`confidence` null. A caller could not tell an answer from
    nothing having run."""
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response(
            {"lens": "l", "status": status, "answer": answer, "verification": None}
        ),
    )
    assert _run_cli(monkeypatch, ["query", "l", "q?", "--token", "dstadm_t"]) == code
    captured = capsys.readouterr()
    # stdout is the answer channel and nothing else, so `$(dst query …)` is
    # either an answer or empty — never a sentence explaining why there isn't one.
    assert captured.out.strip() == ""
    assert status in captured.err and answer[:30] in captured.err


def test_query_json_exit_code_follows_status_too(monkeypatch, capsys) -> None:
    """`--json` already carried the payload; it carried no outcome and still
    exited 0. The structured path needs both."""
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response({"lens": "l", "status": "error", "answer": "boom"}),
    )
    assert _run_cli(monkeypatch, ["query", "l", "q?", "--json", "--token", "dstadm_t"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_unknown_status_is_a_failure_not_an_answer(monkeypatch, capsys) -> None:
    """An old client against a newer server must not report success out of ignorance."""
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response({"lens": "l", "status": "something-new", "answer": "?"}),
    )
    assert _run_cli(monkeypatch, ["query", "l", "q?", "--token", "dstadm_t"]) == 1


# ── asking AS a caller ────────────────────────────────────────────────────────
# An admin token bypasses every lens allow-list, so "I granted B access, does it
# work?" answered 200 no matter what. Agents proved grants with curl instead.


def _capture_auth(seen: dict) -> object:
    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        seen["auth"] = headers["Authorization"]
        return _fake_response({"lens": "l", "answer": "ok", "request_id": "req_abc123"})

    return fake_post


def test_caller_key_flag_authenticates_as_that_caller(monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(httpx, "post", _capture_auth(seen))
    assert _run_cli(monkeypatch, ["query", "l", "q?", "--key", "dst_b", "--token", "dstadm_t"]) == 0
    assert seen["auth"] == "Bearer dst_b"  # explicit key beats the admin token


def test_caller_key_resolves_from_the_dir_project_env(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text("DST_API_KEY=dst_from_env\n", encoding="utf-8")
    monkeypatch.delenv("DST_API_KEY", raising=False)
    seen: dict[str, object] = {}
    monkeypatch.setattr(httpx, "post", _capture_auth(seen))
    assert _run_cli(monkeypatch, ["query", "l", "q?", "--dir", str(tmp_path)]) == 0
    assert seen["auth"] == "Bearer dst_from_env"


def test_dir_does_not_borrow_the_shells_credentials(monkeypatch, tmp_path) -> None:
    """--dir names the project whose .env answers — the cwd's never leaks in."""
    shell, project = tmp_path / "shell", tmp_path / "project"
    shell.mkdir(), project.mkdir()
    (shell / ".env").write_text("DST_ADMIN_TOKEN=dstadm_shell\n", encoding="utf-8")
    (project / ".env").write_text("DST_ADMIN_TOKEN=dstadm_project\n", encoding="utf-8")
    monkeypatch.chdir(shell)
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("DST_API_KEY", raising=False)
    seen: dict[str, object] = {}
    monkeypatch.setattr(httpx, "post", _capture_auth(seen))
    assert _run_cli(monkeypatch, ["query", "l", "q?", "--dir", str(project)]) == 0
    assert seen["auth"] == "Bearer dstadm_project"


def test_denied_caller_exits_nonzero_with_the_servers_reason(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response(
            {"detail": "caller 'other' is not permitted for lens 'sales'"}, status=403
        ),
    )
    assert _run_cli(monkeypatch, ["query", "sales", "q?", "--key", "dst_other"]) == 1
    assert "not permitted for lens 'sales'" in capsys.readouterr().err


def test_request_id_prints_without_json(monkeypatch, capsys) -> None:
    """`dst correct` takes a request_id and nothing else; reaching it used to
    require re-running the query with --json."""
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response({"lens": "l", "answer": "ok", "request_id": "req_abc123"}),
    )
    assert _run_cli(monkeypatch, ["query", "l", "q?", "--token", "dstadm_t"]) == 0
    assert "request_id: req_abc123" in capsys.readouterr().out


def test_request_id_follows_a_refusal_onto_stderr(monkeypatch, capsys) -> None:
    """stdout stays the answer channel: a non-zero outcome puts nothing there,
    request_id included — but `dst correct` still needs the id, so it rides
    on stderr with the status."""
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response(
            {"lens": "l", "status": "error", "answer": "boom", "request_id": "req_dead"}
        ),
    )
    assert _run_cli(monkeypatch, ["query", "l", "q?", "--token", "dstadm_t"]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "request_id: req_dead" in captured.err


def test_define_prints_the_governed_meaning(monkeypatch, capsys) -> None:
    """`dst define` is the read door as a one-liner: the approved definition
    verbatim, with the lens that governs it and its [[cites]] — no SQL, no
    warehouse. Without it, the same question through the query path generates
    SQL and scans the warehouse to answer a question with no data in it."""
    seen: dict[str, object] = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["url"], seen["q"], seen["auth"] = url, params["q"], headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "q": "running balance",
                "definitions": [
                    {
                        "lens": "customer_balances",
                        "term": "running balance",
                        "body": "Cumulative balance, day by day.",
                        "status": "active",
                        "possible_mappings": [],
                        "cites": ["net transaction amount"],
                        "matched": "term",
                    }
                ],
            },
            request=httpx.Request("GET", "http://x"),
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    assert _run_cli(monkeypatch, ["define", "running balance", "--token", "dstadm_t"]) == 0
    out = capsys.readouterr().out
    assert "running balance  (lens: customer_balances)" in out
    assert "Cumulative balance, day by day." in out
    assert "cites: net transaction amount" in out
    assert seen["url"].endswith("/v1/definitions")
    assert seen["q"] == "running balance"
    assert seen["auth"] == "Bearer dstadm_t"


def test_define_exits_nonzero_when_nothing_is_governed(monkeypatch, capsys) -> None:
    """An ungoverned term is a fact to report on stderr with exit 1 — stdout stays
    the meaning channel, so `$(dst define …)` is a definition or empty."""
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200, json={"q": "nope", "definitions": []}, request=httpx.Request("GET", "http://x")
        ),
    )
    assert _run_cli(monkeypatch, ["define", "nope", "--token", "dstadm_t"]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "no governed definition mentions 'nope'" in captured.err
