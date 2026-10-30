"""The security context dst hands Cube: who is asking, as a short-lived JWT.

Cube reads the claims of an HS256 token signed with ``CUBEJS_API_SECRET`` as
its ``security_context``; an ``access_policy`` in the model reads
``security_context.dst.groups`` to withhold members. dst mints one token per
request with the caller's org, name, groups, the lens and the request id, five
minutes of life, so a leaked token names one request and expires before anyone
can replay it usefully. The claims are the same facts the request log carries,
which is what lets a Cube-side refusal be joined back to a dst request.
"""

from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt

if TYPE_CHECKING:
    from services.governance.credentials import CallerIdentity

TOKEN_LIFETIME = timedelta(minutes=5)


def wall_now() -> datetime:
    """The real time, even while this process's clock is pinned to another
    day (tests and replays pin it with time-machine or freezegun, which
    patch ``datetime.now`` and ``time.time`` alike): the clock Cube reads a
    token's expiry against. Each library keeps the real clock it replaced."""
    hatch = getattr(sys.modules.get("time_machine"), "escape_hatch", None)
    if hatch is not None and hatch.is_travelling():
        return datetime.fromtimestamp(hatch.time.time(), UTC)
    frozen = sys.modules.get("freezegun.api")
    real_time = getattr(frozen, "real_time", None)
    if callable(real_time):
        return datetime.fromtimestamp(real_time(), UTC)
    return datetime.now(UTC)


@dataclass(frozen=True)
class CubeContext:
    """What one Cube call is made as."""

    org: str
    caller: str
    groups: list[str] = field(default_factory=list)
    lens: str | None = None
    request_id: str | None = None

    @classmethod
    def of(
        cls, identity: CallerIdentity, *, lens: str | None = None, request_id: str | None = None
    ) -> CubeContext:
        return cls(
            org=str(identity.org_id),
            caller=identity.name,
            groups=sorted(identity.groups),
            lens=lens,
            request_id=request_id,
        )

    @classmethod
    def operator(cls, *, lens: str | None = None) -> CubeContext:
        """The context for an operator verb with no caller (``dst cube status``,
        plan): an org-less, group-less identity, so a policy that withholds by
        group withholds here too."""
        return cls(org="", caller="dst", groups=[], lens=lens, request_id=str(uuid.uuid4()))

    def claims(self, *, now: datetime | None = None) -> dict[str, Any]:
        """The JWT body: ``{dst: {org, caller, groups, lens, request_id}}`` plus
        ``iat``/``exp``."""
        now = now or datetime.now(UTC)
        return {
            "dst": {
                "org": self.org,
                "caller": self.caller,
                "groups": list(self.groups),
                "lens": self.lens,
                "request_id": self.request_id,
            },
            "iat": int(now.timestamp()),
            "exp": int((now + TOKEN_LIFETIME).timestamp()),
        }

    def token(self, secret: str, *, now: datetime | None = None) -> str:
        return jwt.encode(self.claims(now=now), secret, algorithm="HS256")
