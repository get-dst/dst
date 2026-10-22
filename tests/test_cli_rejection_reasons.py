"""A rejection must carry its reason on the default report, and `--lens` must
parse on both push verbs.

`dst apply`'s summarized report printed a rejected lens's errors under its row,
mid-stream; on an apply over many lenses that row scrolled past and the counts
line said only "1 rejected", so the reason needed `--quiet` or `--json`. The
report now repeats every refusal with its reasons right above the counts line.
`dst plan`'s summary names the compile error of an invalid lens even when the
lens is also stale, instead of the stale state alone. httpx is monkeypatched
(the test_cli_apply_legibility pattern): no server, no DB.
"""

from __future__ import annotations

import argparse
import sys

import httpx
import pytest

from services.cli import main as cli


def _run_cli(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return cli.main()


@pytest.fixture()
def project(monkeypatch, tmp_path):
    monkeypatch.delenv("DST_URL", raising=False)
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    (tmp_path / "dst.yaml").write_text("name: bakery\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _answer(monkeypatch, rows: list[dict[str, object]]) -> None:
    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        return httpx.Response(200, json=rows, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)


_REASONS = [
    "eval gate blocked publish: 2 case(s) failing (loaves per route, flour cost by mill)",
    "metric 'crates_shipped' reads column 'crate_qty', which table 'deliveries' lacks",
]


def _noisy_apply() -> list[dict[str, object]]:
    """Many published lenses, each with its own warning, and one rejection in
    the middle — the report a reader skims to the counts line."""
    rows: list[dict[str, object]] = [
        {
            "lens": f"route_{n:02d}",
            "action": "updated",
            "version": 3,
            "errors": [],
            "warnings": [f"route_{n:02d}: column 'tray_note' has no description"],
            "applied": [],
        }
        for n in range(30)
    ]
    rows.insert(
        12,
        {
            "lens": "wholesale_orders",
            "action": "rejected",
            "version": None,
            "errors": list(_REASONS),
            "warnings": [],
            "applied": [],
            "gate": "blocked",
        },
    )
    rows.append({"lens": "oven_schedule", "action": "rejected-recompile", "errors": []})
    rows.append({"scope": "apply", "action": "aborted", "detail": "nothing deployed"})
    return rows


def test_default_apply_report_repeats_each_rejection_with_its_reasons(
    monkeypatch, project, capsys
) -> None:
    _answer(monkeypatch, _noisy_apply())
    assert _run_cli(monkeypatch, ["apply", "--dir", str(project), "--token", "dstadm_t"]) == 1
    out = capsys.readouterr().out
    tail = out[out.rindex("rejected:") :]
    counts = tail.index("Apply finished.")
    recap = tail[:counts]
    assert "lens wholesale_orders: rejected" in recap
    for reason in _REASONS:
        assert f"    - {reason}" in recap
    # a rejection whose row carries no reason still says where it is
    assert "lens oven_schedule: rejected-recompile" in recap
    assert "--json shows it whole" in recap
    assert "2 rejected" in tail[counts:]


def test_the_recap_carries_the_same_reasons_as_json(monkeypatch, project, capsys) -> None:
    import json

    _answer(monkeypatch, _noisy_apply())
    _run_cli(monkeypatch, ["apply", "--json", "--dir", str(project), "--token", "dstadm_t"])
    rows = json.loads(capsys.readouterr().out)
    from_json = next(r for r in rows if r.get("lens") == "wholesale_orders")["errors"]
    _run_cli(monkeypatch, ["apply", "--dir", str(project), "--token", "dstadm_t"])
    out = capsys.readouterr().out
    recap = out[out.rindex("rejected:") : out.index("Apply finished.")]
    assert [line.removeprefix("    - ") for line in recap.splitlines() if "    - " in line][
        : len(from_json)
    ] == from_json


def test_a_clean_apply_prints_no_recap(monkeypatch, project, capsys) -> None:
    _answer(monkeypatch, [{"lens": "route_01", "action": "updated", "version": 2}])
    assert _run_cli(monkeypatch, ["apply", "--dir", str(project), "--token", "dstadm_t"]) == 0
    assert "rejected:" not in capsys.readouterr().out


def test_quiet_keeps_its_one_listing(monkeypatch, project, capsys) -> None:
    _answer(monkeypatch, _noisy_apply())
    _run_cli(monkeypatch, ["apply", "--quiet", "--dir", str(project), "--token", "dstadm_t"])
    out = capsys.readouterr().out
    assert out.count(_REASONS[0]) == 1  # --quiet already prints the row and its reasons


def test_plan_names_the_compile_error_of_a_stale_invalid_lens(monkeypatch, project, capsys) -> None:
    error = "entity 'deliveries': column 'crate_qty' is not on table 'deliveries'"
    _answer(
        monkeypatch,
        [
            {
                "lens": "wholesale_orders",
                "status": "invalid",
                "error": error,
                "errors": [error, "definition 'crates': reads a metric that no longer exists"],
                "stale": ["entity/deliveries"],
                "note": "will recompile on apply",
                "diffs": [],
            },
            {
                "lens": "oven_schedule",
                "status": "stale",
                "stale": ["entity/deliveries"],
                "errors": [],
                "diffs": [],
            },
        ],
    )
    assert _run_cli(monkeypatch, ["plan", "--dir", str(project), "--token", "dstadm_t"]) == 1
    out = capsys.readouterr().out
    head = next(line for line in out.splitlines() if "wholesale_orders" in line)
    assert error in head and "STALE compile (shared changed: entity/deliveries)" in head
    assert "definition 'crates': reads a metric that no longer exists" in out
    # a stale lens that compiles is still a stale row, not an invalid one
    assert "oven_schedule — STALE compile" in out
    assert "1 invalid" in out and "1 stale" in out


def test_plan_counts_a_lens_row_with_errors_as_invalid(monkeypatch, project, capsys) -> None:
    """A row that carries errors under any status would be rejected by apply:
    the summary prints them and the exit code says so."""
    _answer(
        monkeypatch,
        [{"lens": "route_01", "status": "stale", "errors": ["route_01: bad grain"], "diffs": []}],
    )
    assert _run_cli(monkeypatch, ["plan", "--dir", str(project), "--token", "dstadm_t"]) == 1
    assert "route_01: bad grain" in capsys.readouterr().out


# ── --lens parses on both push verbs, through the real parser ────────────────


def _parsed(monkeypatch, argv: list[str]) -> argparse.Namespace:
    seen: dict[str, argparse.Namespace] = {}

    def capture(args: argparse.Namespace) -> int:
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "_apply", capture)
    monkeypatch.setattr(cli, "_plan", capture)
    assert _run_cli(monkeypatch, argv) == 0
    return seen["args"]


def test_apply_takes_lens_repeated(monkeypatch) -> None:
    args = _parsed(monkeypatch, ["apply", "--lens", "x", "--lens", "y"])
    assert args.lens == ["x", "y"]
    assert cli._selectors(args) == ["lens/x", "lens/y"]


def test_plan_takes_lens(monkeypatch) -> None:
    args = _parsed(monkeypatch, ["plan", "--lens", "x"])
    assert cli._selectors(args) == ["lens/x"]


def test_lens_and_select_combine(monkeypatch) -> None:
    args = _parsed(monkeypatch, ["apply", "--select", "entity/loaves+", "--lens", "x"])
    assert cli._selectors(args) == ["entity/loaves+", "lens/x"]


@pytest.mark.parametrize("verb", ["apply", "plan"])
def test_help_lists_lens_and_select(monkeypatch, capsys, verb) -> None:
    monkeypatch.setattr(sys, "argv", ["dst", verb, "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    out = capsys.readouterr().out
    assert "--lens NAME" in out and "--select SELECTOR" in out
