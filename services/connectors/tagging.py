"""Query tagging — every dst-authored statement identifies itself.

Each connector prepends a structured comment to the final query text it executes:

    /* dst: {"app": "dst", "purpose": "serve", "lens": "sales_comp", ...} */

The comment survives verbatim in warehouse query history (BigQuery
INFORMATION_SCHEMA.JOBS, Snowflake QUERY_HISTORY), which buys two things at once:
warehouse admins can attribute dst's traffic without guessing, and the drift
audit can mechanically exclude self-traffic — governed queries come from the
compiled model and must not be mined as organic practice (including the audit's
own variant re-runs, which would otherwise appear as drift in the next round).

Enrichment is ambient: callers wrap work in ``query_context(...)`` (a contextvar,
async-safe) and every execute inside the block carries those fields. Without a
context the tag is the bare ``{"app": "dst"}`` — every authored query is
tagged, never just the ones with a story.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

MARKER = "/* dst:"

_context: ContextVar[dict[str, str] | None] = ContextVar("dst_query_context", default=None)


@contextmanager
def query_context(**fields: str | None) -> Iterator[None]:
    """Ambient metadata for every query executed inside the block (None values drop)."""
    merged = dict(_context.get() or {})
    merged.update({k: v for k, v in fields.items() if v})
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def tag_sql(query: str) -> str:
    """Prepend the dst tag comment to a final query string (idempotent)."""
    if query.lstrip().startswith(MARKER):
        return query
    payload: dict[str, str] = {"app": "dst", **(_context.get() or {})}
    return f"{MARKER} {json.dumps(payload, sort_keys=True)} */ {query}"


def is_dst_query(statement: str) -> bool:
    """Whether a history record is dst's own traffic (tagged by this module)."""
    return MARKER in statement
