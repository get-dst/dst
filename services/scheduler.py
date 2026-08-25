"""Standing drift audits + certified evals — the in-process scheduler.

One asyncio loop in the app lifespan. Each cycle takes a Postgres advisory
lock so multi-worker/replica deployments run it exactly once, then per org:
audits every connection whose latest run — any terminal status, so an
`unsupported`/`error` outcome degrades to one retry per interval instead of
one per cycle — is older than DST_AUDIT_INTERVAL_HOURS, and re-runs the certified
parity suite for every published lens whose latest standing run is older than
DST_EVAL_INTERVAL_HOURS (both default 24; 0 disables that half — `dst audit
run` / `dst test` plus cron is the documented alternative). Sleeps before
the first cycle so short-lived processes (tests, one-shot commands) never
fire it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.api import audit_store, mgmt_audit
from services.config import settings
from services.db.session import admin_engine, org_session, set_org_context
from services.lenses import connection_store

log = logging.getLogger("dst")

_LOCK_KEY = 0x4B_55_52_41  # 'KURA'
_CHECK_SECONDS = 900  # look for stale audits every 15 min; staleness gates the work

last_cycle_at: datetime | None = None  # surfaced for observability/debugging


async def run_forever() -> None:
    if not settings.audit_interval_hours and not settings.eval_interval_hours:
        log.info("scheduler off (DST_AUDIT_INTERVAL_HOURS=0, DST_EVAL_INTERVAL_HOURS=0)")
        return
    log.info(
        "scheduler on: audits every %sh, evals every %sh, checked every %ss",
        settings.audit_interval_hours or "off",
        settings.eval_interval_hours or "off",
        _CHECK_SECONDS,
    )
    while True:
        await asyncio.sleep(_CHECK_SECONDS)
        try:
            await asyncio.to_thread(_cycle)
        except Exception:
            log.exception("scheduler cycle failed")


def _cycle() -> None:
    global last_cycle_at
    now = datetime.now(UTC)
    with admin_engine.connect() as lock_conn:
        got = lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY})
        if not got.scalar():
            return  # another replica owns this cycle
        try:
            orgs = [r[0] for r in lock_conn.execute(text("SELECT id FROM org")).all()]
            for org_id in orgs:
                if settings.audit_interval_hours:
                    _audit_org(org_id, now - timedelta(hours=settings.audit_interval_hours))
                if settings.eval_interval_hours:
                    _evals_org(org_id, now - timedelta(hours=settings.eval_interval_hours))
            last_cycle_at = datetime.now(UTC)
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})


def _evals_org(org_id: uuid.UUID, cutoff: datetime) -> None:
    """Standing certified evals — staleness-gated per lens,
    same retry semantics as audits: a failed lens logs and waits an interval."""
    from services.evals import service as evals_service

    try:
        evals_service.run_standing_certified(org_id, cutoff)
    except Exception:
        log.exception("scheduled evals failed for org %s", org_id)


def _audit_org(org_id: uuid.UUID, cutoff: datetime) -> None:
    with org_session(org_id) as session:
        for rec in connection_store.list_connections(session):
            # latest_any: a recorded unsupported/error run (no history catalog, missing
            # query-history permissions, …) counts as recent, so a connection that
            # can't be audited is retried once per interval, not every 15-min cycle.
            latest = audit_store.latest_any(session, rec.name)
            if latest is not None:
                created = latest.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if created >= cutoff:
                    continue  # fresh enough — also dodges the on-create BackgroundTask
            try:
                # run_tracked never raises: it records the terminal status (ok/empty/
                # unsupported/error) and logs permission problems as a one-line hint.
                mgmt_audit.run_tracked(session, rec.name, org_id=org_id)
                session.commit()
                # The commit ended the transaction that carried SET LOCAL
                # app.current_org — without re-setting it, the NEXT connection's
                # audit INSERT runs org-less and RLS rejects it, while latest_any
                # goes blind (RLS filters every row) so the staleness gate retries
                # it every 15-min cycle instead of once per interval. The symptom
                # is a repeating "scheduled audit failed" log with
                # InsufficientPrivilege on audit_run.
                set_org_context(session, org_id)
                log.info("scheduled drift audit ran: %s (org %s)", rec.name, org_id)
            except Exception:
                log.exception("scheduled audit failed for %s", rec.name)
            # The audit above mines QUERY HISTORY, which a connector may simply
            # not have (DuckDB → status `unsupported`). The schema diff needs
            # only a catalog read, so it runs regardless — a warehouse with no
            # history catalog is exactly the one whose drift nothing else would
            # catch. Same cadence, own try: a diff failure must not be
            # mistaken for "no drift", so it logs by name.
            try:
                _drift_check(session, rec.name, org_id)
                session.commit()
                set_org_context(session, org_id)  # same SET LOCAL rule as above
            except Exception:
                log.exception("scheduled schema-drift check failed for %s", rec.name)


def _drift_check(session: Session, connection: str, org_id: uuid.UUID) -> None:
    """Diff the connection's live catalog against its applied baseline; breaking
    drift files ONE deduplicated review ticket (origin `drift_audit`) naming the
    affected entities, definitions and certified answers — the review queue is
    the push channel."""
    from services.governance import drift_watch

    report = drift_watch.check_connection(session, connection, org_id)
    if report is None or not report.breaking:
        return
    ticket, created = drift_watch.file_ticket(session, report, origin="drift_audit")
    if created:
        log.warning("breaking schema drift on %s: ticket %s filed", connection, ticket.ticket_id)
