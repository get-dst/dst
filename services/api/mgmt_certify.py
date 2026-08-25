"""Control-plane certified-answer management (/mgmt/lenses/{lens}/certified). Admin-authed.

Approve a question→SQL pair for a lens so future matching questions are served from it
(grounded, cheap, trusted). Create directly, or certify a prior answer by request_id.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.api.llm import require_embedder
from services.auth.deps import AdminIdentity, get_admin_identity, get_admin_org, get_app_session
from services.certify import store
from services.certify.bindings import certified_bindings, foreign_tables
from services.certify.generate import generate_for_lens
from services.db import embedding_meta
from services.lenses import store as lens_store
from services.lenses.connections import resolve_connector
from services.llm import registry
from services.llm.assist import assist_llm
from services.reviews.store import get_trace
from services.runtime import sql_guard

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
        shape = sql_guard.check(body.sql, model, trust_tables=True)
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


def _bindings(session: Session, lens: str, sql: str) -> dict[str, str] | None:
    """Derived staleness bindings against the lens's current model (published,
    else draft) — None when the lens has no bundle to bind against."""
    row = lens_store.get_lens(session, lens)
    raw = (row or {}).get("published") or (row or {}).get("draft")
    if raw is None:
        return None
    bundle = lens_store.LensBundle.model_validate(raw)
    return certified_bindings(sql, bundle.semantic_model)


@router.post("/from-request/{request_id}", status_code=201)
def certify_request(
    lens: str,
    request_id: str,
    body: CertifyFromRequestBody | None = None,
    session: Session = Depends(get_app_session),
    identity: AdminIdentity = Depends(get_admin_identity),
) -> dict[str, str]:
    """Certify a prior answer: snapshot its question + SQL from the request log.

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
    bindings = _bindings(session, lens, trace.sql)
    # The trace already holds the prose that was composed ONCE from
    # this SQL's executed result — and certifying the request approves that
    # exact answer, so it becomes the verbatim serve (no per-request composer
    # call from here on). Only a served data answer qualifies: a graded
    # confidence is the tell that prose was written over real rows, never an
    # error/refusal message.
    verified_prose = trace.answer if trace.answer and trace.confidence else None
    if registry.resolve_embedder() is None:
        cid = store.create(
            session,
            lens,
            trace.question,
            trace.sql,
            None,
            created_by=identity.actor,
            source=source,
            verified_by=verified_by,
            bindings=bindings,
            verified_prose=verified_prose,
        )
        return {"id": cid, "lens": lens, "warning": NO_EMBEDDER_WARNING}
    emb = _embed(session, trace.question)
    cid = store.create(
        session,
        lens,
        trace.question,
        trace.sql,
        emb,
        created_by=identity.actor,
        source=source,
        verified_by=verified_by,
        bindings=bindings,
        verified_prose=verified_prose,
    )
    return {"id": cid, "lens": lens}


@router.delete("/{answer_id}", status_code=204)
def delete_certified(
    lens: str, answer_id: str, session: Session = Depends(get_app_session)
) -> None:
    """Delete a certified answer. 404 when unknown."""
    if store.delete(session, answer_id) == 0:
        raise HTTPException(status_code=404, detail="certified answer not found")
