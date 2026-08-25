"""Keep the migration-created app role's login and password in sync.

Migration 0001 creates the dst_app role NOLOGIN with no password — a baked-in
default would be a public credential on every install. After every
`dst migrate`, the role is granted LOGIN with whatever password DATABASE_URL
declares, so a deployment that sets a strong password in its env gets it
applied on first boot — and one that declares no password at all fails loudly
here instead of shipping a role nothing can (or worse, anything can) log into.
Custom role setups (a DATABASE_URL user the migrations didn't create) are the
operator's own and are left untouched. Privilege errors propagate — failing
loud beats silently keeping a stale credential.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

APP_ROLE = "dst_app"


def sync_app_role_password(admin_url: str, app_url: str, *, role: str = APP_ROLE) -> str | None:
    """ALTER the app role to LOGIN with *app_url*'s password; None when *app_url*
    names a custom role that is not ours to manage."""
    u = make_url(app_url)
    if u.username != role:
        return None
    if not u.password:
        # Fits `dst migrate`'s one-line (200-char) error surface uncut.
        raise RuntimeError(
            f"DATABASE_URL has no password for role '{role}', which is created "
            "without one. Add a password to DATABASE_URL (compose: "
            "DST_APP_DB_PASSWORD) and rerun `dst migrate`."
        )
    # ALTER ROLE cannot take a bind parameter for PASSWORD; escape the literal.
    pw = str(u.password).replace("'", "''")
    eng = create_engine(admin_url)
    try:
        with eng.begin() as c:
            c.execute(text(f"ALTER ROLE \"{role}\" WITH LOGIN PASSWORD '{pw}'"))
    finally:
        eng.dispose()
    return f"app role '{role}' password synced from DATABASE_URL"
