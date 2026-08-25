"""Join-key inference between the tables of a scope.

A name/type match (``customer_id`` ↔ ``customers``) is only a *hypothesis* until a
push-down overlap probe confirms it: one SQL query per candidate computes the
containment ratio of child distinct values within the parent key — only the ratio
leaves the warehouse. Confirmed candidates persist as
`JoinCandidate` rows for human approval; approval writes a `Join` onto the lens's
semantic model. FK- and dbt-evidenced candidates outrank heuristics and skip the probe.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from services.contracts.profile import JoinCandidate, TableProfile
from services.contracts.protocols import Connector
from services.contracts.semantic_model import Join
from services.lenses import profile_store
from services.lenses.store import LensBundle

log = logging.getLogger("dst")

# A heuristic candidate is kept only when at least this share of the child column's
# distinct values exist in the parent column (NULLs excluded on both sides).
MIN_OVERLAP = 0.95
# And only when the parent column looks key-like: distinct/rows at or above this.
MIN_KEY_UNIQUENESS = 0.9

_NUMERIC_TYPES = {"int", "bigint", "integer", "smallint", "decimal", "numeric", "double", "float"}


def _base_name(table: str) -> str:
    return table.split(".")[-1].lower()


def _stems(column: str) -> list[str]:
    """'customer_id' -> plausible parent-table stems ('customer', 'customers', …)."""
    if not column.lower().endswith("_id"):
        return []
    stem = column.lower()[:-3]
    return [stem, stem + "s", stem + "es"]


def _type_compatible(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    a_num = any(t in a for t in _NUMERIC_TYPES)
    b_num = any(t in b for t in _NUMERIC_TYPES)
    return a_num == b_num


def hypothesize(profiles: list[TableProfile]) -> list[tuple[str, str, str, str]]:
    """Name/type join hypotheses: (left_table, left_col, right_table, right_col)."""
    out: list[tuple[str, str, str, str]] = []
    for left in profiles:
        left_cols = {c.name: c for c in left.columns}
        for col in left.columns:
            stems = _stems(col.name)
            if not stems:
                continue
            for right in profiles:
                if right.table == left.table:
                    continue
                if _base_name(right.table) not in stems:
                    continue
                right_cols = {c.name: c for c in right.columns}
                rcol = right_cols.get("id") or right_cols.get(col.name)
                if rcol is None or not _type_compatible(col.type, rcol.type):
                    continue
                # avoid the degenerate self-pair where both sides are the same column
                # of a staging duplicate; keep it — overlap decides.
                out.append((left.table, left_cols[col.name].name, right.table, rcol.name))
    return out


def overlap_ratio(
    connector: Connector, left_table: str, left_col: str, right_table: str, right_col: str
) -> float | None:
    """Push-down containment: share of child distinct values present in the parent.

    One aggregate row leaves the warehouse — never raw values."""
    sql = (
        f"SELECT COUNT(DISTINCT {left_col}) * 1.0 / "  # noqa: S608 — identifiers from introspection
        f"NULLIF((SELECT COUNT(DISTINCT {left_col}) FROM {left_table} "
        f"WHERE {left_col} IS NOT NULL), 0) "
        f"FROM {left_table} "
        f"WHERE {left_col} IS NOT NULL AND {left_col} IN "
        f"(SELECT {right_col} FROM {right_table} WHERE {right_col} IS NOT NULL)"
    )
    try:
        result = connector.execute(sql, read_only=True, row_limit=1)
    except Exception:
        log.exception("overlap probe failed for %s.%s -> %s", left_table, left_col, right_table)
        return None
    if not result.rows or result.rows[0][0] is None:
        return None
    try:
        return float(result.rows[0][0])  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _key_like(profile: TableProfile | None, column: str) -> bool:
    """True unless the profile proves the parent column is not key-like."""
    if profile is None or profile.row_count in (None, 0):
        return True  # no evidence either way — let the overlap probe decide
    col = next((c for c in profile.columns if c.name == column), None)
    if col is None or col.distinct_count is None:
        return True
    assert profile.row_count is not None
    return (col.distinct_count / profile.row_count) >= MIN_KEY_UNIQUENESS


def infer_join_candidates(
    session: Session,
    connector: Connector,
    connection: str,
    *,
    tables: list[str] | None = None,
    min_overlap: float = MIN_OVERLAP,
) -> list[JoinCandidate]:
    """Probe name/type hypotheses for a connection's (optionally scoped) tables and
    persist the confirmed ones as candidates. Returns the newly recorded candidates."""
    stored = profile_store.list_profiles(session, connection)
    profiles = [s.profile for s in stored]
    if tables:
        wanted = set(tables)
        profiles = [p for p in profiles if p.table in wanted]
    by_table = {p.table: p for p in profiles}
    existing = {
        (c.left_table, tuple(c.left_columns), c.right_table, tuple(c.right_columns))
        for c in profile_store.list_candidates(session, connection)
    }
    confirmed: list[JoinCandidate] = []
    for left_table, left_col, right_table, right_col in hypothesize(profiles):
        key = (left_table, (left_col,), right_table, (right_col,))
        if key in existing:
            continue
        if not _key_like(by_table.get(right_table), right_col):
            continue
        ratio = overlap_ratio(connector, left_table, left_col, right_table, right_col)
        if ratio is None or ratio < min_overlap:
            continue
        confirmed.append(
            JoinCandidate(
                left_table=left_table,
                left_columns=[left_col],
                right_table=right_table,
                right_columns=[right_col],
                evidence="name+overlap",
                overlap_ratio=round(ratio, 3),
            )
        )
    if confirmed:
        profile_store.record_candidates(session, connection, confirmed)
    return confirmed


def approve_into_model(bundle: LensBundle, candidate: JoinCandidate) -> bool:
    """Write an approved candidate as a `Join` on the bundle's semantic model.

    Maps physical tables to the lens's entities; returns False when either table is
    outside the lens's scope (the candidate may still be approved store-side)."""
    by_table = {e.source.table: e for e in bundle.semantic_model.entities}
    left = by_table.get(candidate.left_table)
    right = by_table.get(candidate.right_table)
    if left is None or right is None:
        return False
    on = " AND ".join(
        f"{left.name}.{lc} = {right.name}.{rc}"
        for lc, rc in zip(candidate.left_columns, candidate.right_columns, strict=True)
    )
    for existing in bundle.semantic_model.joins:
        if {existing.left, existing.right} == {left.name, right.name} and existing.on == on:
            return True  # already present — idempotent
    bundle.semantic_model.joins.append(Join(left=left.name, right=right.name, on=on, type="left"))
    return True
