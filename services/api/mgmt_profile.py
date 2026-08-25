"""Table-profile management. Admin-authed.

Join candidates: inferred join keys (FK / dbt / name+overlap evidence)
surface for the approve-don't-author flow — approval flips the candidate and writes
a `Join` onto the lens's draft semantic model. Refresh + drift: re-profile
a connection on demand and see what changed since the previous pass, scoped per
connection or per lens (with the lens's approved eval cases named for re-baseline).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from services.auth.deps import get_admin_org, get_app_session
from services.evals import store as eval_store
from services.lenses import joins, profile_drift, profile_store
from services.lenses import store as lens_store
from services.lenses.connections import resolve_connector
from services.lenses.profile_enrich_lm import run_description_pass
from services.lenses.profiler import run_sampling_pass
from services.lenses.profiler_catalog import run_catalog_pass

router = APIRouter(prefix="/mgmt/lenses/{lens}/join-candidates", tags=["profile"])
connection_router = APIRouter(prefix="/mgmt/connections/{name}/profile", tags=["profile"])
lens_drift_router = APIRouter(prefix="/mgmt/lenses/{lens}/profile-drift", tags=["profile"])
lens_profile_router = APIRouter(prefix="/mgmt/lenses/{lens}/profile", tags=["profile"])


def _load_bundle(session: Session, lens: str) -> lens_store.LensBundle:
    row = lens_store.get_lens(session, lens)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{lens}' not found")
    raw = row.get("draft") or row.get("published")
    if raw is None:
        raise HTTPException(status_code=400, detail=f"lens '{lens}' has no bundle")
    return lens_store.LensBundle.model_validate(raw)


def _scope(bundle: lens_store.LensBundle) -> tuple[str, set[str]]:
    if not bundle.config.connections:
        raise HTTPException(status_code=400, detail="lens has no connection")
    connection = bundle.config.connections[0]
    tables = {e.source.table for e in bundle.semantic_model.entities}
    return connection, tables


def _candidate_dict(c: object) -> dict[str, object]:
    from services.contracts.profile import JoinCandidate

    assert isinstance(c, JoinCandidate)
    return c.model_dump()


@router.get("")
def list_join_candidates(
    lens: str, session: Session = Depends(get_app_session)
) -> list[dict[str, object]]:
    """Candidates whose tables are inside the lens's scope (all statuses)."""
    bundle = _load_bundle(session, lens)
    connection, tables = _scope(bundle)
    out = []
    for c in profile_store.list_candidates(session, connection):
        if c.left_table in tables and c.right_table in tables:
            out.append(_candidate_dict(c))
    return out


@router.post("/infer", status_code=201)
def infer(
    lens: str,
    org_id: uuid.UUID = Depends(get_admin_org),
    session: Session = Depends(get_app_session),
) -> list[dict[str, object]]:
    """Run name+overlap inference for the lens scope now (push-down probes)."""
    bundle = _load_bundle(session, lens)
    connection, tables = _scope(bundle)
    try:
        connector = resolve_connector(connection, org_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"lens connection error: {exc}") from exc
    confirmed = joins.infer_join_candidates(session, connector, connection, tables=sorted(tables))
    return [_candidate_dict(c) for c in confirmed]


@router.post("/{candidate_id}/approve")
def approve(
    lens: str, candidate_id: str, session: Session = Depends(get_app_session)
) -> dict[str, object]:
    """Approve a candidate; when its tables map to lens entities, the join lands on
    the draft semantic model (publish promotes it)."""
    bundle = _load_bundle(session, lens)
    connection, _ = _scope(bundle)
    candidate = next(
        (c for c in profile_store.list_candidates(session, connection) if c.id == candidate_id),
        None,
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="join candidate not found")
    if profile_store.approve_candidate(session, candidate_id) == 0:
        raise HTTPException(status_code=404, detail="join candidate not found")
    in_model = joins.approve_into_model(bundle, candidate)
    if in_model:
        lens_store.update_draft(session, lens, bundle)
    return {"id": candidate_id, "status": "approved", "join_added_to_model": in_model}


@router.post("/{candidate_id}/reject")
def reject(
    lens: str, candidate_id: str, session: Session = Depends(get_app_session)
) -> dict[str, str]:
    """Reject a join candidate — it stops surfacing as a suggestion; the lens
    model is untouched. 404 unknown."""
    if profile_store.reject_candidate(session, candidate_id) == 0:
        raise HTTPException(status_code=404, detail="join candidate not found")
    return {"id": candidate_id, "status": "rejected"}


@connection_router.post("/refresh")
def refresh_profile(
    name: str,
    sample: bool = False,
    org_id: uuid.UUID = Depends(get_admin_org),
    session: Session = Depends(get_app_session),
) -> dict[str, object]:
    """Manual re-profile: run the catalog pass now and report
    what drifted against the previous version. With ``sample=true``, follow with the
    guarded sampling pass — capped, read-only warehouse reads that fill
    enum literals / null rates / freshness where the catalog left gaps. Either way the
    LLM description pass (F4) then fills any still-undocumented column."""
    try:
        connector = resolve_connector(name, org_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    profiles = run_catalog_pass(session, connector, name)
    if sample:
        profiles = run_sampling_pass(session, connector, name)
    profiles = run_description_pass(session, name)
    drift = profile_drift.connection_drift(session, name)
    return {
        "connection": name,
        "tables": len(profiles),
        "sampled": sample,
        "drift": [d.model_dump() for d in drift],
    }


@connection_router.get("/drift")
def get_connection_drift(
    name: str, session: Session = Depends(get_app_session)
) -> list[dict[str, object]]:
    """Per-table drift for a connection — what the newest profile changed against
    the previously recorded one."""
    return [d.model_dump() for d in profile_drift.connection_drift(session, name)]


@lens_profile_router.get("")
def get_lens_profile(lens: str, session: Session = Depends(get_app_session)) -> dict[str, object]:
    """The stored profiles for the lens's scope tables (the profile cards)."""
    bundle = _load_bundle(session, lens)
    connection, tables = _scope(bundle)
    stored = [
        s for s in profile_store.list_profiles(session, connection) if s.profile.table in tables
    ]
    return {
        "lens": lens,
        "connection": connection,
        "tables": [
            {
                **s.profile.model_dump(mode="json"),
                "profiled_at": s.profiled_at.isoformat(),
            }
            for s in stored
        ],
    }


@lens_drift_router.get("")
def get_lens_drift(lens: str, session: Session = Depends(get_app_session)) -> dict[str, object]:
    """Drift scoped to the lens's tables, with its approved eval cases named for
    re-baseline when the scope drifted — never silently."""
    bundle = _load_bundle(session, lens)
    connection, tables = _scope(bundle)
    items = [d for d in profile_drift.connection_drift(session, connection) if d.table in tables]
    affected_cases: list[str] = []
    if items:
        affected_cases = [c.id for c in eval_store.list_cases(session, lens, status="approved")]
    return {
        "lens": lens,
        "drift": [d.model_dump() for d in items],
        "eval_cases_needing_rebaseline": affected_cases,
    }
