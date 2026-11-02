"""The grain check: rows against distinct keys, for a table a blueprint declares.

A table that keeps one row per sync per record answers `COUNT(*)` with every
record counted once per sync, and nothing about the number says so. The fact
that prevents it is the table's grain, declared in its blueprint
(``semantic/tables/``): the key one record is unique on, and whether rows
repeat per sync. This module measures it — the row count against the distinct
count of the key — from the probe when the probe holds both exactly and is
fresh, else with one live count bounded by the connection's sampling budget.
When neither is possible it says "could not verify" and why; a label never
stands in for the count.

Read by plan and apply (a declared key that is not unique is an error), by
describe (each check's verdict), and by every EXPOSED answer (`query` and the
`sql` tool): the answer carries rows against distinct key for each table it
read, and is never graded verified when the counts disagree with the declared
grain or when no grain is declared.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Literal

from services.contracts.profile import ColumnProfile, TableProfile
from services.contracts.protocols import Connector
from services.contracts.semantic_model import SemanticModel, TableBlueprint
from services.contracts.verification import VerificationCheck, VerificationReport
from services.runtime.bounded import WarehouseTimeout, warehouse_bounded

CHECK = "grain"

# How long a live count serves later answers when the probe clock is off
# (`probe.max_age: 0`) or unknown (nothing profiled): one hour.
LIVE_COUNT_TTL_DEFAULT = timedelta(hours=1)


@dataclass(frozen=True)
class KeyCount:
    """Rows and distinct key values of one table, or why there are none."""

    rows: int | None = None
    distinct: int | None = None
    source: Literal["probe", "live"] | None = None
    # Why no count was taken, as a sentence a reader can act on.
    skipped: str | None = None
    # Distinct tuples of the full unique-on key (the key plus the sync column)
    # when it differs from the counted key: what says a versioned table's
    # declared grain still holds.
    unique: int | None = None
    # When the counts were taken: the probe's stamp, or the live count's.
    taken_at: datetime | None = None

    @property
    def counted(self) -> bool:
        return self.rows is not None and self.distinct is not None


def _column(profile: TableProfile, name: str) -> ColumnProfile | None:
    return next((c for c in profile.columns if c.name.lower() == name.lower()), None)


def probe_count(profile: TableProfile | None, columns: Sequence[str], *, fresh: bool) -> KeyCount:
    """The probe's own counts, when they decide: a one-column key whose distinct
    count was taken over every row, a table read whole, and a profile within
    ``probe.max_age``. Anything else is not counted here."""
    if profile is None:
        return KeyCount(skipped="the probe holds no profile of the table")
    if not fresh:
        return KeyCount(skipped="the probe is older than probe.max_age")
    if len(columns) != 1:
        return KeyCount(skipped="the probe counts one column at a time and the key has several")
    column = _column(profile, columns[0])
    if column is None:
        return KeyCount(skipped=f"the probe holds no column '{columns[0]}'")
    distinct = column.distinct_count
    exact = column.distinct_is_exact
    if profile.row_count is None or distinct is None:
        return KeyCount(skipped="the probe did not count the table's rows and key")
    if not exact or profile.sampled_rows is not None:
        return KeyCount(skipped="the probe's counts come from a sample")
    return KeyCount(
        rows=profile.row_count,
        distinct=int(distinct),
        source="probe",
        taken_at=profile.profiled_at,
    )


def _quote_name(name: str, dialect: str) -> str:
    quote = "`" if dialect in ("mysql", "bigquery") else '"'
    return f"{quote}{name.replace(quote, quote * 2)}{quote}"


def _quote_table(table: str, dialect: str) -> str:
    if dialect == "bigquery":
        return f"`{table.replace('`', '')}`"
    return ".".join(_quote_name(part, dialect) for part in table.split("."))


def count_sql(
    table: str, columns: Sequence[str], dialect: str, unique_on: Sequence[str] = ()
) -> str:
    """One statement, one row: the table's rows, its distinct key tuples and,
    with ``unique_on``, the distinct tuples of that wider key too."""
    t = _quote_table(table, dialect)

    def distinct(cols: Sequence[str], alias: str) -> str:
        listed = ", ".join(_quote_name(c, dialect) for c in cols)
        return f"(SELECT COUNT(*) FROM (SELECT DISTINCT {listed} FROM {t}) AS _{alias}) AS {alias}"

    parts = [f"(SELECT COUNT(*) FROM {t}) AS n", distinct(columns, "d")]
    if unique_on:
        parts.append(distinct(unique_on, "u"))
    return "SELECT " + ", ".join(parts)


def bounded_row(
    connector: Connector, sql: str, *, step: str, connection: str
) -> tuple[Sequence[object] | None, str | None]:
    """One read-only statement, one row, within the connection's sampling
    budget: ``(row, None)``, or ``(None, why)`` when it was not run or did not
    answer. On a warehouse that bills scanned bytes the statement is dry-run
    first and refused, never run, when its estimate exceeds the connection's
    ``sample_bytes_budget`` (the bound the probe works under); the reason
    carries both numbers. A failure is a reason, said, never a raise."""
    budget = getattr(connector, "sample_bytes_budget", None)
    try:
        if budget is not None and (dry := getattr(connector, "dry_run", None)) is not None:
            estimate = warehouse_bounded(
                f"{step}'s dry run", partial(dry, sql), connection=connection
            )
            if not estimate.valid:
                return None, f"{step} failed its dry run: {estimate.error}"
            scanned = estimate.bytes_estimated or 0
            if scanned > budget:
                return None, (
                    f"{step} would scan {scanned:,} bytes, over the connection's "
                    f"{budget:,}-byte sampling budget (config: sample_bytes_budget), "
                    "so it was not run"
                )
        result = warehouse_bounded(
            step,
            partial(connector.execute, sql, read_only=True, row_limit=1),
            connection=connection,
        )
    except WarehouseTimeout as exc:
        return None, f"{step} did not return: {exc}"
    except Exception as exc:  # noqa: BLE001 — a failed read is a reason, said, never a raise
        return None, f"{step} failed: {str(exc)[:200]}"
    if not result.rows:
        return None, f"{step} returned no row"
    return result.rows[0], None


def live_count(
    connector: Connector,
    table: str,
    columns: Sequence[str],
    *,
    connection: str,
    unique_on: Sequence[str] = (),
) -> KeyCount:
    """One live count of rows against distinct key, read-only and one row,
    within the connection's sampling budget (``bounded_row``)."""
    dialect = getattr(connector, "kind", "duckdb")
    sql = count_sql(table, columns, dialect, unique_on)
    row, skipped = bounded_row(connector, sql, step="the live count", connection=connection)
    if row is None:
        return KeyCount(skipped=skipped)
    try:
        return KeyCount(
            rows=int(str(row[0])),
            distinct=int(str(row[1])),
            unique=int(str(row[2])) if unique_on else None,
            source="live",
            taken_at=datetime.now(UTC),
        )
    except (IndexError, TypeError, ValueError):
        return KeyCount(skipped="the live count returned no counts")


# (scope, connection, table, key, unique_on) -> a live count and when it lapses.
# Scope is the org: two orgs may name different warehouses alike.
_LIVE: dict[tuple[str, str, str, tuple[str, ...], tuple[str, ...]], tuple[KeyCount, datetime]] = {}
_LIVE_LOCK = threading.Lock()


def cached_live_count(
    scope: str,
    connector: Connector,
    table: str,
    columns: Sequence[str],
    *,
    connection: str,
    unique_on: Sequence[str] = (),
    ttl: timedelta = LIVE_COUNT_TTL_DEFAULT,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> KeyCount:
    """A live count reused across answers until it is ``ttl`` old (bounded by
    the probe's own ``max_age``, so a cached count is never staler than the
    profile it stands in for). A count that did not run is not cached: the
    next answer tries again."""
    key = (scope, connection, table.lower(), tuple(columns), tuple(unique_on))
    moment = now()
    with _LIVE_LOCK:
        hit = _LIVE.get(key)
    if hit is not None and hit[1] > moment:
        return hit[0]
    counted = live_count(connector, table, columns, connection=connection, unique_on=unique_on)
    if counted.counted:
        counted = replace(counted, taken_at=moment)
        with _LIVE_LOCK:
            _LIVE[key] = (counted, moment + ttl)
    return counted


def clear_live_counts() -> None:
    with _LIVE_LOCK:
        _LIVE.clear()


def key_count(
    profile: TableProfile | None,
    columns: Sequence[str],
    *,
    fresh: bool,
    connector: Connector | None,
    table: str,
    connection: str,
    scope: str = "",
    unique_on: Sequence[str] = (),
    ttl: timedelta = LIVE_COUNT_TTL_DEFAULT,
    unavailable: str = "no live connection to count with",
) -> KeyCount:
    """Probe first; a cached live count when the probe cannot decide; else
    why not. A versioned key (``unique_on`` set) is always counted live: the
    probe counts one column and cannot say the wider key still holds."""
    from_probe = (
        probe_count(profile, columns, fresh=fresh)
        if not unique_on
        else KeyCount(skipped="the probe cannot count a key with its sync column")
    )
    if from_probe.counted or connector is None:
        return (
            from_probe
            if from_probe.counted
            else KeyCount(skipped=f"{from_probe.skipped}, and {unavailable}")
        )
    live = cached_live_count(
        scope, connector, table, columns, connection=connection, unique_on=unique_on, ttl=ttl
    )
    if live.counted:
        return live
    return KeyCount(skipped=f"{from_probe.skipped}, and {live.skipped}")


def _n(value: int) -> str:
    return f"{value:,}"


def key_label(columns: Sequence[str], hidden: frozenset[str] = frozenset()) -> str:
    """The key as a reader may see it: a hidden column is never named."""
    if any(c.lower() in hidden for c in columns):
        return "the declared key"
    return ", ".join(columns)


GrainStatus = Literal["one_per_key", "repeats", "contradicts", "unverified"]


@dataclass(frozen=True)
class GrainReading:
    """What an answer that read one table carries about its grain."""

    table: str
    status: GrainStatus
    line: str
    rows: int | None = None
    distinct: int | None = None
    key: tuple[str, ...] = ()


def reading(
    table: str,
    blueprint: TableBlueprint | None,
    count: KeyCount | None,
    *,
    hidden: frozenset[str] = frozenset(),
) -> GrainReading:
    """Rows against distinct key, in words: what one row of *table* is, as the
    counts say. Without a declared key the answer is "could not verify"."""
    if blueprint is None or not blueprint.key:
        return GrainReading(
            table=table,
            status="unverified",
            line=f"could not verify the grain of {table}: no key is declared for it "
            "(semantic/tables/), so whether its rows repeat is not known",
        )
    key = blueprint.record_key() or blueprint.key
    label = key_label(key, hidden)
    if count is None or not count.counted:
        why = count.skipped if count is not None else "it was not counted"
        return GrainReading(
            table=table,
            status="unverified",
            line=f"could not verify the grain of {table}: {why}",
            key=tuple(key),
        )
    assert count.rows is not None and count.distinct is not None
    rows, distinct = count.rows, count.distinct
    stamp = f"{count.taken_at:%Y-%m-%d %H:%M} UTC" if count.taken_at else "time unknown"
    if count.taken_at and count.taken_at.tzinfo is not None:
        stamp = f"{count.taken_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    where = f"per the probe of {stamp}" if count.source == "probe" else f"counted live {stamp}"
    if count.unique is not None and count.unique != rows:
        wide = blueprint.unique_on()
        return GrainReading(
            table=table,
            status="contradicts",
            line=f"{table}: {_n(rows)} rows for {_n(count.unique)} distinct "
            f"{key_label(wide, hidden)} ({where}), but its blueprint declares one row per "
            f"{label} per sync; its declared grain no longer holds, so a count or sum over "
            "it cannot be checked",
            rows=rows,
            distinct=distinct,
            key=tuple(key),
        )
    if rows == distinct:
        return GrainReading(
            table=table,
            status="one_per_key",
            line=f"{table}: {_n(rows)} rows, {_n(distinct)} distinct {label} ({where}), "
            f"one row per {label}",
            rows=rows,
            distinct=distinct,
            key=tuple(key),
        )
    if blueprint.versions is not None:
        latest = [c for c in blueprint.versions.latest_only if c.lower() not in hidden]
        tail = f", and {', '.join(latest)} holds on the latest version only" if latest else ""
        return GrainReading(
            table=table,
            status="repeats",
            line=f"{table}: {_n(rows)} rows for {_n(distinct)} distinct {label} ({where}): "
            f"rows repeat per {key_label([blueprint.versions.sync_column], hidden)}{tail}; "
            "a count or sum over it is right only when it reads the latest version",
            rows=rows,
            distinct=distinct,
            key=tuple(key),
        )
    return GrainReading(
        table=table,
        status="contradicts",
        line=f"{table}: {_n(rows)} rows for {_n(distinct)} distinct {label} ({where}), "
        f"but its blueprint declares one row per {label}; a count or sum over it is "
        "inflated wherever a key repeats",
        rows=rows,
        distinct=distinct,
        key=tuple(key),
    )


def profile_for(profiles: Mapping[str, TableProfile], table: str) -> TableProfile | None:
    from services.project.lineage import same_table

    hit = profiles.get(table)
    if hit is not None:
        return hit
    return next((p for t, p in profiles.items() if same_table(t, table)), None)


def read_grain(
    model: SemanticModel,
    tables: Sequence[str],
    *,
    profiles: Mapping[str, TableProfile],
    fresh: bool,
    connector: Connector | None,
    scope: str = "",
    ttl: timedelta | None = None,
) -> list[GrainReading]:
    """One reading per table an answer read, in the order given. ``profiles``
    maps a table (as profiled) to its stored profile; ``fresh`` is False past
    ``probe.max_age``, and then only a live count can decide. A live count is
    cached per ``scope`` (the org) for ``ttl``: the probe's ``max_age``, or
    ``LIVE_COUNT_TTL_DEFAULT`` when that is off or unknown."""
    out: list[GrainReading] = []
    for table in tables:
        entity = next((e for e in model.entities if e.source.table == table), None)
        blueprint = model.blueprints.get(table)
        hidden: frozenset[str] = frozenset()
        if entity is not None and blueprint is not None:
            visible = {f.name.lower() for f in entity.fields}
            hidden = frozenset(
                c.lower() for _where, c in blueprint.own_columns() if c.lower() not in visible
            )
        count = None
        if blueprint is not None and blueprint.key:
            key = blueprint.record_key() or blueprint.key
            wide = blueprint.unique_on()
            count = key_count(
                profile_for(profiles, table),
                key,
                fresh=fresh,
                connector=connector,
                table=table,
                connection=entity.source.connection if entity is not None else "?",
                scope=scope,
                unique_on=wide if wide != key else (),
                ttl=ttl or LIVE_COUNT_TTL_DEFAULT,
            )
        out.append(reading(table, blueprint, count, hidden=hidden))
    return out


def fold(report: VerificationReport, readings: Sequence[GrainReading]) -> VerificationReport:
    """The grain readings, as checks on the answer's report. A table whose rows
    match its declared key passes; one whose rows repeat per sync fails and the
    grade caps at partial; one whose counts contradict its declared key fails
    and grades unverified (the declaration is false); a table with no declared
    grain, or one that could not be counted, is a skip that still keeps the
    grade short of verified. Never upgrades a grade."""
    if not readings:
        return report
    from services.runtime.verification import partial_reason

    checks = list(report.checks)
    grade = report.grade
    for r in readings:
        status: Literal["pass", "fail", "skip"] = (
            "pass" if r.status == "one_per_key" else "skip" if r.status == "unverified" else "fail"
        )
        checks.append(VerificationCheck(name=CHECK, status=status, reason=r.line))
        if r.status == "contradicts":
            grade = "unverified"
        elif r.status in ("repeats", "unverified") and grade == "verified":
            grade = "partial"
    return VerificationReport(
        grade=grade, checks=checks, partial_reason=partial_reason(grade, checks)
    )


def lines(readings: Sequence[GrainReading]) -> list[str]:
    """The `degraded` lines an answer carries, in the UNTYPED/EXPOSED idiom."""
    return [f"GRAIN: {r.line}" for r in readings]
