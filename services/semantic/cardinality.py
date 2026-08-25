"""Measure a join's real cardinality, because the compiler believes what it is told.

`Join.relationship` stopped being documentation when the intent compiler started
using it to decide whether a join may be emitted at all: a hop declared
`many_to_one` is joined directly, and if that declaration is a lie every additive
aggregate downstream is silently multiplied: mis-declare one slowly-changing-dimension
join and a true 1,359,168 in deposits is reported as 9,514,176.

Declarations are wrong often enough to matter, and not because authors are careless:
across BIRD's 11 databases, **8 of 101 foreign keys are not one-to-many in the data**,
so anything that infers cardinality from an FK — including dst's own dbt import, which
stamps `many_to_one` unconditionally — is wrong about 8% of the time.

So measure it. Two counts per join, no sampling, no heuristics:

    max rows sharing one key on each side  ->  1:1, N:1, 1:N, or N:N

`many_to_many` has no representation on the contract and no safe direct join; the
caller reports it as a modelling problem rather than inventing a relationship.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

# Runs a SQL string and returns rows. Deliberately a callable rather than a connector
# class — introspect has a live connector, tests have a duckdb cursor, and neither
# should have to know about the other.
Execute = Callable[[str], list[tuple[Any, ...]]]

RELATIONSHIPS = ("one_to_one", "many_to_one", "one_to_many", "many_to_many")


@dataclass(frozen=True)
class Measured:
    """What the data says about one declared join."""

    left: str
    right: str
    relationship: str | None  # None when the tables could not be read
    left_max: int
    right_max: int
    error: str | None = None

    @property
    def fans_out_left(self) -> bool:
        """Would joining right onto left duplicate left's rows?"""
        return self.right_max > 1


def _max_rows_per_key(execute: Execute, table: str, columns: Sequence[str]) -> int:
    """The largest number of rows sharing one value of the key. 0 for an empty table.

    The key is the FULL tuple of columns the join matches on. Measuring one column of
    a composite key is not a conservative approximation, it is a wrong answer in the
    dangerous direction: `bitcoin_prices` has two rows per `market_date` and exactly
    one per `(ticker, market_date)`, so the single-column reading called a correct
    many_to_one declaration "many-to-many" — a false alarm on a shipped lens.

    NULLs are excluded: they never match in a join, so counting them would report a
    fan-out that no query can experience.
    """
    key = ", ".join(columns)
    not_null = " AND ".join(f"{c} IS NOT NULL" for c in columns)
    rows = execute(
        f"SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM {table} "  # noqa: S608 — identifiers
        f"WHERE {not_null} GROUP BY {key}) AS k"
    )
    if not rows or rows[0][0] is None:
        return 0
    return int(rows[0][0])


def measure(
    execute: Execute,
    *,
    left_table: str,
    left_columns: Sequence[str],
    right_table: str,
    right_columns: Sequence[str],
    left: str = "",
    right: str = "",
) -> Measured:
    """Measure the true cardinality of the join between two keyed tables.

    Each side's key is every column the ON clause matches on that side. When the ON
    wraps a column in a function (``strptime(txn_date, …) = strptime(market_date, …)``)
    we group by the raw column, which can only UNDER-report duplication — the
    conservative direction, and the same bias filter_guard takes.
    """
    try:
        left_max = _max_rows_per_key(execute, left_table, left_columns)
        right_max = _max_rows_per_key(execute, right_table, right_columns)
    except Exception as exc:  # noqa: BLE001 — an unreadable table is a report, not a crash
        return Measured(left or left_table, right or right_table, None, 0, 0, str(exc)[:200])

    # "many" on a side means one key value there covers several rows, so the OTHER
    # side's rows get duplicated when the two are joined.
    left_many, right_many = left_max > 1, right_max > 1
    if left_many and right_many:
        relationship = "many_to_many"
    elif right_many:
        relationship = "one_to_many"
    elif left_many:
        relationship = "many_to_one"
    else:
        relationship = "one_to_one"
    return Measured(left or left_table, right or right_table, relationship, left_max, right_max)


def verdict(declared: str | None, measured: Measured) -> tuple[str, str]:
    """``(status, sentence)`` for one join — status is ok | wrong | unsafe | unreadable.

    A declaration is WRONG when it claims safety the data denies (the dangerous
    direction: the compiler joins directly and inflates). A declaration that is merely
    more conservative than reality costs coverage, not correctness, so it is reported
    but not failed.
    """
    pair = f"{measured.left} -> {measured.right}"
    if measured.relationship is None:
        return "unreadable", f"{pair}: could not measure ({measured.error})"
    counts = (
        f"up to {measured.left_max} row(s) per key on the left, {measured.right_max} on the right"
    )
    if measured.relationship == "many_to_many":
        return (
            "unsafe",
            f"{pair}: many-to-many in the data ({counts}) — no direct join is safe; "
            "model a deduplicated entity, or join through the bridge table",
        )
    if declared is None:
        return (
            "wrong",
            f"{pair}: no relationship declared, and the data says "
            f"{measured.relationship} ({counts}) — declare it so joins can compile",
        )
    if declared != measured.relationship:
        fanning = measured.fans_out_left and declared in ("many_to_one", "one_to_one")
        return (
            "wrong" if fanning else "ok",
            f"{pair}: declared {declared}, measured {measured.relationship} ({counts})"
            + (" — a direct join here MULTIPLIES the left side's rows" if fanning else ""),
        )
    return "ok", f"{pair}: {declared}, confirmed ({counts})"
