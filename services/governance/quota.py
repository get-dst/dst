"""Daily quotas — governed answers served in the last 24 hours, counted from request_log.

The per-minute limiter (ratelimit.py) bounds a burst; it says nothing about a
caller who stays under it all day. This is the other bound: how many governed
answers one caller, or the whole org, may draw in a rolling day. It counts rows
that were actually served, so a decline written to audit_log never eats budget
and a quota refusal never inflates the count it reads.

Counted in Postgres rather than in memory because the day outlives a process and
a fleet shares one request_log. The trace row lands in a background task after
the response, so N in-flight requests can all read the same pre-insert count — the
per-minute limiter is what bounds that window. Both caps are enforced at the same
point as the per-minute limit and refuse the same way: 429, Retry-After, audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text

from services.db.session import org_session

WINDOW_SECONDS = 24 * 3600

# Admin passthrough rows are not governed answers (observe.py draws the same line).
_GOVERNED = "COALESCE(generator_tier, '') <> 'probe'"
_WINDOW = "created_at >= NOW() - make_interval(secs => :w)"


@dataclass(frozen=True)
class Usage:
    served: int
    oldest: datetime | None  # the row that leaves the window first

    def retry_after(self, *, now: datetime | None = None) -> int:
        """Seconds until the oldest counted row ages out — the earliest moment the
        next request could be under the cap."""
        if self.oldest is None:
            return WINDOW_SECONDS
        t = now or datetime.now(UTC)
        elapsed = (t - self.oldest).total_seconds()
        return max(1, int(WINDOW_SECONDS - elapsed) + 1)


def usage(
    org_id: uuid.UUID | str,
    *,
    caller: str | None = None,
    lens: str | None = None,
) -> Usage:
    """Governed answers served in the window for the org, narrowed to a caller
    and/or a lens. Runs under the org GUC — request_log is FORCE RLS, and a count
    taken without it is a silent zero that would never throttle anyone."""
    clauses = [_GOVERNED, _WINDOW]
    params: dict[str, object] = {"w": WINDOW_SECONDS}
    if caller is not None:
        clauses.append("caller = :c")
        params["c"] = caller
    if lens is not None:
        clauses.append("lens = :l")
        params["l"] = lens
    where = " AND ".join(clauses)
    with org_session(org_id) as session:
        row = session.execute(
            text(f"SELECT count(*), min(created_at) FROM request_log WHERE {where}"),
            params,
        ).first()
    if row is None:
        return Usage(served=0, oldest=None)
    served, oldest = int(row[0] or 0), row[1]
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    return Usage(served=served, oldest=oldest)


def exceeded(cap: int, use: Usage) -> bool:
    """`cap <= 0` means no quota — the same convention as `per_caller_rpm`."""
    return cap > 0 and use.served >= cap
