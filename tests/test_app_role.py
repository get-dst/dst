"""The app role's password follows DATABASE_URL after migrate."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from services.config import settings
from services.db.app_role import sync_app_role_password


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


def test_foreign_users_are_left_alone() -> None:
    assert sync_app_role_password("unused", "postgresql+psycopg://custom:pw@x/db") is None


def test_passwordless_app_url_fails_loudly() -> None:
    # The migrations create the role without a password; a DATABASE_URL that
    # declares none would leave nothing able to log in — refuse, don't shrug.
    with pytest.raises(RuntimeError, match="DST_APP_DB_PASSWORD"):
        sync_app_role_password("unused", "postgresql+psycopg://dst_app@x/db")


@needs_db
def test_password_syncs_and_role_can_log_in() -> None:
    admin = settings.database_admin_url
    role = "dst_test_pw_sync"
    eng = create_engine(admin)
    with eng.begin() as c:
        c.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        # The fresh-install shape: NOLOGIN, no password (migration 0001).
        c.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
    try:
        app_url = make_url(admin).set(username=role, password="new'pw")  # quoting exercised
        raw = app_url.render_as_string(hide_password=False)
        assert sync_app_role_password(admin, raw, role=role) is not None
        with create_engine(app_url).connect() as c:
            assert c.execute(text("SELECT current_user")).scalar() == role
    finally:
        with eng.begin() as c:
            c.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        eng.dispose()
