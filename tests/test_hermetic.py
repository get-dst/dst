"""The suite's verdict must not depend on the machine it runs on.

Three times in two days it did — a ./.env only one checkout had, the `local-embed`
extra in one venv, a /tmp path two runs shared — and every one produced a wrong
diagnosis first. tests/conftest.py closes the three vectors (the environment, the
CWD, installed extras); this file is what fails when one of them reopens.

Every assertion here is deterministic on ANY host: nothing is asserted only when the
ambient state happens to be present, because a guard that fires only on the
maintainer's laptop is the very class of thing being killed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import _CWD, _EXTRA_AMBIENT, _ROOT, _scratch_dsn, _scrub, _setting_env_names

from services.config import Settings, settings
from services.llm import registry

# ── vector 1: the environment ─────────────────────────────────────────────────


def test_the_scrub_covers_every_setting_the_host_could_supply() -> None:
    """The derivation, not a hand-kept list: plant every name Settings answers to
    and require the scrub to take all of them. A field added tomorrow — or an alias
    shape the derivation does not understand — fails here, not on a contributor."""
    env = dict.fromkeys(_setting_env_names(), "planted")
    _scrub(env)
    assert env == {}


def test_the_scrub_covers_the_names_no_setting_claims() -> None:
    """The model-cache knobs steer services/context/local_embedder.py without
    naming a Settings field, so they are listed — and therefore pinned."""
    env = dict.fromkeys(_EXTRA_AMBIENT, "planted")
    env["DST_ADMIN_TOKEN"] = "dstadm_from_the_shell"  # services/cli/main.py
    env["DST_URL"] = "http://someone-elses-server"  # services/mcp/server.py
    _scrub(env)
    assert env == {}


def test_the_scrub_keeps_the_harness_and_the_lanes_a_run_declared() -> None:
    """DST_TEST_* is the harness's own namespace: the opt-in switches that
    DECLARE a live lane, plus that lane's credentials. Nothing else survives."""
    on = {"DST_TEST_DB": "scratch", "DST_TEST_BIGQUERY": "1", "GCP_PROJECT": "sandbox"}
    _scrub(on)
    assert on == {
        "DST_TEST_DB": "scratch",
        "DST_TEST_BIGQUERY": "1",
        "GCP_PROJECT": "sandbox",
    }
    # …and the same credential with the lane switched off is just ambient state.
    off = {"GCP_PROJECT": "sandbox"}
    _scrub(off)
    assert off == {}


def test_no_setting_is_inherited_from_this_host() -> None:
    """The scrub actually ran in THIS process, and only the declared names came
    back. The list is short on purpose: every addition is a new way to differ."""
    declared = {"DATABASE_URL", "DATABASE_ADMIN_URL", "DST_PROVIDERS"}
    declared |= {"PROJECT_FILE", "DUCKDB_JAFFLE_PATH"}
    survivors = dict(os.environ)
    _scrub(survivors)  # the same pass conftest ran at import: it should find nothing
    leaked = sorted(set(os.environ) - set(survivors) - declared)
    assert not leaked, (
        f"host state reached the suite: {leaked}. It was scrubbed at conftest import, "
        "so something put it back mid-run — look for a write to os.environ in the code "
        "under test (a CLI verb adopting a project .env used to be exactly that)"
    )


def test_the_premise_holds_no_provider_is_configured() -> None:
    """ "This install has no provider configured" is what most of the suite is
    written against — the publish gate scores an unservable lens as a WARNING when
    nothing is configured and an ERROR when something is. It cost 40 failures."""
    assert settings.providers == {}
    assert registry.specs() == {}


# ── vector 2: the current working directory ───────────────────────────────────


def test_a_dotenv_beside_the_shell_cannot_reach_the_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pydantic resolves ``env_file=".env"`` against the CWD, so scrubbing the
    environment is not enough — the file is read at construction, after. The suite
    builds its Settings with the dotenv source off; this proves that is load-bearing
    on every machine instead of only where a ./.env happens to exist."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        'DST_PROVIDERS={"acme": {"type": "anthropic", "api_key": "sk-planted"}}\nEDITION=cloud\n',
        encoding="utf-8",
    )
    assert Settings(_env_file=None).edition == "oss"
    assert Settings(_env_file=None).providers == {}
    # The control: that file IS readable from here, so the line above is about the
    # seam and not about a file pydantic never found. (`edition`, not `providers`
    # — the process env wins over a dotenv, and the suite pins DST_PROVIDERS.)
    assert Settings().edition == "cloud"


def test_the_suite_runs_from_an_empty_directory_of_its_own() -> None:
    """`resolve_env_ref`'s default project dir and the CLI's `--dir` default of "."
    read whatever sits next to the shell, and `_adopt_project_env` configures the
    process from it. Run from the repo root, those pick up the repo's own .env."""
    cwd = Path.cwd()
    assert cwd == _CWD
    assert not (cwd / ".env").exists()
    assert not (cwd / "dst.yaml").exists()
    assert cwd != _ROOT


def test_every_path_the_suite_reads_is_absolute() -> None:
    """Both of these default to a relative path — i.e. to wherever pytest was
    launched from. `dst.yaml` is worse than the fixture: a project file sitting
    next to the shell would become the server's provider config."""
    assert Path(settings.duckdb_jaffle_path) == _ROOT / "fixtures" / "jaffle_shop.duckdb"
    assert Path(settings.project_file).is_absolute()
    assert not Path(settings.project_file).exists()  # …and no project file is loaded

    from services.project.source import project_config

    assert project_config() is None


def test_the_scratch_dsn_survives_a_managed_postgres_url() -> None:
    """Parsed, not string-split: `…/dst?sslmode=require` used to become a
    database named `dst?sslmode`, so a developer whose Postgres needs TLS got a
    failure that looked like anything but the DSN."""
    got = _scratch_dsn("postgresql+psycopg://u:p@db.example.com:6543/dst?sslmode=require")
    assert "sslmode=require" in got
    assert "db.example.com:6543" in got
    assert got.rsplit("/", 1)[1].startswith(os.environ.get("DST_TEST_DB", "dst_test"))


# ── vector 3: installed optional extras ───────────────────────────────────────


def test_optional_extras_are_masked_for_a_test_that_declares_none() -> None:
    """`uv sync --extra local-embed` must not change one result. Masking is scoped
    to the optional backends: a core module still probes normally, so this is a
    seam and not a blanket `find_spec -> None`."""
    for module, _extra in registry._EMBED_SDK.values():
        assert registry.find_spec(module) is None, f"{module} leaked into an undeclared test"
    assert registry.find_spec("json") is not None


def test_an_undeclared_test_resolves_no_embedder_at_all() -> None:
    """The mechanism behind instance 2, asserted directly. With fastembed installed
    and no embedding provider declared, the registry falls back to the in-process
    tier — dim 384 against vector(1024) columns, EmbeddingMismatchError, the
    certified answer silently skipped in services/project/apply.py, and a test whose
    precondition simply never held. A test that wants an embedder pins one."""
    assert registry.embedding_identity() is None
    assert registry.resolve_embedder() is None


def test_a_test_may_take_the_probe_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mask must be overridable, not global: test_provider_registry and
    test_embedder_ladder stub find_spec themselves to exercise real resolution."""
    monkeypatch.setattr(registry, "find_spec", lambda name: object())
    assert registry.embedding_identity() is not None


@pytest.mark.extra("local-embed")
def test_a_declared_extra_is_really_there() -> None:
    """The other half of the contract: naming an extra unmasks it AND guarantees it
    is installed, so "extra missing" reports as a skip and never as a pass."""
    assert registry.find_spec("fastembed") is not None
