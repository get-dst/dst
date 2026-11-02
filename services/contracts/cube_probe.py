"""What the probe and the monitor record about the data behind a Cube model.

``profiles/cube.probe.json`` is machine-owned: the probe fills it through
Cube's own queries and the monitor refreshes it on a cadence. The cube lane's
checks read it as declared facts nobody downstream can infer: where each time
dimension's data ends, which rows are dated after the lens clock, where each
feed stream inside one table stops, which periods are only partly loaded, and
the observed values of every dictionary dimension.

Keys are Cube member names (``cube.member``) and cube names. Unknown keys are
ignored, so a newer monitor's file still reads on an older server.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TimeDimensionFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # "event" = the date something happened (clipped to the clock, checked for
    # gaps); "schedule" = a date something is due (may lie in the future).
    kind: Literal["event", "schedule"] = "event"
    data_end: str | None = None
    max: str | None = None
    future_rows: int = 0
    stopped: bool = False


TimeKind = Literal["event", "schedule"]

# What a date column's name or prose says it records. An event (the day
# something happened) cannot lie after the lens clock: rows dated there are
# wrong or not yet true, and every window on it is clipped. A schedule (the
# day something is due, ends or is planned) can. Words only, the closed lists
# below: a name that carries neither, or both, says nothing.
EVENT_WORDS = frozenset(
    "decided closed created paid sent ordered invoiced issued booked completed signed "
    "shipped delivered resolved submitted posted registered cancelled canceled refunded "
    "won lost approved received placed logged occurred recorded collected settled".split()
)
SCHEDULE_WORDS = frozenset(
    "due end ends planned plan scheduled expires expiry expiration expiring deadline "
    "renewal renews until forecast target budget".split()
)


def _sides(text: str) -> tuple[bool, bool]:
    words = set(re.findall(r"[a-z]+", text.lower().replace("_", " ")))
    return bool(words & EVENT_WORDS), bool(words & SCHEDULE_WORDS)


def date_meaning(name: str, *prose: str | None) -> TimeKind | None:
    """Event or schedule as the column's own name says (``decided_date``,
    ``due_date``), else as its prose says (a title, a description, a
    blueprint clock's ``meaning``); None when neither says one alone."""
    for text in (name, " ".join(p for p in prose if p)):
        event, schedule = _sides(text)
        if event != schedule:
            return "event" if event else "schedule"
    return None


def time_kind(declared: str | None, future_rows: bool, meaning: str | None = None) -> TimeKind:
    """What a time dimension's dates are: the overlay's ``clock_kind`` when
    declared, else what the column's meaning says (``date_meaning``), else a
    schedule when rows are dated after the clock (an event cannot have
    happened yet; a due or end date can lie ahead), else an event. A
    measure's clock is not an event by that alone: contracts can be counted
    on the day they end."""
    if declared in ("event", "schedule"):
        return declared  # type: ignore[return-value]
    if meaning in ("event", "schedule"):
        return meaning  # type: ignore[return-value]
    return "schedule" if future_rows else "event"


def kind_basis(declared: str | None, meaning: str | None) -> str:
    """Why ``time_kind`` decided: declared, meaning or future_rows."""
    if declared in ("event", "schedule"):
        return "declared"
    return "meaning" if meaning in ("event", "schedule") else "future_rows"


# The value a feed axis carries on rows that have none: those rows are a
# stream of their own, read where the axis is not set.
NO_VALUE = "(none)"


class Stream(BaseModel):
    """One feed inside a table: the rows carrying ``values`` on the cube's
    feed axes (one source system per entity, say), and where they end; a
    row with no value on an axis carries ``NO_VALUE`` there."""

    model_config = ConfigDict(extra="ignore")

    values: dict[str, str] = Field(default_factory=dict)
    end: str | None = None
    stopped: bool = False


class CubeFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    clock: str | None = None
    feed_axes: list[str] = Field(default_factory=list)
    streams: list[Stream] = Field(default_factory=list)
    time_dimensions: dict[str, TimeDimensionFacts] = Field(default_factory=dict)
    # Records of this cube the monitor's file holds and the loader could not
    # read (skipped, each said here): the checks on this cube disclose them.
    warnings: list[str] = Field(default_factory=list)


class PartialPeriod(BaseModel):
    """A period loaded well below its own history: the latest month against
    the median of the full months before it, pro-rated to the days loaded."""

    model_config = ConfigDict(extra="ignore")

    period: str
    partial_from: str
    partial_through: str
    partial_ratio: float
    trailing_median: float = 0.0
    trailing_months: int = 0
    value: float = 0.0
    values: dict[str, str] = Field(default_factory=dict)
    # The feed's measure that found the period partial, when not this one
    # (the cube's ``feed_measures``, else its row count).
    judged_by: str | None = None
    # What beyond volume says the period is not fully loaded (an open period,
    # a last load before its end, no rows on its last days), or that it is
    # complete and still maturing.
    evidence: str | None = None
    # The evidence is of loading (a stamp, an open period, day coverage): the
    # period is partially loaded. False: judged on volume alone, or maturing.
    load_evidence: bool = False
    # The period's rows are complete; the overlay declares this measure's
    # values final only ``matures_after`` days past the period, not yet reached.
    maturing: str | None = None


class MeasureFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cube: str
    clock: str | None = None
    type: str | None = None
    # Feed-axis values the measure's own definition pins (a measure that sums
    # one revenue stream reads only that stream's feed).
    streams_pinned: dict[str, list[str]] = Field(default_factory=dict)
    partial: PartialPeriod | None = None
    partial_streams: list[PartialPeriod] = Field(default_factory=list)


class CubeProbe(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # The last loaded day: the lens clock every window is read against.
    clock_end: str | None = None
    stale_after_days: int = 3
    cubes: dict[str, CubeFacts] = Field(default_factory=dict)
    measures: dict[str, MeasureFacts] = Field(default_factory=dict)
    dictionaries: dict[str, list[str]] = Field(default_factory=dict)

    def clock(self) -> date | None:
        return date.fromisoformat(self.clock_end[:10]) if self.clock_end else None
