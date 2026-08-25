"""Project sync surface (/mgmt/project) — the file-first flow's server side.

GET  /mgmt/project/export → the shared layer rendered under semantic/** plus
     every lens rendered under lenses/<name>/… (the tree a repo commits;
     README/compiled.yaml/audit included for humans, ignored by the loader).
     ``?lens=x`` (repeatable) scopes to just those lenses' trees — adoption.
     Connections ride along as declarations (config + has_secret, no secret).
POST /mgmt/project/plan   → terraform-style dry run: per-asset and per-lens
     create/update/unchanged with unified diffs, plus staleness — published
     lenses whose compiled provenance the push invalidates recompile on apply —
     plus server-only objects: DB lenses/connections the push doesn't carry
     (adopt via export or leave for their owner; absence never deletes).
POST /mgmt/project/apply  → files win, blue/green: connections → shared assets
     → lenses (select compiled against the DB post-upsert) → recompile pass
     over every stale published lens, including lenses absent from the push —
     ALL staged in one transaction. Any error (probe failure, malformed file,
     validation, eval-gate block, certified-answer gate, failed recompile)
     rolls the whole apply back: nothing deploys, prior versions keep serving,
     every non-error row is rewritten to action "rolled-back" (version nulled),
     and the response ends with a {"scope": "apply", "action": "aborted"} row.
     Warnings never abort.

The CLI's `dst export/plan/apply` are thin wrappers over these three —
auth and RLS stay server-side.
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.auth.deps import AdminIdentity, get_admin_identity, get_admin_org, get_app_session
from services.certify import store as certify_store
from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import Definition
from services.contracts.shared_semantic import SharedEntity
from services.evals import store as eval_store
from services.lenses import connection_store, store
from services.lenses.repo import render_lens_repo
from services.project import apply as apply_engine
from services.project import plan as plan_engine
from services.project.compile import shared_definition_hash, shared_entity_hash
from services.project.loader import load_lens_source, split_by_lens, split_semantic
from services.project.schema import ConnectionDecl, parse_project_yaml
from services.semantic import store as semantic_store
from services.semantic.files import (
    parse_semantic_files,
    render_semantic_files,
    validate_semantic_files,
)
from services.validate.report import definition_double_truth

router = APIRouter(prefix="/mgmt/project", tags=["project"])

_APPLY_LOCK_NS = 0x4B_41_50_4C  # 'KAPL' — namespace for the per-org apply lock


def _apply_lock_key(org_id: uuid.UUID) -> int:
    """A stable signed-64 advisory-lock key: namespace XOR org-uuid hash (the
    scheduler's advisory-lock precedent, made per-org)."""
    key = int.from_bytes(hashlib.sha256(org_id.bytes).digest()[:8], "big") ^ _APPLY_LOCK_NS
    return key - 2**64 if key >= 2**63 else key


class ProjectFiles(BaseModel):
    files: dict[str, str]


def _db_tree(session: Session, name: str, bundle: store.LensBundle) -> dict[str, str]:
    return render_lens_repo(
        bundle,
        # Oldest-first ≈ the order a file grows by appending (the
        # newest-first listing order made every export disagree with the
        # authored file). Plan's comparison is order-insensitive regardless.
        certified_answers=sorted(
            certify_store.list_for_lens(session, name), key=lambda a: a.created_at
        ),
        eval_cases=eval_store.list_cases(session, name),
        audit=None,
    )


def _db_trees(session: Session) -> dict[str, dict[str, str]]:
    return {
        name: _db_tree(session, name, bundle)
        for (name, _dn, _desc, bundle) in store.list_published(session)
    }


def _db_semantic(session: Session) -> dict[str, str]:
    """The DB's shared assets rendered as semantic/** files."""
    assets = semantic_store.list_assets(session)
    return render_semantic_files(
        [SharedEntity.model_validate(a.body) for a in assets if a.kind == "entity"],
        [Definition.model_validate(a.body) for a in assets if a.kind == "definition"],
    )


def _connection_rows(session: Session, names: set[str] | None) -> list[dict[str, object]]:
    """Connection DECLARATIONS — config + has_secret, never the secret itself.
    The CLI renders these as a dst.yaml snippet (secret_env refs to fill);
    it never edits the user's authored file. ``names=None`` means all."""
    records = connection_store.list_connections(session)
    if names is not None:
        records = [r for r in records if r.name in names]
    return [
        {"name": r.name, "type": r.type, "config": r.config, "has_secret": r.has_secret}
        for r in records
    ]


@router.get("/export")
def export_project(
    session: Session = Depends(get_app_session),
    org_id: uuid.UUID = Depends(get_admin_org),
    lens: list[str] | None = Query(default=None),
) -> dict[str, object]:
    """Bare: the full project tree (semantic/** + every published lens).
    ``?lens=x`` (repeatable): just those lenses' trees — the adoption path for
    server-only lenses, so it reads published-or-draft (an API-born lens is
    usually still a draft). Unknown names 404, naming the lens."""
    files: dict[str, str] = {}
    if lens:
        declared: set[str] = set()
        for name in lens:
            row = store.get_lens(session, name)
            raw = (row or {}).get("published") or (row or {}).get("draft")
            if row is None or raw is None:
                raise HTTPException(status_code=404, detail=f"lens '{name}' not found")
            bundle = store.LensBundle.model_validate(raw)
            declared.update(bundle.config.connections)
            for path, content in _db_tree(session, name, bundle).items():
                files[f"lenses/{name}/{path}"] = content
        return {"files": files, "connections": _connection_rows(session, declared)}
    files.update(_db_semantic(session))
    for name, tree in _db_trees(session).items():
        for path, content in tree.items():
            files[f"lenses/{name}/{path}"] = content
    return {"files": files, "connections": _connection_rows(session, None)}


def _effective_hashes(
    session: Session,
    incoming_entities: dict[str, SharedEntity],
    incoming_definitions: dict[str, Definition],
) -> dict[str, str]:
    """Current DB asset hashes with the push's incoming assets layered on."""
    effective = semantic_store.asset_hashes(session)
    effective.update({f"entity/{n}": shared_entity_hash(e) for n, e in incoming_entities.items()})
    effective.update(
        {f"definition/{t}": shared_definition_hash(d) for t, d in incoming_definitions.items()}
    )
    return effective


def _stale_map(session: Session, effective: dict[str, str]) -> dict[str, list[str]]:
    """Published-lens staleness against the effective hashes."""
    provenances = {
        name: bundle.semantic_model.shared_provenance.assets
        for (name, _dn, _desc, bundle) in store.list_published(session)
        if bundle.semantic_model.shared_provenance is not None
    }
    return plan_engine.stale_lenses(provenances, effective)


def _certified_stale(session: Session, effective: dict[str, str]) -> dict[str, dict[str, object]]:
    """Per-lens re-verify summary for certified answers whose derived bindings
    the effective hashes invalidate. One line per lens; question texts ride in
    the payload so the CLI can show them. Advisory only — NO auto-anything."""
    out: dict[str, dict[str, object]] = {}
    for name in store.lens_names(session):
        # Retired answers are history — never tested, never nagged.
        active = [
            a
            for a in certify_store.list_for_lens(session, name)
            if certify_store.is_active(a.status)
        ]
        questions, assets = plan_engine.stale_certified(
            ((a.question, a.bindings) for a in active), effective
        )
        if questions:
            # The flag persists across applies BY DESIGN (unchanged answers
            # keep their as-verified hashes) — so the note must name the
            # act that clears it, or it reads as a phantom.
            note = (
                f"certified: {len(questions)} answers touch changed assets "
                f"({', '.join(assets)}) — re-verify by updating each answer's "
                "verified_by (or sql) in certified_answers.yaml"
            )
            # A review-promoted answer is NOT in that file — pointing at it
            # leaves the operator hand-reconstructing the entry.
            stale = set(questions)
            if any((a.source or "").startswith("review:") for a in active if a.question in stale):
                note += (
                    " (review-promoted answers are not in the file yet — "
                    f"`dst export --lens {name}` renders them there first)"
                )
            out[name] = {"note": note, "questions": questions, "assets": assets}
    return out


def _declared_connections(
    files: dict[str, str],
) -> tuple[dict[str, ConnectionDecl] | None, str | None]:
    """``({name: declaration}, error)`` from the push's own dst.yaml.

    The declarations are what apply's connection pass would land BEFORE any lens
    compiles — the dry run needs them, or a brand-new project's very first plan
    rejects every lens for naming a connection that does not exist yet. The whole
    declaration rather than its type alone, because compiling also needs the
    catalog its config pins (``project:``/``database:``), and a plan that
    qualified entity tables differently from the apply it predicts is the one
    thing plan may never do. ``None`` means the file is there and unparseable:
    apply stops on that ONE error before a lens compiles, so plan must report the
    same one error rather than a cascade of "names no applied warehouse
    connection" underneath it."""
    if "dst.yaml" not in files:
        return {}, None
    try:
        project = parse_project_yaml(files["dst.yaml"])
    except ValueError as exc:
        return None, str(exc)
    return dict(project.connections), None


def _unselected_definitions(
    session: Session,
    incoming_definitions: dict[str, Definition],
    incoming_configs: dict[str, LensConfig],
) -> list[str]:
    """Shared definition terms no lens selects — the push's configs override the
    published ones for lenses it touches."""
    all_terms = {a.name for a in semantic_store.list_assets(session, "definition")}
    all_terms |= set(incoming_definitions)
    selects = dict(
        (name, bundle.config) for (name, _dn, _desc, bundle) in store.list_published(session)
    )
    selects.update(incoming_configs)
    selected: set[str] = set()
    for config in selects.values():
        picks = config.select.definitions
        if "*" in picks:
            return []
        selected.update(picks)
    return sorted(all_terms - selected)


def _dormant_definition_warnings(session: Session) -> list[str]:
    """validate_bundle only ever sees COMPILED models, so a shared
    definition no lens selects is never linted — its author shipped governance
    believing it active. Lint the unselected ones against the full shared
    entity set, same rule and wording as the compiled path; selected terms are
    left to their lenses' validate, so nothing warns twice."""
    unselected = set(_unselected_definitions(session, {}, {}))
    if not unselected:
        return []
    assets = semantic_store.list_assets(session)
    entities = [SharedEntity.model_validate(a.body) for a in assets if a.kind == "entity"]
    warnings: list[str] = []
    for asset in assets:
        if asset.kind != "definition" or asset.name not in unselected:
            continue
        issue = definition_double_truth(Definition.model_validate(asset.body), entities)
        if issue is not None:
            warnings.append(f"{issue.message} — and no lens selects it, so neither claim is live")
    return warnings


@router.post("/plan")
def plan_project(
    body: ProjectFiles,
    session: Session = Depends(get_app_session),
    org_id: uuid.UUID = Depends(get_admin_org),
) -> list[dict[str, object]]:
    """Dry-run of apply for a pushed file tree: per-path diffs and statuses with
    full validation — a plan never mutates, and its whole job is to predict
    apply. A final `unchecked` row names the gates only a real apply can run."""
    out: list[dict[str, object]] = []

    # ── shared layer: per-path diffs + FULL validation (a plan never mutates) ─
    # Plan's whole job is to predict apply. Without this it renders a clean
    # create-diff for entity files apply then rejects with ten pydantic errors
    # each, so every file is validated here, through the same seam apply
    # parses with, and its error rides on its own row.
    incoming_semantic = split_semantic(body.files)
    semantic_errors = validate_semantic_files(incoming_semantic)
    for asset in plan_engine.plan_semantic(_db_semantic(session), incoming_semantic):
        error = semantic_errors.get(asset.path)
        out.append(
            {
                "scope": "semantic",
                "path": asset.path,
                "status": "invalid" if error else asset.status,
                "diff": "" if error else asset.diff,
                "error": error,
            }
        )
    incoming_entities: dict[str, SharedEntity] = {}
    incoming_definitions: dict[str, Definition] = {}
    semantic_ok = not semantic_errors
    try:
        # Cross-file checks (one name, two files) only once every file parses.
        if semantic_ok:
            incoming_entities, incoming_definitions = parse_semantic_files(incoming_semantic)
    except ValueError as exc:
        semantic_ok = False
        out.append({"scope": "semantic", "status": "invalid", "error": str(exc)})

    # DB assets no pushed file mentions — advisory only (absence never deletes),
    # and only when the push actually carries semantic/ files: a lens-only push
    # must not spam orphans.
    if incoming_semantic and semantic_ok:
        incoming_keys = {f"entity/{n}" for n in incoming_entities} | {
            f"definition/{t}" for t in incoming_definitions
        }
        db_keys = semantic_store.asset_hashes(session)
        for key in plan_engine.semantic_orphans(db_keys, incoming_keys):
            out.append(
                {
                    "scope": "semantic",
                    "status": "orphan",
                    "note": f"{key}: in DB but no file — remove with dst semantic rm, "
                    "or export to recover the file",
                }
            )

    effective = _effective_hashes(session, incoming_entities, incoming_definitions)
    stale = _stale_map(session, effective)
    certified_stale = _certified_stale(session, effective)

    # ── lenses: managed-file diffs + staleness markers + APPLY'S OWN GATES ────
    # A plan that exits 0 on a tree apply aborts is worse than no plan, because
    # people stop reading it: `apply_engine
    # .check_lens` runs every rejection apply would make that a dry run can make,
    # through the same functions apply calls. Connections the push declares are
    # layered in because apply lands them BEFORE any lens compiles.
    incoming = split_by_lens(body.files)
    incoming_connections, project_error = _declared_connections(body.files)
    incoming_configs: dict[str, LensConfig] = {}
    db_trees = _db_trees(session)
    for entry in plan_engine.plan_lenses(db_trees, incoming):
        gate_errors: list[str] = []
        try:
            source = load_lens_source(incoming[entry.lens])
            incoming_configs[entry.lens] = source.config
            parse_error = None
            if incoming_connections is not None:
                gate_errors = apply_engine.check_lens(
                    session,
                    entry.lens,
                    source,
                    incoming_entities=incoming_entities,
                    incoming_definitions=incoming_definitions,
                    incoming_connections=incoming_connections,
                    asset_hashes=effective,
                )
        except (ValueError, KeyError) as exc:
            parse_error = str(exc)
        errors = [parse_error] if parse_error else gate_errors
        row: dict[str, object] = {
            "lens": entry.lens,
            "status": "invalid" if errors else entry.status,
            # ``error`` stays a single string (every reader predates the list);
            # ``errors`` is the honest shape — apply rejects on all of them.
            "error": "; ".join(errors) or None,
            "errors": errors,
            "diffs": [{"path": d.path, "diff": d.diff} for d in entry.diffs],
        }
        if entry.lens in stale:
            row["stale"] = stale[entry.lens]
            row["note"] = "will recompile on apply"
        if entry.lens in certified_stale:
            row["certified"] = certified_stale[entry.lens]
        # Server-side certified answers with no file in this push are the one
        # server-only object plan would otherwise never name: deleting the
        # file looks like a rollback and silently isn't — absence leaves the
        # surface unmanaged and the answers keep serving.
        if "certified_answers.yaml" not in incoming[entry.lens] and (
            orphan := apply_engine.certified_orphan_warning(session, entry.lens)
        ):
            row["note"] = f"{note}; {orphan}" if (note := row.get("note")) else orphan
        out.append(row)
    # Stale published lenses the push never mentions still recompile on apply —
    # the plan must name every one of them before anything mutates, AND try the
    # recompile: one failure there aborts the whole apply just like a pushed
    # lens's, which is how a shared-entity edit gets rejected through a lens the
    # author never opened.
    broken_recompiles = apply_engine.check_recompiles(
        session,
        incoming_entities=incoming_entities,
        incoming_definitions=incoming_definitions,
        asset_hashes=effective,
        incoming_connections=incoming_connections or None,
        skip=set(incoming),
    )
    for lens in sorted(set(stale) - set(incoming)):
        errors = broken_recompiles.get(lens, [])
        row = {
            "lens": lens,
            "status": "invalid" if errors else "stale",
            "error": "; ".join(errors) or None,
            "errors": errors,
            "stale": stale[lens],
            "note": "will recompile on apply",
            "diffs": [],
        }
        if lens in certified_stale:
            row["certified"] = certified_stale[lens]
        out.append(row)
    # Lenses whose certified answers went stale but whose own compile didn't
    # (recompiled since; the answers keep their as-verified hashes until a human
    # re-certifies) — the re-verify flag must still surface.
    for lens in sorted(set(certified_stale) - set(incoming) - set(stale)):
        out.append(
            {
                "lens": lens,
                "status": "certified-stale",
                "certified": certified_stale[lens],
                "diffs": [],
            }
        )
    # Behaviour an UPGRADE changed on lenses whose FILES did not change. Every
    # row above is a file diff, and there is no file diff here to hang this on:
    # the release that reinterpreted a stored config wrote the line onto the
    # lens (migration 0040), and it clears on the apply that republishes it.
    for lens, notice in sorted(store.upgrade_notices(session).items()):
        rows = [r for r in out if r.get("lens") == lens]
        if not rows:
            rows = [{"lens": lens, "status": "unchanged", "diffs": []}]
            out.extend(rows)
        rows[0]["semantics"] = [notice]

    # ── server-only state: DB objects the push doesn't carry — API-created,
    # cloud-authored, or legacy. Advisory (adopt or leave for its owner —
    # deletion is never the default), scoped like semantic orphans: lenses only
    # when the push carries lenses/ files, connections only with dst.yaml.
    if incoming:
        for name in plan_engine.server_only(store.lens_names(session), incoming):
            out.append(
                {
                    "scope": "server_only",
                    "kind": "lens",
                    "name": name,
                    "note": f"lens '{name}': on the server but not in the push — adopt it "
                    f"(dst export --lens {name}) or leave it for its owner; "
                    f"delete only explicitly (dst lens rm {name})",
                }
            )
    if "dst.yaml" in body.files:
        if incoming_connections is None:
            out.append({"scope": "project", "status": "invalid", "error": project_error})
        else:
            db_connections = [c.name for c in connection_store.list_connections(session)]
            for name in plan_engine.server_only(db_connections, set(incoming_connections)):
                out.append(
                    {
                        "scope": "server_only",
                        "kind": "connection",
                        "name": name,
                        "note": f"connection '{name}': on the server but not in dst.yaml — "
                        "adopt it as a declaration + secret_env (dst export prints the "
                        "snippet; secrets stay server-side) or leave it for its owner",
                    }
                )

    unselected = _unselected_definitions(session, incoming_definitions, incoming_configs)
    if unselected:
        out.append(
            {
                "scope": "semantic",
                "status": "hint",
                "hint": "shared definitions selected by no lens: " + ", ".join(unselected),
            }
        )
    # A clean plan is not a clean bill of health. Name the gates apply will run
    # that a dry run cannot, every time a push carries something they apply to.
    if incoming or "dst.yaml" in body.files:
        out.append(
            {"scope": "unchecked", "checks": list(apply_engine.PLAN_UNCHECKED)},
        )
    return out


@router.post("/apply")
def apply_project(
    body: ProjectFiles,
    session: Session = Depends(get_app_session),
    org_id: uuid.UUID = Depends(get_admin_org),
    identity: AdminIdentity = Depends(get_admin_identity),
    probe_certified: bool = Query(default=False),
    require_gates: bool = Query(default=False),
    # The transient, audited escape from a failing-case block.
    # A reason is MANDATORY — the override without one is refused, because
    # "who allowed this red publish, and why" must be answerable from history.
    allow_failing_cases: bool = Query(default=False),
    override_reason: str = Query(default=""),
) -> list[dict[str, object]]:
    """Apply a pushed file tree — the single deployment door (`dst apply`).
    One transaction per org under an advisory lock (a concurrent apply gets a
    409), landing connections → profiles → semantic layer → lenses blue/green.
    Returns one result row per scope."""
    if allow_failing_cases and not override_reason.strip():
        raise HTTPException(
            status_code=422,
            detail="allow_failing_cases needs override_reason — the audited one-off "
            "must say why (e.g. 'intended behaviour change: term now ambiguous')",
        )
    gate_override = override_reason.strip() if allow_failing_cases else None
    # One apply per org at a time: a transaction-scoped advisory lock, released
    # with this request's commit/rollback. A concurrent apply would interleave
    # asset upserts and recompiles halfway through another's — refuse it cleanly.
    locked = session.execute(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _apply_lock_key(org_id)}
    ).scalar()
    if not locked:
        raise HTTPException(
            status_code=409, detail="another apply is in progress for this org — retry shortly"
        )
    out: list[dict[str, object]] = []
    conn_applied: list[str] = []
    if "dst.yaml" in body.files:
        try:
            project = parse_project_yaml(body.files["dst.yaml"])
        except ValueError as exc:
            out.append({"scope": "project", "action": "rejected", "errors": [str(exc)]})
            return _abort(session, out)
        conn_applied, warnings, errors, capabilities = apply_engine.apply_connections(
            session, project
        )
        out.append(
            {
                "scope": "connections",
                "applied": conn_applied,
                "warnings": warnings,
                "errors": errors,
                # Per-connection capability report — present only for probed
                # (i.e. changed) connections; a gap is deploy-time visible,
                # and absence is never a claim.
                "capabilities": capabilities,
            }
        )
    # Probe artifacts land after connections (the same push may declare theirs)
    # and before lenses (whose missing-table check reads the store these fill).
    profile_applied, profile_warnings = apply_engine.apply_profiles(session, body.files)
    if profile_applied or profile_warnings:
        out.append(
            {
                "scope": "profiles",
                "applied": profile_applied,
                "warnings": profile_warnings,
                "errors": [],
            }
        )
    semantic_files = split_semantic(body.files)
    if semantic_files:
        try:
            applied, semantic_warnings = apply_engine.apply_semantic_assets(session, semantic_files)
        except ValueError as exc:
            # Lenses would compile against a shared layer the push meant to
            # replace — stop before any lens compiles.
            out.append({"scope": "semantic", "action": "rejected", "errors": [str(exc)]})
            return _abort(session, out)
        out.append(
            {"scope": "semantic", "applied": applied, "warnings": semantic_warnings, "errors": []}
        )
    rejected: set[str] = set()
    incoming_trees = split_by_lens(body.files)
    # A lens whose managed files match the DB (plan's own comparison) and whose
    # inputs this apply didn't touch skips the publish path: recompile, version
    # bump, and the eval gate's live generation would all re-run against
    # identical inputs — a no-op apply would otherwise pay a real one's full
    # token spend, once per lens. Shared-asset staleness is
    # NOT this skip's concern: recompile_stale below reads compiled provenance
    # and republishes stale lenses whether or not the push carried them.
    # --probe-certified is an explicit ask to (re)walk the certified surface —
    # its "probed 0 new entries" note must not be skipped away.
    inputs_touched = bool(conn_applied or profile_applied or probe_certified)
    unchanged: set[str] = set()
    if incoming_trees and not inputs_touched:
        unchanged = {
            p.lens
            for p in plan_engine.plan_lenses(_db_trees(session), incoming_trees)
            if p.status == "unchanged"
        }
        # An upgrade notice means a release reinterpreted this lens's STORED
        # config — identical files no longer imply an identical compile, so the
        # lens republishes (which recompiles under the new release, surfaces
        # the notice, and clears it).
        unchanged -= set(store.upgrade_notices(session))
        # A lens the push itself made stale (its shared assets landed above)
        # takes the full path too, so a gate rejection stays on the LENS row —
        # not deferred to the recompile pass's rejected-recompile.
        unchanged -= set(_stale_map(session, semantic_store.asset_hashes(session)))
    for lens, tree in sorted(incoming_trees.items()):
        if lens in unchanged:
            row: dict[str, object] = {"lens": lens, "action": "unchanged"}
            if standing := apply_engine.unchanged_lens_warnings(
                session, lens, has_certified_file="certified_answers.yaml" in tree
            ):
                row["warnings"] = standing
            out.append(row)
            continue
        try:
            source = load_lens_source(tree)
        except (ValueError, KeyError) as exc:
            out.append({"lens": lens, "action": "rejected", "errors": [str(exc)]})
            rejected.add(lens)
            continue
        result = apply_engine.apply_lens(
            session,
            lens,
            source,
            org_id=org_id,
            probe_certified=probe_certified,
            created_by=identity.actor,
            gate_override=gate_override,
        )
        out.append(_lens_row(result))
        if result.errors:
            rejected.add(lens)
    # A lens rejected above must not resurrect through the recompile pass: its
    # gate-blocked attempt staged a failing eval run, and re-gating against that
    # just-lowered baseline republished the broken definition as 'recompiled'
    # (the one-shot tripwire). The abort below rolls the run back; skipping
    # keeps the output honest and saves a doomed re-score.
    for result in apply_engine.recompile_stale(session, org_id=org_id, skip=rejected):
        out.append(_lens_row(result))
    if require_gates:
        # Fail closed on demand: a pipeline that asked for
        # gates must be able to tell "gate passed" from "gate never ran" — by
        # default a provider outage silently converts a gated apply into an
        # ungated one that exits 0. Any configured-but-skipped gate is an
        # error here, and blue/green aborts the whole apply below.
        for row in out:
            gate = str(row.get("gate") or "")
            if gate.startswith("skipped"):
                row.setdefault("errors", [])
                row["errors"] += [  # type: ignore[operator]  # rows are dict[str, object]
                    f"--require-gates: eval gate {gate} — fail-closed mode refuses "
                    "to publish on a gate that never ran"
                ]
    if any(row.get("errors") for row in out):
        return _abort(session, out)
    # Landed-state lint: no lens's validate ran on the definitions no
    # lens selects — name their dormant governance in the same apply output.
    dormant = _dormant_definition_warnings(session)
    if dormant:
        out.append({"scope": "semantic", "action": "lint", "warnings": dormant, "errors": []})
    # Router cross-claim detection: a lens pair whose anchors
    # overlap at near-verbatim similarity is inseparable by embedding — a
    # modelling problem reported HERE, at apply, never left as a runtime coin
    # flip. Standing misconfiguration: warns on every apply until the
    # vocabularies split or the lenses merge. Best-effort — an unembedded
    # anchor store (keyless install) just reports nothing.
    try:
        from services.router import anchor_store

        overlaps = anchor_store.cross_claims(session)
    except Exception:
        overlaps = []
    if overlaps:
        out.append(
            {
                "scope": "router",
                "action": "lint",
                "warnings": [
                    f"lenses '{a}' and '{b}' cross-claim: their router anchors overlap at "
                    f"{sim:.3f} cosine — the embedding cannot separate them, so routing "
                    "between them rests entirely on the LLM decider; split the shared "
                    "vocabulary between their use_when/sample questions, or merge the lenses"
                    for a, b, sim in overlaps[:10]
                ],
                "errors": [],
            }
        )
    # A successful apply is the repair act: clear the serve-time drift
    # marks — the layer just caught up with the warehouse, deliberately. If the
    # drift persists, the next serve error re-marks within one request.
    cleared = store.clear_degraded(session)
    if cleared:
        out.append(
            {
                "scope": "lenses",
                "action": "degraded-cleared",
                "warnings": [
                    f"{cleared} lens(es) un-marked degraded — answers stop carrying the "
                    "drift warning; a persisting fault re-marks on its next serve error"
                ],
                "errors": [],
            }
        )
    return out


def _abort(session: Session, out: list[dict[str, object]]) -> list[dict[str, object]]:
    """Blue/green: ANY error aborts the WHOLE apply. Roll back every staged
    write — connections, semantic assets, lens publishes/versions, eval cases,
    certified answers, AND the gate's eval runs, so a blocked attempt's failing
    score never becomes the next apply's baseline. Prior state keeps serving.

    The JSON must not lie either: rows staged before the abort said
    "created"/"updated" with a new version — and a row can carry BOTH a
    success action and errors (a lens that published, then had its certified
    answers rejected). Every row whose action
    claims success is rewritten to "rolled-back" with version nulled, errors
    preserved as the diagnosis; rows already "rejected"/"rejected-recompile"
    keep their action; every row's ``applied`` entries — staged counts that
    rolled back too — are prefixed "rolled back:". The aborted banner closes
    the list."""
    session.rollback()
    for row in out:
        applied = row.get("applied")
        if isinstance(applied, list):
            # The staged per-file counts ("certified answers: created 1, …")
            # rolled back with everything else — left verbatim they read as a
            # partial apply; each entry says so itself.
            row["applied"] = [f"rolled back: {entry}" for entry in applied]
        if row.get("action") in ("rejected", "rejected-recompile"):
            continue  # the diagnosis rows keep their verdict
        row["action"] = "rolled-back"
        if "version" in row:
            row["version"] = None
    out.append(
        {
            "scope": "apply",
            "action": "aborted",
            "detail": "nothing deployed — fix the errors and re-apply",
        }
    )
    return out


def _lens_row(result: apply_engine.LensApplyResult) -> dict[str, object]:
    # The one funnel every lens row (apply AND recompile) passes through, so
    # ordering warnings here orders them everywhere: lint first, DEGRADED last
    # — the skipped-gate line has to be the last thing you read.
    return {
        "lens": result.lens,
        "action": result.action,
        "version": result.version,
        "errors": result.errors,
        "warnings": apply_engine.order_warnings(result.warnings),
        "applied": result.applied,
        "gate": result.gate,
        # The decision behind the label: score/prev_score/failing/skip_reason,
        # so --json consumers can diff applies and gate CI on it.
        "gate_detail": result.gate_detail,
    }
