"""RLS blocks cross-org reads, and fails closed without context.

Integration test — requires the docker-compose Postgres with migrations applied.
Skipped automatically when the DB is unreachable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

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


def test_rls_isolates_orgs() -> None:
    admin = create_engine(settings.database_admin_url)
    app = create_engine(settings.database_url)

    # Bootstrap two orgs + one setting each as the superuser (bypasses RLS).
    with admin.begin() as c:
        a = c.execute(text("INSERT INTO org (name) VALUES ('A') RETURNING id")).scalar_one()
        b = c.execute(text("INSERT INTO org (name) VALUES ('B') RETURNING id")).scalar_one()
        c.execute(text("INSERT INTO setting (org_id, key) VALUES (:o, 'k')"), {"o": a})
        c.execute(text("INSERT INTO setting (org_id, key) VALUES (:o, 'k')"), {"o": b})

    try:
        # The app role sees only the org whose context is set. Use transaction-scoped
        # SET LOCAL (same pattern as services.db.session.org_session) so context does
        # not leak across pooled connections.
        with app.begin() as c:
            c.execute(text(f"SET LOCAL app.current_org = '{a}'"))
            rows_a = c.execute(text("SELECT org_id FROM setting")).scalars().all()
        with app.begin() as c:
            c.execute(text(f"SET LOCAL app.current_org = '{b}'"))
            rows_b = c.execute(text("SELECT org_id FROM setting")).scalars().all()
        assert rows_a == [a]
        assert rows_b == [b]

        # No context -> fail closed (no rows).
        with app.begin() as c:
            rows_none = c.execute(text("SELECT org_id FROM setting")).scalars().all()
        assert rows_none == []
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM setting WHERE org_id IN (:a, :b)"), {"a": a, "b": b})
            c.execute(text("DELETE FROM org WHERE id IN (:a, :b)"), {"a": a, "b": b})
