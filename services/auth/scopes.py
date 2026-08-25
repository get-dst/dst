"""The scope vocabulary, and the one place it is enforced.

Deliberately two scopes. The data plane makes exactly one distinction that a
caller can be restricted along — everything reads governed data except filing a
correction — so a richer vocabulary would be scopes we advertise and never check,
which is the defect this module exists to fix (`scope` used to be accepted at
/oauth/authorize and discarded, so the consent screen named a grant the token did
not carry).

`read` is the load-bearing one. It is the guarantee Linear has and GitHub's
read-only *URL path* does not: enforcement lives on the token, so it survives the
caller finding another route to the same capability. Note that HTTP method is NOT
the discriminator here — `POST /v1/lenses/{n}/query` runs a SELECT and is a read.

An empty scope list means unrestricted. Every credential minted before this
existed has no scopes, and a `dst_` service key issued by an admin who did not ask
for a restriction should not silently acquire one.
"""

from __future__ import annotations

from fastapi import HTTPException

READ = "read"
WRITE = "write"
ALL: tuple[str, ...] = (READ, WRITE)


def parse(requested: str | None) -> list[str]:
    """Space-delimited OAuth `scope` -> a validated list. Unknown scopes raise.

    Rejecting rather than silently dropping: a client that asks for `admin` and
    gets a token back has been told it holds a grant it does not. The spec's
    scope-minimization guidance names "treating claimed scopes as sufficient" as
    the anti-pattern, and the mirror of it is handing back claims we invented.
    """
    if not requested or not requested.strip():
        return []
    asked = requested.split()
    unknown = [s for s in asked if s not in ALL]
    if unknown:
        raise ValueError(f"unknown scope(s): {' '.join(sorted(unknown))}")
    # Deduplicate, keep the canonical order so the stored value is comparable.
    return [s for s in ALL if s in asked]


def permits(granted: list[str] | None, required: str) -> bool:
    """Empty/None = unrestricted. Otherwise the scope must be present."""
    return not granted or required in granted


def require(granted: list[str] | None, required: str) -> None:
    """Raise 403 + a spec-shaped challenge naming the scope that was missing.

    RFC 6750 says the challenge carries `error="insufficient_scope"` and the
    scope needed, so a client can step up in one round trip instead of guessing.
    """
    if permits(granted, required):
        return
    raise HTTPException(
        status_code=403,
        detail=f"this credential is scoped to '{' '.join(granted or [])}' — '{required}' required",
        headers={
            "WWW-Authenticate": (
                f'Bearer error="insufficient_scope", '
                f'error_description="requires the {required} scope", scope="{required}"'
            )
        },
    )
