"""Read-side aggregates over request_log for the Observe dashboard (org-scoped)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.runtime.scope import parse_scope


def kpis(session: Session) -> dict[str, object]:
    """Headline numbers, with the outcome vocabulary kept honest.

    ``errors`` counts status='error' ONLY. Counting every non-ok status brands
    refusals and clarifications — governed outcomes, the product doing its job
    — as faults, so a handful of real errors behind a wall of declines reads as
    a double-digit error rate and sends someone chasing a pipeline problem that
    does not exist. A decline is an authoring/scope signal and gets its own
    number; ``outcomes`` carries the full split so nobody has to re-derive it
    per status.

    Raw-SQL probe rows (``generator_tier = 'probe'`` — the admin passthrough
    and the caller lens probe) are NOT governed answers and are excluded from
    every governed counter here: blending them in overstates governed usage and
    cost, and passthrough volume can be a large share of total rows. They get
    their own ``sql_probes`` block — audited, counted, never blended.
    """
    governed = "COALESCE(generator_tier, '') <> 'probe'"
    row = session.execute(
        text(
            "SELECT count(*) AS queries, "
            "COALESCE(SUM(ai_cost_usd), 0) AS ai_cost, "
            "COALESCE(SUM(wh_cost_usd), 0) AS wh_cost, "
            "COALESCE(SUM(ai_input_tokens), 0) AS in_tok, "
            "COALESCE(SUM(ai_output_tokens), 0) AS out_tok, "
            "count(*) FILTER (WHERE status = 'error') AS errors, "
            "count(*) FILTER (WHERE status IN ('refused', 'clarification', 'rejected')) "
            "AS declined, "
            "count(*) FILTER "
            "(WHERE ai_cost_usd IS NULL AND ai_output_tokens IS NOT NULL) AS unpriced, "
            "count(*) FILTER (WHERE status = 'ok') AS ok, "
            "count(*) FILTER (WHERE status = 'refused') AS refused, "
            "count(*) FILTER (WHERE status = 'clarification') AS clarification, "
            "count(*) FILTER (WHERE status = 'rejected') AS rejected, "
            "count(*) FILTER (WHERE wh_cost_usd IS NULL AND status = 'ok') AS wh_unpriced "
            f"FROM request_log WHERE {governed}"
        )
    ).first()
    assert row is not None
    probe_row = session.execute(
        text(
            "SELECT count(*), "
            "count(*) FILTER (WHERE lens LIKE 'sql:%'), "
            "COALESCE(SUM(wh_cost_usd), 0) "
            "FROM request_log WHERE COALESCE(generator_tier, '') = 'probe'"
        )
    ).first()
    assert probe_row is not None
    # The confidence histogram: without it an operator choosing "only serve
    # verified" as a gate cannot see what that gate would cost — the tier
    # split exists on every row and nowhere in the rollup. Served answers only.
    histogram = {
        str(r[0] or "none"): int(r[1])
        for r in session.execute(
            text(
                "SELECT confidence, count(*) FROM request_log "
                f"WHERE status = 'ok' AND {governed} GROUP BY confidence"
            )
        )
    }
    return {
        "queries": int(row[0]),
        "ai_cost_usd": round(float(row[1]), 4),
        "warehouse_cost_usd": round(float(row[2]), 4),
        "input_tokens": int(row[3]),
        "output_tokens": int(row[4]),
        "errors": int(row[5]),
        "declined": int(row[6]),
        # Requests whose model had no configured price: their AI spend is unknown,
        # not $0 — the headline must not launder the gap (same rule as caller_report).
        "unpriced": int(row[7]),
        # Served answers whose warehouse scan has no price (Snowflake bills
        # credit-seconds the cursor can't see): the wh_cost sum above EXCLUDES
        # them, and this count is what keeps that exclusion legible.
        "wh_unpriced": int(row[12]),
        "outcomes": {
            "ok": int(row[8]),
            "refused": int(row[9]),
            "clarification": int(row[10]),
            "rejected": int(row[11]),
            "error": int(row[5]),
        },
        "confidence_histogram": histogram,
        # Raw-SQL probes, reported as what they are — never as governed
        # queries. `admin_sql` is the connection passthrough (an admin's own
        # SQL against an org credential); `lens_scoped` is a caller probing
        # inside a lens's allow-list. Both remain fully audited in request_log.
        "sql_probes": {
            "queries": int(probe_row[0]),
            "admin_sql": int(probe_row[1]),
            "lens_scoped": int(probe_row[0]) - int(probe_row[1]),
            "warehouse_cost_usd": round(float(probe_row[2]), 4),
        },
    }


def recent_requests(
    session: Session, limit: int = 50, lens: str | None = None, status: str | None = None
) -> list[dict[str, object]]:
    """Recent request-log rows (summaries) for the trace explorer.

    ``status`` IS the triage classification — refused/clarification/rejected
    are governed declines (authoring or scope, never the pipeline), 'error'
    is a fault whose stored ``error`` text the detail view carries verbatim.
    No inferred categories: classifying outcomes by sniffing prose was tried
    and produced false attributions; the deterministic field was already here.
    """
    clauses: list[str] = []
    params: dict[str, object] = {"k": limit}
    if lens:
        clauses.append("lens = :lens")
        params["lens"] = lens
    if status:
        clauses.append("status = :status")
        params["status"] = status
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = session.execute(
        text(
            "SELECT request_id, lens, caller, status, row_count, confidence, "
            "COALESCE(ai_cost_usd,0) + COALESCE(wh_cost_usd,0) AS cost, created_at, question, "
            "generator_tier "
            f"FROM request_log {where} ORDER BY created_at DESC LIMIT :k"
        ),
        params,
    ).all()
    return [
        {
            "request_id": r[0],
            "lens": r[1],
            "caller": r[2],
            "status": r[3],
            "row_count": r[4],
            "confidence": r[5],
            "cost_usd": round(float(r[6]), 6),
            "created_at": r[7].isoformat() if r[7] else None,
            "question": r[8],
            # What KIND of row this is, as an explicit field: a
            # consumer must not need to know that a 'sql:' lens-name prefix
            # means the admin passthrough. For probes, `question` holds SQL.
            "kind": _row_kind(r[9], r[1]),
        }
        for r in rows
    ]


def _row_kind(generator_tier: object, lens: object) -> str:
    """'answer' (a governed serve), 'admin_sql' (the connection passthrough),
    or 'sql_probe' (raw SQL inside a lens's scope)."""
    if str(generator_tier or "") != "probe":
        return "answer"
    return "admin_sql" if str(lens or "").startswith("sql:") else "sql_probe"


def request_detail(session: Session, request_id: str) -> dict[str, object] | None:
    """The full trace for one request (the review artifact, browsable)."""
    row = session.execute(
        text(
            "SELECT request_id, lens, caller, question, sql, valid, row_count, answer, "
            "citations, definition_used, confidence, verification, certification, latency, "
            "ai_input_tokens, ai_output_tokens, ai_cost_usd, wh_bytes, wh_cost_usd, "
            "status, error, created_at, prompt_hash, generator_tier, repairs, context_refs "
            "FROM request_log WHERE request_id = :r"
        ),
        {"r": request_id},
    ).first()
    if row is None:
        return None
    scope = parse_scope(row[4])
    return {
        "request_id": row[0],
        "lens": row[1],
        "caller": row[2],
        "question": row[3],
        "sql": row[4],
        # The governed decomposition of the SQL (table/fields/filters/ranking) so a
        # reviewer can audit the answer without reading SQL. None when unparseable.
        "scope": scope.model_dump() if scope else None,
        "valid": row[5],
        "row_count": row[6],
        "answer": row[7],
        "citations": row[8],
        "definition_used": row[9],
        "confidence": row[10],
        "verification": row[11],
        "certification": row[12],
        "latency": row[13],
        "ai_input_tokens": row[14],
        "ai_output_tokens": row[15],
        "ai_cost_usd": float(row[16]) if row[16] is not None else None,
        "wh_bytes": row[17],
        "wh_cost_usd": float(row[18]) if row[18] is not None else None,
        "status": row[19],
        "error": row[20],
        "created_at": row[21].isoformat() if row[21] else None,
        "prompt_hash": row[22],
        # Which prompt answered (the tiers render different ones), what it cost in
        # repairs, and which context rode along — NULL on pre-0036 rows.
        "generator_tier": row[23],
        "repairs": row[24],
        "context_refs": row[25],
        "kind": _row_kind(row[23], row[1]),
    }


def eval_trend(session: Session) -> list[dict[str, object]]:
    """Per-lens accuracy trend from eval runs.

    Reads the control-plane ``eval_run`` table — the canonical eval history
    (there is no warehouse-native mirror)."""
    rows = session.execute(
        text(
            "SELECT lens, mode, score, passed, failed, errored, started_at "
            "FROM eval_run ORDER BY lens, started_at ASC"
        )
    ).all()
    by_lens: dict[str, list[dict[str, object]]] = {}
    for r in rows:
        by_lens.setdefault(r[0], []).append(
            {
                "mode": r[1],
                "score": float(r[2]) if r[2] is not None else None,
                "passed": int(r[3]),
                "failed": int(r[4]),
                "errored": int(r[5]),
                "started_at": r[6].isoformat() if r[6] else None,
            }
        )
    out: list[dict[str, object]] = []
    for lens, runs in sorted(by_lens.items()):
        latest = next((x["score"] for x in reversed(runs) if x["score"] is not None), None)
        out.append({"lens": lens, "latest_score": latest, "runs": runs})
    return out


def caller_report(session: Session, lens: str | None = None) -> list[dict[str, object]]:
    """Per-caller rollup. Same vocabulary rule as kpis(): ``errors`` is
    status='error' only; declines are their own column — a support asker
    whose questions the layer can't answer yet is a coverage signal, not a
    50%-error-rate incident.

    AI and warehouse spend are separate numbers (they answer different
    questions: model bill vs bytes scanned); ``cost_usd`` keeps the blend for
    totals. ``unpriced`` counts requests whose model had no configured price —
    cost.py deliberately stores NULL there, and COALESCE-ing that to $0 in the
    rollup laundered the gap into "free". The count keeps it visible."""
    # Probe rows are excluded like kpis(): an admin's passthrough
    # statements are not that caller "querying".
    governed = "COALESCE(generator_tier, '') <> 'probe'"
    where = f"WHERE {governed} AND lens = :lens" if lens else f"WHERE {governed}"
    rows = session.execute(
        text(
            "SELECT caller, count(*) AS queries, "
            "COALESCE(SUM(ai_cost_usd), 0) AS ai_cost, "
            "COALESCE(SUM(wh_cost_usd), 0) AS wh_cost, "
            "count(*) FILTER "
            "(WHERE ai_cost_usd IS NULL AND ai_output_tokens IS NOT NULL) AS unpriced, "
            "count(*) FILTER (WHERE status = 'error') AS errors, "
            "count(*) FILTER (WHERE status IN ('refused', 'clarification', 'rejected')) "
            "AS declined "
            f"FROM request_log {where} "
            "GROUP BY caller ORDER BY queries DESC"
        ),
        {"lens": lens} if lens else {},
    ).all()
    return [
        {
            "caller": r[0],
            "queries": int(r[1]),
            "ai_cost_usd": round(float(r[2]), 4),
            "wh_cost_usd": round(float(r[3]), 4),
            "cost_usd": round(float(r[2]) + float(r[3]), 4),
            "unpriced": int(r[4]),
            "errors": int(r[5]),
            "declined": int(r[6]),
        }
        for r in rows
    ]
