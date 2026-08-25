"""Shared fixtures — and the seam that makes the host a DECLARED input.

Everything above the fixtures exists because the suite's verdict must not depend
on the machine it runs on. Three things silently change it otherwise: a ``./.env``
present in one checkout, an optional extra installed in one venv (empty providers
does not mean "no embedder"), and two runs sharing one /tmp clone path. Each
produces failures the code did not cause, and so a wrong diagnosis first.

So: the suite declares its inputs here, and everything else about the host is off.
Three vectors, three closures — the environment (scrubbed, derived from ``Settings``
so a field added tomorrow is covered tomorrow), the CWD (``./.env`` and every
relative path pinned to the repo root), and installed optional extras (masked unless
a test says ``@pytest.mark.extra(...)``). ``tests/test_hermetic.py`` pins all three.

The MCP streamable-HTTP session manager (started in the app lifespan) can only be
``run()`` once per process, so any test that needs a *live* app — the `/mcp` transport
reaching JSON-RPC, or `/ready`'s MCP liveness probe passing — must share a single
lifespan. This session-scoped client is that one instance; tests that only need request
routing can still construct their own ``TestClient(app)`` without a lifespan.
"""

from __future__ import annotations

import os as _os
import re as _re
import tempfile as _tempfile
from pathlib import Path as _Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import MutableMapping

_ROOT = _Path(__file__).resolve().parents[1]

# ── the scratch database ──────────────────────────────────────────────────────
# Tests run against a scratch DB, never the dev database (test orgs were leaking
# into it). The default is PER TREE: concurrent worktrees sit at different
# migration heads, and one sharing scratch DB upgraded past another branch's head
# bricks that branch's runs — the same shape as the /tmp clone path two `make
# ci-clean` runs shared. DST_TEST_DB still names it explicitly.
_TEST_DB = _os.environ.get("DST_TEST_DB") or (
    "dst_test_" + _re.sub(r"\W", "_", _ROOT.name).lower()[:40]
)
# Where that database lives IS a declared input — there is no other way to reach
# the developer's Postgres — so these two are read before the scrub below.
_ORIG_ADMIN = _os.environ.get(
    "DATABASE_ADMIN_URL", "postgresql+psycopg://dst:dst_dev@localhost:5432/dst"
)
_ORIG_APP = _os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://dst_app:dst_app_dev@localhost:5432/dst"
)


def _scratch_dsn(url: str) -> str:
    """*url* pointed at the scratch database, everything else intact. Parsed, not
    ``rsplit("/", 1)``-ed: a managed Postgres hands you ``…/dst?sslmode=require``
    and string surgery turns that into a database literally named ``dst?sslmode``."""
    from sqlalchemy.engine import make_url

    return make_url(url).set(database=_TEST_DB).render_as_string(hide_password=False)


# ── the environment ───────────────────────────────────────────────────────────
# dst's Settings answer to DST_<NAME> *and*, for compatibility, to the bare
# <NAME> they used to carry — so both shapes steer the app and both are removed here:
# a shell with ENVIRONMENT=production crashes the suite at import (the production
# contract), EDITION=cloud fails test_health, DST_PROVIDERS flips the publish gate
# from WARNING to ERROR. The handful the suite needs is set back explicitly.

# Names no Settings field claims but which still steer the code.
_EXTRA_AMBIENT = frozenset(
    {
        "FASTEMBED_CACHE_PATH",  # services/context/local_embedder.py:43 — weights cache
        "XDG_CACHE_HOME",  # …:45 — the base under it
        "HF_HOME",  # fastembed downloads through huggingface_hub
        "HF_HUB_CACHE",
        "HF_HUB_OFFLINE",
        "TOKENIZERS_PARALLELISM",
    }
)

# The live-integration lanes are themselves a declaration: DST_TEST_<X>=1 says
# "I am supplying <X>'s credentials on purpose". Only then do that lane's vars
# survive — and only this lane overlaps Settings at all (GITHUB_TOKEN, PG*,
# MYSQL_*, SNOWFLAKE_* name no field and live outside the DST_ namespace,
# so the scrub never reaches them).
_LIVE_LANES = {
    "DST_TEST_BIGQUERY": (
        "DST_GCP_CREDENTIALS",
        "DST_GCP_PROJECT",
        "DST_BIGQUERY_DATASET",
        # …and the legacy spellings, so a lane declared before the rename still runs.
        "GCP_CREDENTIALS",
        "GCP_PROJECT",
        "BIGQUERY_DATASET",
    ),
}


def _setting_env_names() -> frozenset[str]:
    """Every env var pydantic-settings would read into ``Settings`` — DERIVED from
    the model rather than listed by hand, so a field added tomorrow is covered
    tomorrow rather than the first time it leaks in."""
    from pydantic import AliasChoices

    from services.config import Settings

    names: set[str] = set()
    for field, info in Settings.model_fields.items():
        names.add(field.upper())
        alias = info.validation_alias
        if isinstance(alias, AliasChoices):
            names.update(str(choice).upper() for choice in alias.choices)
        elif alias is not None:
            names.add(str(alias).upper())
    return frozenset(names)


def _scrub(env: MutableMapping[str, str]) -> None:
    """Drop every host-supplied name that can steer dst.

    Kept: ``DST_TEST_*`` (the harness's own namespace — the opt-in switches
    that declare a live lane) and the credentials of any lane switched on."""
    keep = {name for lane, names in _LIVE_LANES.items() if env.get(lane) for name in names}
    settings_names = _setting_env_names()
    for name in list(env):
        upper = name.upper()
        if upper in keep or upper.startswith("DST_TEST_"):
            continue
        if upper in settings_names or upper in _EXTRA_AMBIENT or upper.startswith("DST_"):
            del env[name]


_scrub(_os.environ)

# ── …and back on, declared ────────────────────────────────────────────────────
_os.environ["DATABASE_ADMIN_URL"] = _scratch_dsn(_ORIG_ADMIN)
_os.environ["DATABASE_URL"] = _scratch_dsn(_ORIG_APP)
# "This install has no provider configured" is the suite's PREMISE. Empty is a
# supported value (the providers validator reads "" as unset); a test that wants
# providers declares them itself — monkeypatch, or a dst.yaml it owns.
_os.environ["DST_PROVIDERS"] = ""
# Both of these default to a RELATIVE path, i.e. to whatever directory pytest was
# launched from. Absolute, so the suite reads the same bytes from any CWD — and so
# a dst.yaml sitting next to the shell can never become the server's config.
# Declared in their LEGACY spelling on purpose: every run then exercises the
# compatibility aliases that a `pip install --upgrade` depends on, and the day those
# are dropped this line fails instead of a deployment.
_os.environ["PROJECT_FILE"] = str(_ROOT / "tests" / "no-such-dst.yaml")
_os.environ["DUCKDB_JAFFLE_PATH"] = str(_ROOT / "fixtures" / "jaffle_shop.duckdb")

# ── the working directory ─────────────────────────────────────────────────────
# Two readers still resolve against the CWD no matter what the settings say:
# `resolve_env_ref`'s default project dir and the CLI's `--dir` default of ".", whose
# `_adopt_project_env` reconfigures this process from a project's .env. Eleven test
# files drive CLI verbs without chdir'ing, so in a checkout that has one they would
# adopt the repo's OWN .env — real secrets, a different code path, and a verdict
# that differs between checkouts. So the suite RUNS from an empty directory of its
# own (below, once collection is done — chdir'ing at import time moves the ground
# under pytest's own relative `testpaths` and silently changes which files get
# collected). Per tree, because two runs sharing one scratch path report each
# other's failures.
_CWD = (_Path(_tempfile.gettempdir()) / f"dst-test-cwd-{_TEST_DB}").resolve()

# `services.config` was imported above (deriving the names needs the model), so its
# singleton already exists — built from the dirty environment AND from ./.env, which
# pydantic resolves against the CWD and no amount of env scrubbing can undo. Rebuild
# it in place, dotenv source off: every module holds a reference to this one object.
from services.config import Settings as _Settings  # noqa: E402
from services.config import settings as _settings  # noqa: E402

_settings.__dict__.update(_Settings(_env_file=None).__dict__)


def _ensure_test_db() -> None:
    from alembic import command as _cmd
    from alembic.config import Config as _Cfg
    from sqlalchemy import create_engine as _ce
    from sqlalchemy import text as _t

    eng = _ce(_ORIG_ADMIN, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        if not c.execute(
            _t("SELECT 1 FROM pg_database WHERE datname = :db"), {"db": _TEST_DB}
        ).first():
            c.execute(_t(f'CREATE DATABASE "{_TEST_DB}"'))
    eng.dispose()
    cfg = _Cfg(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    _cmd.upgrade(cfg, "head")
    # What `dst migrate` does after upgrading: give the app role LOGIN and the
    # password DATABASE_URL declares. Migrations alone leave the role unusable on
    # a fresh cluster (0001 creates it passwordless) and 0059 clears the
    # historical default — roles are cluster-global, so without this the suite's
    # own app engine could not log in.
    from services.db.app_role import sync_app_role_password as _sync_role

    _sync_role(_os.environ["DATABASE_ADMIN_URL"], _os.environ["DATABASE_URL"])
    # Reset the global embedding_meta row: leftover rows from earlier runs make
    # migration 0024 seed voyage-3.5, which would block every HashEmbedder write
    # (the guard). Tests claim it fresh on first write instead.
    test_eng = _ce(_os.environ["DATABASE_ADMIN_URL"], isolation_level="AUTOCOMMIT")
    with test_eng.connect() as c:
        c.execute(_t("DELETE FROM embedding_meta"))
        # Every run starts with ZERO tenants. A crashed run skips fixture
        # teardowns and leaks its orgs; eight leaked `P27T` rows then made every
        # by-name org lookup resolve to a stale empty tenant, failing tests that
        # were innocent (and org.name is UNIQUE since 0047, so a leaked name
        # would otherwise kill the next run's fixture INSERT outright).
        c.execute(_t("TRUNCATE org CASCADE"))
    test_eng.dispose()


_ensure_test_db()

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from services.app import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _hermetic_cwd() -> Iterator[None]:
    """Run the tests from the empty directory reserved above (see `_CWD`), so no
    test reads a ./.env, a ./dst.yaml or a ./docker-compose.yml that happens to
    sit next to the shell. Session-scoped rather than import-time: pytest resolves
    its own `testpaths` against the CWD, and moving it before collection quietly
    changes which files get collected."""
    _CWD.mkdir(parents=True, exist_ok=True)
    before = _os.getcwd()
    _os.chdir(_CWD)
    yield
    _os.chdir(before)


@pytest.fixture(autouse=True)
def _no_env_escapes_a_test() -> Iterator[None]:
    """What one test leaves in os.environ is the next test's ambient state.

    ``monkeypatch.setenv`` undoes itself; code under test need not.
    ``services/cli/main.py::_adopt_project_env`` used to seed os.environ with
    ``os.environ.setdefault`` from a project's .env and never unwind it — a single
    CLI test run against a project that had one handed its secrets to every test
    that followed, for the rest of the session. That was fixed in the product (it
    writes nothing to the environment now), and this stays as the general guard:
    a mutation is scoped to the test that made it, whoever makes it."""
    env = _os.environ  # the object, not the name — one test swaps os.environ itself
    before = dict(env)
    yield
    if env != before:
        env.clear()
        env.update(before)


@pytest.fixture(autouse=True)
def _extras_off_unless_declared(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uv sync --extra local-embed` must not change one test's outcome.

    The registry probes optional embedding backends with ``find_spec``, so an
    installed extra silently swaps the embedder: with fastembed present, an install
    that declares NO embedding provider resolves a 384-dim LocalEmbedder instead of
    None. That is how test_plan_predicts_the_starved_eval_gate died — dim 384 against
    vector(1024) columns raised EmbeddingMismatchError, services/project/apply.py
    skipped the certified answer, and the test's precondition simply never held.

    So extras are OFF unless the test says which one it wants::

        @pytest.mark.extra("local-embed")

    which also SKIPS (naming the sync command) when that extra is genuinely absent,
    so "not installed" can never masquerade as "passed". The mask is a plain
    monkeypatch of the registry's own probe, so the tests that stub ``find_spec``
    themselves to exercise real resolution still win."""
    from services.llm import registry

    # {extra → the module it installs}, read off the registry's own table so a new
    # optional backend is masked the moment it is declared there.
    optional = {extra: module for module, extra in registry._EMBED_SDK.values()}
    declared = {str(m.args[0]) for m in request.node.iter_markers("extra")}
    if unknown := declared - optional.keys():
        pytest.fail(f"unknown extra(s) {sorted(unknown)} — declared ones: {sorted(optional)}")
    real = registry.find_spec
    for extra in sorted(declared):
        if real(optional[extra]) is None:
            pytest.skip(f"extra `{extra}` not installed — `uv sync --extra {extra}`")
    masked = {module for extra, module in optional.items() if extra not in declared}
    monkeypatch.setattr(registry, "find_spec", lambda name: None if name in masked else real(name))


@pytest.fixture(autouse=True)
def _fresh_embedder_cache() -> Iterator[None]:
    """``registry.resolve_embedder`` caches per PROCESS (rebuilding a fastembed
    session per request costs more than the embedding does, and a failed build was
    retried per request).
    A process-global cache outlives a test, and its key is the provider config —
    which several tests reuse with a stub embedder where another wants the real
    one. Cleared around every test so no test inherits another's embedder."""
    from services.llm import registry

    registry.reset_embedder_cache()
    yield
    registry.reset_embedder_cache()


@pytest.fixture(scope="session")
def live_client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client
