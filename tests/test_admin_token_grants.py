"""The app role cannot write admin_token — control-plane keys stay admin-only.

admin_token has no RLS (a token hash resolves before any org context exists), so
DB-level grants are the ONLY fence: an app role that could INSERT there could
mint itself an org-admin token. Integration test against the migrated scratch
database; skipped when Postgres is unreachable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from services.config import settings


def _reachable(url: str) -> bool:
    try:
        with create_engine(url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(settings.database_admin_url),
    reason="Postgres not reachable (run `make up && make migrate`)",
)


def test_app_role_has_no_write_privilege_on_admin_token() -> None:
    with create_engine(settings.database_admin_url).connect() as c:
        privs = {
            p: c.execute(
                text("SELECT has_table_privilege('dst_app', 'admin_token', :p)"), {"p": p}
            ).scalar()
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
        }
    # SELECT stays: auth resolution reads the table. Writes are admin-engine only.
    assert privs == {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False}


def test_app_role_insert_is_refused_by_postgres() -> None:
    # Belt to the catalog check's suspenders: the live connection actually errors.
    app = create_engine(settings.database_url)
    with pytest.raises(ProgrammingError, match="permission denied"), app.connect() as c:
        c.execute(
            text("INSERT INTO admin_token (org_id, token_hash) VALUES (:o, :h)"),
            {"o": str(uuid.uuid4()), "h": "x" * 64},
        )
    app.dispose()
