"""Catalog-pass orchestrator: collect a connection's metadata profiles
and persist them.

Uses the connector's `CatalogProfiler` capability where it exists (engine-specific
enrichment: descriptions, partitions, freshness, pg_stats, declared FKs) and falls
back to a basic profile derived from `introspect()` otherwise, so every `Connector`
— including fakes and third-party implementations — profiles for free. Declared-FK
join candidates land as `join_candidate` rows for the approval flow. Never
runs inside a query (profiling is trigger/staleness work).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from services.contracts.profile import TableProfile, profiles_from_snapshot
from services.contracts.protocols import CatalogProfiler, Connector, TargetedIntrospect
from services.lenses import profile_store

logger = logging.getLogger("dst")


def catalog_profiles(
    connector: Connector, connection: str, priority_tables: set[str] | None = None
) -> list[TableProfile]:
    """The catalog pass without the store, re-keyed to the registered connection name.

    `dst introspect --profile` runs on the file-first path, where there is no
    session and nothing stored to read back — it needs the pass, not the store.

    ``priority_tables`` — the tables the semantic layer reads — are GUARANTEED
    in the returned set: wide warehouses cap the catalog listing, and a cap can
    be spent entirely on whichever dataset sorts first (an unrelated sync log,
    say) — zero coverage of every table that mattered, after which the next
    apply tells the author their correct table names are wrong.
    Any priority table the capped pass missed is resolved explicitly through
    the connector's uncapped ``introspect_tables`` path.
    """
    from services.project.warehouse_drift import same_table

    profiles = (
        connector.profile_catalog()
        if isinstance(connector, CatalogProfiler)
        else profiles_from_snapshot(connector.introspect())
    )
    if priority_tables:
        have = [p.table for p in profiles]
        missing = sorted(t for t in priority_tables if not any(same_table(t, h) for h in have))
        if missing and isinstance(connector, TargetedIntrospect):
            logger.warning(
                "catalog pass missed %d semantic-layer table(s) (listing cap on a wide "
                "warehouse) — resolving them explicitly: %s",
                len(missing),
                ", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""),
            )
            backfill = profiles_from_snapshot(connector.introspect_tables(missing))
            profiles = profiles + backfill
    return [p.model_copy(update={"connection": connection}) for p in profiles]


def run_catalog_pass(session: Session, connector: Connector, connection: str) -> list[TableProfile]:
    """Profile `connection`'s tables (metadata only), upsert them, record FK candidates.

    Returns the stored profiles, re-keyed to the registered connection name (a
    connector only knows its own label, not what the org calls the connection).
    Dropped tables are pruned so the store mirrors the catalog; re-discovered
    join candidates never reset an existing approval/rejection.
    """
    profiles = catalog_profiles(connector, connection)
    candidates = (
        connector.catalog_join_candidates() if isinstance(connector, CatalogProfiler) else []
    )
    for profile in profiles:
        profile_store.upsert_profile(session, profile)
    profile_store.prune_profiles(session, connection, [p.table for p in profiles])
    if candidates:
        profile_store.record_candidates(session, connection, candidates)
    return profiles
