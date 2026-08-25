"""Agent-legible schema rendering — the OSS authoring surface.

dst does not draft the semantic layer; the user's agent does, through
`dst introspect` + the scaffolded skills. This module is the deterministic
half that survived the drafter's deletion — the
schema + stored-profile listing an agent grounds on when authoring `semantic/`
files. Profiles enrich what the agent sees (row counts, enum literals, null
rates, ranges); judgment stays on the agent's side.
"""

from __future__ import annotations

from services.contracts.profile import (
    EXACT_PROFILE_MAX_ROWS,
    ColumnProfile,
    TableProfile,
    profile_from_table_schema,
)
from services.contracts.semantic_model import FIELD_TYPES, warehouse_field_type
from services.contracts.warehouse import ColumnSchema, SchemaSnapshot, TableSchema

# The listing is authoring raw material, so the type it prints must be the type
# `fields[].type` accepts — printing the warehouse's own BIGINT/VARCHAR is what
# makes an agent author entity files that apply then rejects.
#
# One column per LINE, name and type split by ": ". The one-line-per-table form
# this replaced separated columns with ", " while printing types that contain
# ", " (STRUCT(a INTEGER, b VARCHAR)) and put no delimiter at all between a
# column's name and its type — so `customer id integer` read left to right
# authored `type: id`, which apply then rejected. `--json` is the contract for
# anything that parses rather than reads.
HEADER = (
    "# Schema for authoring semantic/entities/*.yaml. One column per line as\n"
    "#   - <name>: <fields[].type> (<warehouse type>)\n"
    "# — copy the former, never the latter (" + " | ".join(FIELD_TYPES) + ").\n"
    "# `--json` prints the same facts as a parseable object.\n"
)

_NOT_PROFILED = (
    "# NOT PROFILED{which}: schema only — no enum values, null rates or ranges.\n"
    "# Add `--profile` to read them now (row-capped reads, one pass per table in\n"
    "# scope — narrow it with --tables on a wide warehouse).\n"
)

# The sampling boundary, on screen. Behaviour changes qualitatively at this row
# count and nothing used to say so, so an author could not tell why one table's
# numbers were facts and another's were guesses.
_SAMPLED_NOTE = (
    "# SAMPLED{which}: over {threshold:,} rows, so a distinct count read from the\n"
    "# sample is a LOWER BOUND (`>=`), a value list may be missing rare members\n"
    "# (`Values (partial)`), and `range in sample` is a sub-range of the real one.\n"
    "# Everything unmarked was counted over every row.\n"
)


def _column_facts(profile: ColumnProfile | None, *, sampled: bool = False) -> str:
    """Compact profile suffix for one column.

    An estimate never prints as a fact. A counted cardinality prints bare
    (``distinct: 12333`` — equal to the row count, i.e. a key); one observed in a
    sample prints as the lower bound it is (``distinct: >=9800``). A value list
    that may be missing members says ``Values (partial)`` — silently dropping 2
    of 21 elements is worse than printing none, because the author treats the
    list as the column's whole dictionary.
    """
    if profile is None:
        return ""
    stats: list[str] = []
    if profile.top_values:
        label = "Values" if profile.values_complete else "Values (partial)"
        stats.append(f"{label}: " + ", ".join(f"'{v}'" for v in profile.top_values))
    elif profile.distinct_count is not None:
        prefix = "" if profile.distinct_is_exact else ">="
        stats.append(f"distinct: {prefix}{profile.distinct_count}")
    if profile.null_rate:  # any null at all is an authoring fact; 0.0 is not
        percent = profile.null_rate * 100
        stats.append(f"{'<1' if percent < 1 else round(percent)}% null")
    if profile.min is not None and profile.max is not None:
        # A sampled MIN/MAX is a sub-range of the real one, never the extremes.
        label = "range in sample" if sampled else "range"
        stats.append(f"{label}: {profile.min}..{profile.max}")
    return f" [{'; '.join(stats)}]" if stats else ""


def _column_type(raw: str) -> str:
    """`fields[].type` for this column, with the warehouse type kept alongside —
    the physical type still matters for dialect/cast decisions, it just must not
    be the thing an author copies into an entity file."""
    physical = raw.strip()
    mapped = warehouse_field_type(physical) or "string"
    return mapped if mapped.upper() == physical.upper() else f"{mapped} ({physical})"


def _listed(snapshot: SchemaSnapshot, tables: list[str] | None) -> list[TableSchema]:
    return [t for t in snapshot.tables if not tables or t.name in tables]


def _facts(
    table: TableSchema, profiles: dict[str, TableProfile]
) -> tuple[TableProfile, dict[str, ColumnProfile]]:
    """The profile facts to print for one table.

    Falls back to what `introspect()` already measured (`ColumnSchema.profile`
    carries DuckDB's per-column distinct counts, and every connector's row
    counts) when no profiling pass has stored one — those facts cost no extra
    I/O and were thrown away for as long as the listing existed.

    When BOTH exist, the exact number wins. Introspection already counted
    DuckDB's cardinalities exactly, so on a table too big to profile exactly
    `--profile` was replacing a real 12,333 with a sampled lower bound and
    making the more thorough mode the less accurate one.
    """
    from_schema = profile_from_table_schema(table, connection="")
    prof = profiles.get(table.name)
    if prof is None:
        return from_schema, {c.name: c for c in from_schema.columns}
    exact = {c.name: c for c in from_schema.columns if c.distinct_is_exact}
    columns = [_with_exact_distinct(c, exact.get(c.name)) for c in prof.columns]
    prof = prof.model_copy(update={"columns": columns})
    return prof, {c.name: c for c in prof.columns}


def _with_exact_distinct(column: ColumnProfile, known: ColumnProfile | None) -> ColumnProfile:
    """Overlay an exact cardinality introspection already counted onto a sampled column."""
    if known is None or known.distinct_count is None or column.distinct_is_exact:
        return column
    update: dict[str, object] = {
        "distinct_count": known.distinct_count,
        "distinct_is_exact": True,
        "is_low_cardinality": known.is_low_cardinality,
    }
    # A sampled value list of exactly as many members as the column really has
    # IS the whole dictionary — every member is a real value, and there are no
    # others to find.
    if column.top_values is not None and len(column.top_values) == known.distinct_count:
        update["values_complete"] = True
    return column.model_copy(update=update)


def _profiling_note(listed: list[TableSchema], profiled: dict[str, TableProfile]) -> str:
    """Say when the listing is schema only, and when it is sampled rather than counted.

    Nothing in the output used to distinguish "not profiled" from "no facts
    exist", so agents read a bare schema as a complete answer and concluded the
    documented profile facts were a lie. Nothing marked the sampling boundary
    either, so the same command told the truth about a 500-row table and guessed
    about a 12,333-row one with identical formatting.
    """
    note = ""
    missing = [t.name for t in listed if t.name not in profiled]
    if missing:
        which = "" if len(missing) == len(listed) else " (" + ", ".join(missing[:5]) + ")"
        note += _NOT_PROFILED.format(which=which)
    sampled = [t.name for t in listed if (p := profiled.get(t.name)) and p.sampled_rows is not None]
    if sampled:
        which = "" if len(sampled) == len(listed) else " (" + ", ".join(sampled[:5]) + ")"
        note += _SAMPLED_NOTE.format(which=which, threshold=EXACT_PROFILE_MAX_ROWS)
    return note


def _table_line(table: TableSchema, profile: TableProfile) -> str:
    """The table header: name, row count, whether it is a view, whether it was sampled.

    ``(~N rows)`` stays byte-identical to what it always was and the new marks go
    in a trailing bracket — the same shape a column line already uses, and the
    one place a reader can skip without losing the name or the row count.
    """
    row_count = profile.row_count if profile.row_count is not None else table.row_count
    line = f"- {table.name}" + (f" (~{row_count} rows)" if row_count is not None else "")
    marks = []
    is_view = profile.is_view if profile.is_view is not None else table.is_view
    if is_view:
        marks.append("VIEW")  # derived, not stored — nothing used to say so
    if profile.sampled_rows is not None:
        marks.append(f"SAMPLED {profile.sampled_rows} rows")
    return line + (f" [{'; '.join(marks)}]" if marks else "")


def serialize_schema(
    snapshot: SchemaSnapshot, profiles: list[TableProfile], tables: list[str] | None
) -> str:
    """The schema listing an authoring agent grounds on, enriched with profile facts."""
    by_table = {p.table: p for p in profiles}
    listed = _listed(snapshot, tables)
    lines = []
    for t in listed:
        prof, col_profiles = _facts(t, by_table)
        lines.append(_table_line(t, prof))
        lines.extend(
            f"  - {c.name}: {_column_type(c.type)}"
            + (f" — {c.description}" if c.description else "")
            + _column_facts(col_profiles.get(c.name), sampled=prof.sampled_rows is not None)
            for c in t.columns
        )
    if not lines:
        return ""
    truncation = (
        f"# TRUNCATED: the listing stopped at {len(snapshot.tables)} tables — the\n"
        "# warehouse has more. Scope the connection (`datasets:` under its config in\n"
        "# dst.yaml) or name tables with --tables (matched against the FULL catalog).\n"
        if snapshot.truncated
        else ""
    )
    return HEADER + truncation + _profiling_note(listed, by_table) + "\n".join(lines)


def schema_json(
    snapshot: SchemaSnapshot, profiles: list[TableProfile], tables: list[str] | None
) -> dict[str, object]:
    """The same listing as structure — the contract `dst introspect --json` prints.

    Prose parsing is the disease: no separator survives a warehouse that can put
    ", " inside a type and a space inside a name. `type` is the `fields[].type`
    value to author, `warehouse_type` the physical one.
    """
    by_table = {p.table: p for p in profiles}
    listed = _listed(snapshot, tables)
    out: list[dict[str, object]] = []
    for t in listed:
        prof, col_profiles = _facts(t, by_table)
        out.append(
            {
                "name": t.name,
                "row_count": prof.row_count if prof.row_count is not None else t.row_count,
                "description": t.description or prof.description,
                "profiled": t.name in by_table,
                "is_view": prof.is_view if prof.is_view is not None else t.is_view,
                # None = every row was counted; a number = that many rows were
                # sampled and the column facts below are estimates.
                "sampled_rows": prof.sampled_rows,
                "columns": [_column_json(c, col_profiles.get(c.name)) for c in t.columns],
            }
        )
    return {
        "dialect": snapshot.dialect,
        "schemas_searched": list(snapshot.schemas_searched),
        "truncated": snapshot.truncated,
        "profiled": all(t.name in by_table for t in listed),
        "exact_profile_max_rows": EXACT_PROFILE_MAX_ROWS,
        "tables": out,
    }


def _column_json(column: ColumnSchema, profile: ColumnProfile | None) -> dict[str, object]:
    out: dict[str, object] = {
        "name": column.name,
        "type": warehouse_field_type(column.type.strip()) or "string",
        "warehouse_type": column.type.strip(),
        "nullable": column.nullable,
    }
    description = column.description or (profile.description if profile else None)
    if description:
        out["description"] = description
    if profile is not None:
        facts: dict[str, object | None] = {
            "values": profile.top_values,
            "distinct_count": profile.distinct_count,
            "null_rate": profile.null_rate,
            "min": profile.min,
            "max": profile.max,
        }
        out.update({k: v for k, v in facts.items() if v is not None})
        # The qualifiers ride alongside the numbers they qualify, always present
        # when the number is: a parser must never have to infer whether
        # `distinct_count` is a count or a guess, or whether `values` is the whole
        # dictionary. False for `distinct_is_exact` makes the count a lower bound.
        if profile.distinct_count is not None:
            out["distinct_is_exact"] = profile.distinct_is_exact
        if profile.top_values is not None:
            out["values_complete"] = profile.values_complete
    return out


def _searched_label(snapshot: SchemaSnapshot) -> str:
    """The searched scope, bounded — 150 dataset names in an error message read
    as the problem itself, and read as a permissions failure."""
    names = snapshot.schemas_searched
    if not names:
        return "(the connection's default scope)"
    if len(names) <= 10:
        return ", ".join(names)
    return ", ".join(names[:10]) + f", … ({len(names) - 10} more)"


def empty_listing_reason(
    connection: str, snapshot: SchemaSnapshot, tables: list[str] | None
) -> str | None:
    """Why the listing came out empty, or None when it didn't.

    Introspection must never answer "nothing" quietly: an agent pointed at a
    perfectly good warehouse that gets a blank line and exit 0 abandons the
    documented authoring path. Callers turn this into a stderr line and a
    non-zero exit / a 404."""
    searched = _searched_label(snapshot)
    if not snapshot.tables:
        return (
            f"connection '{connection}' has no tables in the schemas searched: {searched}. "
            "Check the connection's config in dst.yaml (path/database/dataset), or name "
            "the schema explicitly with `schema:` under its config."
        )
    if tables and not any(t.name in tables for t in snapshot.tables):
        found = ", ".join(sorted(t.name for t in snapshot.tables)[:20])
        capped = (
            f" The listing was CAPPED at {len(snapshot.tables)} tables before matching — "
            "scope the connection (`datasets:` under its config) and retry."
            if snapshot.truncated
            else ""
        )
        return (
            f"--tables matched none of the {len(snapshot.tables)} tables in connection "
            f"'{connection}' (searched {searched}).{capped} Names are qualified as "
            f"introspect prints them: {found}"
        )
    return None
