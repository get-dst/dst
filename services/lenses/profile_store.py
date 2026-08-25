"""Table-profile repository — the persisted physical truth per (connection, table).

Profiles upsert per (connection, table); the prior payload survives in ``previous``
for the drift diff. Join candidates land here from the catalog pass
(declared FKs) and later from inference; approve/reject just
flips ``status`` — writing an approved candidate into `SemanticModel.joins` is
the inference pass's job. Org-scoped via RLS, mirroring `services/certify/store.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.contracts.profile import JoinCandidate, TableProfile

# Catalog-pass profiles refresh roughly daily.
DEFAULT_TTL_HOURS = 24.0


@dataclass
class StoredProfile:
    profile: TableProfile
    previous: TableProfile | None
    profiled_at: datetime


# ── profiles ─────────────────────────────────────────────────────────────────


def upsert_profile(session: Session, profile: TableProfile) -> None:
    """Insert or replace the profile for (connection, table), keeping the prior payload."""
    session.execute(
        text(
            """
            INSERT INTO table_profile (org_id, connection, table_name, profile, profiled_at)
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :c, :t, CAST(:p AS jsonb), :at
            )
            ON CONFLICT (org_id, connection, table_name) DO UPDATE
            SET previous = table_profile.profile,
                profile = EXCLUDED.profile,
                profiled_at = EXCLUDED.profiled_at
            """
        ),
        {
            "c": profile.connection,
            "t": profile.table,
            "p": profile.model_dump_json(),
            "at": profile.profiled_at,
        },
    )


def get_profile(session: Session, connection: str, table: str) -> StoredProfile | None:
    row = session.execute(
        text(
            "SELECT profile, previous, profiled_at FROM table_profile "
            "WHERE connection = :c AND table_name = :t"
        ),
        {"c": connection, "t": table},
    ).first()
    if row is None:
        return None
    return _stored(row[0], row[1], row[2])


def list_profiles(session: Session, connection: str) -> list[StoredProfile]:
    rows = session.execute(
        text(
            "SELECT profile, previous, profiled_at FROM table_profile "
            "WHERE connection = :c ORDER BY table_name"
        ),
        {"c": connection},
    ).all()
    return [_stored(r[0], r[1], r[2]) for r in rows]


def prune_profiles(session: Session, connection: str, keep_tables: list[str]) -> int:
    """Drop stored profiles for tables that no longer exist on the connection."""
    res = session.execute(
        text(
            "DELETE FROM table_profile WHERE connection = :c "
            "AND NOT (table_name = ANY(CAST(:keep AS text[])))"
        ),
        {"c": connection, "keep": keep_tables},
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def is_stale(
    profiled_at: datetime, *, ttl_hours: float = DEFAULT_TTL_HOURS, now: datetime | None = None
) -> bool:
    """True when a profile collected at ``profiled_at`` has outlived its TTL."""
    if profiled_at.tzinfo is None:
        profiled_at = profiled_at.replace(tzinfo=UTC)
    moment = now or datetime.now(UTC)
    return profiled_at < moment - timedelta(hours=ttl_hours)


def _stored(profile: object, previous: object, profiled_at: datetime) -> StoredProfile:
    return StoredProfile(
        profile=TableProfile.model_validate(profile),
        previous=TableProfile.model_validate(previous) if previous is not None else None,
        profiled_at=profiled_at,
    )


# ── join candidates ──────────────────────────────────────────────────────────


def record_candidates(session: Session, connection: str, candidates: list[JoinCandidate]) -> int:
    """Insert candidates that aren't already known; never resets an existing status.

    Returns how many rows were actually inserted.
    """
    inserted = 0
    for cand in candidates:
        res = session.execute(
            text(
                """
                INSERT INTO join_candidate (
                    org_id, connection, left_table, left_columns,
                    right_table, right_columns, evidence, overlap_ratio, status
                )
                VALUES (
                    NULLIF(current_setting('app.current_org', true), '')::uuid,
                    :c, :lt, CAST(:lc AS jsonb), :rt, CAST(:rc AS jsonb), :e, :r, :s
                )
                ON CONFLICT (org_id, connection, left_table, left_columns,
                             right_table, right_columns)
                DO NOTHING
                """
            ),
            {
                "c": connection,
                "lt": cand.left_table,
                "lc": json.dumps(cand.left_columns),
                "rt": cand.right_table,
                "rc": json.dumps(cand.right_columns),
                "e": cand.evidence,
                "r": cand.overlap_ratio,
                "s": cand.status,
            },
        )
        inserted += int(res.rowcount)  # type: ignore[attr-defined]
    return inserted


def list_candidates(
    session: Session, connection: str, status: str | None = None
) -> list[JoinCandidate]:
    sql = (
        "SELECT id, left_table, left_columns, right_table, right_columns, "
        "evidence, overlap_ratio, status FROM join_candidate WHERE connection = :c"
    )
    params: dict[str, object] = {"c": connection}
    if status is not None:
        sql += " AND status = :s"
        params["s"] = status
    rows = session.execute(text(sql + " ORDER BY left_table, right_table"), params).all()
    return [
        JoinCandidate(
            id=str(r[0]),
            left_table=r[1],
            left_columns=list(r[2]),
            right_table=r[3],
            right_columns=list(r[4]),
            evidence=r[5],
            overlap_ratio=r[6],
            status=r[7],
        )
        for r in rows
    ]


def approve_candidate(session: Session, candidate_id: str) -> int:
    return _set_candidate_status(session, candidate_id, "approved")


def reject_candidate(session: Session, candidate_id: str) -> int:
    return _set_candidate_status(session, candidate_id, "rejected")


def _set_candidate_status(session: Session, candidate_id: str, status: str) -> int:
    res = session.execute(
        text("UPDATE join_candidate SET status = :s WHERE id = :i"),
        {"s": status, "i": candidate_id},
    )
    return int(res.rowcount)  # type: ignore[attr-defined]
