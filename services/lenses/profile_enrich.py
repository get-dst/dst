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

from dataclasses import dataclass
from datetime import date, datetime

from services.contracts.profile import TableProfile, TimeCoverage
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
                if cp.top_values:
                    # A partial dictionary presented as complete is worse than none:
                    # the model writes `WHERE element IN (…)` and silently drops rows.
                    label = "Values" if cp.values_complete else "Values (partial)"
                    stats.append(f"{label}: " + ", ".join(f"'{v}'" for v in cp.top_values))
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
        )
    return out


def data_as_of(profiles: list[TableProfile], tables: set[str]) -> datetime | None:
    """The scope's freshness floor: the *oldest* last-update across its tables —
    the honest "data as of" (logical freshness preferred over physical)."""
    stamps = [
        p.last_updated_logical or p.last_updated_physical
        for p in profiles
        if p.table in tables and (p.last_updated_logical or p.last_updated_physical)
    ]
    return min(stamps) if stamps else None  # type: ignore[type-var]
