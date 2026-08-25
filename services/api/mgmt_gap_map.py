"""The coverage gap-map, surfaced in the app. Admin-authed.

The capstone binding: ``GET /mgmt/gap-map?connection=…`` decomposes the warehouse's
drifting / uncovered metrics into governed-by-lens-X vs UNGOVERNED gaps, unifying
the router's live misses (``routing_decision`` declines, clustered) with the Audit's
documented ungoverned drift (the latest ``audit_run``'s findings). ``GET
/mgmt/lenses/{name}/coverage`` gives the per-lens slice: only the in-scope governed
findings a lens is accountable to resolve, never out-of-scope drift.

Both endpoints are thin: they pull the three already-persisted surfaces (router
declines, audit findings, published-lens coverage profiles) and hand them to the
pure binder in ``services.probe.gap_map`` — which decides governance by **metric
match, not table overlap** (the load-bearing seam; see that module's docstring).
No new tables; no new migration.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from services.api import audit_store, route_store
from services.api.surface import cluster_declines
from services.auth.deps import get_app_session
from services.lenses import store as lens_store
from services.probe.drift import DriftFinding
from services.probe.gap_map import (
    GapMap,
    LensCoverage,
    build_gap_map,
    lens_in_scope_findings,
)
from services.router import CoverageProfile
from services.router.profiles import coverage_profile

router = APIRouter(tags=["gap-map"])


def _published_profiles(session: Session) -> list[CoverageProfile]:
    """The coverage profile of every published lens — the governed surface the binder
    matches findings against. Best-effort per lens: a malformed bundle is skipped, not
    a 500, mirroring the audit/canon views' tolerance of partial data."""
    profiles: list[CoverageProfile] = []
    for _name, _display, _desc, bundle in lens_store.list_published(session):
        try:
            profiles.append(coverage_profile(bundle))
        except Exception:  # noqa: BLE001 — one bad lens must not sink the whole map
            continue
    return profiles


def _audit_findings(session: Session, connection: str) -> list[DriftFinding]:
    """The latest persisted audit's findings for the connection (empty if never run)."""
    run = audit_store.latest(session, connection)
    return list(run.findings) if run is not None else []


def compute_gap_map(session: Session, connection: str, *, days: int = 30) -> GapMap:
    """Assemble the connection's gap-map from the three persisted surfaces.

    Router declines over the trailing ``days`` are clustered into uncovered-metric
    groups (the same ``surface.cluster_declines`` the surface report uses); the audit
    contributes its latest run's ungoverned drift; the published lenses contribute
    their governed surface. The pure binder de-dupes + resolves governance by metric.
    """
    declines = cluster_declines(
        [d for d in route_store.list_window(session, days=days) if not d.covered]
    )
    return build_gap_map(
        connection,
        findings=_audit_findings(session, connection),
        declines=declines,
        profiles=_published_profiles(session),
    )


@router.get("/mgmt/gap-map")
def gap_map(
    connection: str = Query(..., description="the warehouse connection to decompose"),
    days: int = Query(default=30, ge=1, le=365),
    session: Session = Depends(get_app_session),
) -> GapMap:
    """The warehouse coverage decomposition for a connection: governed metrics (by
    which lens) vs ungoverned gaps, de-duped across the router's misses and the
    Audit's findings. Each gap carries its evidence source(s) and governing lens."""
    return compute_gap_map(session, connection, days=days)


@router.get("/mgmt/lenses/{name}/coverage")
def lens_coverage(
    name: str,
    connection: str = Query(..., description="the warehouse connection to scope against"),
    days: int = Query(default=30, ge=1, le=365),
    session: Session = Depends(get_app_session),
) -> LensCoverage:
    """A lens's in-scope coverage slice for a connection: only the audit/router
    findings whose metric this lens governs — the ones it is accountable to resolve
    canonically. Out-of-scope drift is excluded by construction (metric-governance
    binding, not table overlap). 404 if the lens is not published."""
    bundle = lens_store.resolve_published(session, name)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"published lens '{name}' not found")
    profile = coverage_profile(bundle)
    declines = cluster_declines(
        [d for d in route_store.list_window(session, days=days) if not d.covered]
    )
    return lens_in_scope_findings(
        profile,
        findings=_audit_findings(session, connection),
        declines=declines,
    )
