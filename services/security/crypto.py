"""Symmetric encryption for stored secrets (warehouse SA JSON, context-source tokens).

Fernet (AES-128-CBC + HMAC-SHA256, encrypt-then-MAC) keyed by `settings.secret_key`.
Credentials are encrypted at rest in Postgres and decrypted only when building a
connector.

**`DST_SECRET_KEY` accepts a comma-separated LIST.** The first key encrypts;
every key is tried for decryption. That is what makes rotation possible at all:

    DST_SECRET_KEY=<new>,<old>     # deploy, everything still decrypts
    dst rotate-key                 # re-encrypt every stored secret under <new>
    DST_SECRET_KEY=<new>           # drop <old>

This is Airflow's `fernet_key = new,old` + `rotate-fernet-key` shape, and it is
`cryptography`'s own `MultiFernet` doing the work — no key-version column, no
re-encryption migration, no bespoke envelope scheme. Before this the deployment
doc simply said "keep it byte-stable forever", which is not a rotation story; it
is the absence of one, and it meant a suspected key compromise had no remedy that
did not orphan every stored credential.

The cipher itself is deliberately unchanged. Fernet is where Airflow and
OpenMetadata sit — above Superset's unauthenticated deterministic AES-128-CBC and
below Lightdash's AES-256-GCM. It authenticates, which is the property that
matters; swapping ciphers would be a data migration bought for very little.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from services.config import settings


class CryptoNotConfigured(RuntimeError):
    """Raised when an encrypt/decrypt is attempted without DST_SECRET_KEY set."""


def _keys() -> list[str]:
    """The configured keys, newest first. Blank entries tolerated (trailing comma)."""
    raw = settings.secret_key or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    keys = _keys()
    if not keys:
        raise CryptoNotConfigured(
            "DST_SECRET_KEY is not set — cannot encrypt/decrypt stored secrets. "
            "Generate one: dst secret"
        )
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as exc:
        raise CryptoNotConfigured(f"DST_SECRET_KEY is not a valid Fernet key: {exc}") from exc


def reset_cache() -> None:
    """Drop the memoized MultiFernet. For tests and `dst rotate-key`."""
    _fernet.cache_clear()


def is_configured() -> bool:
    return bool(_keys())


def signing_keys() -> list[str]:
    """The configured keys, newest first, for HMAC use (answer receipts).
    Same rotation contract as Fernet: first key signs, every key verifies."""
    return _keys()


def key_count() -> int:
    return len(_keys())


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string under the PRIMARY (first) key, returning base64."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by `encrypt`, trying every configured key."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CryptoNotConfigured(
            "could not decrypt stored secret — DST_SECRET_KEY may have changed. "
            "If you are rotating, keep the old key in the comma-separated list until "
            "`dst rotate-key` has run."
        ) from exc


def rotate(token: str) -> str:
    """Re-encrypt an existing ciphertext under the primary key.

    `MultiFernet.rotate` decrypts with whichever key works and re-encrypts with the
    first — and it preserves the original timestamp, so rotation does not look like
    the credential was just re-entered.
    """
    try:
        return _fernet().rotate(token.encode()).decode()
    except InvalidToken as exc:
        raise CryptoNotConfigured(
            "could not decrypt stored secret for rotation — the key that encrypted it "
            "is not in DST_SECRET_KEY"
        ) from exc
