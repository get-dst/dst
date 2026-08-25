"""A directory with no project must not report success.

`dst plan` and `dst apply` in an empty directory both exit 0 — plan printing
nothing at all, apply printing `[]` — because `_read_project` returns {} and
the server answers "nothing to do" about nothing. A mistyped --dir, or a shell
one directory out of the project, then reads as "it worked": `exit 0` + `[]`
looks like proof a change landed. The silent-empty shape, on two more verbs.

httpx is monkeypatched (the test_cli_dir_env pattern) — no server, no DB. The
tripwire matters as much as the exit code: the refusal happens BEFORE any
request, so a wrong --dir never reaches a server at all.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest


def _run_cli(monkeypatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("DST_URL", raising=False)
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("DST_URL", "http://server:1")  # configured: no provenance note
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _tripwire(monkeypatch) -> None:
    def never(*_a, **_kw):
        raise AssertionError("a request was made from a directory with no project")

    monkeypatch.setattr(httpx, "post", never)


@pytest.mark.parametrize("verb", ["plan", "apply"])
def test_a_directory_with_no_project_is_an_error_not_a_success(
    monkeypatch, isolated, capsys, verb
) -> None:
    _tripwire(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, [verb, "--token", "dstadm_t"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "no dst project" in err
    assert str(isolated.resolve()) in err  # WHICH directory it looked in
    assert "dst init" in err and "--dir" in err  # and the two ways out


@pytest.mark.parametrize("verb", ["plan", "apply"])
def test_a_mistyped_dir_is_refused_before_any_request(monkeypatch, isolated, capsys, verb) -> None:
    """The commonest form: --dir naming a path that does not exist at all."""
    _tripwire(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, [verb, "--dir", str(isolated / "typo"), "--token", "dstadm_t"])
    assert exc.value.code == 1
    assert "no dst project" in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["plan", "apply"])
def test_a_dst_yaml_alone_is_a_project(monkeypatch, isolated, verb) -> None:
    (isolated / "dst.yaml").write_text("name: t\n", encoding="utf-8")
    sent: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        sent["files"] = json["files"]
        return httpx.Response(200, json=[], request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    assert _run_cli(monkeypatch, [verb, "--token", "dstadm_t"]) == 0
    assert list(sent["files"]) == ["dst.yaml"]  # type: ignore[arg-type]


@pytest.mark.parametrize("verb", ["plan", "apply"])
def test_assets_without_a_dst_yaml_push_but_say_so(monkeypatch, isolated, capsys, verb) -> None:
    """`dst export` writes lenses/ and never authors a dst.yaml, so this
    tree IS a project — but connections and providers are not in the push, and
    an omission nobody announced reads as a push that covered them."""
    lens = isolated / "lenses" / "sales"
    lens.mkdir(parents=True)
    (lens / "lens.yaml").write_text("name: sales\n", encoding="utf-8")
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **_kw: httpx.Response(200, json=[], request=httpx.Request("POST", url)),
    )
    assert _run_cli(monkeypatch, [verb, "--token", "dstadm_t"]) == 0
    err = capsys.readouterr().err
    assert "no dst.yaml" in err and "connections and providers are NOT" in err


def test_read_project_is_the_one_definition_of_present(tmp_path: Path) -> None:
    """The parent of a project is not a project: its lenses/ files are one level
    down, so they carry no `lenses/` prefix and nothing is collected — the
    directory a caller lands in one level too high."""
    from services.cli.main import _read_project

    (tmp_path / "proj" / "lenses" / "sales").mkdir(parents=True)
    (tmp_path / "proj" / "lenses" / "sales" / "lens.yaml").write_text("name: s\n", encoding="utf-8")
    (tmp_path / "proj" / "dst.yaml").write_text("name: p\n", encoding="utf-8")
    assert _read_project(tmp_path) == {}
    assert set(_read_project(tmp_path / "proj")) == {"dst.yaml", "lenses/sales/lens.yaml"}
