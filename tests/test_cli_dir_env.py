"""--dir must relocate secret resolution. `dst apply --dir X` run from
outside X (CI, cron, another repo) used to read DST_URL/DST_ADMIN_TOKEN
from the shell cwd's ./.env and authenticate as whatever project the shell sat
in — 401 at best, the wrong org at worst. Precedence: explicit flag > process
env > <dir>/.env > default; the cwd's ./.env is NOT in the path once --dir
names a project (it is another project's secrets file). httpx is monkeypatched
(the test_cli_governance pattern) — no server, no DB."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest


def _run_cli(monkeypatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def _isolate(
    monkeypatch, tmp_path, *, cwd_env: str | None = None, dir_env: str | None = None
) -> Path:
    """A shell cwd and a separate project dir, each optionally holding a .env.
    chdir lands in the cwd; the returned project dir is what --dir points at."""
    monkeypatch.delenv("DST_URL", raising=False)
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    cwd, project = tmp_path / "elsewhere", tmp_path / "project"
    cwd.mkdir()
    project.mkdir()
    # A project is a directory with a dst.yaml — plan/apply refuse one
    # without it now, before resolving anything (tests/test_cli_no_project.py).
    (project / "dst.yaml").write_text("name: p\n", encoding="utf-8")
    if cwd_env is not None:
        (cwd / ".env").write_text(cwd_env, encoding="utf-8")
    if dir_env is not None:
        (project / ".env").write_text(dir_env, encoding="utf-8")
    monkeypatch.chdir(cwd)
    return project


def _capture_post(monkeypatch, seen: dict[str, str]) -> None:
    def fake_post(url, headers=None, json=None, timeout=None, params=None):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return httpx.Response(200, json=[], request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)


@pytest.mark.parametrize("verb", ["plan", "apply"])
def test_out_of_tree_dir_env_beats_cwd_env(monkeypatch, tmp_path, verb) -> None:
    project = _isolate(
        monkeypatch,
        tmp_path,
        cwd_env="DST_URL=http://wrong:1\nDST_ADMIN_TOKEN=dstadm_wrong\n",
        dir_env="DST_URL=http://right:2\nDST_ADMIN_TOKEN=dstadm_right\n",
    )
    seen: dict[str, str] = {}
    _capture_post(monkeypatch, seen)
    assert _run_cli(monkeypatch, [verb, "--dir", str(project)]) == 0
    assert seen["url"].startswith("http://right:2/")
    assert seen["auth"] == "Bearer dstadm_right"


def test_patches_list_out_of_tree_uses_dir_env(monkeypatch, tmp_path) -> None:
    # The second of the two live 401s: `patches` resolving the shell's secrets.
    project = _isolate(
        monkeypatch,
        tmp_path,
        cwd_env="DST_ADMIN_TOKEN=dstadm_wrong\n",
        dir_env="DST_URL=http://right:2\nDST_ADMIN_TOKEN=dstadm_right\n",
    )
    seen: dict[str, str] = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["url"], seen["auth"] = url, headers["Authorization"]
        return httpx.Response(200, json=[], request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    argv = ["patches", "list", "--lens", "sales", "--dir", str(project)]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["url"] == "http://right:2/mgmt/lenses/sales/patches"
    assert seen["auth"] == "Bearer dstadm_right"


def test_explicit_flags_beat_every_env(monkeypatch, tmp_path) -> None:
    project = _isolate(
        monkeypatch,
        tmp_path,
        cwd_env="DST_URL=http://cwd:1\nDST_ADMIN_TOKEN=dstadm_cwd\n",
        dir_env="DST_URL=http://dir:2\nDST_ADMIN_TOKEN=dstadm_dir\n",
    )
    monkeypatch.setenv("DST_URL", "http://proc:3")
    monkeypatch.setenv("DST_ADMIN_TOKEN", "dstadm_proc")
    seen: dict[str, str] = {}
    _capture_post(monkeypatch, seen)
    argv = ["plan", "--dir", str(project), "--url", "http://flag:4", "--token", "dstadm_flag"]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["url"].startswith("http://flag:4/")
    assert seen["auth"] == "Bearer dstadm_flag"


def test_process_env_beats_dir_env(monkeypatch, tmp_path) -> None:
    project = _isolate(
        monkeypatch,
        tmp_path,
        dir_env="DST_URL=http://dir:2\nDST_ADMIN_TOKEN=dstadm_dir\n",
    )
    monkeypatch.setenv("DST_URL", "http://proc:3")
    monkeypatch.setenv("DST_ADMIN_TOKEN", "dstadm_proc")
    seen: dict[str, str] = {}
    _capture_post(monkeypatch, seen)
    assert _run_cli(monkeypatch, ["plan", "--dir", str(project)]) == 0
    assert seen["url"].startswith("http://proc:3/")
    assert seen["auth"] == "Bearer dstadm_proc"


def test_a_key_missing_from_dir_env_does_not_fall_back_to_cwd(monkeypatch, tmp_path) -> None:
    # <dir>/.env defines only the token. The url is NOT borrowed from the cwd's
    # project — an unrelated .env must not steer where this project's apply goes.
    project = _isolate(
        monkeypatch,
        tmp_path,
        cwd_env="DST_URL=http://cwd:1\n",
        dir_env="DST_ADMIN_TOKEN=dstadm_dir\n",
    )
    seen: dict[str, str] = {}
    _capture_post(monkeypatch, seen)
    assert _run_cli(monkeypatch, ["plan", "--dir", str(project)]) == 0
    assert seen["url"].startswith("http://localhost:8000/")
    assert seen["auth"] == "Bearer dstadm_dir"


def test_an_explicit_dir_never_reads_the_cwds_env(monkeypatch, tmp_path, capsys) -> None:
    """The leak itself, pinned: the shell's ./.env holds a token, the named
    project holds none. Borrowing it authenticates project A's apply as project
    B — so the command must refuse instead, naming bootstrap."""
    project = _isolate(
        monkeypatch,
        tmp_path,
        cwd_env="DST_URL=http://other-project:1\nDST_ADMIN_TOKEN=dstadm_other\n",
    )

    def never(*_a, **_kw):  # a tripwire: reaching httpx at all means the leak is back
        raise AssertionError("a request was made with the cwd project's credentials")

    monkeypatch.setattr(httpx, "post", never)
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, ["plan", "--dir", str(project)])
    assert exc.value.code == 1
    assert "no admin token" in capsys.readouterr().err


def test_default_url_when_nothing_resolves(monkeypatch, tmp_path) -> None:
    project = _isolate(monkeypatch, tmp_path)  # no .env anywhere
    seen: dict[str, str] = {}
    _capture_post(monkeypatch, seen)
    argv = ["plan", "--dir", str(project), "--token", "dstadm_flag"]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["url"] == "http://localhost:8000/mgmt/project/plan"


# ── which server did this command target, and why ─────────────────────────────
# The complement of the leak fix above. That one stopped --dir verbs falling
# through to the cwd's .env for SECRETS; the URL half of the same fall-through
# was never loud: an agent that read DST_URL from its project's .env and ran
# one command a directory out silently got http://localhost:8000 instead — a
# different server, no indication, and the traceback that followed named neither
# the URL nor where it came from. The rule: --url > DST_URL in the process
# env > DST_URL in <dir>/.env > the built-in default; the first three are the
# user's own visible configuration and stay silent, the fourth is a guess and a
# guess is never silent. Every failure to connect names the URL AND its source.


def test_the_built_in_default_announces_itself(monkeypatch, tmp_path, capsys) -> None:
    project = _isolate(monkeypatch, tmp_path)  # no .env anywhere
    _capture_post(monkeypatch, {})
    assert _run_cli(monkeypatch, ["plan", "--dir", str(project), "--token", "dstadm_t"]) == 0
    note = capsys.readouterr().err
    assert "targeting http://localhost:8000" in note
    assert "the built-in default" in note
    assert str((project / ".env").resolve()) in note and "does not exist" in note


def test_a_dot_env_that_defines_no_url_is_named_as_such(monkeypatch, tmp_path, capsys) -> None:
    """The .env exists and simply says nothing about the URL — a different
    mistake from being in the wrong directory, and the note distinguishes them."""
    project = _isolate(monkeypatch, tmp_path, dir_env="DST_ADMIN_TOKEN=dstadm_dir\n")
    _capture_post(monkeypatch, {})
    assert _run_cli(monkeypatch, ["plan", "--dir", str(project)]) == 0
    assert "defines none" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("kwargs", "argv_extra", "env", "expected"),
    [
        ({}, ["--url", "http://flag:4"], None, "--url"),
        ({}, [], "http://proc:3", "DST_URL in the environment"),
        ({"dir_env": "DST_URL=http://dir:2\n"}, [], None, "DST_URL in "),
    ],
)
def test_configured_urls_are_silent_but_named_when_they_fail(
    monkeypatch, tmp_path, capsys, kwargs, argv_extra, env, expected
) -> None:
    project = _isolate(monkeypatch, tmp_path, **kwargs)
    if env:
        monkeypatch.setenv("DST_URL", env)
    seen: dict[str, str] = {}
    _capture_post(monkeypatch, seen)
    argv = ["plan", "--dir", str(project), "--token", "dstadm_t", *argv_extra]
    assert _run_cli(monkeypatch, argv) == 0
    assert capsys.readouterr().err == ""  # visible configuration says nothing

    def refuse(url, **_kw):
        raise httpx.ConnectError(
            "[Errno 61] Connection refused", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", refuse)
    assert _run_cli(monkeypatch, argv) == 1
    err = capsys.readouterr().err
    assert f"That URL came from {expected}" in err
    assert len(err.strip().splitlines()) == 1


def test_the_unreachable_default_says_where_it_came_from(monkeypatch, tmp_path, capsys) -> None:
    """End to end: a directory out of the project, no DST_URL anywhere,
    connection refused. Both lines carry the provenance a raw traceback
    would not."""
    project = _isolate(monkeypatch, tmp_path)

    def refuse(url, **_kw):
        raise httpx.ConnectError(
            "[Errno 61] Connection refused", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", refuse)
    assert _run_cli(monkeypatch, ["plan", "--dir", str(project), "--token", "dstadm_t"]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "could not reach a dst server at http://localhost:8000" in err
    assert "That URL came from the built-in default" in err
    assert str((project / ".env").resolve()) in err


# ── the in-process half — `dst test` had no --dir at all ──────────────────
# The one gate-verification command was the least invocable one: it reads
# DATABASE_URL and the provider keys through the settings singleton, which
# pydantic loads from the SHELL's cwd, so it could only ever sweep the project
# the terminal happened to sit in.


@pytest.fixture()
def sandbox_env(monkeypatch):
    """What the verb could actually SEE, captured while it ran.

    These assertions used to read os.environ afterwards, because
    `_adopt_project_env` seeded it with `os.environ.setdefault` and never unwound
    it — which is precisely the state pollution that got removed: nothing is
    written to the environment now, and the adoption is undone when the
    invocation ends. So the observation moves inside the verb. `dst test`
    probes the registry for a smart model before it touches the database; that
    probe is the hook, and returning None is the exit-1 path it already took."""
    seen: dict[str, object] = {}

    def _probe(_ref):
        from services.config import resolve_env_ref, settings

        seen.update({name: resolve_env_ref(name) for name in _PROBE_NAMES})
        seen["edition"] = settings.edition  # a Settings FIELD, not a declared env ref
        return None

    monkeypatch.setattr("services.llm.registry.resolve", _probe)
    return seen


_PROBE_NAMES = (
    "DST_PROBE_KEY",
    "DST_PROBE_DIR_ONLY",
    "DST_PROBE_CWD_ONLY",
    "DST_PROBE_PROC",
)


def test_test_verb_adopts_the_dir_projects_env(monkeypatch, tmp_path, sandbox_env) -> None:
    project = _isolate(
        monkeypatch,
        tmp_path,
        cwd_env="DST_PROBE_KEY=from_cwd\n",
        dir_env="DST_PROBE_KEY=from_dir\nDST_PROBE_DIR_ONLY=yes\nDST_EDITION=cloud\n",
    )
    # Exit 1 is the no-smart-model path — reached only after the env is adopted.
    assert _run_cli(monkeypatch, ["test", "--dir", str(project)]) == 1
    assert sandbox_env["DST_PROBE_KEY"] == "from_dir"
    assert sandbox_env["DST_PROBE_DIR_ONLY"] == "yes"
    # …and the settings singleton the verb sweeps through, not just the declared
    # env refs: that is the half `dst test` reads DATABASE_URL through.
    assert sandbox_env["edition"] == "cloud"


def test_test_verb_env_precedence_matches_http(monkeypatch, tmp_path, sandbox_env) -> None:
    project = _isolate(
        monkeypatch,
        tmp_path,
        cwd_env="DST_PROBE_KEY=from_cwd\nDST_PROBE_CWD_ONLY=yes\n",
        dir_env="DST_PROBE_KEY=from_dir\nDST_PROBE_PROC=from_dir\n",
    )
    monkeypatch.setenv("DST_PROBE_PROC", "from_proc")
    assert _run_cli(monkeypatch, ["test", "--dir", str(project)]) == 1
    assert sandbox_env["DST_PROBE_PROC"] == "from_proc"  # process env still wins
    assert sandbox_env["DST_PROBE_KEY"] == "from_dir"  # <dir>/.env beats cwd
    # …and the cwd project's own keys are never adopted: `dst test --dir X`
    # would otherwise sweep X's corpus with the shell project's DATABASE_URL.
    assert sandbox_env["DST_PROBE_CWD_ONLY"] is None


def test_a_dir_verb_leaves_the_process_environment_alone(monkeypatch, tmp_path, sandbox_env):
    """The defect itself: `os.environ.setdefault` from a project's .env, never
    unwound. Invisible in a one-shot CLI; in anything longer-lived it handed that
    project's secrets to every later caller — eleven test files did exactly that,
    leaking the repo's real .env into the rest of the session."""
    import os

    project = _isolate(
        monkeypatch,
        tmp_path,
        dir_env="DST_PROBE_DIR_ONLY=yes\nDST_SECRET_KEY=sk-from-the-project\n",
    )
    before = dict(os.environ)
    assert _run_cli(monkeypatch, ["test", "--dir", str(project)]) == 1
    assert sandbox_env["DST_PROBE_DIR_ONLY"] == "yes"  # the verb DID see the project
    assert dict(os.environ) == before  # …and the environment never learned of it


def test_the_adoption_lasts_exactly_one_invocation(monkeypatch, tmp_path, sandbox_env):
    """The restore, at the dispatch: whatever a --dir verb reconfigured is put back
    when it returns. A process that runs two verbs — or a library caller, or the
    suite — must not inherit the first one's project."""
    from services.config import resolve_env_ref, settings

    project = _isolate(monkeypatch, tmp_path, dir_env="DST_PROBE_DIR_ONLY=yes\nDST_EDITION=cloud\n")
    edition_before = settings.edition
    assert _run_cli(monkeypatch, ["test", "--dir", str(project)]) == 1
    assert sandbox_env["edition"] == "cloud"  # it really was adopted…
    assert resolve_env_ref("DST_PROBE_DIR_ONLY") is None  # …and really is gone
    assert settings.edition == edition_before


def test_test_verb_never_pretends_url_and_token_do_something(
    monkeypatch, tmp_path, sandbox_env, capsys
) -> None:
    """The trio is accepted (the `bootstrap --url` precedent) but `test` talks
    to a database, not a server — a --token naming prod while DATABASE_URL
    names localhost must say so out loud."""
    project = _isolate(monkeypatch, tmp_path)
    argv = ["test", "--dir", str(project), "--url", "https://prod:8000", "--token", "dstadm_prod"]
    assert _run_cli(monkeypatch, argv) == 1
    err = capsys.readouterr().err
    assert "--url/--token are ignored" in err
    assert "DATABASE_URL" in err
