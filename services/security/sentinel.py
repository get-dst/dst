"""The encryption-key sentinel: a wrong key crashes at boot, not at query time.

A random UUID stored **encrypted** in a plain `setting` row. On startup we decrypt
it: if it is there and does not come back as that UUID, `DST_SECRET_KEY` is not
the key that wrote this database, and we refuse to serve.

Without this, a changed or mistyped key is silent until the first request that
happens to touch a stored credential — so the failure surfaces as a scattered
503 on one connector, minutes or days after the deploy that caused it, to whoever
is unlucky. The whole point of the deployment contract is that degradations
announce themselves at startup (`validate_production_contract`); this closes the
one that could not, because it needs the database to detect.

Borrowed wholesale from Metabase's `encryption-check`, which is the best version
of this in the OSS field. Deliberately a raw row rather than a typed setting: it
must be readable before anything that might itself need decryption.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text

from services.db.session import admin_engine
from services.security import crypto

log = logging.getLogger("dst.crypto")

_KEY = "encryption-check"


class WrongEncryptionKey(RuntimeError):
    """DST_SECRET_KEY does not match the key this database was written with."""


def verify_or_install() -> None:
    """Check the sentinel, or plant it on a database that has none.

    No-op when crypto is unconfigured: a dev instance with no stored credentials
    is a legitimate state, and demanding a key to boot would break `dst demo`.
    """
    if not crypto.is_configured():
        return
    with admin_engine.begin() as conn:
        row = conn.execute(text("SELECT value FROM dst_meta WHERE key = :k"), {"k": _KEY}).first()
        if row is None:
            token = crypto.encrypt(str(uuid.uuid4()))
            conn.execute(
                text("INSERT INTO dst_meta (key, value) VALUES (:k, :v)"),
                {"k": _KEY, "v": token},
            )
            log.info("encryption sentinel planted")
            return
    try:
        crypto.decrypt(str(row[0]))
    except crypto.CryptoNotConfigured as exc:
        raise WrongEncryptionKey(
            "DST_SECRET_KEY does not decrypt this database's encryption sentinel — "
            "every stored warehouse credential was encrypted with a different key. "
            "Refusing to start rather than failing one connector at a time. "
            "If you are rotating, put the OLD key in the comma-separated list too."
        ) from exc


def rotate_sentinel() -> None:
    """Re-encrypt the sentinel under the primary key. Part of `dst rotate-key`."""
    with admin_engine.begin() as conn:
        row = conn.execute(text("SELECT value FROM dst_meta WHERE key = :k"), {"k": _KEY}).first()
        if row is None:
            return
        conn.execute(
            text("UPDATE dst_meta SET value = :v WHERE key = :k"),
            {"v": crypto.rotate(str(row[0])), "k": _KEY},
        )
