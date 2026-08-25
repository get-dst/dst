"""The credential seam: which secret a connector is built with, given who is asking.

**Default** — the org's stored connection secret. One service account per org, the
managed default that most deployments want and none has to configure. dst owns
this and keeps it opinionated.

**Override** — an operator who wants per-*user* warehouse identity (their own Okta
automation minting a Snowflake keypair per person, an external-OAuth token exchange,
a Vault lookup keyed on the caller) registers a resolver. dst does **not** build
that federation: storing "this person is that Snowflake principal" is customer-specific
state that belongs in the customer's own system, not in `services/`. What dst
owns is the *seam* — the caller identity reaches the point where the
credential is chosen, and the choice is overridable. Possible, not necessarily
frictionless, and never walled off.

A plugin (a `dst.plugins` entry point) installs one from its `register()`:

    from services.lenses import credential_resolver

    def per_user(req):
        if req.caller and req.connection_type == "snowflake":
            return my_vault.keypair_for(req.caller.name)  # your automation
        return req.org_secret                             # everyone else: default

    credential_resolver.set_resolver(per_user)

The resolver receives the caller and returns a secret string (or None). Returning
`req.org_secret` for a caller it does not handle is the correct fall-through — the
default is always available in the request, so an override is additive, never a
cliff where an unhandled caller loses access.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.governance.credentials import CallerIdentity


@dataclass(frozen=True)
class CredentialRequest:
    """Everything a resolver needs to choose a secret — and nothing it shouldn't.

    `caller` is None on the management/admin plane: `/mgmt/*` operations (introspect,
    profile, certify) run as the org, not as a person, so there is no per-user
    identity to key on there and the default is always correct.
    """

    caller: CallerIdentity | None
    connection: str
    connection_type: str
    config: dict[str, Any]
    org_secret: str | None


CredentialResolver = Callable[[CredentialRequest], str | None]


def _default(req: CredentialRequest) -> str | None:
    """The org service account — today's behaviour, and the managed default."""
    return req.org_secret


_resolver: CredentialResolver = _default


def set_resolver(fn: CredentialResolver) -> None:
    """Install a credential resolver. A plugin calls this from its register()."""
    global _resolver
    _resolver = fn


def reset_resolver() -> None:
    """Restore the org-service-account default. For tests, and for completeness."""
    global _resolver
    _resolver = _default


def resolve(req: CredentialRequest) -> str | None:
    return _resolver(req)
