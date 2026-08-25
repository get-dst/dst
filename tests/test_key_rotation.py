"""Key rotation actually rotates, and a wrong key refuses to boot.

Before this, `DST_SECRET_KEY` took one key with no rotation path — the
deployment doc said "keep it byte-stable forever", which is the absence of a
rotation story rather than one. A suspected key compromise had no remedy that did
not orphan every stored warehouse credential.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from services.config import settings
from services.security import crypto


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


@pytest.fixture(autouse=True)
def _clean_crypto_cache() -> object:
    crypto.reset_cache()
    yield
    crypto.reset_cache()


def _set_keys(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    monkeypatch.setattr(settings, "secret_key", ",".join(keys))
    crypto.reset_cache()


def test_a_second_key_decrypts_what_the_first_encrypted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: deploy `new,old` and nothing breaks while you rotate."""
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()

    _set_keys(monkeypatch, old)
    blob = crypto.encrypt("warehouse-password")

    # New key alone cannot read it — this is the failure rotation has to avoid.
    _set_keys(monkeypatch, new)
    with pytest.raises(crypto.CryptoNotConfigured):
        crypto.decrypt(blob)

    # Both configured: readable, and the new key is the one that encrypts.
    _set_keys(monkeypatch, new, old)
    assert crypto.decrypt(blob) == "warehouse-password"
    fresh = crypto.encrypt("new-secret")

    _set_keys(monkeypatch, new)
    assert crypto.decrypt(fresh) == "new-secret", "primary key is not the first in the list"


def test_rotate_moves_ciphertext_to_the_primary_key(monkeypatch: pytest.MonkeyPatch) -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    _set_keys(monkeypatch, old)
    blob = crypto.encrypt("secret")

    _set_keys(monkeypatch, new, old)
    rotated = crypto.rotate(blob)

    # After rotation the old key is droppable — which is the step the operator
    # needs to be able to take, and the one they cannot verify by hand.
    _set_keys(monkeypatch, new)
    assert crypto.decrypt(rotated) == "secret"


def test_unconfigured_crypto_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", None)
    crypto.reset_cache()
    assert not crypto.is_configured()
    with pytest.raises(crypto.CryptoNotConfigured, match="DST_SECRET_KEY"):
        crypto.encrypt("x")


@needs_db
def test_wrong_key_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed key is caught at boot, not by whichever request touches a connector.

    Without the sentinel the failure is silent until the first decrypt, which may
    be days after the deploy that caused it — and it surfaces as a 503 on one
    connector rather than "your key is wrong".
    """
    from services.security import sentinel

    admin = create_engine(settings.database_admin_url)
    right, wrong = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    marker = f"encryption-check-test-{uuid.uuid4()}"
    monkeypatch.setattr(sentinel, "_KEY", marker)

    try:
        # First boot on a database with no sentinel plants one.
        _set_keys(monkeypatch, right)
        sentinel.verify_or_install()
        with admin.connect() as c:
            planted = c.execute(
                text("SELECT value FROM dst_meta WHERE key = :k"), {"k": marker}
            ).scalar_one()
        assert planted and planted != marker, "sentinel must be stored encrypted"

        # Same key: boots.
        sentinel.verify_or_install()

        # Wrong key: refuses, and says what to do about it.
        _set_keys(monkeypatch, wrong)
        with pytest.raises(sentinel.WrongEncryptionKey, match="does not decrypt"):
            sentinel.verify_or_install()

        # Old key still in the list (mid-rotation): boots again.
        _set_keys(monkeypatch, wrong, right)
        sentinel.verify_or_install()

        # And rotation moves the sentinel too, so the old key becomes droppable.
        sentinel.rotate_sentinel()
        _set_keys(monkeypatch, wrong)
        sentinel.verify_or_install()
    finally:
        with admin.begin() as c:
            c.execute(text("DELETE FROM dst_meta WHERE key = :k"), {"k": marker})


@needs_db
def test_rotate_key_refuses_a_single_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: object
) -> None:
    """One key configured means nothing old is readable — a "successful" rotation
    would have silently skipped every row it could not decrypt."""
    import argparse

    from services.cli.main import _rotate_key

    # The verb calls _adopt_project_env, which rebuilds the settings singleton from
    # <dir>/.env — so the key has to arrive through the environment, not a
    # monkeypatched attribute that adoption would overwrite.
    monkeypatch.setenv("DST_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("DATABASE_ADMIN_URL", settings.database_admin_url)
    rc = _rotate_key(argparse.Namespace(force=False, dir=str(tmp_path)))
    assert rc == 1
    assert "needs both" in capsys.readouterr().err
