"""Token generation + hashing.

Tokens are high-entropy secrets, so a fast SHA-256 hash is appropriate for
lookup (unlike passwords, which need a slow KDF). We store only the hash.
"""

from __future__ import annotations

import hashlib
import secrets

ADMIN_PREFIX = "dstadm_"
CALLER_PREFIX = "dst_"
# OAuth access tokens (minted by the AS facade after a browser sign-in). Verified by
# the same hash lookup as caller keys, so the prefix is for routing/display only.
OAUTH_PREFIX = "dsto_"
# Local dashboard sessions (email+password login, no Clerk). Looked up by hash
# in local_session; the prefix routes to the local resolver in auth/deps.py.
SESSION_PREFIX = "dstsess_"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_admin_token() -> str:
    return ADMIN_PREFIX + secrets.token_urlsafe(32)


def new_caller_key() -> str:
    return CALLER_PREFIX + secrets.token_urlsafe(32)


def new_oauth_token() -> str:
    return OAUTH_PREFIX + secrets.token_urlsafe(32)


def new_session_token() -> str:
    return SESSION_PREFIX + secrets.token_urlsafe(32)
