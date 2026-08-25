"""Governance CLI parity — the UI must never be the only
way to govern. `dst rule --certify` promotes in the same act; `dst
reviews --json/--watch` are the agent surfaces. httpx is monkeypatched (the
test_project_sync CLI pattern) — no server, no DB.

`dst patches approve` is the same parity for the self-healing loop —
the ruling comes back as a proposed FILE, and this writes it into the working tree
so the human reviews it with `git diff` and lands it with `dst apply`."""

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


# ── patches: approve writes the proposed file, apply lands it ────────────────


def _approval(**over: object) -> dict[str, object]:
    a: dict[str, object] = {
        "id": "pc_1",
        "status": "approved",
        "kind": "definition",
        "target": "repeat_customer",
        "live": False,
        "applied": {},
        "proposed_file": {
            "path": "semantic/definitions/repeat-customer.md",
            "content": "---\nmetric: repeat_customer\n---\n\nWindowed to 12 months.\n",
            "diff": "--- a/x\n+++ b/x\n+Windowed to 12 months.\n",
        },
        "next_step": (
            "write semantic/definitions/repeat-customer.md, commit it, then run "
            "`dst apply` — this ruling is not live until then"
        ),
        "eval_case_id": "ec_7",
    }
    a.update(over)
    return a


def test_cli_patches_approve_writes_the_proposed_file(monkeypatch, capsys, tmp_path) -> None:
    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        assert url == "http://localhost:8000/mgmt/reviews/patches/pc_1/approve"
        return httpx.Response(200, json=_approval(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    argv = ["patches", "approve", "pc_1", "--dir", str(tmp_path), "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    landed = tmp_path / "semantic" / "definitions" / "repeat-customer.md"
    assert landed.read_text() == "---\nmetric: repeat_customer\n---\n\nWindowed to 12 months.\n"
    out = capsys.readouterr().out
    assert "pc_1: approved (definition repeat_customer)" in out
    assert "NOT live yet" in out and "dst apply" in out
    assert "eval case ec_7 filed (candidate)" in out


def test_cli_patches_approve_patches_the_existing_file_for_the_term(
    monkeypatch, capsys, tmp_path
) -> None:
    """A term like `overtake_scope` proposed as `overtake-scope.md` lands BESIDE
    the author's own `overtake_scope.md` — two files, one term, silently
    diverging. A definition is identified by the term inside the page."""
    authored = tmp_path / "semantic" / "definitions" / "repeat_customer.md"
    authored.parent.mkdir(parents=True)
    authored.write_text("---\nmetric: repeat_customer\n---\n\nOne order or more.\n")

    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        return httpx.Response(200, json=_approval(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    argv = ["patches", "approve", "pc_1", "--dir", str(tmp_path), "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert "Windowed to 12 months." in authored.read_text()
    assert not (tmp_path / "semantic" / "definitions" / "repeat-customer.md").exists()
    assert sorted(p.name for p in authored.parent.glob("*.md")) == ["repeat_customer.md"]
    out = capsys.readouterr().out
    assert "already authored in semantic/definitions/repeat_customer.md" in out


def test_cli_patches_approve_finds_the_page_the_scaffold_nested(
    monkeypatch, capsys, tmp_path
) -> None:
    """Regression sweep 1: the flat case above was fixed, the NESTED one is what
    `dst init` actually produces — it scaffolds definitions one level deeper,
    into `semantic/definitions/examples/`. The resolver globbed a single
    directory while the loader loads the whole subtree, so approve landed a
    SECOND `semantic/definitions/lifetime-value.md` and the next `dst plan`
    exited 1 on a layout the product's own scaffold wrote. Built from the real
    scaffold rather than a fixture: that layout is the entire point."""
    import argparse

    from services.cli.init import run_init
    from services.project.loader import split_semantic
    from services.semantic.files import parse_semantic_files

    root = tmp_path / "stranger"
    ns = argparse.Namespace(dir=str(root), name=None, warehouse="demo", example=True, yes=True)
    assert run_init(ns) == 0
    nested = root / "semantic" / "definitions" / "examples" / "lifetime-value.md"
    assert nested.exists(), "the scaffold stopped nesting — this test is about that layout"

    approval = _approval(
        target="lifetime_value",
        proposed_file={
            "path": "semantic/definitions/lifetime-value.md",
            "content": "---\nmetric: lifetime_value\n---\n\nNet of refunds.\n",
            "diff": "",
        },
    )

    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        return httpx.Response(200, json=approval, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    argv = ["patches", "approve", "pc_1", "--dir", str(root), "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert "Net of refunds." in nested.read_text(encoding="utf-8")
    assert not (root / "semantic" / "definitions" / "lifetime-value.md").exists()
    assert "already authored in semantic/definitions/examples/lifetime-value.md" in (
        capsys.readouterr().out
    )
    # and the flywheel leaves a tree that still plans: two pages for one term is
    # exactly the ValueError plan reports as `semantic: invalid` and exits 1 on.
    files = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in root.rglob("*")
        if p.is_file() and p.suffix in (".md", ".yaml", ".yml")
    }
    parse_semantic_files(split_semantic(files))


def test_cli_patches_approve_leaves_a_matching_filename_alone(
    monkeypatch, capsys, tmp_path
) -> None:
    """The proposed name already IS the term's page — no note, no relocation."""
    authored = tmp_path / "semantic" / "definitions" / "repeat-customer.md"
    authored.parent.mkdir(parents=True)
    authored.write_text("---\nmetric: repeat_customer\n---\n\nOne order or more.\n")
    # a same-directory neighbour for a DIFFERENT term must not attract the write
    (authored.parent / "lifetime-value.md").write_text("---\nmetric: lifetime_value\n---\n\nLTV.\n")

    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        return httpx.Response(200, json=_approval(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    argv = ["patches", "approve", "pc_1", "--dir", str(tmp_path), "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert "Windowed to 12 months." in authored.read_text()
    assert "LTV." in (authored.parent / "lifetime-value.md").read_text()
    assert "already authored" not in capsys.readouterr().out


def test_cli_patches_approve_works_from_a_relative_dir(monkeypatch, capsys, tmp_path) -> None:
    """`--dir` defaults to '.' — the resolved glob paths must still print relative."""
    authored = tmp_path / "semantic" / "definitions" / "repeat_customer.md"
    authored.parent.mkdir(parents=True)
    authored.write_text("---\nmetric: repeat_customer\n---\n\nOne order or more.\n")

    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        return httpx.Response(200, json=_approval(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.chdir(tmp_path)
    assert _run_cli(monkeypatch, ["patches", "approve", "pc_1", "--token", "dstadm_t"]) == 0
    assert "Windowed to 12 months." in authored.read_text()
    assert "wrote semantic/definitions/repeat_customer.md" in capsys.readouterr().out


def test_cli_patches_approve_shared_asks_the_server_for_the_shared_layer(
    monkeypatch, capsys, tmp_path
) -> None:
    """Approve could only ever propose lens-local for a term the lens hadn't already
    compiled from shared, so a ruling on a cross-cutting term was confined to one
    lens. `--shared` is the explicit promotion."""
    sent: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        sent["params"] = params
        return httpx.Response(200, json=_approval(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    argv = ["patches", "approve", "pc_1", "--dir", str(tmp_path), "--shared", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert sent["params"] == {"shared": "true"}
    assert (tmp_path / "semantic" / "definitions" / "repeat-customer.md").exists()


def test_cli_patches_approve_without_shared_sends_no_flag(monkeypatch, capsys, tmp_path) -> None:
    sent: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        sent["params"] = params
        return httpx.Response(200, json=_approval(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    argv = ["patches", "approve", "pc_1", "--dir", str(tmp_path), "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert sent["params"] is None


def test_cli_patches_approve_without_a_file_prints_the_next_step(monkeypatch, capsys) -> None:
    # A certified promotion is DB-first: live immediately, nothing to write.
    approval = _approval(
        kind="certified",
        target="how many repeat customers?",
        live=True,
        applied={"certified_id": "ca_1", "replaced": 0},
        proposed_file=None,
        next_step="live now; `dst export --lens customer_value` round-trips it",
    )

    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        return httpx.Response(200, json=approval, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    assert _run_cli(monkeypatch, ["patches", "approve", "pc_1", "--token", "dstadm_t"]) == 0
    assert "live now; `dst export --lens customer_value`" in capsys.readouterr().out


def test_cli_patches_list_json_is_the_agent_surface(monkeypatch, capsys) -> None:
    candidates = [
        {
            "id": "pc_1",
            "lens": "customer_value",
            "kind": "definition",
            "target": "repeat_customer",
            "status": "candidate",
        }
    ]

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url == "http://localhost:8000/mgmt/lenses/customer_value/patches"
        assert params == {"status": "candidate"}
        return httpx.Response(200, json=candidates, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    argv = [
        "patches",
        "list",
        "--lens",
        "customer_value",
        "--status",
        "candidate",
        "--json",
        "--token",
        "dstadm_t",
    ]
    assert _run_cli(monkeypatch, argv) == 0
    assert json.loads(capsys.readouterr().out) == candidates


def test_cli_patches_list_without_a_lens_says_so(monkeypatch, capsys) -> None:
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no lens, no request"))
    assert _run_cli(monkeypatch, ["patches", "list", "--token", "dstadm_t"]) == 1
    assert "needs --lens" in capsys.readouterr().err


# ── patches: reject carries the reason ───────────────────────────────────────


def test_cli_patches_reject_sends_the_note(monkeypatch, capsys) -> None:
    """A mistargeted draft could only be ignored. `reject --note` rules on it
    AND lands the reason — the most useful feedback the drafter could get."""
    sent: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["url"], sent["body"] = url, json
        return httpx.Response(
            200, json={"id": "pc_1", "status": "rejected"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    note = "mistargeted — belongs on closing balance growth rate"
    argv = ["patches", "reject", "pc_1", "--note", note, "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert sent["url"] == "http://localhost:8000/mgmt/reviews/patches/pc_1/reject"
    assert sent["body"] == {"note": note}
    assert f"pc_1: rejected — {note}" in capsys.readouterr().out


def test_cli_patches_reject_without_a_note_sends_no_body(monkeypatch, capsys) -> None:
    sent: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["body"] = json
        return httpx.Response(
            200, json={"id": "pc_1", "status": "rejected"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    assert _run_cli(monkeypatch, ["patches", "reject", "pc_1", "--token", "dstadm_t"]) == 0
    assert sent["body"] is None
    assert "pc_1: rejected" in capsys.readouterr().out


def test_cli_patches_reject_without_an_id_says_so(monkeypatch, capsys) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no id, no request"))
    assert _run_cli(monkeypatch, ["patches", "reject", "--token", "dstadm_t"]) == 1
    assert "`dst patches reject` needs a patch id" in capsys.readouterr().err


def test_cli_patches_reject_surfaces_the_server_error(monkeypatch, capsys) -> None:
    """Already-ruled patches 404 — the CLI must say so, not exit 0 on a no-op."""

    def fake_post(url, headers=None, json=None, timeout=None):
        return httpx.Response(
            404,
            json={"detail": "patch candidate not found or already ruled"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    assert _run_cli(monkeypatch, ["patches", "reject", "pc_1", "--token", "dstadm_t"]) == 1
    assert "already ruled" in capsys.readouterr().err


# ── serve readiness signal ───────────────────────────────────────────────────


def test_serve_readiness_poll_retries_until_health_answers(monkeypatch) -> None:
    """Serve accepts connections ~8s after the port announcement with
    no hint — the /health poll behind the waiting→ready lines must ride out
    connection refusals until the app answers, and give up honestly."""
    from services.cli.main import _wait_for_health

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
