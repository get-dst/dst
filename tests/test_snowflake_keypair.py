"""Snowflake keypair auth — ahead of the password deprecation.

Snowflake is retiring single-factor password authentication, final enforcement
August–October 2026. A connector whose only credential is a stored password stops
working on their timetable. These are offline: they assert what gets handed to
snowflake.connector.connect, not that a real warehouse accepts it.
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from services.connectors.snowflake import SnowflakeConnector

_BASE = {"account": "a", "user": "u", "warehouse": "w", "database": "d"}


def _pem(passphrase: bytes | None = None) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enc = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=enc,
    ).decode()


def _captured(conn: SnowflakeConnector, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Call _connect with the driver stubbed, return the kwargs it would receive."""
    import snowflake.connector

    seen: dict[str, Any] = {}

    def fake_connect(**kw: Any) -> object:
        seen.update(kw)
        return object()

    monkeypatch.setattr(snowflake.connector, "connect", fake_connect)
    conn._connect()
    return seen


def test_pem_secret_is_detected_as_keypair(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator pasting a PEM into the credential box gets keypair auth.

    Sniffing the shape rather than requiring an explicit `auth` field: the
    alternative is a confusing authentication failure against a key that is
    perfectly valid.
    """
    conn = SnowflakeConnector.from_record(dict(_BASE), _pem())
    kw = _captured(conn, monkeypatch)
    assert "private_key" in kw and isinstance(kw["private_key"], bytes)
    assert "password" not in kw, "a keypair connection must not also send a password"


def test_password_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The existing path is untouched — this is additive until Snowflake forces it."""
    conn = SnowflakeConnector.from_record(dict(_BASE), "hunter2")
    kw = _captured(conn, monkeypatch)
    assert kw["password"] == "hunter2"
    assert "private_key" not in kw


def test_explicit_auth_keypair_overrides_sniffing() -> None:
    conn = SnowflakeConnector.from_record({**_BASE, "auth": "keypair"}, _pem())
    assert conn._private_key is not None and conn._password is None


def test_encrypted_key_needs_its_passphrase(monkeypatch: pytest.MonkeyPatch) -> None:
    pem = _pem(passphrase=b"s3cret")
    ok = SnowflakeConnector.from_record({**_BASE, "private_key_passphrase": "s3cret"}, pem)
    assert "private_key" in _captured(ok, monkeypatch)

    # Wrong/absent passphrase must fail loudly at connect, not silently fall back
    # to some other credential.
    bad = SnowflakeConnector.from_record(dict(_BASE), pem)
    with pytest.raises((TypeError, ValueError)):
        bad._load_private_key()


def test_missing_credential_is_named() -> None:
    with pytest.raises(ValueError, match="no stored credential"):
        SnowflakeConnector.from_record(dict(_BASE), None)
