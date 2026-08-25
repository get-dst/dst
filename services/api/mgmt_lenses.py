"""Control-plane lens management (/mgmt/lenses). Admin-authed, org-scoped.

CRUD + draft->publish. The wizard UI and GitOps/automation share these endpoints.
"""

from __future__ import annotations

import difflib
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from services.api import audit_store
from services.auth.deps import AdminIdentity, get_admin_identity, get_admin_org, get_app_session
from services.certdefs import CertifiedDefinition, load_certified_defs
from services.certify import store as certify_store
from services.contracts.semantic_model import Definition, SampleQuery
from services.contracts.warehouse import SchemaSnapshot
from services.definitions import drift
from services.definitions import standards as std_store
from services.evals import service as eval_service
from services.evals import store as eval_store
from services.lenses import store
from services.lenses.repo import render_lens_repo
from services.lenses.use_when import generate_use_when
from services.llm.assist import assist_llm
from services.probe.drift import DriftFinding
from services.probe.report import draft_definition
from services.router import anchor_store
from services.runtime.preview import PromptPreview, preview
from services.semantic import store as semantic_store
from services.validate.report import ValidationReport, validate_bundle

router = APIRouter(prefix="/mgmt/lenses", tags=["lenses"])
log = logging.getLogger(__name__)


class GithubContextBody(BaseModel):
    repo: str
    paths: list[str]
    ref: str = "main"
    token: str | None = None


class TextContextBody(BaseModel):
    source: str
    text: str


class DraftBody(BaseModel):
    name: str
    display_name: str | None = None
    connection: str = "jaffle"
    prompt: str
    tables: list[str] | None = None


class SampleQueriesBody(BaseModel):
    queries: list[SampleQuery]


class UseWhenBody(BaseModel):
    items: list[str]


class CertifiedDefsView(BaseModel):
    """The certified definitions bound to a lens: the certified directory it
    declares plus the per-metric pages loaded from it. ``metrics`` is empty when no
    directory is set or the path is missing/unreadable — a bad path never 500s."""

    certified_dir: str | None
    metrics: list[CertifiedDefinition]


class DraftFromFindingsBody(BaseModel):
    """Seed a brand-new lens from audit findings — the funnel's audit→lens
    bridge that does NOT need a lens to already exist. ``findings`` are the verbatim
    audit objects the dashboard holds (GET .../audit/latest)."""

    name: str
    display_name: str | None = None
    connection: str
    findings: list[DriftFinding]


def _seed_tables(findings: list[DriftFinding], snapshot: SchemaSnapshot) -> list[str] | None:
    """The snapshot tables the findings' variants touch, so the drafter focuses there.

    Matched best-effort on bare or qualified name — drift reports lowercased
    catalog.db.table while snapshots name tables differently per connector. None when
    nothing matches, so the drafter falls back to the full schema rather than an empty one.
    """
    wanted = {
        part.lower()
        for f in findings
        for v in f.variants
        for tbl in v.source_tables
        for part in (tbl, tbl.split(".")[-1])
    }
    matched = [
        t.name
        for t in snapshot.tables
        if t.name.lower() in wanted or t.name.split(".")[-1].lower() in wanted
    ]
    return matched or None


def _seed_prompt(findings: list[DriftFinding]) -> str:
    metrics = "; ".join(
        f"“{f.metric_intent}” (computed {len(f.variants)} conflicting ways)" for f in findings
    )
    return (
        "Build a governed lens that resolves these contested metrics surfaced by the "
        f"dst definition-drift audit: {metrics}. Cover the tables these metrics are "
        "computed from and define each so one canonical reading is enforced."
    )


def _inject_canon_definitions(
    definitions: list[Definition], findings: list[DriftFinding]
) -> tuple[list[Definition], list[str]]:
    """Fold the audit's canon definition for each conflict finding into the drafted
    definitions, replacing any same-term one the drafter emitted. Returns the new
    list plus the terms seeded. These land as SHARED definitions —
    upserted with the rest of the draft and selected by the new lens.

    Reuses ``services.probe.report.draft_definition`` verbatim — the same deterministic
    canon choice the report and the patch bridge use — so the contested metric lands
    DEFINED with the rejected readings kept in the body, not as a hint. The audit
    provenance lives in that body; ``source`` stays ``authored`` (the model's enum)."""
    seeded: list[str] = []
    out = list(definitions)
    for finding in findings:
        if finding.severity != "conflict" or not finding.variants:
            continue
        draft = draft_definition(finding)
        out = [d for d in out if d.term.lower() != draft["term"].lower()]
        out.append(Definition(term=draft["term"], body=draft["body"], source="authored"))
        seeded.append(draft["term"])
    return out, seeded


@router.get("")
def list_lenses(session: Session = Depends(get_app_session)) -> list[store.LensSummary]:
    """Every lens in the org, as summaries — the cockpit list."""
    return store.list_lenses(session)


@router.post("", status_code=201)
def create_lens(
    bundle: store.LensBundle, session: Session = Depends(get_app_session)
) -> dict[str, str]:
    """Create a lens from a full bundle (config + semantic model) — it lands as
    a draft; publish is a separate step. 409 when the name exists."""
    if store.lens_exists(session, bundle.config.name):
        raise HTTPException(status_code=409, detail=f"lens '{bundle.config.name}' already exists")
    name = store.create_lens(session, bundle)
    return {"name": name, "status": "draft"}


@router.get("/{name}")
def get_lens(name: str, session: Session = Depends(get_app_session)) -> dict[str, object]:
    """One lens's full record — draft and published bundles, plus `from_files`:
    False means it was never applied from files (API/wizard-born), so `dst
    plan` lists it server-only until adopted via `dst export --lens`."""
    row = store.get_lens(session, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    # Provenance signal: False = never applied from files (API/wizard/
    # cloud-born) — any surface renders "not in files" from it; plan lists the
    # lens server-only until adopted via `dst export --lens`.
    row["from_files"] = name in store.lenses_from_files(session)
    return row


def _guard_shared_entities(name: str, row: dict[str, object], incoming: store.LensBundle) -> None:
    """Shared-layer clobber guard: when a lens's model was compiled from shared entity
    assets, its entities/joins are DERIVED state — an API/wizard edit here would
    be silently clobbered by the next recompile pass. Entity edits go through the
    shared layer; config/queries/local-definition edits stay editable."""
    stored_raw = row.get("draft") or row.get("published")
    if stored_raw is None:
        return
    protected = False
    for key in ("published", "draft"):
        raw = row.get(key)
        if raw is None:
            continue
        provenance = store.LensBundle.model_validate(raw).semantic_model.shared_provenance
        if provenance is not None and any(a.startswith("entity/") for a in provenance.assets):
            protected = True
            break
    if not protected:
        return
    stored = store.LensBundle.model_validate(stored_raw).semantic_model
    unchanged = [e.model_dump() for e in stored.entities] == [
        e.model_dump() for e in incoming.semantic_model.entities
    ] and [j.model_dump() for j in stored.joins] == [
        j.model_dump() for j in incoming.semantic_model.joins
    ]
    if unchanged:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"lens '{name}' entities are compiled from the shared layer — edit "
            "semantic/entities/*.yaml or /mgmt/semantic, then apply"
        ),
    )


@router.put("/{name}")
def update_lens(
    name: str, bundle: store.LensBundle, session: Session = Depends(get_app_session)
) -> dict[str, str]:
    """Replace the draft bundle. 409 when the entities are compiled from the shared
    layer — those are derived state; edit `semantic/entities/*.yaml` or
    `/mgmt/semantic` instead. Config, queries, and local definitions stay editable."""
    row = store.get_lens(session, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    _guard_shared_entities(name, row, bundle)
    if store.update_draft(session, name, bundle) == 0:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    return {"name": name, "status": "draft"}


def _lens_certified_dir(row: dict[str, object]) -> str | None:
    """The certified directory this lens declares — preferring the published bundle (what
    the serving path reads) and falling back to the draft, matching the UI."""
    for key in ("published", "draft"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            return store.LensBundle.model_validate(raw).config.model.certified_dir
        except ValidationError:
            continue
    return None


@router.get("/{name}/certified-definitions")
def get_lens_certified_defs(
    name: str, session: Session = Depends(get_app_session)
) -> CertifiedDefsView:
    """The certified definitions bound to this lens: its declared directory
    and the per-metric pages loaded from it. Best-effort — a missing or unreadable
    directory yields an empty ``metrics`` list, never a 500."""
    row = store.get_lens(session, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    certified_dir = _lens_certified_dir(row)
    if not certified_dir:
        return CertifiedDefsView(certified_dir=None, metrics=[])
    try:
        metrics = load_certified_defs(Path(certified_dir))
    except Exception:
        # A missing/unreadable dir or a malformed page must not break the view —
        # mirror the serving path's best-effort certified load (query._certified_context).
        log.exception("certified load failed for lens %s at %s", name, certified_dir)
        metrics = []
    return CertifiedDefsView(certified_dir=certified_dir, metrics=metrics)


# ---------------------------------------------------------------------------
# Lens as a versioned file tree (lens-as-repo).
# ---------------------------------------------------------------------------


def _current_bundle(row: dict[str, object]) -> store.LensBundle:
    """The lens's live bundle — published if present, else the draft."""
    raw = row.get("published") or row.get("draft")
    if raw is None:
        raise HTTPException(status_code=400, detail="lens has no bundle yet")
    return store.LensBundle.model_validate(raw)


def _materialize_current(session: Session, name: str, row: dict[str, object]) -> dict[str, str]:
    """Render the lens's live tree: bundle + its certified definitions, eval cases, latest audit.

    Peripheral artifacts are best-effort — a missing certified dir or absent audit simply
    omits those files, never a 500 (mirrors the certified view)."""
    bundle = _current_bundle(row)
    certified: list[CertifiedDefinition] = []
    certified_dir = _lens_certified_dir(row)
    if certified_dir:
        try:
            certified = load_certified_defs(Path(certified_dir))
        except Exception:
            log.exception("certified load failed for lens %s at %s", name, certified_dir)
    cases = eval_store.list_cases(session, name)
    connections = bundle.config.connections
    audit = audit_store.latest(session, connections[0]) if connections else None
    return render_lens_repo(
        bundle,
        certified=certified,
        certified_answers=certify_store.list_for_lens(session, name),
        eval_cases=cases,
        audit=audit,
    )


@router.get("/{name}/repo")
def get_lens_repo(name: str, session: Session = Depends(get_app_session)) -> dict[str, object]:
    """The lens materialized as a file tree — the path + size of every file."""
    row = store.get_lens(session, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    files = _materialize_current(session, name, row)
    return {"files": [{"path": p, "size": len(c)} for p, c in files.items()]}


@router.get("/{name}/repo/file")
def get_lens_repo_file(
    name: str, path: str, session: Session = Depends(get_app_session)
) -> dict[str, str]:
    """One file's contents from the live tree. ``?path=`` query param dodges slashes."""
    row = store.get_lens(session, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    files = _materialize_current(session, name, row)
    if path not in files:
        raise HTTPException(status_code=404, detail=f"no file '{path}' in lens '{name}'")
    return {"path": path, "content": files[path]}


@router.get("/{name}/versions")
def list_lens_versions(
    name: str, session: Session = Depends(get_app_session)
) -> list[store.LensVersionRow]:
    """A lens's published history, newest first."""
    if not store.lens_exists(session, name):
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    return store.list_versions(session, name)


@router.get("/{name}/diff")
def diff_lens_versions(
    name: str,
    session: Session = Depends(get_app_session),
    from_version: int = Query(..., alias="from"),
    to_version: int = Query(..., alias="to"),
) -> dict[str, object]:
    """Unified diff of two published versions' bundle-derived trees (stdlib difflib)."""
    a = store.get_version(session, name, from_version)
    b = store.get_version(session, name, to_version)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="unknown version for lens")
    a_files, b_files = render_lens_repo(a), render_lens_repo(b)
    changed: list[dict[str, str]] = []
    for p in sorted(set(a_files) | set(b_files)):
        before = a_files.get(p, "").splitlines(keepends=True)
        after = b_files.get(p, "").splitlines(keepends=True)
        if before == after:
            continue
        diff = "".join(
            difflib.unified_diff(
                before, after, fromfile=f"v{from_version}/{p}", tofile=f"v{to_version}/{p}"
            )
        )
        changed.append({"path": p, "diff": diff})
    return {"from": from_version, "to": to_version, "files": changed}


# ---------------------------------------------------------------------------
# What the model actually sees (services/runtime/preview.py).
# ---------------------------------------------------------------------------


def _unselected_shared(session: Session, bundle: store.LensBundle) -> list[tuple[str, str]]:
    """Shared-layer assets this lens does NOT select — the commonest reason a
    definition an author swears they wrote is nowhere in the prompt."""
    selected = {("entity", e.name) for e in bundle.semantic_model.entities} | {
        ("definition", d.term) for d in bundle.semantic_model.definitions
    }
    return [
        (a.kind, a.name)
        for a in semantic_store.list_assets(session)
        if (a.kind, a.name) not in selected
    ]


@router.get("/{name}/prompt")
def get_lens_prompt(
    name: str,
    q: str,
    org_id: uuid.UUID = Depends(get_admin_org),
    session: Session = Depends(get_app_session),
) -> PromptPreview:
    """The generation prompt this lens would send for question ``q``, plus a
    per-asset reach checklist (what made it in, and what dropped it).

    ZERO LLM calls and zero warehouse calls: `assembly.assemble` — the same seam
    serving runs — already builds every piece; this only renders it. Reads the
    live bundle (published if there is one, else the draft)."""
    row = store.get_lens(session, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    bundle = _current_bundle(row)
    return preview(bundle, q, org_id, unselected=_unselected_shared(session, bundle))


@router.post("/{name}/validate")
def validate_lens(name: str, session: Session = Depends(get_app_session)) -> ValidationReport:
    """Validate the draft — model checks plus definition drift against org
    standards and every other published lens. 400 when there is no draft."""
    row = store.get_lens(session, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    draft = row.get("draft")
    if draft is None:
        raise HTTPException(status_code=400, detail=f"lens '{name}' has no draft to validate")
    bundle = store.LensBundle.model_validate(draft)
    standards = std_store.list_standards(session)
    other = [
        (other_name, b.semantic_model.definitions)
        for (other_name, _dn, _desc, b) in store.list_published(session)
        if other_name != name
    ]
    return validate_bundle(bundle, standards, other)


def _seed_use_when(session: Session, name: str, bundle: store.LensBundle) -> None:
    """Fill the draft's routing examples if it has none — best-effort, never raises."""
    if bundle.semantic_model.use_when:
        return
    pair = assist_llm()
    if pair is None:
        return
    llm, model = pair.llm, pair.name
    examples = generate_use_when(
        llm,
        model,
        bundle.semantic_model,
        display_name=bundle.config.display_name,
        description=bundle.config.description,
    )
    if examples:
        bundle.semantic_model.use_when = examples
        store.update_draft(session, name, bundle)


@router.post("/{name}/publish")
def publish_lens(
    name: str,
    session: Session = Depends(get_app_session),
    org_id: uuid.UUID = Depends(get_admin_org),
    identity: AdminIdentity = Depends(get_admin_identity),
) -> dict[str, object]:
    """Publish the draft. The eval regression gate runs first when the lens opts
    in — `block` refuses with a 409 carrying the scores, `warn` reports and
    proceeds, and configured-but-unscoreable is surfaced, never silent. Routing
    examples are seeded if absent, and the published bundle is snapshotted as an
    immutable version (lens-as-repo)."""
    row = store.get_lens(session, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")

    # Eval regression gate: only when the draft opts in and there are approved
    # cases. "block" refuses publish on a regression; "warn" surfaces it and proceeds.
    # The check itself is shared with apply's publish path (eval_service.publish_gate).
    eval_summary: dict[str, object] | None = None
    draft = row.get("draft")
    if draft is not None:
        bundle = store.LensBundle.model_validate(draft)
        decision = eval_service.publish_gate(
            session=session, org_id=org_id, lens=name, bundle=bundle
        )
        if decision is not None and not decision.gated:
            # Configured but couldn't score (no smart-tier model / no approved
            # cases) — surface the skip; a silent stand-down reads as a pass.
            eval_summary = {"skipped": decision.detail}
        elif decision is not None:
            if decision.blocked:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": (
                            "eval gate blocked publish: "
                            + (
                                "certified answers diverged"
                                if decision.certified_failures
                                else "accuracy regressed"
                            )
                        ),
                        "score": decision.score,
                        "prev_score": decision.prev_score,
                        "failing": decision.failing,
                        "certified": decision.certified_failures,
                    },
                )
            eval_summary = {
                "score": decision.score,
                "prev_score": decision.prev_score,
                "regressed": decision.regressed,
                "failing": decision.failing,
            }
            if decision.certified_failures:
                eval_summary["certified"] = decision.certified_failures

    # Seed routing examples if the lens has none, so every published lens advertises
    # itself to the router. Best-effort: never blocks publish.
    if draft is not None:
        _seed_use_when(session, name, store.LensBundle.model_validate(draft))

    if store.publish(session, name) == 0:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    result: dict[str, object] = {"name": name, "status": "live"}
    # Snapshot the published bundle into version history (lens-as-repo): every
    # publish becomes an immutable, diffable version.
    if draft is not None:
        bundle = store.LensBundle.model_validate(draft)
        result["version"] = store.record_version(
            session, name, bundle, summary="publish (dashboard)", created_by=identity.actor
        )
        # Pre-warm the router's anchor embeddings — best-effort, never
        # blocks publish; the /v1/query read path repairs on the next request.
        anchor_store.warm(session, bundle)
    if eval_summary is not None:
        result["eval"] = eval_summary
    return result


@router.get("/{name}/drift")
def lens_drift(name: str, session: Session = Depends(get_app_session)) -> dict[str, object]:
    """Definition conflicts between this lens's draft and org standards / other lenses."""
    row = store.get_lens(session, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
    draft = row.get("draft")
    if draft is None:
        return {"conflicts": []}
    bundle = store.LensBundle.model_validate(draft)
    standards = std_store.list_standards(session)
    other = [
        (other_name, b.semantic_model.definitions)
        for (other_name, _dn, _desc, b) in store.list_published(session)
        if other_name != name
    ]
    conflicts = drift.compare(bundle.semantic_model.definitions, standards, other)
    return {"conflicts": [c.model_dump() for c in conflicts]}


@router.delete("/{name}", status_code=204)
def delete_lens(name: str, session: Session = Depends(get_app_session)) -> None:
    """Delete a lens outright — draft and published state both. 404 unknown."""
    if store.delete_lens(session, name) == 0:
        raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
