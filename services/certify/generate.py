"""Generate certified question→SQL pairs from a lens's governed definitions (lens-ux).

The definitions the wizard scaffolds from a lens's data + context (including the audit
canon) carry the *meaning* but nothing executable. For each one this drafts a
representative question and the SQL that answers it (grounded in the lens's tables),
guards it, runs it read-only to capture a **verified value**, and stores it as a
certified answer — so the generated pairs are actually served (search/run_certified)
and graded against the warehouse, instead of living as a disconnected directory of files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from services.certify import store
from services.contracts.protocols import Connector, Embedder, LLMProvider
from services.contracts.semantic_model import Definition, SemanticModel
from services.db import embedding_meta
from services.llm.assist import complete_json
from services.runtime import sql_guard

_MAX_ROWS = 5

_SYSTEM = (
    "You write a single representative analytical question for a governed metric, and the "
    "exact SQL that answers it. Use ONLY the tables and columns provided, in the given SQL "
    "dialect. The SQL must be one read-only SELECT (no DML/DDL, no SELECT *). Prefer the "
    'reference SQL expression when given. Respond with strict JSON: {"question": "<one line>", '
    '"sql": "<select …>"}.'
)


@dataclass
class GeneratedCertified:
    """One generation outcome — stored (id set) or skipped/failed (error set)."""

    term: str
    question: str
    sql: str
    id: str | None = None
    verified_value: dict[str, Any] | None = None
    error: str | None = None


def _schema_text(model: SemanticModel) -> str:
    return "\n".join(
        f"- {e.source.table}: {', '.join(f.name for f in e.fields)}" for e in model.entities
    )


def _cell(value: object) -> object:
    """JSON-safe cell — numbers/strings/bools pass; Decimal/date/etc. become text."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _value_summary(columns: list[str], rows: list[list[object]]) -> dict[str, Any]:
    if len(rows) == 1 and len(rows[0]) == 1:
        return {"value": _cell(rows[0][0])}
    return {
        "columns": columns,
        "rows": [[_cell(c) for c in r] for r in rows[:_MAX_ROWS]],
    }


def _draft(
    llm: LLMProvider, model_name: str, sm: SemanticModel, d: Definition
) -> dict[str, str] | None:
    user = (
        f"SQL dialect: {sm.dialect}\n"
        f"Available tables:\n{_schema_text(sm)}\n\n"
        f"Governed metric: {d.term}\n"
        f"Definition: {d.body}\n"
        + (f"Reference SQL expression: {d.sql_expr}\n" if d.sql_expr else "")
    )
    try:
        data = complete_json(llm, model_name, _SYSTEM, user, max_tokens=700)
    except Exception:  # noqa: BLE001 — a bad draft skips this metric, never sinks the batch
        return None
    question = str(data.get("question") or "").strip()
    sql = str(data.get("sql") or "").strip()
    return {"question": question, "sql": sql} if question and sql else None


def generate_for_lens(
    session: Session,
    *,
    lens: str,
    semantic_model: SemanticModel,
    connector: Connector,
    embedder: Embedder,
    llm: LLMProvider,
    model_name: str,
    created_by: str = "generated",
) -> list[GeneratedCertified]:
    """Draft, guard, execute, and store a certified answer for each governed definition.

    Per definition: draft {question, sql} → guard (trusting the lens's own tables, these
    are governed) → run read-only for the verified value → embed the question → persist.
    A definition that can't be drafted or whose SQL fails the guard is returned with an
    ``error`` and not stored; an execution error is stored with the value left null so the
    pair is still served while the failure is visible.
    """
    embedding_meta.guard_write(session, embedder)
    out: list[GeneratedCertified] = []
    for d in semantic_model.definitions:
        pair = _draft(llm, model_name, semantic_model, d)
        if pair is None:
            out.append(GeneratedCertified(d.term, "", "", error="no question/SQL drafted"))
            continue
        guard = sql_guard.check(pair["sql"], semantic_model, trust_tables=True)
        if not guard.ok or not guard.sql:
            out.append(
                GeneratedCertified(
                    d.term, pair["question"], pair["sql"], error=f"guard: {guard.reason}"
                )
            )
            continue
        verified: dict[str, Any] | None = None
        error: str | None = None
        try:
            result = connector.execute(guard.sql, read_only=True, row_limit=_MAX_ROWS)
            verified = _value_summary(result.columns, result.rows)
        except Exception as exc:  # noqa: BLE001 — surface per-metric, don't fail the batch
            error = str(exc)
        emb = embedder.embed([pair["question"]])[0]
        cid = store.create(
            session, lens, pair["question"], guard.sql, emb, created_by, verified_value=verified
        )
        out.append(
            GeneratedCertified(
                d.term, pair["question"], guard.sql, id=cid, verified_value=verified, error=error
            )
        )
    return out
