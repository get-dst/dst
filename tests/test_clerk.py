"""Clerk auth: publishable-key parsing + org provisioning (JWT verify needs a live token)."""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import create_engine, text

from services.auth.clerk import _frontend_host, org_for_clerk
from services.config import settings


def test_frontend_host_parsing() -> None:
    host = "example.clerk.accounts.dev"
    pk = "pk_test_" + base64.b64encode((host + "$").encode()).decode()
    assert _frontend_host(pk) == host


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


@needs_db
def test_org_for_clerk_is_idempotent() -> None:
    ref = f"clerktest:{uuid.uuid4()}"
    try:
        first = org_for_clerk(ref, "Test Org")
        second = org_for_clerk(ref, "Test Org")
        assert first == second
    finally:
        with create_engine(settings.database_admin_url).begin() as c:
            c.execute(text("DELETE FROM org WHERE clerk_ref = :r"), {"r": ref})
