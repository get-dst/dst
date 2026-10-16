"""Feed table profiles into generation.

The stored profile enriches the lens's *effective* semantic model right before
generation: sampled/warehouse column descriptions fill documentation gaps, enum
literals land next to their columns ("Values: 'active', 'churned'"), and partitioned
tables carry a pruning hint — all rendered into the generator's system prompt by the
existing `serialize_model`, no QueryGenerator seam change. Compact shape stats ride
along as suffixes: null rate, distinct count, min..max range, row count.
`data_as_of` is the scope's honest freshness floor, stated on every answer.

What rides the prompt is what the profiling pass collected: a column excluded
from literal collection (`exclude_columns`) has no top_values and no range to
render, so the exclusion carries through to generation without a second filter
here. The prompt is a broadcast of values sampled from the whole table, unrelated
to the rows any caller asked for, and it leaves the building for a third-party
LLM — which is why the decision about what may be sampled is made once, at
collection, and not per caller. `dst lens prompt` renders exactly what every
caller's model is sent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from services.contracts.profile import TableProfile, TimeCoverage, prompt_values
from services.contracts.semantic_model import SemanticModel

# A null rate below this is noise, not signal — it renders nothing.
NULL_RATE_MIN = 0.05

# Field types where a min..max range is meaningful in a prompt.
_RANGE_TYPES = frozenset({"date", "timestamp", "number", "integer"})


def _coverage_note(tc: TimeCoverage) -> str:
    """The measured span as prompt prose — snapshot tables say their AS-OF."""
    lo, hi = tc.min.date().isoformat(), tc.max.date().isoformat()
    if lo == hi:
        return f"single snapshot as of {hi} — this table holds one day of data"
    return f"data covers {lo}..{hi} — no rows exist beyond this span"


def _compact_rows(n: int) -> str:
    """1_234_567 -> '1.2M'; small counts stay exact."""
    for div, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


def enrich_model(model: SemanticModel, profiles: list[TableProfile]) -> SemanticModel:
    """A copy of the model with profile facts folded into entity/field descriptions."""
    by_table = {p.table: p for p in profiles}
    entities = []
    changed = False
    for entity in model.entities:
        prof = by_table.get(entity.source.table)
        if prof is None:
            entities.append(entity)
            continue
        col_profiles = {c.name: c for c in prof.columns}
        tc = prof.time_coverage
        fields = []
        for field in entity.fields:
            cp = col_profiles.get(field.name)
            covers = _coverage_note(tc) if tc is not None and field.name == tc.column else None
            if cp is None and covers is None:
                fields.append(field)
                continue
            desc = field.description or (cp.description if cp is not None else None)
            stats: list[str] = []
            if covers is not None:
                # The measured span rides the time field itself — both prompt
                # tiers render field descriptions, so the generator sees where
                # the data ends before it writes a period filter.
                stats.append(covers)
            if cp is not None:
                if cp.value_shape and cp.access_hint:
                    # The shape rule outranks value lists — a model that
                    # compares a raw JSON column to a plain string serves nothing.
                    stats.append(cp.access_hint)
                if shown := prompt_values(cp):
                    # A partial dictionary presented as complete is worse than none:
                    # the model writes `WHERE element IN (…)` and silently drops rows.
                    # A dictionary longer than an enum is for value matching, not
                    # for the prompt: the column reads as its distinct count.
                    label = "Values" if cp.values_complete else "Values (partial)"
                    stats.append(f"{label}: " + ", ".join(f"'{v}'" for v in shown))
                elif cp.distinct_count is not None:  # high-cardinality signal
                    prefix = "" if cp.distinct_is_exact else ">="
                    stats.append(f"distinct: {prefix}{cp.distinct_count}")
                if cp.null_rate is not None and cp.null_rate >= NULL_RATE_MIN:
                    stats.append(f"~{round(cp.null_rate * 100)}% null")
                if (
                    covers is None  # the exact coverage span supersedes a sampled range
                    and cp.min is not None
                    and cp.max is not None
                    and field.type in _RANGE_TYPES
                ):
                    stats.append(f"range: {cp.min}..{cp.max}")
            for stat in stats:
                desc = f"{desc} — {stat}" if desc else stat
            if desc != field.description:
                field = field.model_copy(update={"description": desc})
                changed = True
            fields.append(field)
        description = entity.description
        if not description and prof.description:
            # Same rule as columns: hand-authored wins, the warehouse
            # table comment fills the blank.
            description = prof.description
            changed = True
        if prof.row_count is not None:
            note = f"(~{_compact_rows(prof.row_count)} rows)"
            if note not in (description or ""):
                description = f"{description} {note}" if description else note
                changed = True
        if prof.partitioning is not None and prof.partitioning.column:
            hint = (
                f"Partitioned by {prof.partitioning.column} — filter on it "
                "when a time range is asked (partition pruning)."
            )
            if hint not in (description or ""):
                description = f"{description} {hint}" if description else hint
                changed = True
        entities.append(entity.model_copy(update={"fields": fields, "description": description}))
    if not changed:
        return model
    return model.model_copy(update={"entities": entities})


@dataclass(frozen=True)
class EntityCoverage:
    """One entity's measured date coverage — the profile's whole-table
    MIN/MAX of its source table's best time column, resolved to the entity so
    the serve-time check can name what the caller asked about. ``declared``
    records whether the measured column IS the entity's own
    ``default_time_field``: the "data begins" direction fires only then (an
    ETL load stamp's MIN says when loading started, not where history does)."""

    table: str
    column: str
    start: date
    end: date
    declared: bool
    # False when the entity declares no ``default_time_field`` at all — a
    # dimension table. Its business date (a customer's created_at) says when
    # members were added, never which periods were loaded, so it can skip a
    # coverage check with its reason but never fail one.
    time_series: bool = True

    @property
    def snapshot(self) -> bool:
        """A single day of data — the table is a snapshot and ``end`` its AS-OF."""
        return self.start == self.end


def entity_coverage(
    model: SemanticModel, profiles: list[TableProfile]
) -> dict[str, EntityCoverage]:
    """Per-entity date coverage from the stored profiles — {} when unmeasured."""
    by_table = {p.table: p for p in profiles}
    out: dict[str, EntityCoverage] = {}
    for entity in model.entities:
        prof = by_table.get(entity.source.table)
        tc = prof.time_coverage if prof is not None else None
        if tc is None:
            continue
        out[entity.name] = EntityCoverage(
            table=entity.source.table,
            column=tc.column,
            start=tc.min.date(),
            end=tc.max.date(),
            declared=entity.default_time_field == tc.column,
            time_series=entity.default_time_field is not None,
        )
    return out


def data_as_of(
    profiles: list[TableProfile],
    tables: set[str],
    time_fields: dict[str, str | None] | None = None,
) -> datetime | None:
    """The scope's freshness floor: the *oldest* last-update across its tables —
    the honest "data as of" (logical freshness preferred over physical).

    Logical freshness is the newest value of a time column. It is freshness only
    for a table the semantic layer declares as a time series (its entity names a
    ``default_time_field``; ``time_fields``: table -> that field). A reference
    table's dates are ATTRIBUTES — a patch's release date, a customer's signup
    date: taken as a load time, every answer over a fresh warehouse was dated to
    the last release and called stale. Such a table falls back to physical
    freshness, or to no claim at all. Without ``time_fields`` every stamp counts."""

    def stamp(p: TableProfile) -> datetime | None:
        logical = p.last_updated_logical
        if time_fields is not None and not time_fields.get(p.table):
            logical = None
        return logical or p.last_updated_physical

    stamps = [s for p in profiles if p.table in tables and (s := stamp(p)) is not None]
    return min(stamps) if stamps else None


def lens_today(timezone: str | None) -> date:
    """Today on the lens clock (UTC when the lens declares no timezone)."""
    try:
        return datetime.now(ZoneInfo(timezone)).date() if timezone else datetime.now(UTC).date()
    except Exception:
        return datetime.now(UTC).date()


def clamp_as_of(stamp: str, today: date) -> tuple[str, str | None]:
    """``(as-of date, the future date it hid)``. A time column whose newest value
    lies ahead of the clock — bookings, scheduled deliveries, forecast rows —
    does not make the data fresher than today: "data as of" is a freshness
    claim, and the newest row a loader can have written is dated today at the
    latest. The hidden horizon is returned so the answer can still say how far
    ahead the rows run; a stamp on or before today passes through untouched."""
    try:
        when = date.fromisoformat(stamp[:10])
    except ValueError:
        return stamp, None
    if when <= today:
        return stamp, None
    return today.isoformat(), when.isoformat()


def clamp_table_as_of(
    stamps: Mapping[str, str], today: date
) -> tuple[dict[str, str], dict[str, str]]:
    """`clamp_as_of` over a table -> as-of map: the clamped map and, per
    table that was clamped, the future date its rows run to."""
    clamped: dict[str, str] = {}
    horizons: dict[str, str] = {}
    for table, stamp in stamps.items():
        clamped[table], ahead = clamp_as_of(stamp, today)
        if ahead is not None:
            horizons[table] = ahead
    return clamped, horizons


def table_as_of(
    profiles: list[TableProfile], time_fields: Mapping[str, str | None]
) -> dict[str, str]:
    """table -> its own as-of date (ISO), by `data_as_of`'s rule per table —
    the stamps the serving path dates an answer from, once it knows which
    tables the served SQL read."""
    out: dict[str, str] = {}
    for p in profiles:
        if p.table not in time_fields:
            continue
        stamp = data_as_of([p], {p.table}, dict(time_fields))
        if stamp is not None:
            out[p.table] = stamp.date().isoformat()
    return out


@dataclass(frozen=True)
class ReadTable:
    """One table the served SQL reads, as the guard resolved it: its source
    name, the name the answer may call it by, and whether its entity declares
    a clock (``default_time_field``) — a time series dates an answer, a
    dimension or reference table only when nothing else was read."""

    table: str
    name: str
    dated: bool


def scope_as_of(
    stamps: Mapping[str, str], read: Iterable[ReadTable]
) -> tuple[str | None, str | None]:
    """``(as-of date, the table it names)`` over the tables the SQL actually
    read — the oldest of them, never the oldest in the lens. A lens-wide floor
    dated every answer to its slowest table; a question over orders alone is
    as fresh as orders. Time-series tables decide; a dimension (no declared
    clock) dates the answer only when the read set holds nothing else. The
    named table is always given, so the caller can say "orders" when one
    table was read and name the oldest when several were."""
    tables = list(read)
    dated = [t for t in tables if t.dated and t.table in stamps]
    pool = dated or [t for t in tables if t.table in stamps]
    if not pool:
        return None, None
    oldest = min(pool, key=lambda t: (stamps[t.table], t.name))
    return stamps[oldest.table], oldest.name
