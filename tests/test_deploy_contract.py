"""The production deployment contract is enforced, not implied.

Pins the startup guarantees: DSNs opt into TLS in production, the required env
vars fail loudly by name, the well-known dev DB password is refused, and none
of it applies outside production.
"""

from __future__ import annotations

import pytest

from services.config import Settings, _require_sslmode, settings, validate_production_contract

_DSN = "postgresql+psycopg://dst_app:pw@db.example.com:5432/dst"


def test_sslmode_appended_to_production_dsns() -> None:
    s = Settings(
        _env_file=None, environment="production", database_url=_DSN, database_admin_url=_DSN
    )
    assert s.database_url.endswith("?sslmode=require")
    assert s.database_admin_url.endswith("?sslmode=require")


def test_sslmode_respects_an_explicit_choice() -> None:
    url = _DSN + "?sslmode=disable"
    assert _require_sslmode(url) == url


def test_sslmode_skips_unix_sockets() -> None:
    # Cloud SQL connector style: SSL doesn't apply to unix-domain sockets.
    url = "postgresql+psycopg://u:p@/dst?host=/cloudsql/proj:region:inst"
    assert _require_sslmode(url) == url


def test_local_dsns_untouched() -> None:
    s = Settings(_env_file=None, environment="local", database_url=_DSN, database_admin_url=_DSN)
    assert "sslmode" not in s.database_url


def test_production_contract_names_every_missing_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "secret_key", None)
    monkeypatch.setattr(settings, "public_base_url", None)
    with pytest.raises(RuntimeError, match="DST_SECRET_KEY.*PUBLIC_BASE_URL"):
        validate_production_contract()


def test_production_contract_passes_when_met(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "secret_key", "fernet-key")
    monkeypatch.setattr(settings, "public_base_url", "https://dst.example.com")
    monkeypatch.setattr(settings, "database_url", _DSN)
    validate_production_contract()


def test_production_refuses_the_historical_default_db_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "secret_key", "fernet-key")
    monkeypatch.setattr(settings, "public_base_url", "https://dst.example.com")
    monkeypatch.setattr(
        settings, "database_url", "postgresql+psycopg://dst_app:dst_app_dev@db:5432/dst"
    )
    with pytest.raises(RuntimeError, match="dst_app_dev"):
        validate_production_contract()
    # A real password sails through; so does the same string in another position.
    monkeypatch.setattr(
        settings, "database_url", "postgresql+psycopg://dst_app:s3cret@db:5432/dst_app_dev"
    )
    validate_production_contract()


def test_contract_not_enforced_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "secret_key", None)
    validate_production_contract()


def test_mcp_allowed_hosts_follow_public_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The transport's DNS-rebinding defaults 421 every deployed hostname, so
    # the deployment's origin must be allowlisted.
    from services.mcp.server import _allowed_hosts

    monkeypatch.setattr(settings, "public_base_url", "https://dst.example.com")
    hosts = _allowed_hosts()
    assert "dst.example.com" in hosts
    assert "dst.example.com:*" in hosts
    assert "localhost:*" in hosts  # dev forms survive


def test_mcp_allowed_hosts_local_only_without_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.mcp.server import _allowed_hosts

    monkeypatch.setattr(settings, "public_base_url", None)
    assert all(h.startswith(("localhost", "127.0.0.1")) for h in _allowed_hosts())
