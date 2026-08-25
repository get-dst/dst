"""Warehouse attribution: stamp WHO ran a query into the warehouse's own logs.

dst connects to a warehouse as one service account (or a per-user credential —
see credential_resolver), so without this the warehouse's `QUERY_HISTORY` /
`pg_stat_activity` show only that account. This carries the person, the acting
client, and the request_id into the warehouse session, so the warehouse's own audit
trail can answer "who ran this" and join back to dst's request_log by request_id.

**Self-asserted, and forgeable — observability, not non-repudiation.** dst writes
the tag; nothing stops the warehouse account from running an untagged query by other
means. It closes the gap where an agent's SQL is *anonymous* in the warehouse, not
the one where a determined operator lies about identity. The strong form is per-user
warehouse identity (credential_resolver), where the warehouse authenticates the
person itself.

Delivered as a ContextVar rather than a parameter on `execute()`: attribution is
ambient request identity, orthogonal to what every connector and caller already
passes, and threading it through the `Connector` protocol + fifteen call sites to be
read by three of them is the wrong trade. Connectors that can tag (Snowflake, Postgres)
read `current()` at connect time; the rest ignore it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Attribution:
    principal: str  # the person the credential resolves to
    agent: str | None  # the acting client (MCP client name, "mcp", or None)
    request_id: str  # joins to request_log.request_id
    org_id: str


_current: ContextVar[Attribution | None] = ContextVar("dst_attribution", default=None)


def current() -> Attribution | None:
    return _current.get()


@contextmanager
def attributed(attr: Attribution) -> Iterator[None]:
    """Bind attribution for the duration of a request's warehouse work.

    A context manager, not a bare set, because a connector pools/reuses at the
    process level: a value left set would attach one caller's identity to the next
    caller's query. Reset on exit is the guarantee that cannot be forgotten."""
    token = _current.set(attr)
    try:
        yield
    finally:
        _current.reset(token)


# Warehouse identifiers have length and charset limits (Snowflake QUERY_TAG ≤ 2000
# chars; Postgres application_name ≤ 63 bytes and truncates silently). Keep the tag
# small and predictable.
_TAG_MAX = 900


def query_tag() -> str | None:
    """The current attribution as a compact JSON tag, or None if unbound.

    JSON so a warehouse-side query can parse it back into columns; `dst` prefix on
    the keys so it is greppable in a shared audit log next to other tools' tags."""
    attr = current()
    if attr is None:
        return None
    tag = json.dumps(
        {
            "app": "dst",
            "principal": attr.principal,
            "agent": attr.agent,
            "rid": attr.request_id,
        },
        separators=(",", ":"),
    )
    return tag[:_TAG_MAX]


def application_name() -> str:
    """A Postgres `application_name` (≤ 63 bytes) — request_id is the useful half,
    since it joins to request_log where the rest of the identity already lives."""
    attr = current()
    if attr is None:
        return "dst"
    return f"dst:{attr.request_id}"[:63]
