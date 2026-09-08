"""`dst runs` — the run history, listed and diffed.

httpx is monkeypatched: these pin the CLI's contract — which endpoints it
reads, how A/B resolve, and the exit semantics CI branches on (exit 1 = score
regressed beyond the publish gate's epsilon, or any case newly failing).
"""

from __future__ import annotations

import httpx
import pytest


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


RUN_B = {  # newest
    "id": "bbbb1111-0000-0000-0000-000000000000",
    "mode": "test",
    "score": 0.5,
    "passed": 1,
    "failed": 1,
    "errored": 0,
    "started_at": "2026-08-29T10:00:00+00:00",
}
RUN_A = {  # older
    "id": "aaaa2222-0000-0000-0000-000000000000",
    "mode": "test",
    "score": 1.0,
    "passed": 2,
    "failed": 0,
    "errored": 0,
    "started_at": "2026-08-28T10:00:00+00:00",
}


def _result(question: str, passed: bool, reason: str | None = None) -> dict[str, object]:
    return {
        "case_id": f"c-{question}",
        "question": question,
        "passed": passed,
        "grade": "pass" if passed else "fail",
        "checks": {},
        "actual_sql": None,
        "actual_value": None,
        "reason": reason,
    }


def _fake_get(results_by_run: dict[str, list[dict[str, object]]], runs: list[dict[str, object]]):
    calls: list[str] = []
    auths: list[str] = []

    def fake(url, headers=None, params=None, timeout=None):
        calls.append(url)
        auths.append((headers or {}).get("Authorization", ""))
        req = httpx.Request("GET", url)
        for run_id, results in results_by_run.items():
            if url.endswith(f"/runs/{run_id}/results"):
                return httpx.Response(200, json=results, request=req)
        if url.endswith("/evals/runs"):
            return httpx.Response(200, json=runs, request=req)
        return httpx.Response(404, json={"detail": "not found"}, request=req)

    fake.auths = auths  # type: ignore[attr-defined]
    return fake, calls


@pytest.fixture(autouse=True)
def _url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DST_URL", "http://localhost:8000")
    monkeypatch.setenv("DST_ADMIN_TOKEN", "dstadm_t")


def test_runs_lists_newest_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, calls = _fake_get({}, [RUN_B, RUN_A])
    monkeypatch.setattr(httpx, "get", fake)
    assert _run_cli(monkeypatch, ["runs", "customer_value"]) == 0
    out = capsys.readouterr().out
    assert calls == ["http://localhost:8000/mgmt/lenses/customer_value/evals/runs"]
    assert out.index("bbbb1111") < out.index("aaaa2222")
    assert "50%" in out and "100%" in out


def test_runs_diff_regression_exits_1_and_names_the_flip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = {
        RUN_A["id"]: [_result("q1", True), _result("q2", True)],
        RUN_B["id"]: [_result("q1", True), _result("q2", False, "rows diverged")],
    }
    fake, _ = _fake_get(results, [RUN_B, RUN_A])
    monkeypatch.setattr(httpx, "get", fake)
    code = _run_cli(monkeypatch, ["runs", "customer_value", "--diff", "prev", "latest"])
    out = capsys.readouterr().out
    assert code == 1
    assert "newly failing: q2" in out
    assert "rows diverged" in out
    assert "regressed" in out


def test_runs_diff_flip_without_score_drop_still_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One case up, one down, same score — masked by score alone; the flip is
    what CI must see."""
    run_b = {**RUN_B, "score": 1.0, "passed": 2, "failed": 0}
    results = {
        RUN_A["id"]: [_result("q1", True), _result("q2", False)],
        RUN_B["id"]: [_result("q1", False, "diverged"), _result("q2", True)],
    }
    fake, _ = _fake_get(results, [run_b, {**RUN_A, "score": 1.0}])
    monkeypatch.setattr(httpx, "get", fake)
    code = _run_cli(monkeypatch, ["runs", "customer_value", "--diff", "prev", "latest"])
    out = capsys.readouterr().out
    assert code == 1
    assert "newly failing: q1" in out
    assert "newly passing: q2" in out


def test_runs_diff_clean_exits_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_b = {**RUN_B, "score": 1.0, "passed": 2, "failed": 0}
    results = {
        RUN_A["id"]: [_result("q1", True), _result("q2", False)],
        RUN_B["id"]: [_result("q1", True), _result("q2", True)],
    }
    fake, _ = _fake_get(results, [run_b, RUN_A])
    monkeypatch.setattr(httpx, "get", fake)
    code = _run_cli(monkeypatch, ["runs", "customer_value", "--diff", "aaaa", "bbbb"])
    out = capsys.readouterr().out
    assert code == 0
    assert "newly passing: q2" in out
    assert "no regression" in out


def test_runs_diff_ambiguous_prefix_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    twin = {**RUN_A, "id": "aaaa9999-0000-0000-0000-000000000000"}
    fake, _ = _fake_get({}, [RUN_B, RUN_A, twin])
    monkeypatch.setattr(httpx, "get", fake)
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, ["runs", "customer_value", "--diff", "aaaa", "latest"])
    assert exc.value.code == 1
    assert "2 runs match" in capsys.readouterr().err


def test_runs_diff_cross_env_reads_b_from_other_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_b = {**RUN_B, "score": 1.0, "passed": 1, "failed": 0}
    results = {
        RUN_A["id"]: [_result("q1", True)],
        RUN_B["id"]: [_result("q1", True)],
    }
    fake, calls = _fake_get(results, [run_b, RUN_A])
    monkeypatch.setattr(httpx, "get", fake)
    code = _run_cli(
        monkeypatch,
        [
            "runs",
            "customer_value",
            "--diff",
            "aaaa",
            "bbbb",
            "--other-url",
            "http://sandbox:8000",
            "--other-token",
            "dstadm_s",
        ],
    )
    assert code == 0
    assert "http://sandbox:8000/mgmt/lenses/customer_value/evals/runs" in calls
    assert (
        f"http://sandbox:8000/mgmt/lenses/customer_value/evals/runs/{RUN_B['id']}/results" in calls
    )
    # A stays on the primary server
    assert (
        f"http://localhost:8000/mgmt/lenses/customer_value/evals/runs/{RUN_A['id']}/results"
        in calls
    )


def test_runs_diff_other_token_alone_is_another_org_same_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """org-per-env: side B = same URL, different org token. Keying the other
    side off the URL alone silently reused A's runs — found dogfooding."""
    results = {
        RUN_A["id"]: [_result("q1", True)],
        RUN_B["id"]: [_result("q1", True)],
    }
    run_b = {**RUN_B, "score": 1.0, "passed": 1, "failed": 0}
    fake, calls = _fake_get(results, [run_b, RUN_A])
    monkeypatch.setattr(httpx, "get", fake)
    code = _run_cli(
        monkeypatch,
        ["runs", "customer_value", "--diff", "aaaa", "bbbb", "--other-token", "dstadm_b"],
    )
    assert code == 0
    runs_url = "http://localhost:8000/mgmt/lenses/customer_value/evals/runs"
    # B's runs are fetched separately, with B's token — never reused from A.
    fetches = [(u, a) for u, a in zip(calls, fake.auths, strict=True) if u == runs_url]
    assert (
        "http://localhost:8000/mgmt/lenses/customer_value/evals/runs",
        "Bearer dstadm_t",
    ) in fetches
    assert (
        "http://localhost:8000/mgmt/lenses/customer_value/evals/runs",
        "Bearer dstadm_b",
    ) in fetches


def test_runs_diff_prev_stays_within_latest_runs_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Probe finding: the pool mixes `dst test` runs with the apply gate's
    `regression` runs; `prev` crossing modes silently shifted the baseline."""
    gate_run = {
        "id": "cccc3333-0000-0000-0000-000000000000",
        "mode": "regression",
        "score": 0.8,
        "passed": 4,
        "failed": 1,
        "errored": 0,
        "started_at": "2026-08-29T11:00:00+00:00",
    }
    latest = {**RUN_B, "started_at": "2026-08-29T12:00:00+00:00"}
    results = {
        RUN_A["id"]: [_result("q1", True), _result("q2", True)],
        RUN_B["id"]: [_result("q1", True), _result("q2", False, "diverged")],
    }
    fake, calls = _fake_get(results, [latest, gate_run, RUN_A])
    monkeypatch.setattr(httpx, "get", fake)
    code = _run_cli(monkeypatch, ["runs", "customer_value", "--diff", "prev", "latest"])
    out = capsys.readouterr().out
    # prev skipped the regression-mode gate run and picked the older test run.
    assert f"A {str(RUN_A['id'])[:8]}" in out
    assert code == 1


def _record_env(tmp_path: object, name: str, token: str) -> None:
    from pathlib import Path

    d = Path(str(tmp_path)) / ".dst"
    d.mkdir(exist_ok=True)
    (d / "envs.json").write_text(f'{{"{name}": "{token}"}}\n', encoding="utf-8")


def test_runs_env_flag_resolves_recorded_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: object
) -> None:
    """--env <name> injects the recorded token — it beats the ambient
    DST_ADMIN_TOKEN, so the command lands in THAT env."""
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    _record_env(tmp_path, "exp1", "dstadm_exp1")
    fake, calls = _fake_get({}, [RUN_B, RUN_A])
    monkeypatch.setattr(httpx, "get", fake)
    assert _run_cli(monkeypatch, ["runs", "customer_value", "--env", "exp1"]) == 0
    assert fake.auths == ["Bearer dstadm_exp1"]  # not the fixture's dstadm_t


def test_runs_env_flag_unknown_name_lists_known(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: object
) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    _record_env(tmp_path, "exp1", "dstadm_exp1")
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, ["runs", "customer_value", "--env", "nope"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no environment named 'nope'" in err
    assert "exp1" in err  # the known names are listed


def test_runs_env_and_token_together_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: object
) -> None:
    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    _record_env(tmp_path, "exp1", "dstadm_exp1")
    with pytest.raises(SystemExit) as exc:
        _run_cli(
            monkeypatch,
            ["runs", "customer_value", "--env", "exp1", "--token", "dstadm_other"],
        )
    assert exc.value.code == 1
    assert "pick one" in capsys.readouterr().err


def test_runs_diff_other_url_requires_other_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, _ = _fake_get({}, [RUN_B, RUN_A])
    monkeypatch.setattr(httpx, "get", fake)
    code = _run_cli(
        monkeypatch,
        ["runs", "customer_value", "--diff", "a", "b", "--other-url", "http://sandbox:8000"],
    )
    assert code == 1
    assert "--other-token" in capsys.readouterr().err
