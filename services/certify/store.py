"""Certified-answer repository — per-lens approved question→SQL pairs (pgvector).

The keystone trust + grounding + cost lever: on a new question we embed it and search
this store; an exact/near match lets us run the approved SQL directly (skipping the LLM),
and partial matches feed the generator as few-shot exemplars. Org-scoped via RLS.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

#: The invariant is ``active ⟹ embedded``: certified matching is pgvector
#: cosine, so a NULL vector can NEVER be returned. An answer written without an
#: embedding (no provider configured, provider down) still lands — a human
#: APPROVE must not dead-end on provider config (0027 doctrine) — but it lands
#: in THIS state instead of a silently unmatchable ``active``. `dst reindex`
#: backfills the vector and promotes it back. A DB CHECK constraint (migration
#: 0035) makes the silent variant impossible, and create/update below demote/
#: promote so no write path has to remember.
PENDING_EMBEDDING = "pending_embedding"


def is_active(status: str) -> bool:
    """Serving/testing predicate: retired is the only state that opts OUT.
    A pending_embedding answer is active in intent — it just cannot MATCH
    (search filters NULL vectors on its own), so the explicit-id door, the eval
    gate and the `dst test` sweep must still see it."""
    return status != "retired"


def authored_status(status: str) -> str:
    """The status a FILE round-trips. pending_embedding is derived server state
    (like bindings), never authored: certified_answers.yaml carries the intent,
    so it renders/compares as 'active' — otherwise a pull would write a status
    apply rejects, and every apply would re-'edit' the row."""
    return "active" if status == PENDING_EMBEDDING else status


def _vec(values: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


def _verified(raw: object) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw  # jsonb already decoded by the driver
    if isinstance(raw, str | bytes | bytearray):
        return dict(json.loads(raw))
    return None


def _jsonlist(raw: object) -> list[dict[str, Any]] | None:
    if isinstance(raw, list):
        return raw  # jsonb already decoded by the driver
    if isinstance(raw, str | bytes | bytearray):
        return list(json.loads(raw))
    return None


@dataclass
class CertifiedAnswer:
    id: str
    lens: str
    question: str
    sql: str
    created_by: str
    created_at: str = ""  # ISO timestamp; provenance surfaced on certified answers
    # The value this answer's SQL produced when generated (lens-ux certified pipeline) —
    # a small summary dict ({"value": …} for a scalar, columns/rows for a table, or
    # {"error": …}). None for hand-certified answers that were never executed here.
    verified_value: dict[str, Any] | None = None
    # Where this pair came from ("looker:dashboards/42 'MRR'", "review:<request_id>")
    # and who/what vouches for it (created_by stays the acting identity; this is the
    # authority). Free text, round-tripped through certified_answers.yaml when set.
    source: str | None = None
    verified_by: str | None = None
    # The answer's English, composed ONCE from the executed result at certify
    # time (apply --probe-certified / rule --certify) and served VERBATIM on
    # every certified match — no composer call, no numeric_grounding pass, no
    # badge wobble. None = legacy/no-probe/template: the serve path
    # falls back to composing as before. Round-tripped through
    # certified_answers.yaml when set; a SQL re-author clears it (the prose
    # described the OLD SQL's result).
    verified_prose: str | None = None
    # Derived {asset_key: content_hash} of the shared assets the SQL touches — the
    # staleness signal (same hashes as the lens's SharedProvenance). Computed at
    # apply/certify time, DB-only (never rendered to files). None = not yet computed.
    bindings: dict[str, str] | None = None
    # active | pending_embedding | retired. Retired =
    # kept for history: listed and exported, but never served, never matched
    # (search filters it), never tested. pending_embedding = active in INTENT but
    # unembedded, so it can never match — see PENDING_EMBEDDING.
    status: str = "active"
    # Set ⇒ this answer is a TEMPLATE — sql/question carry {slot}
    # placeholders typed by ``slots`` (see services/certify/binding.py), and
    # ``sample_bindings`` (non-empty, validated at certify time) make it
    # executable: [0] is the match anchor and the eval witness.
    slots: dict[str, Any] | None = None
    sample_bindings: list[dict[str, Any]] | None = None
    # The SQL dialect the probe VERIFIED this answer against (stamped on a
    # successful probe execution, re-stamped by a re-probe). None = never
    # executed here — advisory verification, no pin. Apply refuses to publish a
    # lens whose compiled dialect differs from a non-null pin: certified SQL is
    # dialect-bound text and must not outlive its warehouse silently.
    verified_dialect: str | None = None


@dataclass
class CertifiedHit:
    answer: CertifiedAnswer
    score: float  # cosine similarity in [0, 1]


_COLS = (
    "id, lens, question, sql, created_by, created_at, verified_value, source, verified_by, "
    "bindings, status, slots, sample_bindings, verified_dialect, verified_prose"
)


def _row(r: object) -> CertifiedAnswer:
    return CertifiedAnswer(
        str(r[0]),  # type: ignore[index]
        r[1],  # type: ignore[index]
        r[2],  # type: ignore[index]
        r[3],  # type: ignore[index]
        r[4],  # type: ignore[index]
        r[5].isoformat(),  # type: ignore[index]
        _verified(r[6]),  # type: ignore[index]
        r[7],  # type: ignore[index]
        r[8],  # type: ignore[index]
        bindings=_verified(r[9]),  # type: ignore[index]
        status=r[10],  # type: ignore[index]
        slots=_verified(r[11]),  # type: ignore[index]
        sample_bindings=_jsonlist(r[12]),  # type: ignore[index]
        verified_dialect=r[13],  # type: ignore[index]
        verified_prose=r[14],  # type: ignore[index]
    )


def create(
    session: Session,
    lens: str,
    question: str,
    sql: str,
    embedding: list[float] | None,
    created_by: str,
    verified_value: dict[str, Any] | None = None,
    *,
    source: str | None = None,
    verified_by: str | None = None,
    bindings: dict[str, str] | None = None,
    status: str = "active",
    slots: dict[str, Any] | None = None,
    sample_bindings: list[dict[str, Any]] | None = None,
    verified_dialect: str | None = None,
    verified_prose: str | None = None,
) -> str:
    """``embedding=None`` stores the pair unembedded (no provider configured yet):
    it is listed but never matched until ``dst reindex`` backfills it — and
    it lands ``status=pending_embedding``, never a lying ``active``."""
    if embedding is None and status == "active":
        status = PENDING_EMBEDDING
    row = session.execute(
        text(
            """
            INSERT INTO certified_answer (
                org_id, lens, question, sql, embedding, created_by, verified_value,
                source, verified_by, bindings, status, slots, sample_bindings,
                verified_dialect, verified_prose
            )
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :lens, :q, :sql, CAST(:e AS vector), :by, CAST(:vv AS jsonb),
                :src, :vb, CAST(:b AS jsonb), :st, CAST(:slots AS jsonb),
                CAST(:samples AS jsonb), :vd, :vp
            )
            RETURNING id
            """
        ),
        {
            "lens": lens,
            "q": question,
            "sql": sql,
            "e": _vec(embedding) if embedding is not None else None,
            "by": created_by,
            "vv": json.dumps(verified_value) if verified_value is not None else None,
            "src": source,
            "vb": verified_by,
            "b": json.dumps(bindings) if bindings is not None else None,
            "st": status,
            "slots": json.dumps(slots) if slots is not None else None,
            "samples": json.dumps(sample_bindings) if sample_bindings is not None else None,
            "vd": verified_dialect,
            "vp": verified_prose,
        },
    ).first()
    return str(row[0])  # type: ignore[index]


def get(session: Session, answer_id: str) -> CertifiedAnswer | None:
    row = session.execute(
        text(f"SELECT {_COLS} FROM certified_answer WHERE id = :i"),
        {"i": answer_id},
    ).first()
    return _row(row) if row is not None else None


def list_for_lens(session: Session, lens: str) -> list[CertifiedAnswer]:
    rows = session.execute(
        text(f"SELECT {_COLS} FROM certified_answer WHERE lens = :l ORDER BY created_at DESC"),
        {"l": lens},
    ).all()
    return [_row(r) for r in rows]


def search(
    session: Session, lens: str, query_embedding: list[float], k: int = 5
) -> list[CertifiedHit]:
    """Active answers only: a retired answer is history — it must never be served
    on an exact match nor fed to the generator as an exemplar."""
    rows = session.execute(
        text(
            "SELECT id, lens, question, sql, created_by, created_at, "
            "slots, sample_bindings, verified_prose, "
            "1 - (embedding <=> CAST(:q AS vector)) AS score "
            "FROM certified_answer WHERE lens = :l AND embedding IS NOT NULL "
            "AND status = 'active' "
            "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
        ),
        {"q": _vec(query_embedding), "l": lens, "k": k},
    ).all()
    return [
        CertifiedHit(
            CertifiedAnswer(
                str(r[0]),
                r[1],
                r[2],
                r[3],
                r[4],
                r[5].isoformat(),
                slots=_verified(r[6]),
                sample_bindings=_jsonlist(r[7]),
                verified_prose=r[8],
            ),
            float(r[9]),
        )
        for r in rows
    ]


#: The one sentence every surface says about that condition (`dst apply`,
#: `dst test`) — one wording, one named recovery.
UNEMBEDDED_WARNING = (
    "{n} certified answers have no embedding — they can never match; run `dst reindex`"
)


def count_unembedded(session: Session, lens: str) -> int:
    """Answers that CANNOT match, counted off the vector itself rather than off
    status (the point is not to trust the status column). Retired ones are
    excluded — never matching is their job."""
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM certified_answer "
                "WHERE lens = :l AND embedding IS NULL AND status <> 'retired'"
            ),
            {"l": lens},
        ).scalar_one()
    )


def update(
    session: Session,
    answer_id: str,
    *,
    sql: str,
    verified_value: dict[str, Any] | None = None,
    source: str | None = None,
    verified_by: str | None = None,
    bindings: dict[str, str] | None = None,
    status: str | None = None,
    slots: dict[str, Any] | None = None,
    sample_bindings: list[dict[str, Any]] | None = None,
    embedding: list[float] | None = None,
    verified_dialect: str | None = None,
    clear_verified_dialect: bool = False,
    verified_prose: str | None = None,
    clear_verified_prose: bool = False,
) -> int:
    """Update an answer's SQL/verified_value in place (apply's upsert path). The
    question is the upsert key and never changes here. source/verified_by/
    bindings/status/slots/sample_bindings COALESCE: None keeps the stored value
    (a file that omits source must not erase the stamped one), a value
    overwrites. ``embedding`` COALESCEs too — passed only when a template's
    anchor binding changed (the vector is of the sample-bound
    question, so a new first binding re-anchors the match). Returns rows
    affected.

    ``verified_prose`` COALESCEs like provenance; ``clear_verified_prose`` is
    the explicit erase — apply passes it when the SQL is re-authored, because
    stored prose describes the OLD SQL's result and must never outlive it.

    Status is reconciled against the RESULTING embedding, not taken on
    faith — re-activating an unembedded answer lands pending_embedding, and an
    anchor vector arriving for a pending one promotes it. Every SET reads the
    OLD row, so the two COALESCEs agree on what the update leaves behind."""
    res = session.execute(
        text(
            "UPDATE certified_answer SET sql = :sql, verified_value = CAST(:vv AS jsonb), "
            "source = COALESCE(:src, source), verified_by = COALESCE(:vb, verified_by), "
            "verified_prose = CASE WHEN :clear_vp THEN NULL "
            "  ELSE COALESCE(:vp, verified_prose) END, "
            "bindings = COALESCE(CAST(:b AS jsonb), bindings), "
            "status = CASE "
            "  WHEN COALESCE(:st, status) = 'active' "
            "       AND COALESCE(CAST(:e AS vector), embedding) IS NULL "
            f"    THEN '{PENDING_EMBEDDING}' "
            f" WHEN COALESCE(:st, status) = '{PENDING_EMBEDDING}' "
            "       AND COALESCE(CAST(:e AS vector), embedding) IS NOT NULL "
            "    THEN 'active' "
            "  ELSE COALESCE(:st, status) END, "
            "slots = COALESCE(CAST(:slots AS jsonb), slots), "
            "sample_bindings = COALESCE(CAST(:samples AS jsonb), sample_bindings), "
            "embedding = COALESCE(CAST(:e AS vector), embedding), "
            "verified_dialect = CASE WHEN :vd_clear THEN NULL "
            "  ELSE COALESCE(:vd, verified_dialect) END "
            "WHERE id = :i"
        ),
        {
            "sql": sql,
            "vv": json.dumps(verified_value) if verified_value is not None else None,
            "src": source,
            "vb": verified_by,
            "b": json.dumps(bindings) if bindings is not None else None,
            "st": status,
            "slots": json.dumps(slots) if slots is not None else None,
            "samples": json.dumps(sample_bindings) if sample_bindings is not None else None,
            "e": _vec(embedding) if embedding is not None else None,
            "vd": verified_dialect,
            "vd_clear": clear_verified_dialect,
            "vp": verified_prose,
            "clear_vp": clear_verified_prose,
            "i": answer_id,
        },
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def delete(session: Session, answer_id: str) -> int:
    res = session.execute(text("DELETE FROM certified_answer WHERE id = :i"), {"i": answer_id})
    return int(res.rowcount)  # type: ignore[attr-defined]
