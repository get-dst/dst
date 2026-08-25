"""Alembic environment. Runs as the admin (superuser) role from settings."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

import services.db.models  # noqa: F401  (register tables on Base.metadata)
from services.config import settings
from services.db.base import Base

target_metadata = Base.metadata
url = settings.database_admin_url


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
