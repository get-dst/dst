"""How old the stored profiles are, against the project's ``probe.max_age``.

A profile is a snapshot: a value dictionary proves a literal absent only as of
the probe that read it, and ``data_as_of`` dates the warehouse to that same
probe. Nothing at serve time used to read ``profiled_at``, so a stale snapshot
proved a NEW value absent (a false clarification from a guard whose contract
forbids false positives) and dated a fresh warehouse to the last probe. The
user owns the probe clock; this module is how dst notices it stopped.

One fact, every rail: an answer carries a PROFILE line, the freshness and
date-coverage checks skip, the value guard consults no dictionary, observe and
readiness carry the age, the test sweep refuses to verify against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from services.contracts.profile import TableProfile

# The reason a check gives when it abstains over a stale profile.
STALE_REASON = "profile older than max_age; freshness not measured"


def _days(delta: timedelta) -> float:
    return round(delta.total_seconds() / 86_400, 1)


def _fmt(days: float) -> str:
    return f"{days:g}"


@dataclass(frozen=True)
class ProfileAge:
    """``age`` is now minus the NEWEST ``profiled_at`` in scope (None: nothing
    profiled); ``max_age`` is the declared limit (None: the rails are off)."""

    age: timedelta | None
    max_age: timedelta | None

    @property
    def stale(self) -> bool:
        return self.age is not None and self.max_age is not None and self.age > self.max_age

    @property
    def age_days(self) -> float | None:
        return None if self.age is None else _days(self.age)

    @property
    def max_age_days(self) -> float | None:
        return None if self.max_age is None else _days(self.max_age)

    def line(self) -> str:
        """The degraded line an answer carries — same idiom as UNTYPED/EXPOSED."""
        assert self.age is not None and self.max_age is not None
        return (
            f"PROFILE: last probed {_fmt(_days(self.age))} days ago "
            f"(max {_fmt(_days(self.max_age))}) — run dst probe"
        )


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def profile_age(
    profiles: list[TableProfile],
    max_age: timedelta | None,
    *,
    tables: set[str] | None = None,
    now: datetime | None = None,
) -> ProfileAge:
    """The age of the scope's newest profile. ``tables`` narrows to a scope
    (an answer's entities); None takes every profile given (a connection)."""
    stamps = [_aware(p.profiled_at) for p in profiles if tables is None or p.table in tables]
    if not stamps:
        return ProfileAge(age=None, max_age=max_age)
    moment = now or datetime.now(UTC)
    return ProfileAge(age=max(timedelta(0), moment - max(stamps)), max_age=max_age)


def connection_ages(session: Session) -> dict[str, object]:
    """The org's profile age by connection, for observe and readiness:
    ``{oldest_age_days, max_age_days, stale_connections}``. The oldest is the
    age of the connection whose newest profile is furthest back — the number an
    operator needs to decide whether the probe job is running at all."""
    from services.lenses import connection_store, profile_store
    from services.project import probe_settings

    max_age = probe_settings.load(session).max_age_delta
    oldest: float | None = None
    stale: list[str] = []
    for record in connection_store.list_connections(session):
        profiles = [s.profile for s in profile_store.list_profiles(session, record.name)]
        age = profile_age(profiles, max_age)
        if age.age_days is None:
            continue
        oldest = age.age_days if oldest is None else max(oldest, age.age_days)
        if age.stale:
            stale.append(record.name)
    return {
        "oldest_age_days": oldest,
        "max_age_days": None if max_age is None else _days(max_age),
        "stale_connections": sorted(stale),
    }


def headline(status: dict[str, object]) -> str | None:
    """`dst observe`'s one line, or None when nothing is profiled."""
    oldest = status.get("oldest_age_days")
    if oldest is None:
        return None
    limit = status.get("max_age_days")
    cap = f" (max {_fmt(float(str(limit)))} d)" if limit is not None else " (no max_age)"
    stale = status.get("stale_connections")
    tail = f" — stale: {', '.join(stale)}" if isinstance(stale, list) and stale else ""
    return f"profiles: oldest {_fmt(float(str(oldest)))} d{cap}{tail}"
