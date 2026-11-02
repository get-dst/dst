"""Control-plane certified-answer management (/mgmt/lenses/{lens}/certified). Admin-authed.

Approve a question→SQL pair for a lens so future matching questions are served from it
(grounded, cheap, trusted). Create directly, or certify a prior answer by request_id.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.api.llm import require_embedder
from services.auth.deps import AdminIdentity, get_admin_identity, get_admin_org, get_app_session
from services.certify import store
from services.certify.bindings import certified_bindings, foreign_tables
from services.certify.generate import generate_for_lens
from services.contracts.cube_query import CubeQuery
from services.cube.context import CubeContext
from services.db import embedding_meta
from services.lenses import store as lens_store
from services.lenses.connections import resolve_connector
from services.llm import registry
from services.llm.assist import assist_llm
from services.reviews.store import get_trace
from services.runtime import sql_guard
from services.runtime.pipeline import WITHHELD_ANSWER
from services.serve import door as cube_door
from services.serve.cube_lane import inline_params

router = APIRouter(prefix="/mgmt/lenses/{lens}/certified", tags=["certified"])


class CertifyBody(BaseModel):
    question: str
    sql: str


def _embed(session: Session, question: str) -> list[float]:
    embedder = require_embedder()
    embedding_meta.guard_write(session, embedder)
    vec: list[float] = embedder.embed([question])[0]
    return vec


@router.get("")
def list_certified(
    lens: str, session: Session = Depends(get_app_session)
) -> list[dict[str, object]]:
    """The lens's certified library including retired answers — the history view;
    serving, matching, and `dst test` see only active ones."""
    return [
        {
            "id": a.id,
            "question": a.question,
            "sql": a.sql,
            "created_by": a.created_by,
            "created_at": a.created_at,
            "verified_value": a.verified_value,
            "verified_prose": a.verified_prose,
            "source": a.source,
            "verified_by": a.verified_by,
            # History view: retired answers still list here (and in export) —
            # only serving/matching/testing filter them out.
            "status": a.status,
        }
        for a in store.list_for_lens(session, lens)
    ]


@router.post("/generate", status_code=201)
def generate_certified(
    lens: str,
    org_id: uuid.UUID = Depends(get_admin_org),
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """Generate certified question→SQL pairs from the lens's governed definitions.

    For each definition the wizard scaffolded from data + context, draft a representative
    question + SQL, run it read-only for a verified value, and store it as a certified
    answer (so the serving path uses it). Returns the generated set — stored ones carry an
    id + verified value; skipped/failed ones carry an error.
    """
    row = lens_store.get_lens(session, lens)
    if row is None or row.get("draft") is None:
        raise HTTPException(status_code=404, detail=f"lens '{lens}' has no draft")
    bundle = lens_store.LensBundle.model_validate(row["draft"])
    if not bundle.config.connections:
        raise HTTPException(status_code=400, detail="lens has no connection")
    if not bundle.semantic_model.definitions:
        raise HTTPException(status_code=400, detail="lens has no definitions to certify")
    require_embedder()
    pair = assist_llm()
    if pair is None:
        raise HTTPException(status_code=503, detail="no assist model configured")
    llm, model_name = pair.llm, pair.name
    connector = resolve_connector(bundle.config.connections[0], org_id)
    # embedding failures (incl. rate limits) raise ProviderError -> global 502 handler
    results = generate_for_lens(
        session,
        lens=lens,
        semantic_model=bundle.semantic_model,
        connector=connector,
        embedder=require_embedder(),
        llm=llm,
        model_name=model_name,
    )
    stored = sum(1 for r in results if r.id is not None)
    return {
        "lens": lens,
        "generated": stored,
        "results": [
            {
                "term": r.term,
                "question": r.question,
                "sql": r.sql,
                "id": r.id,
                "verified_value": r.verified_value,
                "error": r.error,
            }
            for r in results
        ],
    }


@router.post("", status_code=201)
def create_certified(
    lens: str,
    body: CertifyBody,
    session: Session = Depends(get_app_session),
    identity: AdminIdentity = Depends(get_admin_identity),
) -> dict[str, str]:
    """Certify a question→SQL pair directly. The SQL passes the same gates as every
    certified entry point — shape check + lens-boundary table check, 422 on either.
    Without an embedding provider it stores unembedded and returns a `warning`
    (backfilled by `dst reindex`)."""
    # Same gates every certified-SQL entry point runs (the apply path's CB3
    # pair): shape (single SELECT, no bare star, reserved schema) + the
    # lens-boundary table check. A direct POST must not be the ungated door.
    row = lens_store.get_lens(session, lens)
    raw = (row or {}).get("published") or (row or {}).get("draft")
    if raw is not None:
        model = lens_store.LensBundle.model_validate(raw).semantic_model
        try:
            foreign = foreign_tables(body.sql, model)
        except Exception as exc:  # noqa: BLE001 — parse failure is the same gate
            raise HTTPException(
                status_code=422, detail=f"SQL does not parse as {model.dialect}: {exc}"
            ) from exc
        shape = sql_guard.check(body.sql, model.every_exception(), trust_tables=True)
        if not shape.ok:
            raise HTTPException(status_code=422, detail=str(shape.reason))
        if foreign:
            named = ", ".join(f"'{t}'" for t in foreign)
            raise HTTPException(
                status_code=422,
                detail=f"references {named} — not in this lens's model; a certified "
                "answer must not smuggle ungoverned tables past the lens boundary",
            )
    # Embedder-less installs degrade exactly like from-request (0027): stored
    # unembedded + a warning, never a dead-end.
    emb = None if registry.resolve_embedder() is None else _embed(session, body.question)
    cid = store.create(
        session,
        lens,
        body.question,
        body.sql,
        emb,
        # The resolved identity, not a role string — `certified_by` reaches the
        # served answer's trust_summary, and "approved by admin" names nobody.
        created_by=identity.actor,
        bindings=_bindings(session, lens, body.sql),
    )
    out = {"id": cid, "lens": lens}
    if emb is None:
        out["warning"] = NO_EMBEDDER_WARNING
    return out


NO_EMBEDDER_WARNING = (
    "certified without an embedding — no embedding provider is configured, so this "
    "answer will not be served or matched until one is added and `dst reindex` runs"
)


class CertifyFromRequestBody(BaseModel):
    source: str | None = None  # defaults to "review:<request_id>" — trust must be traceable
    verified_by: str | None = None
    # An UNTYPED serve (the raw-SQL escalation a caller accepted) is a
    # different certification: a human has read the SQL. Say so.
    allow_untyped: bool = False
    # For an outside-lens answer on a lens served through Cube: the Cube
    # query the reviewer supplies as the governed reading of the question.
    # dst compiles it, runs both and certifies only on row equality; the
    # response then carries the ``kind: cube`` pair for the lens's
    # certified_answers.yaml (the file is where a pair's query lives).
    cube_query: dict[str, Any] | None = None


def _bindings(session: Session, lens: str, sql: str) -> dict[str, str] | None:
    """Derived staleness bindings against the lens's current model (published,
    else draft) — None when the lens has no bundle to bind against."""
    row = lens_store.get_lens(session, lens)
    raw = (row or {}).get("published") or (row or {}).get("draft")
    if raw is None:
        return None
    bundle = lens_store.LensBundle.model_validate(raw)
    return certified_bindings(sql, bundle.semantic_model)


def _ruled_correction_sql(session: Session, request_id: str) -> str | None:
    """The corrected SQL a human ruled onto this request, if any.

    `dst rule --certify` promotes by request_id, so the correction the reviewer
    just wrote lives one hop away on the ticket. Reading it here is what makes
    "correct, then certify" mean what it says — the alternative (certifying the
    SQL the reviewer rejected) is defect A in the field ledger.
    """
    from services.reviews import store as review_store

    for t in review_store.list_queue(session, None):
        if t.request_id == request_id and t.correction and t.correction.corrected_sql:
            return t.correction.corrected_sql
    return None


def _replace_same_question(session: Session, lens: str, question: str) -> int:
    """Delete existing pairs for the exact question — one question, one pair.

    Without this, every re-certification stacked another active row: which one
    served was decided by pgvector over identical embeddings, and `dst apply`'s
    question-keyed map could not see (or retire) the extras.
    """
    q = question.strip().lower()
    return sum(
        store.delete(session, a.id)
        for a in store.list_for_lens(session, lens)
        if a.question.strip().lower() == q
    )


@router.post("/{answer_id}/retire", status_code=200)
def retire_certified(
    lens: str,
    answer_id: str,
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """Stop serving a certified answer, keeping its history.

    The counterpart to DELETE: retiring is what you want when the answer was
    real and is now wrong — it is never served, matched, or tested again, but
    the row (and its provenance) survives. Server-origin answers had no other
    way out: files own only file-originated rows, so a review-promoted pair was
    unremovable without destroying the record.
    """
    answer = store.get(session, answer_id)
    if answer is None or answer.lens != lens:
        raise HTTPException(status_code=404, detail=f"certified answer '{answer_id}' not found")
    if not store.set_status(session, answer_id, "retired"):
        raise HTTPException(status_code=409, detail="answer could not be retired")
    return {"id": answer_id, "lens": lens, "status": "retired", "question": answer.question}


@router.post("/from-request/{request_id}", status_code=201)
def certify_request(
    lens: str,
    request_id: str,
    body: CertifyFromRequestBody | None = None,
    session: Session = Depends(get_app_session),
    identity: AdminIdentity = Depends(get_admin_identity),
    org_id: uuid.UUID = Depends(get_admin_org),
) -> dict[str, object]:
    """Certify a prior answer: snapshot its question + SQL from the request log.

    An outside-lens answer (the `sql` tool with a question, an answer read
    from an exposed table) certifies the same way: ``kind: sql``, pinned to
    the blueprints of the tables it read. With ``cube_query`` on a lens served
    through Cube the response also carries a ``kind: cube`` pair, verified by
    row equality, for the lens's certified_answers.yaml.

    Without an embedding provider the promotion still succeeds — the answer is stored
    with a NULL embedding (skipped by the matcher, backfilled by ``dst reindex``)
    and the response carries a ``warning`` — a human APPROVE ruling must never dead-end
    on provider config.
    """
    trace = get_trace(session, request_id)
    if trace is None or not trace.sql:
        raise HTTPException(status_code=404, detail=f"no traced SQL for request '{request_id}'")
    source = (body.source if body else None) or f"review:{request_id}"
    verified_by = body.verified_by if body else None
    # A ruled correction OUTRANKS the traced SQL. Certifying a corrected answer
    # against the original SQL is the one thing this endpoint must never do: it
    # is what the reviewer just declared wrong, and it then serves verbatim
    # forever.
    sql = trace.sql
    corrected = _ruled_correction_sql(session, request_id)
    if corrected and corrected.strip() and corrected.strip() != trace.sql.strip():
        sql = corrected.strip()
    bindings = _bindings(session, lens, sql)
    if trace.question.strip() == sql.strip():
        raise HTTPException(
            status_code=409,
            detail="this read carried no question (the sql tool was called without one), "
            "so there is no question→SQL pair to certify; ask it again with `question`",
        )
    pair = (
        _cube_pair(session, lens, org_id, trace.question, sql, body.cube_query)
        if body is not None and body.cube_query is not None
        else None
    )
    # The trace already holds the prose that was composed ONCE from
    # this SQL's executed result — and certifying the request approves that
    # exact answer, so it becomes the verbatim serve (no per-request composer
    # call from here on). Only a served data answer qualifies: a graded
    # confidence is the tell that prose was written over real rows, never an
    # error/refusal message.
    # Prose belongs to the SQL it was composed from: keep it only when the
    # traced SQL is what we are certifying (a correction invalidates it).
    verified_prose = (
        trace.answer
        if trace.answer
        and trace.confidence
        and sql == trace.sql
        and not trace.answer.startswith(WITHHELD_ANSWER)
        else None
    )
    # One question, one certified pair. Re-certifying the same question REPLACES
    # (the same rule `_apply_certified` follows) — stacking a second active row
    # made the served answer arbitrary and unreachable from files.
    replaced = _replace_same_question(session, lens, trace.question)
    # The typed gold: the trace's resolution ledger, when the traced SQL is
    # what we certify (a correction resolves differently; it carries none).
    resolution = _trace_resolution(session, request_id) if sql == trace.sql else None
    if (
        resolution is not None
        and resolution.get("typed") is False
        and not (body is not None and body.allow_untyped)
    ):
        raise HTTPException(
            status_code=409,
            detail="this answer was served UNTYPED — raw-SQL generation the caller opted into, "
            "not a typed resolution. Certifying it is a different act: read the SQL, then "
            "pass allow_untyped: true (dst reviews rule … --certify --allow-untyped).",
        )
    if registry.resolve_embedder() is None:
        cid = store.create(
            session,
            lens,
            trace.question,
            sql,
            None,
            created_by=identity.actor,
            source=source,
            verified_by=verified_by,
            bindings=bindings,
            verified_prose=verified_prose,
            resolution=resolution,
        )
        return {
            "id": cid,
            "lens": lens,
            "replaced": replaced,
            "corrected": sql != trace.sql,
            "warning": NO_EMBEDDER_WARNING,
            "pair": pair,
        }
    emb = _embed(session, trace.question)
    cid = store.create(
        session,
        lens,
        trace.question,
        sql,
        emb,
        created_by=identity.actor,
        source=source,
        verified_by=verified_by,
        bindings=bindings,
        verified_prose=verified_prose,
        resolution=resolution,
    )
    return {
        "id": cid,
        "lens": lens,
        "replaced": replaced,
        "corrected": sql != trace.sql,
        "pair": pair,
    }


def _rows_key(result: object) -> list[str]:
    rows = getattr(result, "rows", [])
    return sorted(json.dumps([_cell(v) for v in r], default=str) for r in rows)


def _cell(value: object) -> object:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    return int(number) if number == int(number) else round(number, 9)


def _sha(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _cube_pair(
    session: Session, lens: str, org_id: uuid.UUID, question: str, sql: str, raw: dict[str, Any]
) -> dict[str, Any]:
    """The ``kind: cube`` pair for an outside-lens answer: the reviewer's Cube
    query compiled through the lens's Cube, run beside the traced SQL, and
    accepted only when the two return the same rows. 400 when the lens does
    not serve through Cube or the query is not one; 409 when the rows differ
    (the detail says how), so a pair never certifies a reading its SQL did
    not have."""
    row = lens_store.get_lens(session, lens)
    bundle_raw = (row or {}).get("published") or (row or {}).get("draft")
    if bundle_raw is None:
        raise HTTPException(status_code=404, detail=f"lens '{lens}' not found")
    bundle = lens_store.LensBundle.model_validate(bundle_raw)
    if cube_door.serving_core(bundle.config, bundle.semantic_model) != "cube":
        raise HTTPException(
            status_code=400,
            detail=f"lens '{lens}' does not serve through Cube: a cube_query certifies "
            "nothing here; approve the SQL pair instead",
        )
    try:
        query = CubeQuery.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — pydantic's message names the field
        raise HTTPException(status_code=400, detail=f"cube_query: {exc}") from exc
    ctx = CubeContext.operator(lens=lens)
    try:
        client, view = cube_door.open_view(lens, bundle.config, ctx=ctx)
        compiled = client.sql(query, ctx)
    except cube_door.CubeUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — Cube's refusal is the answer
        raise HTTPException(status_code=400, detail=f"Cube did not compile it: {exc}") from exc
    dialect = bundle.semantic_model.dialect
    connector = resolve_connector(bundle.config.connections[0], org_id, session=session)
    statement = inline_params(compiled.sql, compiled.params, dialect)
    theirs = connector.execute(statement, read_only=True, row_limit=5001)
    mine = connector.execute(sql, read_only=True, row_limit=5001)
    if _rows_key(theirs) != _rows_key(mine):
        raise HTTPException(
            status_code=409,
            detail=f"the Cube query's rows differ from the answer's: {len(theirs.rows)} row(s) "
            f"against {len(mine.rows)}; a pair is certified on row equality only",
        )
    hashed = {"columns": list(mine.columns), "rows": _rows_key(mine)}
    return {
        "kind": "cube",
        "question": question,
        "query": query.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        "sql": sql,
        "verified": {
            "model_version": view.model_version,
            "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
            "result_sha256": _sha(hashed),
            "as_of": datetime.now(UTC).date().isoformat(),
        },
    }


def _trace_resolution(session: Session, request_id: str) -> dict[str, object] | None:
    row = session.execute(
        text("SELECT resolution FROM request_log WHERE request_id = :r"), {"r": request_id}
    ).first()
    return row[0] if row and isinstance(row[0], dict) else None


@router.delete("/{answer_id}", status_code=204)
def delete_certified(
    lens: str, answer_id: str, session: Session = Depends(get_app_session)
) -> None:
    """Delete a certified answer. 404 when unknown."""
    if store.delete(session, answer_id) == 0:
        raise HTTPException(status_code=404, detail="certified answer not found")
