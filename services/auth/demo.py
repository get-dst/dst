"""Public demo mode: every signed-in person is a demo caller in ONE org.

Clerk's normal mapping provisions a tenant per person and makes them its admin —
right for a customer, wrong for a public deployment where a stranger signing in
must land in the seeded org as a limited, attributable, non-admin caller. With
DST_DEMO_ORG_ID set, a verified Clerk session resolves to a caller named by the
sign-in email, in group `demo`, in that org; lens allow-lists grant `group: demo`,
the per-caller quotas key on the email, and admin is reachable only with a
dstadm_ token. Three doors share this one mapping: the bearer data plane, the
MCP OAuth grant, and the self-serve key mint.
"""

from __future__ import annotations

import uuid

from services.auth import clerk
from services.config import settings
from services.db.session import org_session
from services.governance import credentials
from services.governance.credentials import CallerIdentity

GROUP = "demo"
CALLER_TYPE = "demo"


def enabled() -> bool:
    return bool(settings.demo_org_id)


def org_id() -> uuid.UUID:
    return uuid.UUID(str(settings.demo_org_id))


def resolve(raw: str) -> CallerIdentity | None:
    """The demo caller behind a Clerk session token — provisioned on first sight.
    None when demo mode is off or the token does not verify; never an admin."""
    if not enabled():
        return None
    claims = clerk.verify_token(raw)
    if claims is None:
        return None
    name = str(claims.get("email") or claims.get("sub") or "").strip().lower()
    if not name:
        return None
    oid = org_id()
    with org_session(oid) as session:
        cid = credentials.caller_id_by_name(session, name) or credentials.create_caller(
            session, name, CALLER_TYPE, [GROUP]
        )
    return CallerIdentity(org_id=oid, name=name, is_admin=False, caller_id=cid, groups=[GROUP])
