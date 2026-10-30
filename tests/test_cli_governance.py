"""Governance CLI parity — the UI must never be the only
way to govern. `dst rule --certify` promotes in the same act; `dst
reviews --json/--watch` are the agent surfaces. httpx is monkeypatched (the
test_project_sync CLI pattern) — no server, no DB."""

from __future__ import annotations

import json
import time

import httpx
import pytest


def _run_cli(monkeypatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def _ticket(**over: object) -> dict[str, object]:
    t: dict[str, object] = {
        "ticket_id": "rev_1",
        "request_id": "req_9",
        "lens": "customer_value",
        "caller": "agent-1",
        "state": "pending",
        "ai_verdict": None,
        "ai_reasoning": None,
        "human_verdict": None,
        "human_reasoning": None,
        "correction": None,
    }
    t.update(over)
    return t


# ── rule --certify ───────────────────────────────────────────────────────────


def test_cli_rule_certify_promotes_and_passes_warning_verbatim(monkeypatch, capsys) -> None:
    warning = (
        "certified without an embedding — no embedding provider is configured, so this "
        "answer will not be served or matched until one is added and `dst reindex` runs"
    )
    posts: list[str] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(url)
        req = httpx.Request("POST", url)
        if url.endswith("/rule"):
            assert json == {"verdict": "approve", "reasoning": ""}
            return httpx.Response(
                200, json=_ticket(state="ruled", human_verdict="approve"), request=req
            )
        return httpx.Response(
            201, json={"id": "ca_1", "lens": "customer_value", "warning": warning}, request=req
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    argv = ["rule", "rev_1", "--verdict", "approve", "--certify", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    # ruled first, then promoted via the lens + request id the rule response carries
    assert posts == [
        "http://localhost:8000/mgmt/reviews/rev_1/rule",
        "http://localhost:8000/mgmt/lenses/customer_value/certified/from-request/req_9",
    ]
    out = capsys.readouterr()
    assert "rev_1: ruled approve (state=ruled)" in out.out
    assert "certified ca_1 (lens=customer_value)" in out.out
    assert warning in out.err  # the server's embedder warning, verbatim


def test_cli_rule_certify_without_warning_stays_quiet(monkeypatch, capsys) -> None:
    # DST_URL set: an UNCONFIGURED url is announced on stderr now (it is a
    # guess, and a silent guess targeted the wrong server) — this test is about
    # the certify warning, so give the command a configured one and stderr is
    # empty for the reason it means.
    monkeypatch.setenv("DST_URL", "http://localhost:8000")

    def fake_post(url, headers=None, json=None, timeout=None):
        req = httpx.Request("POST", url)
        if url.endswith("/rule"):
            return httpx.Response(
                200, json=_ticket(state="ruled", human_verdict="approve"), request=req
            )
        return httpx.Response(201, json={"id": "ca_1", "lens": "customer_value"}, request=req)

    monkeypatch.setattr(httpx, "post", fake_post)
    argv = ["rule", "rev_1", "--verdict", "approve", "--certify", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert capsys.readouterr().err == ""


def test_cli_certify_requires_approve_verdict(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: pytest.fail("argparse error must precede any HTTP")
    )
    argv = ["rule", "rev_1", "--verdict", "reject", "--certify", "--token", "dstadm_t"]
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, argv)
    assert exc.value.code == 2
    assert "--certify requires --verdict approve" in capsys.readouterr().err


# ── reviews --json ───────────────────────────────────────────────────────────


def test_cli_reviews_json_defaults_unfiltered(monkeypatch, capsys) -> None:
    # Defaulting --json to needs_human hid auto-approved
    # origin:ai tickets, making auto_review look like a no-op. The default is
    # ALL states — agents filter themselves with --state/--origin.
    def fake_get(url, headers=None, params=None, timeout=None):
        assert url == "http://localhost:8000/mgmt/reviews"
        assert params == {}  # no implicit state filter
        return httpx.Response(200, json=[_ticket()], request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert _run_cli(monkeypatch, ["reviews", "--json", "--token", "dstadm_t"]) == 0
    tickets = json.loads(capsys.readouterr().out)  # stdout is the JSON, nothing else
    assert tickets == [_ticket()]


def test_cli_reviews_origin_filters_client_side(monkeypatch, capsys) -> None:
    # The list endpoint filters by state only — --origin narrows client-side.
    def fake_get(url, headers=None, params=None, timeout=None):
        assert params == {}  # origin never leaks into the query string
        tickets = [
            _ticket(origin="human"),
            _ticket(ticket_id="rev_2", state="approved", origin="ai"),
        ]
        return httpx.Response(200, json=tickets, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    argv = ["reviews", "--json", "--origin", "ai", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    tickets = json.loads(capsys.readouterr().out)
    assert [t["ticket_id"] for t in tickets] == ["rev_2"]


def test_cli_reviews_json_honors_explicit_state(monkeypatch, capsys) -> None:
    def fake_get(url, headers=None, params=None, timeout=None):
        assert params == {"state": "ruled"}
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    argv = ["reviews", "--json", "--state", "ruled", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert json.loads(capsys.readouterr().out) == []  # empty list, not prose


# ── reviews --watch ──────────────────────────────────────────────────────────


def test_cli_reviews_watch_prints_new_tickets_once(monkeypatch, capsys) -> None:
    polls = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        req = httpx.Request("GET", url)
        if "/mgmt/observe/requests/" in url:
            return httpx.Response(
                200, json={"question": "how many repeat\ncustomers?"}, request=req
            )
        assert params == {"state": "needs_human"}
        tickets = [_ticket()]
        if polls["n"] >= 2:  # a second ticket appears on the third poll
            tickets.append(_ticket(ticket_id="rev_2", request_id="req_10", lens="ops"))
        polls["n"] += 1
        return httpx.Response(200, json=tickets, request=req)

    sleeps = {"n": 0}

    def fake_sleep(secs: float) -> None:
        assert secs == 5
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            raise KeyboardInterrupt  # the Ctrl-C that always ends a watch

    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr(httpx, "get", fake_get)
    argv = ["reviews", "--watch", "--interval", "5", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    lines = capsys.readouterr().out.splitlines()
    # rev_1 pending across all three polls → printed exactly once, question one-lined
    assert [ln for ln in lines if ln.startswith("rev_1")] == [
        "rev_1  customer_value  how many repeat customers?"
    ]
    assert "rev_2  ops  how many repeat customers?" in lines


def test_serve_readiness_poll_retries_until_health_answers(monkeypatch) -> None:
    """Serve accepts connections ~8s after the port announcement with
    no hint — the /health poll behind the waiting→ready lines must ride out
    connection refusals until the app answers, and give up honestly."""
    from services.cli.serve import _wait_for_health

    calls = {"n": 0}

    def fake_get(url, timeout=None):
        calls["n"] += 1
        assert url == "http://127.0.0.1:9/health"
        if calls["n"] < 3:
            raise httpx.ConnectError("not up yet")
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", fake_get)
    assert _wait_for_health("http://127.0.0.1:9", tries=5, delay=0) is True
    assert calls["n"] == 3  # stops polling the moment /health answers

    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(503))
    assert _wait_for_health("http://127.0.0.1:9", tries=2, delay=0) is False


def _resp(payload: dict, status: int = 200, method: str = "GET"):  # -> httpx.Response
    import httpx

    return httpx.Response(status, json=payload, request=httpx.Request(method, "http://x"))


def test_connection_rm_refuses_with_dependents(monkeypatch, capsys) -> None:
    """`dst connection rm` — without CLI parity for the mgmt DELETE, plan flags
    a server-only connection forever. It shows dependents and refuses while
    lenses still read through the connection."""
    import httpx

    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _resp({"name": "jaffle", "lenses": [{"name": "sales"}]})
    )
    argv = ["connection", "rm", "jaffle", "--yes", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 1
    err = capsys.readouterr().err
    assert "sales" in err and "refusing" in err


def test_connection_rm_deletes_when_clean(monkeypatch, capsys) -> None:
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp({"name": "jaffle", "lenses": []}))
    monkeypatch.setattr(
        httpx,
        "delete",
        lambda *a, **k: httpx.Response(204, request=httpx.Request("DELETE", "http://x")),
    )
    argv = ["connection", "rm", "jaffle", "--yes", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert "removed connection 'jaffle'" in capsys.readouterr().out
