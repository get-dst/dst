"""An empty answer for a period the data has not reached yet.

"Fee revenue today" asked before today's rows are loaded runs over nothing and
comes back empty or zero, a figure that reads as "nothing happened". When the
result holds no data, one read-only ``MAX`` of the time column the query
filters on says where the data ends; a window that begins after it is answered
with that fact instead of the empty result. The read is live, so a stale
profile cannot make the claim; any doubt (no window, no dated entity, a probe
that fails) serves the result as before.

A level metric asked for today, now or currently reads its latest snapshot,
which is often yesterday's or older; the answer names that snapshot's day,
read live by the same kind of ``MAX``.
"""

from __future__ import annotations

from datetime import date, datetime

import sqlglot
from sqlglot import exp

from services.contracts.protocols import Connector
from services.contracts.query_intent import QueryIntent
from services.contracts.semantic_model import Entity, SemanticModel
from services.contracts.warehouse import QueryResult
from services.runtime import sql_guard, value_guard
from services.runtime.identifiers import quote_ident, quote_table
from services.runtime.timewindow import asks_present, temporal_terms, window_ranges


def no_data(sql: str, dialect: str, result: QueryResult) -> bool:
    """No row, the one-row aggregate over nothing (``value_guard.zero_evidence``),
    or one row of nothing but NULLs (a ratio or a comparison over no rows)."""
    if not result.rows:
        return True
    if value_guard.zero_evidence(sql, dialect, result):
        return True
    return len(result.rows) == 1 and all(cell is None for cell in result.rows[0])


def _day(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _filtered(sql: str, dialect: str) -> set[str]:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — unparseable SQL is not this read's business
        return set()
    return {c.name.lower() for w in tree.find_all(exp.Where) for c in w.find_all(exp.Column)}


def _clock(entity: Entity, intent: QueryIntent | None) -> str | None:
    field = entity.default_time_field
    if intent is not None and intent.entity == entity.name:
        for m in entity.metrics:
            if m.name in intent.metrics and m.agg_time_field:
                field = m.agg_time_field
    return field


def _window_start(
    question: str, intent: QueryIntent | None, field: str, today: date
) -> date | None:
    """Where the asked window begins: the compiled intent's lower bound on the
    time field (a binding included), else the question's stated window."""
    if intent is not None:
        bounds = [
            d
            for f in intent.filters
            if f.field.rsplit(".", 1)[-1].lower() == field.lower() and f.op in (">=", ">", "=")
            if (d := _day(f.value)) is not None
        ]
        if bounds:
            return max(bounds)
    ranges = window_ranges(temporal_terms(question), today)
    return min(start for start, _end in ranges) if ranges else None


def window_after_data(
    question: str,
    sql: str,
    intent: QueryIntent | None,
    model: SemanticModel,
    connector: Connector,
    today: date,
) -> str | None:
    """The sentence that answers instead, when the asked window begins after
    the newest row of a dated table the query filters on; None otherwise."""
    filtered = _filtered(sql, model.dialect)
    for entity in sql_guard.entities_read(sql, model):
        field = _clock(entity, intent)
        if not field or field.lower() not in filtered:
            continue
        start = _window_start(question, intent, field, today)
        if start is None:
            continue
        probe = (
            f"SELECT MAX({quote_ident(field, model.dialect)}) "
            f"FROM {quote_table(entity.source.table, model.dialect)}"
        )
        try:
            got = connector.execute(probe, read_only=True, row_limit=1)
        except Exception:  # noqa: BLE001 — the probe is evidence, never the serve
            continue
        newest = _day(got.rows[0][0]) if got.rows and got.rows[0] else None
        if newest is not None and newest < start:
            return (
                f"There is no {entity.name} data for that period yet: it runs to "
                f"{newest.isoformat()} ({field}), and the period asked begins "
                f"{start.isoformat()}."
            )
    return None


def latest_snapshot_line(
    question: str, intent: QueryIntent | None, model: SemanticModel, connector: Connector
) -> str | None:
    """The level reading's disclosure naming the snapshot day a figure asked
    for today, now or currently stands on; None for any other reading, or
    when the day cannot be read."""
    from services.runtime.resolution import LATEST_READING, level_reading

    if intent is None or not asks_present(question):
        return None
    reading = level_reading(intent, model)
    if reading is None or not reading[1].startswith(LATEST_READING):
        return None
    entity = next((e for e in model.entities if e.name == intent.entity), None)
    if entity is None:
        owners = [e for e in model.entities if {m.name for m in e.metrics} >= set(intent.metrics)]
        entity = owners[0] if len(owners) == 1 else None
    if entity is None:
        return None
    field = reading[0]
    probe = (
        f"SELECT MAX({quote_ident(field, model.dialect)}) "
        f"FROM {quote_table(entity.source.table, model.dialect)}"
    )
    try:
        got = connector.execute(probe, read_only=True, row_limit=1)
    except Exception:  # noqa: BLE001 — the probe is evidence, never the serve
        return None
    newest = _day(got.rows[0][0]) if got.rows and got.rows[0] else None
    if newest is None:
        return None
    names = ", ".join(f"`{m}`" for m in intent.metrics)
    return (
        f"Reading applied: {names} is a level (a snapshot), so the figure for the present "
        f"is the latest snapshot in the data, {newest.isoformat()} on `{field}`, not a sum "
        "over every snapshot."
    )
