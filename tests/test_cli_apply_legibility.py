"""A slow or failed apply must not become a zombie the operator can't read.

With a hard-coded client timeout, blowing it prints a raw httpx ReadTimeout
traceback — while the sync handler keeps running in the threadpool, holds the
per-org advisory lock, and COMMITS minutes later. The operator concludes
nothing landed, retries into a 409, and reads the lock as wreckage. Both verbs
take `--timeout`; a timeout, a 409 and a 5xx each say
what the server actually did with the push. That the 5xx line can promise
"nothing was deployed" is pinned server-side in tests/test_certified_gates.py
(one transaction, rolled back on any error). httpx is monkeypatched (the
test_cli_dir_env pattern) — no server, no DB.
"""

from __future__ import annotations

import httpx
import pytest


def _run_cli(monkeypatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


@pytest.fixture()
def project(monkeypatch, tmp_path):
    """A minimal project dir and no ambient credentials — every test passes
    --token so _client resolves without touching the developer's .env. The
    dst.yaml is what makes it a PROJECT: a directory holding none is
    refused before any request now (a mistyped --dir used to exit 0)."""
    monkeypatch.delenv("DST_URL", raising=False)
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    (tmp_path / "dst.yaml").write_text("name: t\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _capture(monkeypatch) -> dict[str, object]:
    seen: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        seen["timeout"] = timeout
        return httpx.Response(200, json=[], request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def _timeout_post(monkeypatch) -> None:
    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        raise httpx.ReadTimeout("timed out", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)


def _status_post(monkeypatch, status: int, payload: dict[str, object]) -> None:
    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        return httpx.Response(status, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)


@pytest.mark.parametrize(("verb", "default"), [("plan", 120), ("apply", 300)])
def test_default_timeout_is_unchanged(monkeypatch, project, verb, default) -> None:
    seen = _capture(monkeypatch)
    assert _run_cli(monkeypatch, [verb, "--dir", str(project), "--token", "dstadm_t"]) == 0
    assert seen["timeout"] == default


@pytest.mark.parametrize("verb", ["plan", "apply"])
def test_timeout_flag_reaches_httpx(monkeypatch, project, verb) -> None:
    seen = _capture(monkeypatch)
    argv = [verb, "--dir", str(project), "--token", "dstadm_t", "--timeout", "900"]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["timeout"] == 900


def test_apply_timeout_says_the_server_may_still_be_applying(monkeypatch, project, capsys) -> None:
    """A client timeout is not a failed apply: the message must name the lock,
    the eventual commit, and a liveness check that does NOT go through the
    possibly-wedged server's work plane — polling `dst plan` just hangs a
    second process against the same wedged server."""
    _timeout_post(monkeypatch)
    argv = ["apply", "--dir", str(project), "--token", "dstadm_t", "--timeout", "5"]
    assert _run_cli(monkeypatch, argv) == 1
    err = capsys.readouterr().err
    assert "within 5s" in err
    assert "the server may still be applying" in err
    assert "holds the org apply lock and will commit when done" in err
    assert "/ready" in err  # liveness first — wedged and working must look different
    assert "dst plan` polls the SAME server" in err
    assert "--timeout" in err
    assert "Traceback" not in err and "ReadTimeout" not in err


def test_plan_timeout_says_nothing_changed(monkeypatch, project, capsys) -> None:
    """plan takes no lock and writes nothing — it must NOT inherit apply's
    'may still be applying' hedge, or every timeout reads as a pending write."""
    _timeout_post(monkeypatch)
    argv = ["plan", "--dir", str(project), "--token", "dstadm_t", "--timeout", "7"]
    assert _run_cli(monkeypatch, argv) == 1
    err = capsys.readouterr().err
    assert "within 7s" in err
    assert "read-only and changed nothing" in err
    assert "--timeout" in err
    assert "still be applying" not in err
    assert "Traceback" not in err and "ReadTimeout" not in err


def test_apply_409_says_the_holder_will_commit(monkeypatch, project, capsys) -> None:
    _status_post(monkeypatch, 409, {"detail": "another apply is in progress for this org"})
    assert _run_cli(monkeypatch, ["apply", "--dir", str(project), "--token", "dstadm_t"]) == 1
    err = capsys.readouterr().err
    assert "another apply is in progress" in err
    assert "still running and will commit when it finishes" in err


def test_apply_5xx_states_that_nothing_landed(monkeypatch, project, capsys) -> None:
    """Apply is atomic, and the failure line has to say so: which stages
    survived — none — instead of leaving the operator to guess (and re-apply on
    top of a half-imagined state)."""
    _status_post(monkeypatch, 500, {"detail": "provider 404 mid-gate"})
    assert _run_cli(monkeypatch, ["apply", "--dir", str(project), "--token", "dstadm_t"]) == 1
    err = capsys.readouterr().err
    assert "provider 404 mid-gate" in err
    assert "nothing was deployed" in err
    assert "certified answers, eval cases and the lens publish rolled back together" in err


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_5xx_does_not_claim_nothing_landed(monkeypatch, project, capsys, status) -> None:
    """The rollback promise is the SERVER's. A proxy verdict means upstream
    never answered — the apply may still be in flight, exactly like a client
    timeout, so this family gets the in-flight message instead."""
    _status_post(monkeypatch, status, {"detail": "upstream did not respond"})
    assert _run_cli(monkeypatch, ["apply", "--dir", str(project), "--token", "dstadm_t"]) == 1
    err = capsys.readouterr().err
    assert f"HTTP {status} from a gateway" in err
    assert "the server may still be applying" in err
    assert "nothing was deployed" not in err


# ── a plan that predicts a rejection must not exit 0 ─────────────────────────


def _plan_rows(monkeypatch, rows: list[dict[str, object]]) -> None:
    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        return httpx.Response(200, json=rows, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)


def test_plan_exits_nonzero_when_a_file_would_be_rejected(monkeypatch, project, capsys) -> None:
    """The invalid row printed, then scrolled past under a screenful of clean
    create-diffs, and exit 0 sent the agent straight on to apply."""
    _plan_rows(
        monkeypatch,
        [
            {
                "scope": "semantic",
                "path": "semantic/entities/player.yaml",
                "status": "create",
                "diff": "+ name: player",
            },
            {
                "scope": "semantic",
                "path": "semantic/entities/match.yaml",
                "status": "invalid",
                "diff": "",
                "error": "semantic/entities/match.yaml: 'BIGINT' is not one of string, number",
            },
        ],
    )
    assert _run_cli(monkeypatch, ["plan", "--dir", str(project), "--token", "dstadm_t"]) == 1
    captured = capsys.readouterr()
    # the offending PATH leads its own row — "semantic: invalid" names no file.
    # (Summarized default; --full keeps the long form.)
    assert "✗ semantic/entities/match.yaml — " in captured.out
    assert "'BIGINT' is not one of" in captured.out
    assert "1 invalid" in captured.out  # the terraform-style counts line
    assert "1 file(s) would be REJECTED by apply" in captured.err
    # --full keeps the pre-summary byte layout for review flows
    assert (
        _run_cli(monkeypatch, ["plan", "--full", "--dir", str(project), "--token", "dstadm_t"]) == 1
    )
    assert "semantic/entities/match.yaml: invalid — " in capsys.readouterr().out


def test_plan_exits_nonzero_on_an_invalid_lens_or_project_file(monkeypatch, project) -> None:
    for row in (
        {"lens": "sales", "status": "invalid", "error": "lens.yaml: bad", "diffs": []},
        {"scope": "project", "status": "invalid", "error": "dst.yaml: bad"},
    ):
        _plan_rows(monkeypatch, [row])
        assert _run_cli(monkeypatch, ["plan", "--dir", str(project), "--token", "dstadm_t"]) == 1


def test_a_clean_plan_still_exits_zero(monkeypatch, project) -> None:
    _plan_rows(
        monkeypatch,
        [
            {"scope": "semantic", "path": "semantic/entities/a.yaml", "status": "unchanged"},
            {"scope": "semantic", "status": "hint", "hint": "no lens selects: x"},
            {"scope": "server_only", "kind": "lens", "name": "old", "note": "adopt or leave"},
            {"lens": "sales", "status": "unchanged", "diffs": []},
        ],
    )
    assert _run_cli(monkeypatch, ["plan", "--dir", str(project), "--token", "dstadm_t"]) == 0


def test_apply_summary_surfaces_a_certified_deletion(monkeypatch, project, capsys) -> None:
    """Files-win removal of a certified answer must not print only "updated",
    leaving --json as the sole evidence of the deletion. A deletion is the one
    staged count the summarized default must not swallow; a non-deleting apply
    stays terse."""
    _status_post(
        monkeypatch,
        200,
        [
            {
                "lens": "customer_value",
                "action": "updated",
                "applied": ["certified answers: created 0, updated 0, deleted 1, unchanged 1"],
            },
            {
                "lens": "sales",
                "action": "updated",
                "applied": ["certified answers: created 1, updated 0, unchanged 2"],
            },
        ],
    )
    assert _run_cli(monkeypatch, ["apply", "--dir", str(project), "--token", "dstadm_t"]) == 0
    out = capsys.readouterr().out
    assert "deleted 1" in out
    assert "created 1, updated 0, unchanged 2" not in out  # no deletion -> stays summarized
