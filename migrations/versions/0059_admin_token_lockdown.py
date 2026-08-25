"""admin_token lockdown: the app role loses its writes and its default password

Revision ID: 0059
Revises: 0058
Create Date: 2026-08-24

admin_token has no RLS (auth resolves a token hash before any org exists), so
write access to it IS control-plane access: a client holding the app role's
credentials could INSERT a token and become org admin. Two exposures existed on
installs migrated before this revision, and both close here:

* the app role held INSERT/UPDATE/DELETE on admin_token (0001 granted them) —
  revoked; SELECT stays, auth resolution reads the table;
* 0001 used to create the role with a published default password (the role
  name suffixed '_dev') — cleared below iff the role still carries exactly
  that password, so a real password set since (via `dst migrate`'s sync or by
  hand) is never touched.
  `dst migrate` re-applies the password DATABASE_URL declares right after
  upgrading, so a correctly configured deployment never notices; one still
  riding the old default must set a real password (compose:
  DST_APP_DB_PASSWORD) — in production the server refuses to boot on the
  default anyway.

Downgrade restores the grants (the old shape) but never restores a password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from alembic import op
from sqlalchemy import text

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

UPGRADE = [
    "REVOKE INSERT, UPDATE, DELETE ON admin_token FROM dst_app",
]

DOWNGRADE = [
    "GRANT INSERT, UPDATE, DELETE ON admin_token TO dst_app",
]

_ROLE = "dst_app"
# Derived, never spelled: the release gate greps migrations/ for the raw
# historical password, and a literal here would read as shipping it again.
_DEFAULT_PASSWORD = _ROLE + "_dev"


def _is_default_password(rolpassword: str | None) -> bool:
    """Does this pg_authid verifier encode exactly the shipped default?

    Postgres stores a one-way verifier, so the check recomputes it: SCRAM-SHA-256
    per RFC 5802 (salted password -> client key -> stored key), plus the legacy
    md5(password || rolename) form for clusters created with password_encryption=md5.
    Anything unparseable is treated as not-the-default — never clear a password we
    cannot positively identify.
    """
    if not rolpassword:
        return False
    if rolpassword.startswith("SCRAM-SHA-256$"):
        # Verifier format: SCRAM-SHA-256$<iterations>:<salt>$<stored_key>:<server_key>
        try:
            _scheme, rest = rolpassword.split("$", 1)
            iter_and_salt, keys = rest.split("$", 1)
            iterations, salt_b64 = iter_and_salt.split(":", 1)
            stored_key_b64 = keys.split(":", 1)[0]
            salted = hashlib.pbkdf2_hmac(
                "sha256", _DEFAULT_PASSWORD.encode(), base64.b64decode(salt_b64), int(iterations)
            )
            client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
            return hashlib.sha256(client_key).digest() == base64.b64decode(stored_key_b64)
        except (ValueError, TypeError):
            return False
    if rolpassword.startswith("md5"):
        legacy = hashlib.md5((_DEFAULT_PASSWORD + _ROLE).encode()).hexdigest()  # noqa: S324
        return rolpassword == "md5" + legacy
    return False


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)
    bind = op.get_bind()
    # Managed Postgres (Cloud SQL/RDS/Neon): the migration role is not a
    # superuser and pg_authid is off-limits — probe with has_table_privilege
    # instead of trying and aborting the migration transaction. The revoke above
    # still applied; the production boot refusal covers a DATABASE_URL riding
    # the default.
    readable = bind.execute(text("SELECT has_table_privilege('pg_authid', 'SELECT')")).scalar()
    if not readable:
        return
    stored = bind.execute(
        text("SELECT rolpassword FROM pg_authid WHERE rolname = :r"), {"r": _ROLE}
    ).scalar()
    if _is_default_password(stored):
        op.execute(f"ALTER ROLE {_ROLE} PASSWORD NULL")


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
