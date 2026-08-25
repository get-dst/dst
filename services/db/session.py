"""Engines + tenant-scoped sessions.

Two engines: the **app** engine (non-superuser role, RLS-enforced) for normal work,
and the **admin** engine (superuser) for migrations + bootstrap/seed. Every tenant
query runs inside `org_session(org_id)`, which sets the `app.current_org` GUC that
RLS policies read. Fail-closed: without it, RLS returns no rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.config import settings

_pool_kwargs = dict(
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle,
)
engine = create_engine(settings.database_url, **_pool_kwargs)
admin_engine = create_engine(settings.database_admin_url, **_pool_kwargs)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
AdminSessionLocal = sessionmaker(bind=admin_engine, expire_on_commit=False)


def set_org_context(session: Session, org_id: uuid.UUID | str) -> None:
    """Set the per-transaction tenant context RLS reads. Validates the UUID first."""
    oid = uuid.UUID(str(org_id))  # validate -> safe to inline (SET takes no bind params)
    session.execute(text(f"SET LOCAL app.current_org = '{oid}'"))


@contextmanager
def org_session(org_id: uuid.UUID | str) -> Iterator[Session]:
    """A transaction-scoped session with the tenant context set."""
    session = SessionLocal()
    try:
        set_org_context(session, org_id)  # autobegins the transaction
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def admin_session() -> Iterator[Session]:
    """A superuser session for migrations/bootstrap (bypasses RLS)."""
    session = AdminSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
