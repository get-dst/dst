"""`dst export --out` writes the lens tree THERE, not over the project.

When --out is osi-only — accepted with --lens and silently ignored — the flag
that means "write output here" rewrites the project's hand-authored lens files
in place. These pin the redirect, the refusal to stub
empty list files into being, and the absolute-path summary."""

from __future__ import annotations

import httpx
import pytest


def _run_cli(monkeypatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


_FILES = {
    "lenses/account_360/lens.yaml": "name: account_360\n",
    "lenses/account_360/certified_answers.yaml": "[]\n",
    "lenses/account_360/evals/cases.yaml": "[]\n",
}


def _fake_export(monkeypatch) -> None:
    def fake_get(url, headers=None, params=None, timeout=None):
        assert url.endswith("/mgmt/project/export")
        return httpx.Response(200, json={"files": dict(_FILES)}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "lenses" / "account_360").mkdir(parents=True)
    (proj / "lenses" / "account_360" / "lens.yaml").write_text(
        "name: account_360\n# hand-authored comment\n", encoding="utf-8"
    )
    monkeypatch.chdir(proj)
    return proj


def test_export_out_redirects_the_lens_tree(project, tmp_path, monkeypatch, capsys) -> None:
    _fake_export(monkeypatch)
    dest = tmp_path / "scratch"
    argv = ["export", "--lens", "account_360", "--out", str(dest), "--yes", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    # The tree landed under --out …
    assert (dest / "lenses/account_360/lens.yaml").read_text() == "name: account_360\n"
    # … and the project's hand-authored file was not touched.
    assert "# hand-authored comment" in (project / "lenses/account_360/lens.yaml").read_text()
    # The summary names the real destination, absolutely.
    assert str(dest.resolve()) in capsys.readouterr().out


def test_export_never_stubs_empty_list_files(project, tmp_path, monkeypatch) -> None:
    # A `[]` certified_answers.yaml that export creates would delete server-side
    # answers on the next apply (files win) — omit, never stub.
    _fake_export(monkeypatch)
    dest = tmp_path / "scratch"
    argv = ["export", "--lens", "account_360", "--out", str(dest), "--yes", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert not (dest / "lenses/account_360/certified_answers.yaml").exists()
    assert not (dest / "lenses/account_360/evals/cases.yaml").exists()


def test_export_still_rewrites_an_existing_empty_list_file(project, monkeypatch) -> None:
    # Only file CREATION is suppressed: a file the project already authors keeps
    # round-tripping to server state.
    _fake_export(monkeypatch)
    existing = project / "lenses/account_360/certified_answers.yaml"
    existing.write_text("[]  # authored empty\n", encoding="utf-8")
    argv = ["export", "--lens", "account_360", "--yes", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert existing.read_text() == "[]\n"
