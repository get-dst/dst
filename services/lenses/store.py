"""DB-backed lens storage (draft -> publish). Org-scoped via RLS.

A lens row stores a draft and (after publish) a published bundle, each a
{config, semantic_model} pair. Callers query the *published* bundle.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import SemanticModel
from services.db.session import org_session


class LensBundle(BaseModel):
    config: LensConfig
    semantic_model: SemanticModel


class LensSummary(BaseModel):
    name: str
    display_name: str
    description: str
    status: str
    # Shape of the lens (from its bundle — published if live, else draft).
    entity_count: int = 0
    definition_count: int = 0
    question_count: int = 0
    # Usage (from request_log).
    query_count: int = 0
    last_queried_at: datetime | None = None
    # Provenance: False = never applied from files (API/wizard/
    # cloud-born) — surfaces render it "not in files"; plan lists it
    # server-only until adopted via `dst export --lens`.
    from_files: bool = True
    # The latest published lens_version (surfaces rendered a
    # version column from a key the listing never carried). None = never
    # published with a recorded version.
    version: int | None = None


def _stored(bundle: LensBundle) -> str:
    """The bundle as it goes into JSONB — an unset key is OMITTED, never ``null``.

    A stored payload must stay readable by the release that could have written it:
    ``lens_version.bundle_json`` is immutable history and a downgrade is a real
    operation. When ``ModelConfig.provider``/``model``/``temperature`` widened from
    ``str``/``float`` to optional, apply began writing literal ``null`` into all three
    columns for any lens with no ``model:`` block. The previous release, where those
    fields are plain ``str``/``float``, raised ValidationError on every read of such a
    bundle — and since ``plan`` enumerates every published bundle, the whole project
    surface 500'd, so the old code could not even repair its own project. Only a
    ``pg_restore`` got it back.

    Omitting the key instead leaves an older reader on its own default and a newer one
    reading "unset", which is what the value means. Every optional field in the bundle
    defaults to ``None``, so the round-trip through the current contract is exact —
    both properties are pinned in tests/test_bundle_rollback.py. The file renders
    (services/lenses/repo.py, services/project/plan.py) already dump this way; storage
    was the one boundary that did not.
    """
    return bundle.model_dump_json(exclude_none=True)


def _bundle_shape(bundle: dict[str, Any] | None) -> tuple[int, int, int]:
    """(entities, definitions, sample questions) of a stored bundle."""
    if not bundle:
        return (0, 0, 0)
    sm = bundle.get("semantic_model") or {}
    return (
        len(sm.get("entities") or []),
        len(sm.get("definitions") or []),
        len(sm.get("sample_queries") or []),
    )


def create_lens(session: Session, bundle: LensBundle) -> str:
    session.execute(
        text(
            """
            INSERT INTO lens (org_id, name, display_name, description, status, draft_json)
            VALUES (
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :name, :display_name, :description, 'draft', CAST(:draft AS jsonb)
            )
            """
        ),
        {
            "name": bundle.config.name,
            "display_name": bundle.config.display_name,
            "description": bundle.config.description,
            "draft": _stored(bundle),
        },
    )
    return bundle.config.name


def list_lenses(session: Session) -> list[LensSummary]:
    rows = session.execute(
        text(
            """
            SELECT l.name, l.display_name, l.description, l.status,
                   COALESCE(l.published_json, l.draft_json) AS bundle,
                   COALESCE(q.queries, 0) AS queries, q.last_asked,
                   v.version
            FROM lens l
            LEFT JOIN (
                SELECT lens, count(*) AS queries, max(created_at) AS last_asked
                FROM request_log
                WHERE COALESCE(generator_tier, '') <> 'probe'
                GROUP BY lens
            ) q ON q.lens = l.name
            LEFT JOIN (
                SELECT lens, max(version) AS version FROM lens_version GROUP BY lens
            ) v ON v.lens = l.name
            ORDER BY l.name
            """
        )
    ).all()
    file_born = lenses_from_files(session)
    out: list[LensSummary] = []
    for r in rows:
        entities, definitions, questions = _bundle_shape(r[4])
        out.append(
            LensSummary(
                name=r[0],
                display_name=r[1],
                description=r[2],
                status=r[3],
                entity_count=entities,
                definition_count=definitions,
                question_count=questions,
                query_count=int(r[5]),
                last_queried_at=r[6],
                from_files=r[0] in file_born,
                version=int(r[7]) if r[7] is not None else None,
            )
        )
    return out


def lens_names(session: Session) -> list[str]:
    """Every lens name (draft or live) — the DB side of plan's server-only diff."""
    return [r[0] for r in session.execute(text("SELECT name FROM lens ORDER BY name")).all()]


def _bundle_uses_connection(bundle: dict[str, Any] | None, connection: str) -> bool:
    """True if a stored lens bundle references `connection` — either declared in
    config.connections or as the physical source of any semantic-model entity."""
    if not bundle:
        return False
    config = bundle.get("config") or {}
    if connection in (config.get("connections") or []):
        return True
    semantic_model = bundle.get("semantic_model") or {}
    return any(
        (entity.get("source") or {}).get("connection") == connection
        for entity in (semantic_model.get("entities") or [])
    )


def list_dependent_lenses(session: Session, connection: str) -> list[LensSummary]:
    """Lenses that rely on a warehouse `connection`, scanning both the draft and the
    published bundle. Surfaced before a connection is deleted so the operator sees
    exactly which lenses would stop answering once the credential is gone."""
    rows = session.execute(
        text(
            "SELECT name, display_name, description, status, draft_json, published_json "
            "FROM lens ORDER BY name"
        )
    ).all()
    return [
        LensSummary(name=r[0], display_name=r[1], description=r[2], status=r[3])
        for r in rows
        if _bundle_uses_connection(r[4], connection) or _bundle_uses_connection(r[5], connection)
    ]


def update_draft(session: Session, name: str, bundle: LensBundle) -> int:
    res = session.execute(
        text(
            "UPDATE lens SET draft_json = CAST(:d AS jsonb), display_name = :dn, "
            "description = :desc, updated_at = now() WHERE name = :n"
        ),
        {
            "d": _stored(bundle),
            "dn": bundle.config.display_name,
            "desc": bundle.config.description,
            "n": name,
        },
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def publish(session: Session, name: str) -> int:
    # Publishing clears any upgrade notice: the owner has now re-applied this
    # lens under the new code, so whatever a release changed under them is
    # theirs, deliberately. That clearing is the whole reason the notice can be
    # loud without becoming permanent noise.
    res = session.execute(
        text(
            "UPDATE lens SET published_json = draft_json, status = 'live', "
            "published_at = now(), upgrade_notice = NULL WHERE name = :n"
        ),
        {"n": name},
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def set_degraded(session: Session, name: str, note: str) -> int:
    """Mark a lens degraded (serve-time drift confirmed behind an
    execution error). The note rides every subsequent answer's `degraded` list
    until a successful apply clears it — a standing fault must not depend on
    the NEXT request also happening to hit the broken table."""
    res = session.execute(
        text("UPDATE lens SET degraded = :d WHERE name = :n"), {"d": note, "n": name}
    )
    return int(res.rowcount)  # type: ignore[attr-defined]


def get_degraded(session: Session, name: str) -> str | None:
    row = session.execute(text("SELECT degraded FROM lens WHERE name = :n"), {"n": name}).first()
    return row[0] if row else None


def clear_degraded(session: Session) -> int:
    """Un-mark every degraded lens — the successful-apply act. Rows
    cleared. Org-wide on purpose: the mark means 'the warehouse moved under the
    layer', and the apply that just landed IS the layer catching up; if the
    drift persists, the next serve error re-marks within one request."""
    res = session.execute(text("UPDATE lens SET degraded = NULL WHERE degraded IS NOT NULL"))
    return int(res.rowcount)  # type: ignore[attr-defined]


def delete_lens(session: Session, name: str) -> int:
    """Delete a lens AND the rows keyed to its name — no FK ties them, so
    leftovers would silently resurface on a later same-named lens (old
    certified answers serving, a stale eval baseline gating its first publish,
    inherited context chunks). request_log stays: observability history
    outlives the lens. `dst lens rm` prints the cascade before calling."""
    res = session.execute(text("DELETE FROM lens WHERE name = :n"), {"n": name})
    if int(res.rowcount) == 0:  # type: ignore[attr-defined]
        return 0
    # review rows stay alongside request_log (rulings are observability history);
    # patch_candidate goes — a stale candidate resurfacing on a same-named lens
    # is exactly the hazard this cascade exists to kill.
    for table in (
        "lens_version",
        "certified_answer",
        "eval_case",
        "eval_run",
        "patch_candidate",
        "router_anchor",
    ):
        session.execute(text(f"DELETE FROM {table} WHERE lens = :n"), {"n": name})
    return int(res.rowcount)  # type: ignore[attr-defined]


def list_published(session: Session) -> list[tuple[str, str, str, LensBundle]]:
    """All live lenses with their published bundle. For caller-scoped discovery."""
    rows = session.execute(
        text(
            "SELECT name, display_name, description, published_json "
            "FROM lens WHERE status = 'live' AND published_json IS NOT NULL ORDER BY name"
        )
    ).all()
    return [(r[0], r[1], r[2], LensBundle.model_validate(r[3])) for r in rows]


def upgrade_notices(session: Session) -> dict[str, str]:
    """{lens: the one-time line an UPGRADE left on it}.

    The slot for a release that changes what an already-published bundle MEANS
    without changing any file — `dst plan` compares files to the DB and by
    construction sees nothing, so the release that did it writes the sentence
    onto the rows it changed (see migration 0040) and plan reads it here.
    ``publish`` clears the column, so the notice lives exactly until its owner
    next applies."""
    rows = session.execute(
        text("SELECT name, upgrade_notice FROM lens WHERE upgrade_notice IS NOT NULL")
    ).all()
    return {r[0]: r[1] for r in rows}


def list_published_for_org(org_id: uuid.UUID | str) -> list[tuple[str, str, str, LensBundle]]:
    with org_session(org_id) as session:
        return list_published(session)


def resolve_published(session: Session, name: str) -> LensBundle | None:
    row = session.execute(
        text("SELECT published_json FROM lens WHERE name = :n AND status = 'live'"),
        {"n": name},
    ).first()
    if row is None or row[0] is None:
        return None
    return LensBundle.model_validate(row[0])


def get_lens(session: Session, name: str) -> dict[str, object] | None:
    row = session.execute(
        text(
            "SELECT name, display_name, description, status, draft_json, published_json "
            "FROM lens WHERE name = :n"
        ),
        {"n": name},
    ).first()
    if row is None:
        return None
    return {
        "name": row[0],
        "display_name": row[1],
        "description": row[2],
        "status": row[3],
        "draft": row[4],
        "published": row[5],
    }


def lens_exists(session: Session, name: str) -> bool:
    return (
        session.execute(text("SELECT 1 FROM lens WHERE name = :n"), {"n": name}).first() is not None
    )


def load_published_lens(org_id: uuid.UUID | str, name: str) -> LensBundle | None:
    with org_session(org_id) as session:
        return resolve_published(session, name)


# ---------------------------------------------------------------------------
# Version history (lens-as-repo) — every publish snapshots the bundle (0021).
# ---------------------------------------------------------------------------

# The summary a files apply records on its lens_version — the provenance marker
# behind ``from_files`` (recompiles and UI publishes record different summaries:
# only a push of the lens's own tree counts as "applied from files").
APPLY_SUMMARY = "apply (files won)"


def lenses_from_files(session: Session) -> set[str]:
    """Lens names with at least one lens_version recorded by a files apply.
    The complement was never applied from files — API/wizard/cloud-born."""
    rows = session.execute(
        text("SELECT DISTINCT lens FROM lens_version WHERE summary = :s"),
        {"s": APPLY_SUMMARY},
    ).all()
    return {r[0] for r in rows}


class LensVersionRow(BaseModel):
    """One entry in a lens's published history — metadata only (no bundle_json)."""

    version: int
    summary: str
    created_at: datetime
    # Who published: human:<email> | token:<label> | process:<id>. '' = unknown
    # (pre-0051 rows, or a path with no identity).
    created_by: str = ""


def record_version(
    session: Session, name: str, bundle: LensBundle, summary: str = "", created_by: str = ""
) -> int:
    """Snapshot a published bundle as the lens's next immutable version.

    Numbering is ``MAX(version)+1`` per (org, lens) under the unique constraint —
    fine for the single-writer-per-org reality. Returns the new version int.
    """
    row = session.execute(
        text(
            """
            INSERT INTO lens_version (org_id, lens, version, bundle_json, summary, created_by)
            SELECT
                NULLIF(current_setting('app.current_org', true), '')::uuid,
                :n, COALESCE(MAX(version), 0) + 1, CAST(:b AS jsonb), :s, :who
            FROM lens_version WHERE lens = :n
            RETURNING version
            """
        ),
        {"n": name, "b": _stored(bundle), "s": summary, "who": created_by},
    ).first()
    return int(row[0])  # type: ignore[index]


def list_versions(session: Session, name: str) -> list[LensVersionRow]:
    """A lens's published history, newest first. Cheap — omits the bundle payload."""
    rows = session.execute(
        text(
            "SELECT version, summary, created_at, created_by FROM lens_version "
            "WHERE lens = :n ORDER BY version DESC"
        ),
        {"n": name},
    ).all()
    return [
        LensVersionRow(version=int(r[0]), summary=r[1], created_at=r[2], created_by=r[3])
        for r in rows
    ]


def get_version(session: Session, name: str, version: int) -> LensBundle | None:
    """The exact bundle published as ``version``, or None if that version is unknown."""
    row = session.execute(
        text("SELECT bundle_json FROM lens_version WHERE lens = :n AND version = :v"),
        {"n": name, "v": version},
    ).first()
    if row is None or row[0] is None:
        return None
    return LensBundle.model_validate(row[0])
