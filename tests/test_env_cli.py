"""`dst env` — disposable environments.

An environment is an org on the server; `env rm` is one `DELETE FROM org` that
cascades. Two invariants pinned here, because `env rm` silently breaks without
them:

- every table carrying an org_id column declares ON DELETE CASCADE to org —
  a future table that forgets it would turn `env rm` into an FK error;
- the delete works through a managed-PG-shaped admin role (no SUPERUSER, no
  BYPASSRLS): referential actions bypass RLS, so the cascade must empty
  FORCE-RLS tables the role itself cannot see.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from services.config import settings


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(
    not _reachable(settings.database_admin_url), reason="Postgres not reachable"
)


def _args(action: str, name: str | None = None, yes: bool = False) -> argparse.Namespace:
    return argparse.Namespace(action=action, name=name, yes=yes)


def _org_id(name: str) -> uuid.UUID | None:
    admin = create_engine(settings.database_admin_url)
    try:
        with admin.begin() as c:
            return c.execute(  # type: ignore[no-any-return]
                text("SELECT id FROM org WHERE name = :n"), {"n": name}
            ).scalar()
    finally:
        admin.dispose()


@pytest.fixture
def env_name() -> Iterator[str]:
    name = f"envtest-{uuid.uuid4().hex[:8]}"
    yield name
    admin = create_engine(settings.database_admin_url)
    try:
        with admin.begin() as c:
            c.execute(text("DELETE FROM org WHERE name = :n"), {"n": name})
    finally:
        admin.dispose()


@needs_db
def test_env_new_creates_org_and_token(env_name: str, capsys: pytest.CaptureFixture[str]) -> None:
    from services.cli.main import _env

    assert _env(_args("new", env_name)) == 0
    out = capsys.readouterr().out
    assert "created" in out
    assert "dstadm_" in out
    assert "DST_ADMIN_TOKEN=" in out  # the CI-wiring line
    oid = _org_id(env_name)
    assert oid is not None
    admin = create_engine(settings.database_admin_url)
    try:
        with admin.begin() as c:
            n = c.execute(
                text("SELECT count(*) FROM admin_token WHERE org_id = :o AND label = 'env'"),
                {"o": oid},
            ).scalar_one()
    finally:
        admin.dispose()
    assert n == 1


@needs_db
def test_env_new_is_idempotent(env_name: str, capsys: pytest.CaptureFixture[str]) -> None:
    from services.cli.main import _env

    assert _env(_args("new", env_name)) == 0
    assert _env(_args("new", env_name)) == 0
    assert "reused" in capsys.readouterr().out
    admin = create_engine(settings.database_admin_url)
    try:
        with admin.begin() as c:
            n = c.execute(
                text("SELECT count(*) FROM org WHERE name = :n"), {"n": env_name}
            ).scalar_one()
    finally:
        admin.dispose()
    assert n == 1  # rerun mints a token, never a duplicate org


@needs_db
def test_env_new_never_touches_dotenv(
    env_name: str, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.cli.main import _env

    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    dotenv = tmp_path / ".env"  # type: ignore[operator]
    dotenv.write_text("DST_ADMIN_TOKEN=original\n", encoding="utf-8")
    assert _env(_args("new", env_name)) == 0
    # bootstrap rewrites DST_ADMIN_TOKEN; a sandbox must not steal the
    # project's identity.
    assert dotenv.read_text(encoding="utf-8") == "DST_ADMIN_TOKEN=original\n"


@needs_db
def test_env_ls_lists_and_counts(env_name: str, capsys: pytest.CaptureFixture[str]) -> None:
    from services.cli.main import _env

    assert _env(_args("new", env_name)) == 0
    capsys.readouterr()
    assert _env(_args("ls")) == 0
    out = capsys.readouterr().out
    assert env_name in out
    assert "LENSES" in out


@needs_db
def test_env_rm_requires_yes_and_exact_name(
    env_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from services.cli.main import _env

    assert _env(_args("new", env_name)) == 0
    assert _env(_args("rm", env_name)) == 1  # no --yes
    assert "--yes" in capsys.readouterr().err
    assert _env(_args("rm", "no-such-env", yes=True)) == 1
    assert _org_id(env_name) is not None  # still there


@needs_db
def test_env_rm_cascades_org_content(env_name: str) -> None:
    from services.cli.main import _env

    assert _env(_args("new", env_name)) == 0
    oid = _org_id(env_name)
    admin = create_engine(settings.database_admin_url)
    try:
        with admin.begin() as c:
            c.execute(
                text("INSERT INTO caller (org_id, name) VALUES (:o, 'envtest-caller')"),
                {"o": oid},
            )
        assert _env(_args("rm", env_name, yes=True)) == 0
        with admin.begin() as c:
            c.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": str(oid)})
            left = c.execute(
                text("SELECT count(*) FROM caller WHERE org_id = :o"), {"o": oid}
            ).scalar_one()
    finally:
        admin.dispose()
    assert _org_id(env_name) is None
    assert left == 0


@needs_db
def test_env_rm_works_without_bypassrls(env_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The managed-PG shape: `env rm` through a role with SUPERUSER and
    BYPASSRLS stripped — the cascade must still empty FORCE-RLS tables."""
    from services.cli import main as cli_main

    url = make_url(settings.database_admin_url)
    role = f"env_probe_{url.database}"[:63]
    admin = create_engine(settings.database_admin_url)
    with admin.begin() as c:
        c.execute(text(f"DROP ROLE IF EXISTS {role}"))
        c.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'probe' NOSUPERUSER NOBYPASSRLS"))
        c.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        c.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
        )
    probe: Engine = create_engine(url.set(username=role, password="probe"))
    try:
        assert cli_main._env(_args("new", env_name)) == 0
        oid = _org_id(env_name)
        with admin.begin() as c:
            c.execute(
                text("INSERT INTO caller (org_id, name) VALUES (:o, 'envtest-caller')"),
                {"o": oid},
            )
        import services.db.session as db_session

        monkeypatch.setattr(db_session, "admin_engine", probe)
        assert cli_main._env(_args("rm", env_name, yes=True)) == 0
        assert _org_id(env_name) is None
    finally:
        probe.dispose()
        with admin.begin() as c:
            c.execute(text(f"DROP OWNED BY {role}"))
            c.execute(text(f"DROP ROLE {role}"))
        admin.dispose()


@needs_db
def test_env_new_records_name_and_rm_forgets_it(
    env_name: str, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token lands in .dst/envs.json so local commands use --env
    <name> and no secret changes hands."""
    import json as _json
    from pathlib import Path

    from services.cli.main import _env

    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    assert _env(_args("new", env_name)) == 0
    envs = Path(str(tmp_path)) / ".dst" / "envs.json"
    recorded = _json.loads(envs.read_text(encoding="utf-8"))
    assert recorded[env_name].startswith("dstadm_")
    assert _env(_args("rm", env_name, yes=True)) == 0
    assert env_name not in _json.loads(envs.read_text(encoding="utf-8"))


@needs_db
def test_project_org_resolves_env_name_as_org(
    env_name: str, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On DB-direct verbs (dst test) the env NAME is the org selector —
    the local no-secret path."""
    import argparse as _ap

    from services.cli.main import _env, _project_org

    monkeypatch.chdir(tmp_path)  # type: ignore[arg-type]
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    assert _env(_args("new", env_name)) == 0
    resolved = _project_org(_ap.Namespace(org=None, env=env_name, dir="."))
    assert resolved is not None and resolved[1] == env_name


@needs_db
def test_every_org_scoped_table_cascades() -> None:
    """Tripwire: a future table with an org_id column but no ON DELETE CASCADE
    turns `dst env rm` into an FK error. Fails here first, with the table named."""
    admin = create_engine(settings.database_admin_url)
    try:
        with admin.begin() as c:
            offenders = (
                c.execute(
                    text(
                        """
                    SELECT cols.table_name
                    FROM information_schema.columns cols
                    WHERE cols.column_name = 'org_id'
                      AND cols.table_schema = 'public'
                      AND NOT EXISTS (
                        SELECT 1
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON kcu.constraint_name = tc.constraint_name
                         AND kcu.table_schema = tc.table_schema
                        JOIN information_schema.referential_constraints rc
                          ON rc.constraint_name = tc.constraint_name
                         AND rc.constraint_schema = tc.table_schema
                        WHERE tc.table_name = cols.table_name
                          AND tc.table_schema = 'public'
                          AND tc.constraint_type = 'FOREIGN KEY'
                          AND kcu.column_name = 'org_id'
                          AND rc.delete_rule = 'CASCADE'
                      )
                    """
                    )
                )
                .scalars()
                .all()
            )
    finally:
        admin.dispose()
    assert offenders == [], (
        f"org-scoped tables without ON DELETE CASCADE to org: {offenders} — "
        "`dst env rm` breaks on these; add the cascade in the table's migration"
    )
