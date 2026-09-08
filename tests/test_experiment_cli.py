"""`dst experiment` — N variants, measured, side by side.

The orchestrator composes product verbs via the `_dst_run` seam; these tests
fake that seam and pin the contract: variant expansion, lens.yaml patching on
a COPY only, verb order, the env removed even when a variant fails, --keep.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_parse_vary_yaml_scalars_and_errors() -> None:
    from services.cli.main import _parse_vary

    dims = _parse_vary(["model.temperature=0.0,0.7", "answer_mode=strict,balanced"])
    assert dims == [
        ("model.temperature", [0.0, 0.7]),
        ("answer_mode", ["strict", "balanced"]),
    ]
    for bad in ["model.temperature", "k=", "k=onlyone"]:
        with pytest.raises(ValueError):
            _parse_vary([bad])


def test_expand_variants_is_cartesian_in_order() -> None:
    from services.cli.main import _expand_variants

    variants = _expand_variants([("a", [1, 2]), ("b", ["x", "y"])])
    assert variants == [
        {"a": 1, "b": "x"},
        {"a": 1, "b": "y"},
        {"a": 2, "b": "x"},
        {"a": 2, "b": "y"},
    ]


def test_patch_lens_yaml_sets_nested_keys(tmp_path: Path) -> None:
    from services.cli.main import _patch_lens_yaml

    p = tmp_path / "lens.yaml"
    p.write_text("name: l\nmodel:\n  temperature: 0.0\n", encoding="utf-8")
    _patch_lens_yaml(p, {"model.temperature": 0.7, "answer_mode": "strict"})
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert doc == {"name": "l", "model": {"temperature": 0.7}, "answer_mode": "strict"}


# ── orchestration ────────────────────────────────────────────────────────────

RUNS_V1 = [{"id": "run-v1", "mode": "test", "score": 1.0, "passed": 5, "failed": 0}]
RUNS_V2 = [{"id": "run-v2", "mode": "test", "score": 0.8, "passed": 4, "failed": 1}]


class FakeVerbs:
    """Canned outcomes per verb; records every invocation. `env new` records
    the map file exactly like the real verb — the orchestrator reads it back."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.test_round = 0

    def __call__(self, argv: list[str], cwd: str) -> tuple[int, str]:
        self.calls.append(argv)
        verb = argv[0]
        if verb == "env":
            if argv[1] == "new":
                d = Path(cwd) / ".dst"
                d.mkdir(exist_ok=True)
                (d / "envs.json").write_text(
                    json.dumps({argv[2]: "dstadm_exp_tok"}), encoding="utf-8"
                )
            return 0, "env: recorded"
        if verb == "apply":
            # The patched copy, never the project root, is what applies.
            workdir = argv[argv.index("--dir") + 1]
            doc = yaml.safe_load(
                (Path(workdir) / "lenses" / "l1" / "lens.yaml").read_text(encoding="utf-8")
            )
            self.calls[-1] = [*argv, f"seen-temp={doc['model']['temperature']}"]
            return 0, "Apply complete."
        if verb == "test":
            self.test_round += 1
            return (0 if self.test_round == 1 else 1), "suite ran"
        if verb == "runs" and "--diff" in argv:
            return 1, json.dumps({"regressed": True, "newly_failing": ["q2"], "newly_passing": []})
        if verb == "runs":
            return 0, json.dumps(RUNS_V1 if self.test_round == 1 else RUNS_V2)
        raise AssertionError(f"unexpected verb {argv}")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    lens = tmp_path / "lenses" / "l1"
    lens.mkdir(parents=True)
    (lens / "lens.yaml").write_text("name: l1\nmodel:\n  temperature: 0.0\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return tmp_path


def test_experiment_measures_variants_and_removes_env(
    monkeypatch: pytest.MonkeyPatch, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.cli import main as cli_main

    fake = FakeVerbs()
    monkeypatch.setattr(cli_main, "_dst_run", fake)
    code = _run_cli(
        monkeypatch,
        [
            "experiment",
            "l1",
            "--vary",
            "model.temperature=0.0,0.7",
            "--dir",
            str(project),
        ],
    )
    out = capsys.readouterr().out
    assert code == 0
    # Verb order: env new, then per variant apply+test+runs, one diff, env rm.
    verbs = [c[0] + ("/" + c[1] if c[0] == "env" else "") for c in fake.calls]
    assert verbs == [
        "env/new",
        "apply",
        "test",
        "runs",
        "apply",
        "test",
        "runs",
        "runs",  # the diff
        "env/rm",
    ]
    # Each apply saw ITS variant's patched copy — the project itself untouched.
    applies = [c for c in fake.calls if c[0] == "apply"]
    assert applies[0][-1] == "seen-temp=0.0"
    assert applies[1][-1] == "seen-temp=0.7"
    # apply's --dir is the COPY, where no .env or envs map exists — the
    # orchestrator must hand it BOTH the resolved token and the resolved URL,
    # never --env and never the built-in default (both found on live laps:
    # the URL fallback aimed an apply at whatever owned localhost:8000).
    for c in applies:
        assert "--env" not in c
        assert c[c.index("--token") + 1] == "dstadm_exp_tok"
        assert "--url" in c
    assert (
        yaml.safe_load((project / "lenses" / "l1" / "lens.yaml").read_text(encoding="utf-8"))[
            "model"
        ]["temperature"]
        == 0.0
    )
    assert "baseline" in out
    assert "1 newly failing" in out
    assert "newly failing: q2" in out


def test_experiment_removes_env_even_when_a_variant_breaks(
    monkeypatch: pytest.MonkeyPatch, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.cli import main as cli_main

    calls: list[list[str]] = []

    def broken(argv: list[str], cwd: str) -> tuple[int, str]:
        calls.append(argv)
        if argv[0] == "env":
            if argv[1] == "new":
                d = Path(cwd) / ".dst"
                d.mkdir(exist_ok=True)
                (d / "envs.json").write_text(json.dumps({argv[2]: "dstadm_t"}), encoding="utf-8")
            return 0, "ok"
        return 2, "boom: gate exploded"

    monkeypatch.setattr(cli_main, "_dst_run", broken)
    code = _run_cli(
        monkeypatch,
        ["experiment", "l1", "--vary", "model.temperature=0.0,0.7", "--dir", str(project)],
    )
    assert code == 1
    assert ["env", "rm", calls[0][2], "--yes"] == calls[-1]
    assert "apply-failed" in capsys.readouterr().out


def test_experiment_keep_skips_removal(
    monkeypatch: pytest.MonkeyPatch, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.cli import main as cli_main

    fake = FakeVerbs()
    monkeypatch.setattr(cli_main, "_dst_run", fake)
    code = _run_cli(
        monkeypatch,
        [
            "experiment",
            "l1",
            "--vary",
            "model.temperature=0.0,0.7",
            "--dir",
            str(project),
            "--keep",
        ],
    )
    assert code == 0
    assert all(not (c[0] == "env" and c[1] == "rm") for c in fake.calls)
    assert "env kept for inspection" in capsys.readouterr().out


def test_experiment_caps_the_cartesian(
    monkeypatch: pytest.MonkeyPatch, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_cli(
        monkeypatch,
        [
            "experiment",
            "l1",
            "--vary",
            "a=1,2,3",
            "--vary",
            "b=1,2,3",
            "--dir",
            str(project),
        ],
    )
    assert code == 1
    assert "cap is 8" in capsys.readouterr().err
