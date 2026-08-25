"""Per-lens authorization: allow-list, deny-by-default."""

from __future__ import annotations

from services.contracts.lens_config import LensConfig
from services.governance.credentials import CallerIdentity


def authorize(caller: CallerIdentity, config: LensConfig) -> tuple[bool, str]:
    if caller.is_admin:
        return True, "admin"
    rules = config.access.allow
    allowed_callers = {r.caller for r in rules if r.caller}
    if caller.name in allowed_callers:
        return True, "allow-list (caller)"
    allowed_groups = {r.group for r in rules if r.group}
    # "everyone" is the well-known open-access group the UI offers: any caller
    # with a valid key in this org (auth + RLS still apply; this is not public).
    if "everyone" in allowed_groups:
        return True, "allow-list (everyone)"
    if allowed_groups & set(caller.groups):
        return True, "allow-list (group)"
    return False, "not on lens allow-list"
