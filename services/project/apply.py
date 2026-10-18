"""Apply: incoming project files → the database. Files win; apply = publish.

Blue/green: every function here stages writes on the CALLER's session
and never commits — the apply endpoint holds the one transaction and rolls the
whole thing back if any step errors, so an apply deploys everything or nothing.

Order is dependency order (the Octavia lesson): connections → shared semantic
assets (semantic/** → semantic_asset upserts) → lenses (each lens's ``select``
compiles against the DB's shared layer post-upsert into the embedded
SemanticModel the runtime consumes) → a recompile pass over every published
lens whose compiled provenance went stale — including lenses the push never
mentioned. Per lens: validate the compiled bundle (same validate_bundle as the
wizard), upsert eval cases AND certified answers first (both are the publish
gate's inputs — the gate must see cases and certified state arriving in the
same push; certified embeddings re-derived here, the one write-time model
dependency, skipped with a warning when no embedder key), self-test the
answers the push itself landed (alert-only — certify-to-override is a primary
use case), refuse gate starvation (a push emptying the active certified corpus
aborts under eval_gate: block, warns loudly under warn), gate (the
binding-affected certified corpus tests against the candidate bundle), upsert
draft, publish, snapshot a lens_version. DB-side objects the files don't
mention are left alone with one deliberate exception: a pushed
certified_answers.yaml owns its file-originated answers, so entries absent
from it DELETE (review-approved answers are server-origin and survive);
everything else deletes only explicitly.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from services.certify import binding as certify_binding
from services.certify import store as certify_store
from services.certify.bindings import certified_bindings, foreign_tables, restamp_bindings
from services.certify.generate import _MAX_ROWS, _PROSE_ROWS, _value_summary
from services.config import EnvRefError, settings
from services.contracts.authoring import collapse_notes
from services.contracts.lens_config import LensConfig
from services.contracts.profile import TableProfile
from services.contracts.protocols import Connector, Embedder
from services.contracts.semantic_model import Definition, SemanticModel
from services.contracts.shared_semantic import (
    SharedEntity,
    SharedRelationship,
    asset_content_hash,
)
from services.contracts.warehouse import DryRunResult, QueryResult, SchemaSnapshot
from services.db import embedding_meta
from services.definitions import standards as std_store
from services.evals import service as eval_service
from services.evals import store as eval_store
from services.evals.certified_suite import format_result
from services.evals.rewrite import rewrite_to_sources
from services.lenses import connection_store, store
from services.project.compile import (
    CompileError,
    compile_lens_model,
    default_qualifier,
    dialect_for,
    relationship_pairs,
)
from services.project.loader import LensSource
from services.project.plan import stale_asset_keys
from services.project.schema import ConnectionDecl, ProjectConfig
from services.router import anchor_store
from services.runtime import sql_guard
from services.runtime.bounded import ApplyStepTimeout, apply_bounded, apply_first_open
from services.runtime.canon import canon_summary
from services.runtime.identifiers import reserved_in
from services.security.crypto import CryptoNotConfigured
from services.semantic import store as semantic_store
from services.semantic.files import parse_semantic_files
from services.validate.report import (
    ValidationReport,
    collapse_warnings,
    status_done_family_warnings,
    validate_bundle,
)

if TYPE_CHECKING:
    from services.lenses.connection_eval import EvalResult

log = logging.getLogger("dst")

DEGRADED = "DEGRADED: "


def _degraded(message: str) -> str:
    """Mark a warning that says a configured guarantee did NOT run: a skipped
    gate, a corpus stored unembedded or unprobed, a self-test that could not
    execute. Unmarked, the one warning that matters (`eval_gate: warn
    configured but … gate SKIPPED`) sits in the middle of a wall of
    per-definition lint, and readers learn to skip the block. Prefixed to be
    greppable, and sorted last by ``order_warnings`` so it is the final thing
    an apply row says."""
    return DEGRADED + message


def order_warnings(warnings: list[str]) -> list[str]:
    """Lint first, degradations last — the last lines of a row are the ones
    saying the system did less than you configured. Stable within each group."""
    return [w for w in warnings if not w.startswith(DEGRADED)] + [
        w for w in warnings if w.startswith(DEGRADED)
    ]


def _resolve_secret(env_name: str | None) -> str | None:
    from services.config import resolve_env_ref

    return resolve_env_ref(env_name)


def certified_orphan_warning(session: Session, name: str) -> str | None:
    """The line for a push that carries no certified_answers.yaml while the
    server holds file-managed answers for this lens.

    Deleting the file is the natural "remove them all" gesture, and it is the
    one gesture the files-win rule does not cover: absence leaves the surface
    unmanaged, so the answers keep serving — the obvious rollback for a bad
    certification silently does nothing. Absence stays unmanaged BY DESIGN
    (export omits empty files, so treating absence as a delete would open a
    data-loss path through the other door); the honest half is saying so, with
    the remedy, on every apply that hits the state."""
    orphans = [
        a
        for a in certify_store.list_for_lens(session, name)
        if certify_store.is_active(a.status) and not str(a.source or "").startswith("review:")
    ]
    if not orphans:
        return None
    return (
        f"no certified_answers.yaml in this push, but the server holds {len(orphans)} "
        "file-managed certified answer(s) for this lens — they keep serving (file "
        "ABSENCE never deletes); to remove them, commit a certified_answers.yaml "
        "containing [] , or adopt them with `dst export --lens " + name + "`"
    )


def unchanged_lens_warnings(
    session: Session, name: str, *, has_certified_file: bool = True
) -> list[str]:
    """Standing degradations a lens carries whether or not it republishes.

    The publish-path skip must not silence corpus-wide conditions that warn on
    EVERY apply by doctrine — the failure mode is answers left unembedded by an
    earlier degraded apply, with every later apply reporting success while the
    corpus could never match. The certified-orphan line rides here too: deleting
    ONLY certified_answers.yaml plans the lens `unchanged` (absence is not a
    diff), so the skip path is exactly where that push lands."""
    warnings: list[str] = []
    if unembedded := certify_store.count_unembedded(session, name):
        warnings.append(_degraded(certify_store.UNEMBEDDED_WARNING.format(n=unembedded)))
    if not has_certified_file and (orphan := certified_orphan_warning(session, name)):
        warnings.append(orphan)
    return warnings


def _bare_path_hint(secret: str | None, env_name: str | None) -> str:
    """The GCP ecosystem takes bare service-account paths, so muscle memory sets
    `DST_API_KEY_BIGQUERY=/path/to/sa.json` — which then JSON-parses downstream
    into `Expecting value: line 1 column 1`. When the failed secret's value
    names an existing file, say what was meant."""
    if not secret or "\n" in secret or not env_name:
        return ""
    try:
        if not Path(secret).expanduser().is_file():
            return ""
    except OSError:
        return ""
    return (
        f" — {env_name} is the path to an existing file, not its contents; "
        f"did you mean {env_name}=@{secret} (a leading @ loads the file)"
    )


_PROBED_TYPES = {"duckdb", "bigquery", "postgres", "mysql", "snowflake"}


def apply_connections(
    session: Session, project: ProjectConfig
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Declared connections → DB, applied BEFORE lenses (dependency order —
    the Octavia lesson). Missing secrets skip the declaration with a warning;
    an existing connection keeps its stored secret when none is supplied.

    Every warehouse declaration is probed (build + read, the same evaluation
    the create endpoint runs) BEFORE it lands: a dead credential in a file must
    never replace a working stored one — the failure comes back as an error
    naming the connection and the env ref to fix. A probed connection also
    reports its CAPABILITIES (query, query history) so a permission gap is a
    visible degradation at deploy time, not a silent nightly skip; unchanged
    connections skip the probe and report nothing — absence of the line, never
    a false ✓."""
    from services.lenses.connections import build_connector, config_warnings

    applied: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    capabilities: list[str] = []
    for name, decl in project.connections.items():
        # Config-key typos warn on EVERY apply, before the change short-circuit:
        # an unread key is a standing misconfiguration, not a one-time event
        # (a stray `schema:` sits in dst.yaml looking effective, doing nothing).
        warnings.extend(
            f"connection '{name}': {w}" for w in config_warnings(decl.type, decl.config)
        )
        try:
            secret = _resolve_secret(decl.secret_env)
        except EnvRefError as exc:
            # A typo'd @-ref is a per-connection misconfiguration, not a reason
            # to sink the whole apply.
            errors.append(f"connection '{name}' NOT applied: {exc}; prior state kept")
            continue
        record = connection_store.get_connection(session, name)
        exists = record is not None
        if record is not None:
            # Declaration matches stored state → nothing to apply: no probe
            # round-trip, no noisy "updated 'jaffle'" on every unrelated apply.
            # Access rides in stored config only.
            stored_cfg = {k: v for k, v in (record.config or {}).items() if k != "access"}
            if stored_cfg == decl.config and (
                secret is None or secret == connection_store.get_secret(session, name)
            ):
                continue
        if decl.secret_env and secret is None and not exists:
            warnings.append(
                f"connection '{name}' skipped: {decl.secret_env} is not set (add it to .env)"
            )
            continue
        if decl.type in _PROBED_TYPES:
            probe_secret = secret
            if probe_secret is None and exists:
                probe_secret = connection_store.get_secret(session, name)
            hint = f" — check {decl.secret_env} in .env" if decl.secret_env else ""
            try:
                # The warehouse round-trip (read probe, capability report) runs
                # under the apply deadline: a warehouse that accepts the
                # connection and never answers must not hold the org's apply lock
                # for as long as the process lives. The warehouse's one-time cost
                # in this process (a cold machine's extension download) is paid
                # first, under its own bound, so the deadline measures the probe.
                connector = build_connector(decl.type, decl.config, probe_secret)
                apply_first_open(name, connector)
                result, report = apply_bounded(
                    f"the probe of connection '{name}'", partial(_probe_declaration, connector)
                )
            except ApplyStepTimeout:
                raise  # the deadline is the apply's verdict, not this connection's error
            except Exception as exc:  # noqa: BLE001 — bad params must not land either
                path_hint = _bare_path_hint(probe_secret, decl.secret_env)
                errors.append(
                    f"connection '{name}' NOT applied: {exc}{path_hint}{hint}; prior state kept"
                )
                continue
            if not result.ok:
                failed = result.failure
                detail = failed.error if failed else "probe failed"
                errors.append(f"connection '{name}' NOT applied: {detail}{hint}; prior state kept")
                continue
            if report is not None:
                capabilities.append(f"{name}: read ✓ · {report}")
        try:
            if not exists:
                connection_store.create_connection(session, name, decl.type, decl.config, secret)
                applied.append(f"created '{name}' ({decl.type})")
            else:
                connection_store.update_connection(session, name, decl.config, secret)
                applied.append(f"updated '{name}'")
        except CryptoNotConfigured:
            warnings.append(
                f"connection '{name}' skipped: DST_SECRET_KEY is not set "
                "(run `dst secret`, add it to .env)"
            )
    return applied, warnings, errors, capabilities


def _probe_declaration(connector: Connector) -> tuple[EvalResult, str | None]:
    """Probe a declared connection for read — the same evaluation the create
    endpoint runs — plus its capability report when the read passed (None when
    the report itself failed: it must never fail an apply). No database access:
    this is the half of `apply_connections` that runs under the apply deadline,
    on its own thread."""
    from services.lenses.connection_eval import capability_report, evaluate_connection

    result = evaluate_connection(connector, ["read"])
    if not result.ok:
        return result, None
    try:
        return result, capability_report(connector)
    except Exception:  # noqa: BLE001 — the report must never fail an apply
        return result, None


class _BoundedConnector:
    """The probe connector with every warehouse call under the apply deadline
    (`apply_bounded`): a certified answer's probe or an eval case's oracle that
    the warehouse accepts and never answers ends the apply loudly instead of
    holding the org's apply lock for as long as the process lives."""

    def __init__(self, inner: Connector, step: str) -> None:
        self._inner = inner
        self._step = step
        self.kind = inner.kind

    def introspect(self) -> SchemaSnapshot:
        return apply_bounded(self._step, self._inner.introspect)

    def dry_run(self, sql: str) -> DryRunResult:
        return apply_bounded(self._step, partial(self._inner.dry_run, sql))

    def execute(
        self, sql: str, *, read_only: bool = True, row_limit: int | None = None
    ) -> QueryResult:
        return apply_bounded(
            self._step,
            partial(self._inner.execute, sql, read_only=read_only, row_limit=row_limit),
        )


def _aware(dt: datetime) -> datetime:
    """A naive timestamp (an older artifact's) compares as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def apply_profiles(session: Session, files: dict[str, str]) -> tuple[list[str], list[str]]:
    """Pushed probe artifacts (``profiles/<conn>.probe.json``) → table_profile upserts.

    The seam that makes `dst probe` reach serving: the committed artifact
    lands in the store `assembly.profile_facts` already reads, so value
    dictionaries and partition hints enter the prompt with no runtime change.
    Runs after connections and BEFORE lenses, so `_missing_warehouse_tables`
    checks against the profiles this same push carries.

    Advisory enrichment, never governance state: a malformed artifact WARNS and
    skips (contrast semantic files, which abort — a truncated JSON must not hold
    lens publishes hostage), an unapplied connection warns and skips, and a
    stored profile newer than the incoming table's is kept — a REST-refreshed
    server must not be downgraded by an old commit. Absence never deletes.
    """
    from services.lenses import profile_store
    from services.project.probe import is_probe_path, parse_probe

    applied: list[str] = []
    warnings: list[str] = []
    for path in sorted(files):
        if not is_probe_path(path):
            continue
        try:
            artifact = parse_probe(files[path], path)
        except ValueError as exc:
            warnings.append(f"{exc} — skipped (re-run `dst probe` and commit the result)")
            continue
        if connection_store.get_connection(session, artifact.connection) is None:
            warnings.append(
                f"{path}: connection '{artifact.connection}' is not applied — skipped "
                "(declare it in dst.yaml so the same push lands it first)"
            )
            continue

        landed = kept = 0
        for profile in artifact.tables:
            stored = profile_store.get_profile(session, artifact.connection, profile.table)
            if stored is not None and _aware(stored.profile.profiled_at) > _aware(
                profile.profiled_at
            ):
                kept += 1
                continue
            # The name inside decides (read_probe's rule) — a table row must not
            # ride in under a different connection than its artifact claims.
            profile_store.upsert_profile(
                session, profile.model_copy(update={"connection": artifact.connection})
            )
            landed += 1
        note = f", kept {kept} newer server-side" if kept else ""
        applied.append(f"profiles '{artifact.connection}': landed {landed} table(s){note}")
    return applied, warnings


def apply_semantic_assets(session: Session, files: dict[str, str]) -> tuple[list[str], list[str]]:
    """Incoming ``semantic/**`` files → semantic_asset upserts, before any lens
    compiles. Returns (per-asset actions, warnings); raises ValueError on a
    malformed file (parse_semantic_files names the path). Absence never deletes.

    Two kinds of warning ride out of here: reserved entity names, and the
    inert-key findings (frontmatter keys that parsed and are read by nothing).
    An unknown key never reaches this point — it is an error at the parse seam."""
    notes: list[str] = []
    entities, definitions, relationships = parse_semantic_files(files, notes=notes)
    before = semantic_store.asset_hashes(session)
    applied: list[str] = []
    items: list[tuple[str, str, dict[str, object]]] = (
        [("entity", name, entity.model_dump(mode="json")) for name, entity in entities.items()]
        + [
            ("definition", term, definition.model_dump(mode="json"))
            for term, definition in definitions.items()
        ]
        + [
            ("relationship", name, relationship.model_dump(mode="json"))
            for name, relationship in relationships.items()
        ]
    )
    for kind, name, body in sorted(items):
        key = f"{kind}/{name}"
        if asset_content_hash(kind, body) == before.get(key):
            continue
        semantic_store.upsert_asset(session, kind, name, body)  # type: ignore[arg-type]
        applied.append(f"{'updated' if key in before else 'created'} {key}")
    return applied, _reserved_name_warnings(entities) + collapse_notes(notes)


def _reserved_name_warnings(entities: Iterable[str]) -> list[str]:
    """Name the entities whose names are SQL keywords, and say what happens next.

    `order` is one of the commonest table names there is; a curator whose warehouse
    has one cannot rename it, so this is a NOTE, not a rejection — dst quotes
    the name wherever it emits SQL. It is worth saying out loud because the author
    now knows why their own hand-written SQL against that lens needs quoting too."""
    flagged = sorted((name, reserved_in(name)) for name in entities if reserved_in(name))
    return [
        f"entity '{name}' is a SQL keyword in {', '.join(dialects)} — dst quotes it "
        f"in every statement it emits; quote it yourself in authored expressions"
        for name, dialects in flagged
    ]


@dataclass
class LensApplyResult:
    lens: str
    # created | updated | unchanged | recompiled | rejected | rejected-recompile
    # ("unchanged": republished through a touched input with its managed files
    # identical to the DB's — the plan's verdict, kept on the apply row).
    action: str
    version: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)  # honest per-file counts
    # The eval gate's outcome for this lens: "passed", "blocked", or
    # "skipped (<reason>)"; None when eval_gate: off or the lens never reached
    # the gate. Rides the row so the apply footer can say what the safety net
    # actually did — a wall of identical skip warnings otherwise hides it.
    gate: str | None = None
    # The decision behind the label: score, prev_score, failing case questions,
    # skip_reason. The label alone made --json a strict subset
    # of what the gate knew — nothing could be tracked over time, diffed
    # between applies, or asserted in CI without a direct eval_run query.
    gate_detail: dict[str, Any] | None = None


def _samples_embedding_stale_definitions(
    session: Session, name: str, bundle: store.LensBundle
) -> list[str]:
    """The THIRD shadowing vector (instructions were the first, certified are
    exempt by design): a sample query that canonically embeds a definition's
    OLD sql_expr keeps teaching the retired logic — the serving model and the
    eval runner both copy exemplars, so a broken definition can score as
    passing and serve stale answers. When a definition's sql_expr CHANGES,
    any sample still carrying the previous expression is an ERROR naming both
    sides; update the sample in the same push. Exact historical containment —
    no false positives (the embed was verbatim when the sample was written).
    Containment is over canonical TREES (``sql_canon.carries``), not characters:
    `position` is a substring of `position_name`, and a definition that moved to
    the longer column must not flag every sample that reads it; a sample writing
    the retired expression with other parentheses or qualifiers still carries it."""
    from services.runtime import sql_canon

    row = store.get_lens(session, name)
    raw = (row or {}).get("published")
    if raw is None:
        return []
    prev = store.LensBundle.model_validate(raw).semantic_model
    new_by_term = {d.term: d for d in bundle.semantic_model.definitions}

    out: list[str] = []
    for prev_def in prev.definitions:
        old_expr = (prev_def.sql_expr or "").strip()
        current = new_by_term.get(prev_def.term)
        if not old_expr or current is None or (current.sql_expr or "").strip() == old_expr:
            continue
        for sample in bundle.semantic_model.sample_queries:
            if sql_canon.carries(sample.sql, old_expr, bundle.semantic_model):
                out.append(
                    f"sample query '{sample.question}' embeds the PREVIOUS logic of "
                    f"definition '{prev_def.term}' ({old_expr!r}) — the definition "
                    "changed in this push; update or remove the sample in the same "
                    "apply, or it will keep steering answers to the retired meaning"
                )
    return out


def _missing_warehouse_tables(session: Session, bundle: store.LensBundle) -> list[str]:
    """The dbt relation-exists analog as an apply-time WARNING: each
    entity's source.table checked against the connection's STORED profiles
    (populated by background profiling — zero extra warehouse round-trips).
    No profiles for the connection → silent (nothing to check against);
    the warehouse may legitimately lag or lead the model, hence never an error."""
    from services.certify.bindings import _ref_matches
    from services.lenses import profile_store

    out: list[str] = []
    known: dict[str, list[str]] = {}
    for entity in bundle.semantic_model.entities:
        conn = entity.source.connection
        if conn not in known:
            known[conn] = [p.profile.table for p in profile_store.list_profiles(session, conn)]
        tables = known[conn]
        if tables and not any(_ref_matches(entity.source.table.lower(), t) for t in tables):
            # "not in the profiling pass", never "not found": a capped/scoped
            # pass proves nothing about existence — the strong claim blames
            # authors for correct table names the probe never looked at.
            out.append(
                f"entity '{entity.name}': source table '{entity.source.table}' is not in "
                f"connection '{conn}'s last profiling pass — either the name is wrong, or "
                "the pass did not cover it (listing caps, dataset scoping); "
                f"`dst probe --tables {entity.source.table}` settles which"
            )
    return out


def _dictionary_warnings(
    session: Session,
    sm: SemanticModel,
    *,
    incoming: Mapping[str, Sequence[TableProfile]] | None = None,
) -> list[str]:
    """Value-dictionary lints over each entity's source table, read from the
    connection's STORED profiles with the push's own artifacts overlaid (the
    layering ``_expose_profiles`` uses, for the same reason: plan must see
    what apply will land). No profile for a table → nothing to lint."""
    from services.lenses import profile_store

    profiles: dict[str, TableProfile] = {}
    for entity in sm.entities:
        conn = entity.source.connection
        if any(k.startswith(f"{conn}:") for k in profiles):
            continue
        for stored in profile_store.list_profiles(session, conn):
            profiles[f"{conn}:{stored.profile.table}"] = stored.profile
        for pushed in (incoming or {}).get(conn, []):
            profiles[f"{conn}:{pushed.table}"] = pushed
    by_table = {
        key.partition(":")[2]: profile
        for key, profile in profiles.items()
        if key.partition(":")[0] in {e.source.connection for e in sm.entities}
    }
    from services.project.probe import join_cardinality_warnings

    return [
        *(i.message for i in status_done_family_warnings(sm, by_table)),
        *join_cardinality_warnings(sm, by_table),
    ]


def _embedder() -> Embedder | None:
    from services.llm import registry

    return registry.resolve_embedder()


def _shared_layer(
    session: Session,
) -> tuple[dict[str, SharedEntity], dict[str, Definition], dict[str, SharedRelationship]]:
    """The DB's shared assets as compile inputs (entities by name, defs by
    term, relationships by name)."""
    entities: dict[str, SharedEntity] = {}
    definitions: dict[str, Definition] = {}
    relationships: dict[str, SharedRelationship] = {}
    for asset in semantic_store.list_assets(session):
        if asset.kind == "entity":
            entity = SharedEntity.model_validate(asset.body)
            entities[entity.name] = entity
        elif asset.kind == "relationship":
            relationship = SharedRelationship.model_validate(asset.body)
            relationships[relationship.name] = relationship
        else:
            definition = Definition.model_validate(asset.body)
            definitions[definition.term] = definition
    return entities, definitions, relationships


def _default_qualifiers(
    session: Session, *, declared: dict[str, ConnectionDecl] | None = None
) -> dict[str, str]:
    """Connection name → the catalog its tables live under (BigQuery project,
    Snowflake database), for every connection this org has.

    Rides the same rail as ``_lens_dialect`` and for the same reason: apply
    lands connections before any lens compiles, so the DRY RUN has to read the
    push's own dst.yaml or a brand-new project qualifies nothing on its first
    plan and then qualifies everything on its first apply. Declarations win over
    records where both exist — including when the declaration DROPS the pin, so
    removing `project:` un-qualifies on the same push that removes it."""
    out: dict[str, str] = {}
    for record in connection_store.list_connections(session):
        if qualifier := default_qualifier(record.type, record.config):
            out[record.name] = qualifier
    for name, decl in (declared or {}).items():
        qualifier = default_qualifier(decl.type, decl.config)
        if qualifier:
            out[name] = qualifier
        else:
            out.pop(name, None)
    return out


def _expose_profiles(
    session: Session,
    config: LensConfig,
    *,
    incoming: Mapping[str, Sequence[TableProfile]] | None = None,
) -> tuple[dict[str, list[TableProfile]], dict[str, list[str]]]:
    """``(profiles, dropped)`` by connection, for every connection the lens's
    `expose:` grants name: the table profiles a grant resolves against, and the
    tables a stored profile still describes but the push's probe says are gone.

    ``incoming`` is the push's own probe artifacts. For the DRY RUN they are
    layered over the store because apply lands them before any lens compiles,
    so a plan that read only the store would reject a grant the same push is
    about to satisfy. They are also the catalog: `dst probe` writes every
    table the connection lists (`--tables` narrows the SAMPLING pass only), so
    a stored profile absent from a NEWER artifact is a table dropped upstream
    — withheld here, and named in ``dropped`` so the grant that matched only
    it fails by name. The store itself is never pruned by a push (absence
    never deletes, so an old commit cannot downgrade a server); the same
    no-downgrade rule is why a stored profile fresher than the artifact stays."""
    from services.lenses import profile_store

    profiles: dict[str, list[TableProfile]] = {}
    dropped: dict[str, list[str]] = {}
    for grant in config.expose:
        if grant.connection in profiles:
            continue
        stored = {
            p.profile.table: p.profile
            for p in profile_store.list_profiles(session, grant.connection)
        }
        pushed = list((incoming or {}).get(grant.connection, ()))
        if pushed:
            catalog = {p.table for p in pushed}
            probed_at = max(_aware(p.profiled_at) for p in pushed)
            gone = sorted(
                table
                for table, profile in stored.items()
                if table not in catalog and _aware(profile.profiled_at) < probed_at
            )
            for table in gone:
                del stored[table]
            if gone:
                dropped[grant.connection] = gone
        for profile in pushed:
            stored[profile.table] = profile
        profiles[grant.connection] = list(stored.values())
    return profiles, dropped


def incoming_profiles(files: Mapping[str, str]) -> dict[str, list[TableProfile]]:
    """The probe artifacts a push carries, by connection — the plan-side twin
    of ``apply_profiles``. A malformed artifact contributes nothing here, the
    way apply skips it with a warning."""
    from services.project.probe import is_probe_path, parse_probe

    out: dict[str, list[TableProfile]] = {}
    for path in sorted(files):
        if not is_probe_path(path):
            continue
        try:
            artifact = parse_probe(files[path], path)
        except ValueError:
            continue
        out.setdefault(artifact.connection, []).extend(
            p.model_copy(update={"connection": artifact.connection}) for p in artifact.tables
        )
    return out


def expose_plan_notes(
    session: Session,
    config: LensConfig,
    *,
    incoming: Mapping[str, Sequence[TableProfile]] | None = None,
) -> tuple[str | None, list[str]]:
    """What `dst plan` says about a lens's `expose:` grants: the count line
    (None when the grants do not resolve — the compile error already names
    why) and the warnings, chiefly a grant on a lens whose `query` door
    cannot serve it."""
    from services.project.expose import UNTYPED_WARNING, expose_summary, resolve_expose

    if not config.expose:
        return None, []
    profiles, dropped = _expose_profiles(session, config, incoming=incoming)
    entities, errors = resolve_expose(
        config,
        profiles,
        declared=[p.name for p in config.select.entities],
        dropped=dropped,
    )
    warnings = [UNTYPED_WARNING] if not config.untyped_fallback else []
    return (None if errors else expose_summary(entities, profiles)), warnings


def _lens_dialect(
    session: Session, config: LensConfig, *, declared: dict[str, ConnectionDecl] | None = None
) -> str:
    """The SQL dialect of the lens's warehouse connection (looked up by type).

    ``declared`` (name → type) is the push's own dst.yaml, for the DRY RUN:
    apply lands connections before any lens compiles, so a plan that saw only the
    DB would reject the very first apply of a brand-new project. Apply passes
    nothing — by then the declarations are records.

    The failure names the file that actually needs editing, which is not the same
    file in the two cases: an EMPTY `connections` is a hole in the lens's own
    lens.yaml, while a listed-but-unresolved one is dst.yaml's problem. One
    message said "declare one in dst.yaml" for both, and sent every author of
    the first (commoner) case to edit a file that was already correct."""
    for name in config.connections:
        record = connection_store.get_connection(session, name)
        declaration = (declared or {}).get(name)
        conn_type = (
            record.type if record is not None else (declaration.type if declaration else None)
        )
        if conn_type is None:
            continue
        try:
            return dialect_for(conn_type)
        except CompileError:
            continue  # a context source — keep looking for the warehouse
    if not config.connections:
        raise CompileError(
            f"lens '{config.name}' names no connection — add `connections: [<name>]` to "
            f"lenses/{config.name}/lens.yaml, naming a connection dst.yaml declares"
        )
    raise CompileError(
        f"lens '{config.name}' names {config.connections} in lenses/{config.name}/lens.yaml, "
        "but none of those is an applied warehouse connection — declare it in dst.yaml "
        "(type + config + secret_env) and `dst apply`, or fix the name"
    )


def _other_definitions(session: Session, name: str) -> list[tuple[str, list[Definition]]]:
    """Every OTHER published lens's definitions — drift's comparison set."""
    return [
        (other, b.semantic_model.definitions)
        for (other, _dn, _desc, b) in store.list_published(session)
        if other != name
    ]


def _deterministic_gates(
    session: Session, name: str, bundle: store.LensBundle
) -> tuple[list[str], ValidationReport]:
    """Publish's DETERMINISTIC half — everything it rejects on without dialling
    anything: validate_bundle's errors, then the stale-sample check (skipped when
    validate already failed, as this has always done). Returns (errors, report);
    the report carries the warnings the caller keeps.

    One implementation, called by ``_publish_bundle`` and by the dry run
    (``check_lens``), so ``dst plan`` rejects exactly what apply rejects. The
    non-deterministic half — the scored publish eval gate — stays in
    ``_publish_bundle``; ``PLAN_UNCHECKED`` names it."""
    report = validate_bundle(
        bundle, std_store.list_standards(session), _other_definitions(session, name)
    )
    if not report.ok:
        return [i.message for i in report.issues if i.severity == "error"], report
    return _samples_embedding_stale_definitions(session, name, bundle), report


def override_applies(
    decision: eval_service.GateDecision,
    gate_override: str | None,
    allow_cases: frozenset[str] | None,
) -> bool:
    """May a reasoned override carry a blocked publish past the gate?

    A per-case override (``--allow-case`` / ``--allow-failing-cases <id>``)
    applies only when it names EVERY failing case, certified divergences
    included (their ids are ``certified:<answer id>``) — an override for
    yesterday's red case cannot silently cover today's new one, which is how
    the blanket flag became standing policy. The blanket form (no cases named)
    still works and is reported as blanket, so its use is visible in review.
    Whatever the override covers is recorded on the apply row and in the
    version history."""
    if not (decision.blocked and gate_override):
        return False
    if allow_cases is None:
        return True
    return bool(decision.failing) and set(decision.failing) <= set(allow_cases)


def _publish_bundle(
    session: Session,
    name: str,
    bundle: store.LensBundle,
    *,
    org_id: uuid.UUID | str,
    summary: str,
    created_by: str = "",
    gate_override: str | None = None,
    allow_cases: frozenset[str] | None = None,
    known_divergences: Mapping[str, str] | None = None,
) -> tuple[int | None, list[str], list[str], str | None, dict[str, Any] | None]:
    """validate → eval gate → upsert draft → publish → snapshot. Returns (version,
    errors, warnings, gate outcome, gate detail); errors non-empty means no lens rows were
    written (prior bundle stands) — and, blue/green, the apply endpoint aborts the
    whole apply, rolling back the gate's staged eval run with everything else.
    The gate is the same check the interactive publish endpoint runs
    (eval_service.publish_gate) — apply must not bypass it: ``block`` on a
    regression rejects, ``warn`` publishes with the report as a warning.

    ``gate_override`` is the transient, audited escape from a
    failing-case block: the REASON the operator gave rides the version
    summary and the apply row, and the publish proceeds with the failure
    surfaced loudly. It never overrides certified divergences — those are
    served-answer contradictions, and the same push can re-certify instead.
    The alternative was editing ``eval_gate: block`` to ``warn`` in the file:
    a committed downgrade of the governance setting that someone must
    remember to restore."""
    errors, report = _deterministic_gates(session, name, bundle)
    if errors:
        return None, errors, [], None, None
    # Collapsed: one lens routinely carries a dozen-odd prose-only
    # definitions, each of which would otherwise warn on its own identical line.
    warnings = collapse_warnings([i for i in report.issues if i.severity == "warning"])
    warnings += _missing_warehouse_tables(session, bundle)
    warnings += _dictionary_warnings(session, bundle.semantic_model)
    decision = eval_service.publish_gate(
        session=session,
        org_id=org_id,
        lens=name,
        bundle=bundle,
        known_divergences=known_divergences,
    )
    gate = eval_service.gate_label(decision)
    detail = eval_service.gate_decision_dict(decision)
    if decision is not None and decision.recorded:
        warnings.append(
            f"eval gate: recorded the verified value of {decision.recorded} certified "
            "answer(s) (none stored, or stored as text by an earlier release) — the next "
            "push grades them against it"
        )
    # What the file-side allow-list covered this push, each with its reason:
    # an override nobody can see is the blanket flag by another name.
    if decision is not None and decision.known_covered:
        warnings += [
            f"eval gate: known divergence covered by evals/known_divergences.yaml: {line}"
            for line in decision.known_covered
        ]
    # A generation divergence under block/warn: the certified answer is the
    # override, so the push lands with the divergence named, never silenced.
    if decision is not None and decision.generation_divergences:
        warnings += [
            _degraded(
                f"{message} — reported, not blocking (eval_gate: {bundle.config.eval_gate} "
                "grades certified values; `eval_gate: block_generation` blocks on this, "
                "evals/known_divergences.yaml records it as judged)"
            )
            for message in decision.generation_divergences
        ]
    if decision is not None and decision.flaky:
        # Wobble is visible, never blocking: the fix for a case
        # that passes only on retry is certification, and the operator can't
        # certify what the footer never names.
        warnings.append(
            f"eval gate: {len(decision.flaky)} case(s) passed only on retry (flaky) — "
            "generation wobble, not failure; certify those questions to pin them"
        )
    if decision is not None and not decision.gated:
        # Configured but couldn't score (no smart-tier model / no approved
        # cases) — a silently-inert seatbelt is the worst state; say so, loudly.
        warnings.append(_degraded(decision.detail))
    elif decision is not None and (
        decision.blocked or decision.regressed or decision.certified_failures or decision.failing
    ):
        bar = (
            f"score {decision.score} < prev {decision.prev_score}"
            if decision.regressed
            else f"score {decision.score} ({decision.baseline or 'no previous run'})"
        )
        gate_report = f"{bar} — failing: {', '.join(decision.failing) or 'none listed'}"
        ids = " ".join(decision.failing) or "<case ids>"
        if override_applies(decision, gate_override, allow_cases):
            # The audited one-off: publish proceeds, the gate
            # stays `block` in the file, and the override + reason + the cases
            # it named are loud on the row and durable in the version history.
            by = (
                f"--allow-case {', '.join(sorted(allow_cases))}"
                if allow_cases
                else "--allow-failing-cases"
            )
            cases = f" — cases {', '.join(sorted(allow_cases))}" if allow_cases else ""
            gate = f"overridden ({gate_report})"
            warnings.append(
                _degraded(
                    f"eval gate blocked publish ({gate_report}) — PUBLISHED ANYWAY by "
                    f"{by}: {gate_override}"
                )
            )
            # A certified divergence the override covered is said in its own
            # words: the served answer now contradicts generation, by decision.
            warnings += [
                _degraded(f"{message} — published anyway by {by}")
                for message in decision.certified_failures
            ]
            summary = f"{summary} [gate override: {gate_override}{cases}]"
        elif decision.blocked:
            # Certified divergences are errors in their own right (reported
            # verbatim) — each named, then any score regression or failing
            # case (a first gated run has no baseline to hide behind — a red
            # case blocks the first time and the second time alike);
            # blue/green aborts the whole apply on any of them.
            errors = list(decision.certified_failures)
            if decision.regressed:
                errors.append(
                    f"eval gate blocked publish: accuracy regressed ({gate_report}) — "
                    "reconcile the cases, or publish once with "
                    f"--allow-failing-cases {ids} --reason '…' (audited; the gate stays "
                    f"{bundle.config.eval_gate})"
                )
            elif decision.failing:
                errors.append(
                    f"eval gate blocked publish: {len(decision.failing)} case(s) failing "
                    f"({gate_report}) — fix the cases or the lens, {_park_hint(decision.failing)}, "
                    f"or publish once with --allow-failing-cases {ids} --reason '…' "
                    f"(audited; the gate stays {bundle.config.eval_gate})"
                )
            return None, errors, [], "blocked", detail
        else:
            warnings += [
                _degraded(f"{message} — published anyway (eval_gate: warn)")
                for message in decision.certified_failures
            ]
            if decision.regressed:
                warnings.append(
                    _degraded(
                        f"eval gate: accuracy regressed ({gate_report}) — published anyway "
                        "(eval_gate: warn)"
                    )
                )
            elif decision.failing:
                warnings.append(
                    _degraded(
                        f"eval gate: {len(decision.failing)} approved case(s) failing "
                        f"(score {decision.score}) — published anyway (eval_gate: warn)"
                    )
                )
    if store.lens_exists(session, name):
        store.update_draft(session, name, bundle)
    else:
        store.create_lens(session, bundle)
    # A release that reinterpreted this lens's stored config left a line on it
    # for plan to show. Publishing CLEARS that line, so say it once here as
    # well: an operator who applies without planning first must not be the one
    # person the notice never reaches.
    notice = store.upgrade_notices(session).get(name)
    if notice is not None:
        warnings.append(notice)
    store.publish(session, name)
    # Pre-warm the router's anchor embeddings — best-effort, never
    # blocks apply; the /v1/query read path repairs on the next request.
    anchor_store.warm(session, bundle)
    version = store.record_version(session, name, bundle, summary=summary, created_by=created_by)
    return version, [], warnings, gate, detail


def apply_lens(
    session: Session,
    name: str,
    source: LensSource,
    *,
    org_id: uuid.UUID | str,
    probe_certified: bool = False,
    refresh_samples: bool = False,
    created_by: str = "",
    gate_override: str | None = None,
    allow_cases: frozenset[str] | None = None,
    incoming_profiles: Mapping[str, Sequence[TableProfile]] | None = None,
) -> LensApplyResult:
    """``incoming_profiles`` is the push's own probe artifacts (already landed
    by ``apply_profiles``): the catalog an `expose:` grant resolves against,
    so a table the probe no longer lists fails the grant here as it does in
    the plan."""
    config = source.config
    if config.name != name:
        return LensApplyResult(
            lens=name,
            action="rejected",
            errors=[f"lens.yaml names '{config.name}' but the tree is lenses/{name}/"],
        )
    try:
        dialect = _lens_dialect(session, config)
        shared_entities, shared_definitions, shared_relationships = _shared_layer(session)
        profiles, dropped = _expose_profiles(session, config, incoming=incoming_profiles)
        model, compile_warnings = compile_lens_model(
            config=config,
            shared_entities=shared_entities,
            shared_definitions=shared_definitions,
            shared_relationships=shared_relationships,
            local_definitions=source.local_definitions,
            use_when=source.use_when,
            sample_queries=source.sample_queries,
            dialect=dialect,
            asset_hashes=semantic_store.asset_hashes(session),
            default_qualifiers=_default_qualifiers(session),
            profiles=profiles,
            dropped_tables=dropped,
        )
    except CompileError as exc:
        return LensApplyResult(lens=name, action="rejected", errors=[str(exc)])

    existed = store.lens_exists(session, name)
    bundle = store.LensBundle(config=config, semantic_model=model)
    result = LensApplyResult(lens=name, action="updated" if existed else "created")
    # Keys the tree authored that nothing reads. An UNKNOWN key never gets this
    # far (the loader rejects it naming the file); these are real keys with no
    # reader, and staying quiet about them is what made the whole class of bug
    # survivable in the first place.
    result.warnings.extend(collapse_notes(source.notes))
    # Eval cases land BEFORE the gate: the apply that introduces a lens's cases
    # (typically the same one that sets eval_gate) must score them and record
    # the baseline run. Gating first left the seatbelt inert for one apply and
    # the baseline unrecorded, so the next — breaking — apply had nothing to
    # regress against and published as 'updated'. Cases are
    # gate inputs staged with everything else — under blue/green a rejection
    # aborts the whole apply and they roll back with it. A case whose
    # expected_sql fails the parse/probe is an ERROR (aborts), consistent with
    # everything-or-nothing; the no-connector parse-only path stays a warning.
    _apply_eval_cases(session, name, source, result, model, org_id)
    # Certified answers are gate inputs too: the corpus IS the suite,
    # so the push's certified state — a new answer, a same-push re-certify or
    # retire — must land BEFORE the gate reads it (the eval-case doctrine).
    # Blue/green covers the reorder: a rejected publish aborts the whole apply
    # and the staged upserts roll back with it.
    # Gate starvation by retirement (or, since files win, deletion) is measured
    # around the staged upserts: active answers before vs after. Only a push
    # carrying certified_answers.yaml can empty the corpus. `block` turns the
    # ≥1→0 transition into an abort; `warn` publishes but must WARN — the probe
    # caught the transition sailing through silently whenever approved eval
    # cases kept the publish gate scoreable (publish_gate's SKIPPED fallback
    # only fires when NOTHING scores). A lens that never HAD active answers
    # (fresh lens, first applies) is the 0→0 case, never the ≥1→0 transition.
    guard_starvation = config.eval_gate != "off" and (source.certified_answers is not None)
    had_active = guard_starvation and _has_active_certified(session, name)
    _apply_certified_answers(
        session,
        name,
        source,
        result,
        model,
        org_id=org_id,
        probe=probe_certified,
        refresh_samples=refresh_samples,
    )
    # A push managing this lens WITHOUT a certified_answers.yaml leaves any
    # server-side file-managed answers serving — say so with the remedy
    # (deleting the file looks like a rollback and isn't).
    if source.certified_answers is None and (orphan := certified_orphan_warning(session, name)):
        result.warnings.append(orphan)
    if had_active and not _has_active_certified(session, name):
        if eval_service.blocks(config.eval_gate):
            # The corpus IS the gate's suite: retiring the last active answer
            # under eval_gate: block would starve it — every later apply
            # publishing on a skipped gate. Blue/green: this aborts the apply.
            return LensApplyResult(
                lens=name,
                action="rejected",
                errors=[
                    "eval gate starved: this apply retires the last active certified "
                    "answer while eval_gate: block — keep at least one active answer, "
                    "or set `eval_gate: warn`/`off` in lens.yaml and re-apply",
                    *result.errors,
                ],
                applied=result.applied,
                warnings=result.warnings,
            )
        result.warnings.append(
            _degraded(
                "eval gate starved: this apply retires the last active certified "
                "answer while eval_gate: warn — published anyway, but the certified "
                "gate now has nothing to test (gate SKIPPED for certified coverage); "
                "certify a replacement to restore it"
            )
        )
    version, errors, validate_warnings, gate, gate_detail = _publish_bundle(
        session,
        name,
        bundle,
        org_id=org_id,
        summary=store.APPLY_SUMMARY,
        created_by=created_by,
        gate_override=gate_override,
        allow_cases=allow_cases,
        known_divergences=eval_service.known_divergence_ids(source.known_divergences),
    )
    if errors:
        return LensApplyResult(
            lens=name,
            action="rejected",
            errors=errors + result.errors,  # keep per-case skips visible on a reject
            applied=result.applied,
            warnings=result.warnings,
            gate=gate,
            gate_detail=gate_detail,
        )

    result.version = version
    result.warnings += compile_warnings + validate_warnings
    result.gate = gate
    result.gate_detail = gate_detail
    return result


# The gates apply runs that a DRY RUN cannot: each one needs the warehouse on the
# wire or a scored eval run against live generation. `dst plan` prints these
# rather than implying a clean bill of health — a dry run silent about what it
# skipped is the same defect as one that passes what apply rejects.
PLAN_UNCHECKED = (
    "warehouse connection probes — apply builds every declared connection and "
    "reads through it before it lands, and a dead credential aborts the apply",
    "eval-case expected_sql EXECUTED against the warehouse (plan parses it only) — "
    "an oracle that parses but errors aborts the apply",
    "the publish eval gate — it scores the certified corpus with live generation, "
    "so under `eval_gate: block` a regression can still abort the apply",
)


def check_lens(
    session: Session,
    name: str,
    source: LensSource,
    *,
    incoming_entities: dict[str, SharedEntity],
    incoming_definitions: dict[str, Definition],
    incoming_relationships: dict[str, SharedRelationship],
    incoming_connections: dict[str, ConnectionDecl],
    asset_hashes: dict[str, str],
    incoming_profiles: Mapping[str, Sequence[TableProfile]] | None = None,
    warnings_out: list[str] | None = None,
) -> list[str]:
    """Every gate ``apply_lens`` would REJECT this lens on that a dry run can run.

    ``warnings_out``, when given, collects the deterministic WARNINGS apply
    would print that need the compiled model (the value-dictionary lints) —
    plan shows them on the lens row, so an author sees them before applying.

    Plan's whole job is to predict apply, and without these it runs none of them:
    a cross-entity metric and an unservable ``model:`` each plan clean and then
    abort the apply. Every check below is the same function
    apply calls — name, dialect, compile, the certified-answer and eval-case entry
    gates, gate starvation, and publish's deterministic half — so the two cannot
    drift apart again.

    The inputs are layered the way apply layers them: the connections and shared
    assets THIS PUSH carries land before any lens compiles, so plan neither misses
    a rejection nor invents one against state the push is about to replace.

    What is NOT run here is named in ``PLAN_UNCHECKED``.
    """
    config = source.config
    if config.name != name:
        return [f"lens.yaml names '{config.name}' but the tree is lenses/{name}/"]
    shared_entities, shared_definitions, shared_relationships = _shared_layer(session)
    shared_entities.update(incoming_entities)
    shared_definitions.update(incoming_definitions)
    shared_relationships.update(incoming_relationships)
    profiles, dropped = _expose_profiles(session, config, incoming=incoming_profiles)
    try:
        model, compile_warnings = compile_lens_model(
            config=config,
            shared_entities=shared_entities,
            shared_definitions=shared_definitions,
            shared_relationships=shared_relationships,
            local_definitions=source.local_definitions,
            use_when=source.use_when,
            sample_queries=source.sample_queries,
            dialect=_lens_dialect(session, config, declared=incoming_connections),
            asset_hashes=asset_hashes,
            default_qualifiers=_default_qualifiers(session, declared=incoming_connections),
            profiles=profiles,
            dropped_tables=dropped,
        )
    except CompileError as exc:
        return [str(exc)]
    if warnings_out is not None:
        # The compile's own warnings (a dropped relationship, a sensitive
        # column with no hide) are apply's warnings too; plan predicts apply.
        warnings_out.extend(compile_warnings)
    stored_sql = {
        c.question.strip().lower(): c.expected_sql for c in eval_store.list_cases(session, name)
    }
    errors = [e for case in source.eval_cases for e in _eval_case_errors(case, model, stored_sql)]
    errors += [
        e
        for answer in source.certified_answers or []
        if str(answer.get("question", "")).strip()
        for e in _certified_entry_errors(answer, model)
    ]
    errors += _cross_match_errors(session, name, source, model, warnings_out)
    errors += _starvation_errors(session, name, source)
    if warnings_out is not None:
        warnings_out.extend(_binding_range_warnings(source))
        warnings_out.extend(_certified_definition_warnings(source.certified_answers or [], model))
    gate_errors, _report = _deterministic_gates(
        session, name, store.LensBundle(config=config, semantic_model=model)
    )
    if warnings_out is not None:
        warnings_out.extend(_dictionary_warnings(session, model, incoming=incoming_profiles))
    return errors + gate_errors


def check_recompiles(
    session: Session,
    *,
    incoming_entities: dict[str, SharedEntity],
    incoming_definitions: dict[str, Definition],
    incoming_relationships: dict[str, SharedRelationship],
    asset_hashes: dict[str, str],
    incoming_connections: dict[str, ConnectionDecl] | None = None,
    skip: frozenset[str] | set[str] = frozenset(),
) -> dict[str, list[str]]:
    """``{lens: errors}`` for published lenses whose RECOMPILE this push would break.

    ``recompile_stale`` runs over lenses the push never mentions, and one failure
    there aborts the whole apply exactly like a pushed lens's — which is how a
    shared-entity edit can be rejected through a lens the author was not editing.
    Plan announced "will recompile on apply" and left it at that; this tries it.
    ``skip`` names the lenses the push carries (``check_lens`` covers those, and
    apply republishes them with fresh provenance before the recompile pass runs)."""
    shared_entities, shared_definitions, shared_relationships = _shared_layer(session)
    shared_entities.update(incoming_entities)
    shared_definitions.update(incoming_definitions)
    shared_relationships.update(incoming_relationships)
    pairs = relationship_pairs(shared_relationships)
    out: dict[str, list[str]] = {}
    for name, _dn, _desc, bundle in store.list_published(session):
        provenance = bundle.semantic_model.shared_provenance
        if name in skip or provenance is None:
            continue
        if not stale_asset_keys(provenance.assets, asset_hashes, pairs):
            continue
        sm = bundle.semantic_model
        try:
            model, _warnings = compile_lens_model(
                config=bundle.config,
                shared_entities=shared_entities,
                shared_definitions=shared_definitions,
                shared_relationships=shared_relationships,
                local_definitions=[d for d in sm.definitions if d.source != "shared"],
                use_when=sm.use_when,
                sample_queries=sm.sample_queries,
                dialect=sm.dialect,
                asset_hashes=asset_hashes,
                default_qualifiers=_default_qualifiers(session, declared=incoming_connections),
                profiles=_expose_profiles(session, bundle.config)[0],
            )
        except CompileError as exc:
            out[name] = [str(exc)]
            continue
        errors, _report = _deterministic_gates(
            session, name, store.LensBundle(config=bundle.config, semantic_model=model)
        )
        if errors:
            out[name] = errors
    return out


def recompile_stale(
    session: Session, *, org_id: uuid.UUID | str, skip: frozenset[str] | set[str] = frozenset()
) -> list[LensApplyResult]:
    """Recompile every published lens whose shared provenance is stale vs the
    current asset hashes — including lenses absent from the push. Local state
    comes from the lens's own compiled model (local defs = source != "shared";
    use_when/sample_queries verbatim). A failure keeps the prior published
    bundle and reports action "rejected-recompile", loudly. ``skip`` names
    lenses this apply already rejected: re-gating one against its own staged
    failing run would 'recompile' the very definition the gate just blocked."""
    hashes = semantic_store.asset_hashes(session)
    shared_entities, shared_definitions, shared_relationships = _shared_layer(session)
    pairs = relationship_pairs(shared_relationships)
    # Connections landed before this pass, so the records ARE the push's own.
    qualifiers = _default_qualifiers(session)
    results: list[LensApplyResult] = []
    for name, _dn, _desc, bundle in store.list_published(session):
        if name in skip:
            continue  # rejected earlier in this apply — the abort covers it
        provenance = bundle.semantic_model.shared_provenance
        if provenance is None:
            continue  # never compiled from the shared layer — nothing to be stale against
        changed = stale_asset_keys(provenance.assets, hashes, pairs)
        if not changed:
            continue
        sm = bundle.semantic_model
        try:
            model, compile_warnings = compile_lens_model(
                config=bundle.config,
                shared_entities=shared_entities,
                shared_definitions=shared_definitions,
                shared_relationships=shared_relationships,
                local_definitions=[d for d in sm.definitions if d.source != "shared"],
                use_when=sm.use_when,
                sample_queries=sm.sample_queries,
                dialect=sm.dialect,
                asset_hashes=hashes,
                default_qualifiers=qualifiers,
                profiles=_expose_profiles(session, bundle.config)[0],
            )
        except CompileError as exc:
            results.append(
                LensApplyResult(lens=name, action="rejected-recompile", errors=[str(exc)])
            )
            continue
        fresh = store.LensBundle(config=bundle.config, semantic_model=model)
        version, errors, validate_warnings, gate, gate_detail = _publish_bundle(
            session,
            name,
            fresh,
            org_id=org_id,
            summary="recompile (shared assets changed)",
            created_by="process:recompile",
        )
        if errors:
            results.append(
                LensApplyResult(
                    lens=name,
                    action="rejected-recompile",
                    errors=errors,
                    gate=gate,
                    gate_detail=gate_detail,
                )
            )
            continue
        results.append(
            LensApplyResult(
                lens=name,
                action="recompiled",
                version=version,
                warnings=compile_warnings
                + validate_warnings
                + [f"shared changed: {', '.join(changed)}"],
                gate=gate,
                gate_detail=gate_detail,
            )
        )
    return results


def _apply_eval_cases(
    session: Session,
    name: str,
    source: LensSource,
    result: LensApplyResult,
    model: SemanticModel,
    org_id: uuid.UUID | str,
) -> None:
    """Upsert evals/cases.yaml keyed by question: unseen questions insert, an
    edited expected_sql/expected_answer updates in place (an edit used to be a
    silent no-op reported as success). Absence never deletes.

    Two case shapes, mutually exclusive per entry: value
    cases (expected_sql — these no longer gate anywhere; certified answers are
    the regression suite, and upserting one warns toward `dst evals migrate`)
    and behavioral expectations (expect: clarify|refuse|answer + optional term,
    scored on response shape by run_behavioral).

    A new or edited expected_sql is gated before it lands: parse in the lens
    dialect, then execute read-only + row-capped against the lens's warehouse
    (leaf table names repointed at their live sources — the eval plane's own
    mapping rule). A mis-shaped oracle used to sail through apply and crash the
    publish gate with a raw BinderException 500. Failures are
    per-case errors naming the question — the case is skipped, everything else
    lands. No resolvable connector → parse-checked only, stored with the
    warning _probe_connector raised (probing is infrastructure; the upsert must
    not rot on a dead credential)."""
    if not source.eval_cases:
        return
    existing = {c.question.strip().lower(): c for c in eval_store.list_cases(session, name)}
    stored_sql = {q: c.expected_sql for q, c in existing.items()}
    leaf_to_table = {e.source.table.split(".")[-1].lower(): e.source.table for e in model.entities}
    connector: Connector | None = None
    connector_failed = False

    def _oracle_problem(sql: str) -> str | None:
        nonlocal connector, connector_failed
        try:
            probe_sql = rewrite_to_sources(sql, model.dialect, leaf_to_table)
        except Exception as exc:  # noqa: BLE001 — any parse failure is the same gate
            return f"expected_sql does not parse as {model.dialect}: {exc}"
        if connector is None and not connector_failed:
            connector = _probe_connector(session, source.config, org_id, result, label="eval cases")
            connector_failed = connector is None
        if connector is None:
            return None
        try:
            connector.execute(probe_sql, read_only=True, row_limit=_MAX_ROWS)
        except ApplyStepTimeout:
            raise  # the deadline is the apply's verdict, not this case's
        except Exception as exc:  # noqa: BLE001 — a broken oracle must not land
            return f"expected_sql failed against the warehouse: {exc}"
        return None

    created = updated = unchanged = value_shaped = 0
    known_keys = (
        "question",
        "expected_sql",
        "expected_answer",
        "expect",
        "term",
        "status",
        "source",
        "tags",
    )
    for case in source.eval_cases:
        question = str(case.get("question", "")).strip()
        # A silently-dropped key costs the author several attempts (an entry
        # keyed `expected:` just vanishes) — name every unrecognized key.
        for key in sorted(set(case) - set(known_keys)):
            result.warnings.append(
                f"eval case '{question or '<no question>'}': unknown key '{key}' — "
                f"known keys: {', '.join(known_keys)}"
            )
        if not question:
            result.warnings.append("eval case entry without a 'question' key — skipped")
            continue
        expect = case.get("expect")
        if case_errors := _eval_case_errors(case, model, stored_sql):
            result.errors.extend(case_errors)
            continue
        if expect is None and case.get("term") is not None:
            result.warnings.append(
                f"eval case '{question}': 'term' only means something with "
                "'expect: clarify' — ignored"
            )
        stored = existing.get(question.lower())
        sql_in = case.get("expected_sql")
        if sql_in:
            value_shaped += 1
        if sql_in and (stored is None or sql_in != stored.expected_sql):
            problem = _oracle_problem(str(sql_in))
            if problem is not None:
                # An error under blue/green — the whole apply aborts; fix the
                # case and re-apply (a bad oracle must never gate silently).
                result.errors.append(f"eval case '{question}' rejected: {problem}")
                continue
        # expect/term are real columns since migration 0031; expected_answer is
        # back to being the prose oracle its name promises. `term` without
        # `expect` was warned about above as ignored — so drop it here too.
        expect_in = str(expect) if expect is not None else None
        term_in = case.get("term") if expect is not None else None
        answer_in = case.get("expected_answer")
        tags_in = [str(t) for t in case.get("tags") or []]
        if stored is None:
            eval_store.create_case(
                session,
                name,
                question,
                str(case.get("source", "authored")),
                expected_sql=case.get("expected_sql"),
                expected_answer=answer_in,
                expect=expect_in,
                term=term_in,
                status=str(case.get("status", "approved")),
                created_by="apply",
                tags=tags_in,
            )
            created += 1
        elif (
            case.get("expected_sql"),
            answer_in,
            expect_in,
            term_in,
            tags_in,
            str(case.get("status", "approved")),
        ) != (
            stored.expected_sql,
            stored.expected_answer,
            stored.expect,
            stored.term,
            stored.tags,
            stored.status,
        ):
            # tags AND status ride the change tuple: a single-field edit must
            # count as `updated`, never the silent no-op class. Omitting status
            # here would leave promoting candidate->approved in cases.yaml a
            # silent no-op, so the eval gate could never be armed from the
            # files. Files win; promotion is a reviewable git diff.
            eval_store.update_case(
                session,
                stored.id,
                expected_sql=case.get("expected_sql"),
                expected_answer=answer_in,
                expect=expect_in,
                term=term_in,
                tags=tags_in,
                status=str(case.get("status", "approved")),
            )
            updated += 1
        else:
            unchanged += 1
    if value_shaped:
        result.warnings.append(
            f"{value_shaped} value case(s) in evals/cases.yaml — value cases no "
            "longer gate: certified answers are the regression suite; run "
            "`dst evals migrate` to convert them"
        )
    result.applied.append(
        f"eval cases: created {created}, updated {updated}, unchanged {unchanged}"
    )


def _eval_case_errors(
    case: dict[str, Any], model: SemanticModel, stored_sql: dict[str, str | None]
) -> list[str]:
    """The eval-case gates that need nothing on the wire, in ONE place.

    The two mutually exclusive case shapes, the ``expect`` vocabulary, and
    expected_sql PARSING in the lens dialect (only for a new or edited oracle —
    apply gates the same subset, and being stricter here would invent a plan
    rejection apply does not make). The half left behind is EXECUTING the oracle
    against the warehouse, which a dry run cannot do — see ``PLAN_UNCHECKED``."""
    question = str(case.get("question", "")).strip()
    expect = case.get("expect")
    if expect is not None and case.get("expected_sql"):
        # A case is one shape or the other, never both: a behavioral pin
        # asserts the response SHAPE, a value oracle belongs in certified.
        return [
            f"eval case '{question}' rejected: 'expect' and 'expected_sql' are "
            "mutually exclusive — a behavioral case asserts response shape; a "
            "value case belongs in certified_answers.yaml (`dst evals migrate`)"
        ]
    if expect is not None and expect not in ("clarify", "refuse", "answer"):
        # 'answer' is the must-answer pin — this gate must accept it,
        # not just the two refusal shapes.
        return [
            f"eval case '{question}' rejected: expect must be 'clarify', "
            f"'refuse', or 'answer', got '{expect}'"
        ]
    sql_in = case.get("expected_sql")
    if sql_in and sql_in != stored_sql.get(question.lower()):
        leaf_to_table = {
            e.source.table.split(".")[-1].lower(): e.source.table for e in model.entities
        }
        try:
            rewrite_to_sources(str(sql_in), model.dialect, leaf_to_table)
        except Exception as exc:  # noqa: BLE001 — any parse failure is the same gate
            return [
                f"eval case '{question}' rejected: expected_sql does not parse as "
                f"{model.dialect}: {exc}"
            ]
    return []


def _probe_connector(
    session: Session,
    config: LensConfig,
    org_id: uuid.UUID | str,
    result: LensApplyResult,
    *,
    label: str = "--probe-certified",
) -> Connector | None:
    """The lens's warehouse connector for probing (first connection with a SQL
    dialect — _lens_dialect's rule; compile already guaranteed one). Shared by
    --probe-certified and the eval-case oracle gate (*label* names the caller
    in the warning). Failure to build one is a warning, never a gate: probing
    is advisory. Every call on the connector, and the open itself, runs under
    the apply deadline (`apply_bounded`) — that one is a gate."""
    from services.lenses.connections import connector_for_record

    for cname in config.connections:
        record = connection_store.get_connection(session, cname)
        if record is None:
            continue
        try:
            dialect_for(record.type)
        except CompileError:
            continue
        try:
            # The reads stay on THIS session and transaction (the connection may
            # be staged by this apply and not yet committed); only the warehouse
            # open runs under the deadline, on its own thread.
            secret = connection_store.get_secret(session, cname)
            connector = apply_bounded(
                f"opening connection '{cname}'", partial(connector_for_record, record, secret)
            )
            # The one-time cost of a warehouse this process has not opened yet
            # (an unchanged connection skipped its probe) is not a step's.
            apply_first_open(cname, connector)
        except ApplyStepTimeout:
            raise  # the deadline is the apply's verdict, never an advisory warning
        except Exception as exc:  # noqa: BLE001 — a dead credential must not sink the apply
            result.warnings.append(
                _degraded(f"{label}: connector '{cname}' unavailable ({exc}) — stored unprobed")
            )
            return None
        return _BoundedConnector(connector, f"a query on connection '{cname}'")
    result.warnings.append(
        _degraded(f"{label}: no warehouse connection to probe through — stored unprobed")
    )
    return None


def _has_active_certified(session: Session, name: str) -> bool:
    return any(
        certify_store.is_active(a.status) for a in certify_store.list_for_lens(session, name)
    )


def _starvation_errors(session: Session, name: str, source: LensSource) -> list[str]:
    """The dry run's reading of the gate-starvation abort: would this push leave
    ``eval_gate: block`` with no active certified answer to test?

    Apply MEASURES the ≥1→0 transition around its own staged upserts; a plan
    cannot stage, so it replays the same three rules the upsert applies — an
    explicit ``status:`` is the new status, an absent one keeps the stored intent,
    and a file-originated row the file no longer carries is deleted (review-origin
    rows survive). Empty when the push cannot starve anything."""
    answers = source.certified_answers
    if not eval_service.blocks(source.config.eval_gate) or answers is None:
        return []
    stored = certify_store.list_for_lens(session, name)
    if not any(certify_store.is_active(a.status) for a in stored):
        return []  # the 0→0 case: never a transition
    authored = {q: a for a in answers if (q := str(a.get("question", "")).strip().lower())}
    survivors = [
        str(entry["status"])
        if (entry := authored.get(row.question.strip().lower())) is not None
        and entry.get("status") is not None
        else certify_store.authored_status(row.status)
        for row in stored
        if row.question.strip().lower() in authored or (row.source or "").startswith("review:")
    ]
    survivors += [
        str(entry.get("status") or "active")
        for q, entry in authored.items()
        if q not in {row.question.strip().lower() for row in stored}
    ]
    if any(certify_store.is_active(s) for s in survivors):
        return []
    return [
        "eval gate starved: this apply retires the last active certified "
        "answer while eval_gate: block — keep at least one active answer, "
        "or set `eval_gate: warn`/`off` in lens.yaml and re-apply"
    ]


def _certified_terms(sql: str, model: SemanticModel) -> list[str]:
    """The metric and definition names and the aggregated columns a certified
    answer's SQL reads, stored on the row for the lexical veto
    (``resolution.governed_terms`` over the sample-bound SQL for a template)."""
    from services.runtime import resolution

    try:
        return resolution.governed_terms(sql, model)
    except Exception:  # noqa: BLE001 — attribution never gates an apply
        return []


def _refresh_samples(
    session: Session,
    source: LensSource,
    result: LensApplyResult,
    model: SemanticModel,
    stored: list[certify_store.CertifiedAnswer],
    *,
    org_id: uuid.UUID | str,
) -> None:
    """`dst apply --refresh-samples`: re-record the sample result of every
    stored template — execute the sample-bound SQL once more (read-only,
    row-capped), store the value it returns as ``verified_value``, and
    re-stamp bindings and terms from the SQL as it renders today. Every
    template whose value moved prints its old -> new line (compared on the
    canonical reading, so a stored form that differs only in key order or
    number/timestamp spelling is not a change) and the applied count says how
    many were re-recorded and how many moved; a failed execution warns and
    leaves the row as it was. This is the
    file-side acceptance route for a template whose sample moved (an
    unslotted entry writes its new ``verified_value`` instead)."""
    done = 0
    moved = 0
    if not stored:
        result.applied.append("--refresh-samples: no stored template to re-record")
        return
    connector = _probe_connector(session, source.config, org_id, result)
    for a in stored:
        rendered, errors = _template_gate_sql(
            {"slots": a.slots, "sample_bindings": a.sample_bindings}, a.question, a.sql, model
        )
        if rendered is None:
            result.warnings.append(
                f"certified answer '{a.question}' sample not re-recorded: " + "; ".join(errors)
            )
            continue
        verified = a.verified_value
        if connector is not None:
            try:
                probed = connector.execute(rendered, read_only=True, row_limit=_MAX_ROWS)
                verified = _value_summary(probed.columns, probed.rows)
            except ApplyStepTimeout:
                raise
            except Exception as exc:  # noqa: BLE001 — advisory, the row stays as it was
                result.warnings.append(
                    _degraded(f"certified answer '{a.question}' sample re-record failed: {exc}")
                )
                continue
        certify_store.update(
            session,
            a.id,
            sql=a.sql,
            verified_value=verified,
            bindings=certified_bindings(rendered, model),
            terms=_certified_terms(rendered, model),
        )
        done += 1
        if canon_summary(a.verified_value) == canon_summary(verified):
            continue
        moved += 1
        # A flag that rewrites the signed value of a certified answer says
        # before and after, per template — the sample binding it ran and the
        # value it replaced, so the diff is in the report, not only in git.
        binding = ", ".join(
            f"{slot}={value}" for slot, value in (a.sample_bindings or [{}])[0].items()
        )
        result.applied.append(
            f"--refresh-samples: '{a.question}' [{binding}]: "
            f"{format_result(a.verified_value) if a.verified_value else 'unrecorded'} -> "
            f"{format_result(verified) if verified else 'unrecorded'}"
        )
    result.applied.append(
        f"--refresh-samples: re-recorded the sample result of {done} of {len(stored)} "
        f"template(s); {moved} moved"
        + ("" if moved else " (the stored form was re-read, no value changed)")
    )


def _probe_noop(pre_existing: int) -> str:
    """--probe-certified probes NEW entries only; saying so when zero qualify
    kills the ordering trap (answers landed by an earlier apply leave nothing
    to probe, and a silent flag reads as a clean sweep)."""
    return (
        f"--probe-certified: probed 0 new certified answers — {pre_existing} "
        "pre-existing; the probe covers new entries only — re-probe by "
        "re-authoring sql, or run `dst test` for the full sweep"
    )


def _certify_prose(
    config: LensConfig,
    model: SemanticModel,
    question: str,
    sql: str,
    probed: QueryResult,
) -> str | None:
    """The certified answer's English, composed ONCE from the result the
    probe just executed — stored as ``verified_prose`` and served VERBATIM on
    every certified match after that (no per-request composer call, no badge
    wobble, no recompose latency). Best-effort by doctrine: no servable model
    (keyless test env), a compose failure, or prose whose figures do not ground
    in the probed result all degrade to None — the answer stores prose-less and
    serves exactly as before. Never a gate, never sinks the apply."""
    from services.contracts.protocols import GeneratedQuery
    from services.llm import registry
    from services.runtime import faithfulness, verification
    from services.runtime.answer import AnswerComposer

    pair = registry.resolve(config.model.model_ref())
    if pair is None:
        return None
    try:
        ans = AnswerComposer(pair.llm, model=pair.name).compose(
            question=question,
            generated=GeneratedQuery(sql=sql),
            result=probed,
            semantic_model=model,
            prose_context=[],
            max_rows=_PROSE_ROWS,
        )
        # The same deterministic passes the serve path runs after composing:
        # reconcile rewrites rounded restatements back to the rows' own values,
        # and the grounding check refuses to FREEZE prose stating figures the
        # result does not hold (e.g. a row cell promoted to a company
        # total under `verified` — ungrounded prose must never become the
        # verbatim serve).
        prose = faithfulness.reconcile(
            ans.text,
            probed,
            question_text=question,
            sql_text=sql,
            dialect=model.dialect,
            clock_years=verification.clock_years(model.timezone),
        )
        status, _reason = faithfulness.numeric_check(
            prose,
            probed,
            question_text=question,
            sql_text=sql,
            dialect=model.dialect,
            clock_years=verification.clock_years(model.timezone),
        )
    except Exception:  # noqa: BLE001 — prose is advisory; the apply must not sink
        log.exception("certify-time prose compose failed for %r", question)
        return None
    return None if status == "fail" else prose


def _certify_self_test(
    session: Session,
    name: str,
    source: LensSource,
    result: LensApplyResult,
    model: SemanticModel,
    landed: list[str],
    *,
    org_id: uuid.UUID | str,
) -> int:
    """Certifying unit-tests itself in the same apply: the answers this push
    CREATED or re-AUTHORED (SQL/slots/samples — not provenance- or status-only
    edits, the binding lifecycle's edit classes) run through the certified
    suite against the candidate model, generation (matching disabled by
    construction) vs the just-stored oracle. A divergence is an ALERT, never a
    block — certify-to-override is a primary use case, so the warning names
    both executed results and the override reading instead of aborting.

    A SAMPLE, NOT A PROOF, and it says so now. Generation is stochastic: it runs
    each case ONCE, so agreement is evidence that the oracle reproduces, not a
    verification that it always will. Measured — the self-test agreed on a case
    at 01:32 that `dst test` failed at 02:31 (generation returned the 0-1
    fraction where the certified oracle returns the percentage; same question,
    bimodal generator). Claiming more than one sample establishes is how the
    `run dst test` nudge started reading like paperwork.

    UNCONDITIONAL: applying a certification unit-tests it and
    alerts, period — eval_gate governs the PUBLISH gate, never this. The only
    skips are generation being unavailable — no smart-tier model (loud warn,
    the gate's own degradation), no connector — the suite failing to run, or the
    self-test's wall-clock budget running out; returns how many landed answers
    stayed UNTESTED so the caller keeps the sweep nudge for exactly those.

    BOUNDED (``DST_CERTIFY_SELFTEST_BUDGET_S``, default 120s — read it in ANSWERS:
    one case is one full generation, ~30s, so the default buys 4 self-tests of a
    7-answer push). Every case here is a full generation, and apply is a sync
    handler holding the org's apply advisory lock and one Postgres transaction
    for its whole duration. Unbounded, a handful of answers can run past the
    client's own timeout while the backend sits `idle in transaction` at 0% CPU
    — indistinguishable from a deadlock: the CLI gives up, the lock stays held,
    and every later apply gets 409 until that backend is terminated (which rolls
    the whole apply back, so nothing lands). The budget stops the sweep between
    cases; the rest land untested, loudly."""
    stored = {a.question.strip().lower(): a for a in certify_store.list_for_lens(session, name)}
    to_test = [
        answer
        for q in landed
        if (answer := stored.get(q.strip().lower())) is not None
        and certify_store.is_active(answer.status)
    ]
    if not to_test:
        return 0
    from services.llm import registry

    # The lens's OWN ref, and nothing else. This used to fall back to
    # `registry.resolve(registry.tier("smart"))` for the client while
    # `select_generators` took the NAME from the lens config — so a lens pinned
    # to `deepseek/deepseek-v4-pro`, applied on an Anthropic-only server, sent a
    # real request to Anthropic naming a DeepSeek model. The operator's question
    # (and whatever rides in that prompt) went to a vendor they did not choose
    # and are not paying, and the 404 that came back talked about a model name
    # instead of a missing provider. The fallback only ever fired when the lens
    # PINNED something unservable: an unpinned lens has an EMPTY ref, which
    # already resolves through `default_ref()` (smart tier, else fast).
    ref = source.config.model.model_ref()
    pair = registry.resolve(ref)
    if pair is None:
        result.warnings.append(
            _degraded(
                "certify self-test SKIPPED: this lens's model cannot be served here: "
                f"{registry.unservable_detail(ref)}"
            )
        )
        return len(to_test)
    connector = _probe_connector(session, source.config, org_id, result, label="certify self-test")
    if connector is None:
        return len(to_test)
    from services.evals.certified_suite import run_certified_suite
    from services.runtime import assembly
    from services.runtime.answer import AnswerComposer

    # The client and its wire NAME travel together, as one value — the harness
    # cannot be handed one without the other (services/llm/registry.py).
    bundle = store.LensBundle(config=source.config, semantic_model=model)
    budget = settings.certify_selftest_budget_s
    try:
        assemble_for, generators_for = assembly.eval_harness(bundle, org_id, pair)
        outcome = run_certified_suite(
            connector=connector,
            lens=name,
            answers=to_test,
            assemble_for=assemble_for,
            generators_for=generators_for,
            composer=AnswerComposer(pair.llm, model=pair.name),
            model_name=pair.name,
            deadline=time.monotonic() + budget if budget > 0 else None,
        )
    except ApplyStepTimeout:
        raise  # the warehouse stopped answering: the apply's verdict, not the self-test's
    except Exception as exc:  # noqa: BLE001 — the self-test must never sink an apply
        result.warnings.append(_degraded(f"certify self-test failed to run: {exc}"))
        return len(to_test)
    # The suite only ever returns fewer results than it was given by stopping on
    # the deadline (every answer in `to_test` is active), so this is the budget
    # and nothing else. Say WHY: the caller's nudge names the action, this names
    # the cause and the dial.
    untested = len(to_test) - len(outcome.results)
    if untested:
        result.warnings.append(
            _degraded(
                f"certify self-test stopped after {len(outcome.results)} of "
                f"{len(to_test)} answer(s): the {budget:.0f}s self-test budget "
                "(DST_CERTIFY_SELFTEST_BUDGET_S) ran out — apply holds this org's apply "
                "lock for its whole duration, so the sweep is bounded here. One case "
                "is one full generation (~30s measured), so read that budget in "
                "ANSWERS, not seconds, when raising it"
            )
        )
    # Say what the self-test RAN, and what one run of a stochastic generator
    # actually establishes: a case that agrees here can diverge on the very next
    # `dst test`, so the output must never claim more than a single sample.
    if outcome.results:
        result.applied.append(
            f"certify self-test: {len(outcome.results)} of {len(to_test)} landed "
            "answer(s) sampled ONCE against generation — agreement is evidence the "
            f"oracle reproduces, not proof that it always will (`dst test {name}` "
            "is the sweep)"
        )
    for r in outcome.results:
        if r.passed:
            continue
        why = f" ({r.reason})" if r.reason else ""
        result.warnings.append(
            f"certified '{r.question}' diverged at certification: certified SQL → "
            f"{format_result(r.oracle_result)}, generated → "
            f"{format_result(r.generated_result)}{why} — divergence at certification "
            "time can be the point — the certified answer overrides generation; "
            "re-check the oracle if that's not what you meant"
        )
    return untested


def _provenance_edited(answer: dict[str, Any], stored: certify_store.CertifiedAnswer) -> bool:
    """True when the file EXPLICITLY sets source/verified_by to a new value.
    An absent key is not an edit — a hand-written file without provenance must
    never erase the stamped values (store.update COALESCEs the same way)."""
    return (answer.get("source") is not None and str(answer["source"]) != stored.source) or (
        answer.get("verified_by") is not None and str(answer["verified_by"]) != stored.verified_by
    )


def _template_gate_sql(
    answer: dict[str, Any], question: str, sql: str, model: SemanticModel
) -> tuple[str | None, list[str]]:
    """For a template entry, validate slots/sample_bindings and return
    the first-sample-bound RENDERED sql — what the parse/shape/boundary gates,
    the probe, and the asset bindings all run against (template SQL with
    ``{placeholders}`` cannot parse). (sql, []) for a plain pair."""
    slots_raw = answer.get("slots")
    samples_raw = answer.get("sample_bindings")
    if slots_raw is None and samples_raw is None:
        return sql, []
    errors = certify_binding.validate_template(question, sql, slots_raw, samples_raw)
    if errors:
        return None, errors
    specs, _ = certify_binding.parse_slots(slots_raw)
    canonical, _ = certify_binding.validate_binding(specs, samples_raw[0])  # type: ignore[index]
    return certify_binding.render_sql(sql, specs, canonical, model.dialect), []


def _certified_entry_errors(answer: dict[str, Any], model: SemanticModel) -> list[str]:
    """The gates every incoming certified answer must pass, in ONE place.

    Status enum, template slots/sample_bindings, the SQL parsing in the lens
    dialect, the shape guard (single SELECT, no bare star, reserved schema) and
    the lens-boundary guard, reported the way apply reports them. Pure — nothing
    here touches the warehouse — so ``dst plan`` runs the identical function
    and cannot promise an apply these would abort."""
    question = str(answer.get("question", "")).strip()
    status_in = answer.get("status")  # absent = not an edit (provenance doctrine)
    if status_in is not None and str(status_in) not in ("active", "retired"):
        # A typo'd status must not silently default to active — that would
        # un-retire an answer nobody meant to resurrect.
        return [
            f"certified answer '{question}' rejected: status must be "
            f"'active' or 'retired', not {str(status_in)!r}"
        ]
    # A template validates its slots/sample_bindings first, and every downstream
    # gate runs on the first-sample-bound RENDERED sql.
    gated, template_errors = _template_gate_sql(answer, question, str(answer.get("sql", "")), model)
    if gated is None:
        return [f"certified answer '{question}' rejected: " + "; ".join(template_errors)]
    try:
        foreign = foreign_tables(gated, model)
    except Exception as exc:  # noqa: BLE001 — any parse failure is the same gate
        return [
            f"certified answer '{question}' rejected: SQL does not parse as {model.dialect}: {exc}"
        ]
    # Shape gate and boundary gate report TOGETHER — "SELECT * is not allowed"
    # otherwise masks the real foreign-table rejection, and the author fixes
    # syntax only to hit the governance wall on the next apply.
    shape = sql_guard.check(gated, model, trust_tables=True)
    reasons: list[str] = []
    if not shape.ok:
        reasons.append(str(shape.reason))
    if foreign:
        named = ", ".join(f"'{t}'" for t in foreign)
        reasons.append(
            f"references {named} — not in this lens's model; a certified answer "
            f"must not smuggle ungoverned tables past the lens boundary"
        )
    if reasons:
        return [f"certified answer '{question}' rejected: " + "; also: ".join(reasons)]
    return []


def _certified_definition_warnings(
    answers: Sequence[Mapping[str, Any]], model: SemanticModel
) -> list[str]:
    """``certified_ignores_definition``: the serve-time period check
    (verification.ignored_definitions), asked of every certified answer at
    plan and apply over its gate-rendered SQL, so a certified answer that
    reads past a period rule is named with the rule before it serves below
    `verified`. Pure — the same function plan runs."""
    from services.runtime import verification

    out: list[str] = []
    for answer in answers:
        question = str(answer.get("question", "")).strip()
        if not question or str(answer.get("status") or "active") != "active":
            continue
        gated, _errors = _template_gate_sql(
            dict(answer), question, str(answer.get("sql", "")), model
        )
        if gated is None:
            continue
        for d, why in verification.ignored_definitions(gated, model, question):
            out.append(
                f"certified_ignores_definition: certified answer '{question}' {why} "
                f"(definition '{d.term}'"
                f"{f', period {d.period}' if d.period else ''}) — it serves below "
                "verified, naming the definition, until the SQL applies the rule or "
                "the answer is retired"
            )
    return out


def _sql_text(sql: object) -> str:
    return " ".join(str(sql or "").split()).lower()


def _cross_match_errors(
    session: Session,
    name: str,
    source: LensSource,
    model: SemanticModel,
    warnings_out: list[str] | None = None,
) -> list[str]:
    """Two certified answers that would answer each other's question.

    Serving matches by meaning, so a corpus holding two questions inside the
    paraphrase band with DIFFERENT SQL is a corpus where one answer serves the
    other's question as ``certified``. That is a static inconsistency, so it
    dies here, at plan and apply alike: every incoming question's anchor is
    embedded once and compared against the other incoming anchors and against
    the stored corpus (rows the file does not carry). Same SQL is one answer
    under two phrasings, which is fine.

    The score alone is not the verdict — real corpora hold legitimate pairs
    above the band ("MRR today" / "ARR today"), and serving already separates
    those by the governed names each question carries (the lexical veto). So a
    pair above the equivalence band whose SQL differs is an ERROR only when the
    two questions name the same governed names (metrics, definitions, entities)
    with a metric or definition among them (or none, in the exact band) AND,
    once stopwords and those shared names are removed, neither question says
    anything the other does not — no grain word, no shape word, no dimension,
    no value, nothing. "MRR level" and "MRR growth" share a metric and differ
    in a word, so they are a WARNING in ``warnings_out``, as is any pair that
    names different governed terms or sits below the exact band without one.
    A stored row the file no longer carries is deleted by this apply (files
    win), so it is not compared; a stored row under a question the file
    carries is the same answer, not a pair. Without an embedder nothing can be
    measured and nothing is reported (the unembedded-corpus warning already
    says matching is off)."""
    entries = [
        a
        for a in source.certified_answers or []
        if str(a.get("question", "")).strip()
        and str(a.get("status") or "active") != "retired"
        and not _certified_entry_errors(a, model)
    ]
    if not entries:
        return []
    try:
        embedder = _embedder()
    except Exception:  # noqa: BLE001 — a broken embedder is reported where rows store
        return []
    if embedder is None:
        return []
    from services.certify.lexical import (
        distinguishing_tokens,
        governed_names,
        measure_names,
        name_aliases,
        names_in,
    )
    from services.runtime.assembly import certified_bands

    anchors = [_anchor_question(a) for a in entries]
    try:
        vectors = [list(v) for v in embedder.embed(anchors)]
    except Exception:  # noqa: BLE001 — same: the store path names a failing embedder
        return []
    bands = certified_bands()
    names = governed_names(model)
    measures = measure_names(model)
    aliases = name_aliases(model)
    incoming = {str(a.get("question", "")).strip().lower() for a in entries}
    seen: set[frozenset[str]] = set()
    errors: list[str] = []

    def _report(  # noqa: PLR0913
        q1: str, a1: str, sql1: object, q2: str, a2: str, sql2: object, score: float
    ) -> None:
        pair = frozenset({q1.lower(), q2.lower()})
        if pair in seen or len(pair) < 2 or _sql_text(sql1) == _sql_text(sql2):
            return
        seen.add(pair)
        n1, n2 = names_in(a1, names, aliases), names_in(a2, names, aliases)
        only1, only2 = distinguishing_tokens(a1, a2, n1 & n2)
        if n1 == n2 and (n1 & measures or score >= bands.exact) and not (only1 or only2):
            errors.append(
                f"certified answers '{q1}' and '{q2}' would answer each other's question "
                f"(similarity {score:.2f}) with different SQL — one would serve the other's "
                "question as certified; reword one so the questions differ, or merge them"
            )
        elif warnings_out is not None:
            if n1 != n2:
                named = (
                    f"they name different governed terms ({_names_list(n1)} vs "
                    f"{_names_list(n2)}), which keeps them apart at serve time"
                )
            elif only1 or only2:
                named = (
                    f"they share {_names_list(n1)} and differ in wording "
                    f"({_names_list(only1)} vs {_names_list(only2)}), which keeps them "
                    "apart at serve time"
                )
            else:
                named = "no metric or definition tells them apart, only the score"
            warnings_out.append(
                f"certified answers '{q1}' and '{q2}' sit inside the paraphrase band "
                f"(similarity {score:.2f}) with different SQL; {named} — reword one if "
                "the questions should read further apart"
            )

    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    for i, a in enumerate(entries):
        for j in range(i + 1, len(entries)):
            score = _cosine(vectors[i], vectors[j])
            if score >= bands.equiv:
                b = entries[j]
                _report(
                    str(a["question"]).strip(),
                    anchors[i],
                    a.get("sql"),
                    str(b["question"]).strip(),
                    anchors[j],
                    b.get("sql"),
                    score,
                )
        try:
            hits = certify_store.search(session, name, vectors[i], k=5)
        except Exception:  # noqa: BLE001 — a corpus embedded by another model cannot be compared
            break
        for h in hits:
            if h.answer.question.strip().lower() in incoming or h.score < bands.equiv:
                continue
            if not (h.answer.source or "").startswith("review:"):
                continue  # file-originated and absent from the file: deleted by this apply
            stored_anchor = _anchor_question(
                {
                    "question": h.answer.question,
                    "slots": h.answer.slots,
                    "sample_bindings": h.answer.sample_bindings,
                }
            )
            _report(
                str(a["question"]).strip(),
                anchors[i],
                a.get("sql"),
                h.answer.question,
                stored_anchor,
                h.answer.sql,
                h.score,
            )
    return errors


def _binding_range_warnings(source: LensSource) -> list[str]:
    """Explicit date ranges that read as exclusive ends — a plan WARNING on
    every template whose sample binding ends on the first of a month or spans
    a month or year plus one day. Inclusive ranges are the contract; this says
    where a stored binding most likely did not mean it."""
    out: list[str] = []
    for answer in source.certified_answers or []:
        question = str(answer.get("question", "")).strip()
        slots = answer.get("slots")
        samples = answer.get("sample_bindings")
        if not question or not isinstance(slots, dict) or not isinstance(samples, list):
            continue
        ranged = {
            str(name)
            for name, spec in slots.items()
            if (spec.get("type") if isinstance(spec, dict) else spec) == "date_range"
        }
        for binding in samples:
            if not isinstance(binding, dict):
                continue
            for name in sorted(ranged & {str(k) for k in binding}):
                value = binding[name]
                raw = value.isoformat() if isinstance(value, date) else str(value)
                if note := certify_binding.explicit_range_note(raw):
                    out.append(f"certified answer '{question}', slot '{name}': {note}")
    return out


def _names_list(names: frozenset[str]) -> str:
    return ", ".join(sorted(names)) if names else "none"


def _anchor_question(answer: dict[str, Any]) -> str:
    """A template's match anchor is its first sample-bound
    question — an ordinary concrete question, embedded like every other."""
    question = str(answer.get("question", "")).strip()
    samples = answer.get("sample_bindings")
    if answer.get("slots") and isinstance(samples, list) and samples:
        return certify_binding.render_question(question, samples[0])
    return question


def _gate_dialect_pins(
    session: Session,
    source: LensSource,
    result: LensApplyResult,
    model: SemanticModel,
    existing: dict[str, certify_store.CertifiedAnswer],
    *,
    org_id: uuid.UUID | str,
    probe: bool,
) -> None:
    """Certified SQL is dialect-bound text. A row's ``verified_dialect`` names
    the warehouse dialect that actually executed it (stamped by the probe);
    when the lens now compiles to a different dialect — the deploy-contract
    move: same files, new connection — that verification vouches for nothing.

    Enforce at the earliest deterministic point: with
    ``--probe-certified`` each pinned row re-executes on the new connection and
    a pass re-stamps the pin (re-verification is the clearing act); without it,
    or on a failed re-probe, the apply errors BY NAME and (blue/green) nothing
    lands. Unpinned rows (never executed here) stay advisory, exactly as
    before. Retired rows are never served, so they are never gated; rows this
    apply is about to delete are not gated either."""
    kept = {
        q
        for a in source.certified_answers or []
        if (q := str(a.get("question", "")).strip().lower())
    }
    pinned = [
        a
        for key, a in existing.items()
        if a.verified_dialect
        and a.verified_dialect != model.dialect
        and a.status != "retired"
        and (key in kept or (a.source or "").startswith("review:"))
    ]
    if not pinned:
        return
    connector = _probe_connector(session, source.config, org_id, result) if probe else None
    for stored in pinned:
        if connector is not None:
            gated, gate_errors = _template_gate_sql(
                {"slots": stored.slots, "sample_bindings": stored.sample_bindings},
                stored.question,
                stored.sql,
                model,
            )
            try:
                if gated is None:
                    raise ValueError("; ".join(gate_errors) or "template did not render")
                probed = connector.execute(gated, read_only=True, row_limit=_MAX_ROWS)
            except ApplyStepTimeout:
                raise  # the deadline is the apply's verdict, not this answer's
            except Exception as exc:  # noqa: BLE001 — a failed re-verify must gate
                result.errors.append(
                    f"certified answer '{stored.question}' was verified on "
                    f"{stored.verified_dialect} and its re-probe on {model.dialect} "
                    f"failed: {exc}"
                )
                continue
            certify_store.update(
                session,
                stored.id,
                sql=stored.sql,
                verified_value=_value_summary(probed.columns, probed.rows),
                verified_dialect=model.dialect,
            )
            result.warnings.append(
                f"certified answer '{stored.question}' re-verified on {model.dialect} "
                f"(was {stored.verified_dialect})"
            )
            continue
        result.errors.append(
            f"certified answer '{stored.question}' was verified on "
            f"{stored.verified_dialect}; this lens now compiles to {model.dialect} — "
            "re-apply with --probe-certified to re-verify it on the new connection, "
            "or retire the answer"
        )


def _park_hint(failing: list[str]) -> str:
    """How to park a red case, in the status its file accepts: a certified
    answer (``certified:<id>``) is retired in certified_answers.yaml —
    ``status: candidate`` is an eval case's status, and apply rejects it on
    a certified entry — while a behavioral case is parked as a candidate in
    evals/cases.yaml."""
    certified = any(c.startswith("certified:") for c in failing)
    behavioral = any(not c.startswith("certified:") for c in failing)
    parts = []
    if certified:
        parts.append("retire red certified answers (status: retired in certified_answers.yaml)")
    if behavioral:
        parts.append("park red cases as status: candidate in evals/cases.yaml")
    return " and ".join(parts) or "park red cases"


def _apply_certified_answers(
    session: Session,
    name: str,
    source: LensSource,
    result: LensApplyResult,
    model: SemanticModel,
    *,
    org_id: uuid.UUID | str = "",
    probe: bool = False,
    refresh_samples: bool = False,
) -> None:
    """Upsert certified_answers.yaml keyed by question: unseen questions embed +
    insert (skipped with a warning when no embedder key, as before), an edited
    sql/verified_value/source/verified_by updates in place — no re-embed needed,
    the embedding is of the unchanged question, so edits land even keyless.
    Files win on deletion too: a pushed certified_answers.yaml
    OWNS its file-originated rows, so a stored answer absent from it deletes —
    loudly, in the applied count. Review-approved answers (source review:*) are
    server-origin and survive file absence; a tree with NO certified_answers.yaml
    (certified_answers is None) leaves the whole surface untouched.

    Two gates run per incoming answer, both errors naming the answer (and, blue/
    green, any error aborts the whole apply): the SQL must parse in the lens
    dialect, and its source tables must all be modeled by the lens — a certified
    answer must not smuggle ungoverned tables past the lens boundary. ``probe``
    (opt-in: `dst apply --probe-certified`) additionally executes each NEW
    answer once, read-only + row-capped, recording verified_value; a probe
    failure is a warning and the answer stores anyway — verification is
    advisory, the gates are not.

    Bindings (the derived staleness signal) recompute on create and update — an
    edit IS the re-verify act, so its bindings snapshot the model it was verified
    against. Unchanged answers keep their stored bindings (a recompile must not
    silently clear a re-verify flag); rows with none yet (pre-CB) backfill."""
    if source.certified_answers is None:
        # No file — the corpus is untouched, but review-origin rows still
        # SERVE, so their dialect pins still gate a connection swap.
        untouched = {
            a.question.strip().lower(): a for a in certify_store.list_for_lens(session, name)
        }
        _gate_dialect_pins(session, source, result, model, untouched, org_id=org_id, probe=probe)
        if probe and untouched:
            # A lens with no certified_answers.yaml and nothing stored has
            # nothing to probe — saying "probed 0" there reads as the probe
            # having skipped the lens that DID carry answers.
            result.warnings.append(_probe_noop(len(untouched)))
        return
    existing = {a.question.strip().lower(): a for a in certify_store.list_for_lens(session, name)}
    _gate_dialect_pins(session, source, result, model, existing, org_id=org_id, probe=probe)
    fresh: list[dict[str, Any]] = []
    changed: list[tuple[certify_store.CertifiedAnswer, dict[str, Any]]] = []
    refreshed: list[certify_store.CertifiedAnswer] = []
    unchanged = 0
    result.errors.extend(_cross_match_errors(session, name, source, model, result.warnings))
    result.warnings.extend(_binding_range_warnings(source))
    result.warnings.extend(_certified_definition_warnings(source.certified_answers or [], model))
    for answer in source.certified_answers:
        question = str(answer.get("question", "")).strip()
        if not question:
            continue
        status_in = answer.get("status")  # absent = not an edit (provenance doctrine)
        if entry_errors := _certified_entry_errors(answer, model):
            result.errors.extend(entry_errors)
            continue
        stored = existing.get(question.lower())
        if stored is None:
            fresh.append(answer)
        elif (
            str(answer.get("sql", "")) != stored.sql
            # A file that omits verified_value is not clearing it — a probe's
            # server-stamped value must survive re-applies of unchanged files.
            or (
                "verified_value" in answer
                and (answer.get("verified_value") or None) != stored.verified_value
            )
            # verified_prose follows the same doctrine: omission keeps the
            # certify-time prose, an explicit value (or explicit null) edits it.
            or (
                "verified_prose" in answer
                and (answer.get("verified_prose") or None) != stored.verified_prose
            )
            or _provenance_edited(answer, stored)
            # Slot/sample edits re-author the template (rendered SQL
            # changes); omitted keys keep the stored ones (provenance doctrine).
            or ("slots" in answer and (answer.get("slots") or None) != stored.slots)
            or (
                "sample_bindings" in answer
                and (answer.get("sample_bindings") or None) != stored.sample_bindings
            )
            # An explicit status is an edit (retire/re-activate); absence keeps
            # the stored one — an old file must never silently un-retire.
            # Compared against the AUTHORED status: `status: active` on a row
            # sitting in pending_embedding is the intent already stored, not an
            # edit — otherwise every apply would re-"update" it.
            or (
                status_in is not None
                and str(status_in) != certify_store.authored_status(stored.status)
            )
        ):
            changed.append((stored, answer))
        else:
            unchanged += 1
            if refresh_samples and stored.slots:
                refreshed.append(stored)
                continue
            if not stored.bindings or stored.terms is None:
                stored_gated, stored_errors = _template_gate_sql(
                    {"slots": stored.slots, "sample_bindings": stored.sample_bindings},
                    stored.question,
                    stored.sql,
                    model,
                )
                if stored_gated is None:
                    result.warnings.append(
                        f"certified answer '{stored.question}' bindings not backfilled: "
                        + "; ".join(stored_errors)
                    )
                else:
                    # Terms backfill beside bindings: a row applied before they
                    # were stored reads its SQL once, no warehouse involved.
                    certify_store.update(
                        session,
                        stored.id,
                        sql=stored.sql,
                        verified_value=stored.verified_value,
                        bindings=certified_bindings(stored_gated, model),
                        terms=_certified_terms(stored_gated, model),
                    )
    # (run-3 shipped a loud "absence never deletes, they KEEP SERVING" orphan
    # warning here; the follow-up decision made files WIN — file-originated
    # entries absent from the push now DELETE below, and review-origin
    # survivors are the server-only idiom, not orphans to warn about.)
    if refresh_samples:
        _refresh_samples(session, source, result, model, refreshed, org_id=org_id)
    reauthored: list[str] = []
    for stored, answer in changed:
        new_sql = str(answer.get("sql", ""))
        value_edited = (
            "verified_value" in answer
            and (answer.get("verified_value") or None) != stored.verified_value
        )
        # Gates already passed (this loop only sees gated entries) —
        # re-render for bindings/probe surfaces. Slot/sample edits count as a
        # SQL-class edit: the RENDERED sql they verify against changed.
        gated_new, _ = _template_gate_sql(answer, stored.question, new_sql, model)
        effective_sql = gated_new or new_sql
        slots_edited = "slots" in answer and (answer.get("slots") or None) != stored.slots
        samples_edited = (
            "sample_bindings" in answer
            and (answer.get("sample_bindings") or None) != stored.sample_bindings
        )
        sql_edited = new_sql != stored.sql or slots_edited or samples_edited
        if sql_edited:
            # A SQL edit re-AUTHORS the answer: membership computed fresh
            # against the model it was just written against.
            bindings = certified_bindings(effective_sql, model)
            reauthored.append(stored.question)
        elif _provenance_edited(answer, stored) or value_edited:
            # A provenance/value bump re-VERIFIES an unmoved SQL: refresh the
            # hashes, never recompute membership (a definition whose expr moved
            # away no longer canonically embeds — recomputing would drop it).
            bindings = restamp_bindings(effective_sql, stored.bindings, model)
        else:
            # Status-only (retire/reactivate) verifies NOTHING: freeze the
            # bindings (None COALESCEs to stored). The confirmation run proved
            # a same-push retire + definition change silently severed the
            # definition key, leaving a later reactivation blind to it.
            bindings = None
        # A changed FIRST sample binding moves the match anchor (the
        # vector is of the sample-bound question) — re-embed when possible,
        # warn (and keep the old anchor) when not.
        anchor_vec: list[float] | None = None
        if samples_edited:
            new_samples = answer.get("sample_bindings") or []
            old_first = stored.sample_bindings[0] if stored.sample_bindings else None
            if new_samples and new_samples[0] != old_first:
                embedder = _embedder()
                if embedder is None:
                    result.warnings.append(
                        f"certified answer '{stored.question}': anchor binding changed "
                        "but no embedding provider — old match anchor kept until "
                        "`dst reindex`"
                    )
                else:
                    try:
                        embedding_meta.guard_write(session, embedder)
                        anchor_vec = embedder.embed(
                            [_anchor_question({**answer, "question": stored.question})]
                        )[0]
                    except Exception as exc:  # noqa: BLE001 — mismatch OR a down
                        # provider: the old anchor stands either way, and a
                        # provider failure must not sink the whole apply.
                        result.warnings.append(
                            f"certified answer '{stored.question}': anchor not re-embedded: {exc}"
                        )
        # verified_prose: the file's explicit value wins (explicit null clears);
        # otherwise a SQL-class edit CLEARS it — the stored prose was composed
        # from the OLD SQL's result and must never serve over the new one
        # (probe covers new entries only, so no recompose here; the serve path
        # falls back to composing until a re-certify) — and any other edit
        # keeps it.
        prose_in = str(answer["verified_prose"]) if answer.get("verified_prose") else None
        clear_prose = ("verified_prose" in answer and prose_in is None) or (
            "verified_prose" not in answer and sql_edited
        )
        certify_store.update(
            session,
            stored.id,
            sql=new_sql,
            # A verified value is the value of ONE sql: the file's explicit
            # value wins; otherwise a SQL-class edit clears it (the gate's
            # value lane records the new SQL's value on the next run, or
            # --probe-certified does now) and any other edit keeps it.
            verified_value=(
                answer.get("verified_value")
                if "verified_value" in answer
                else (None if sql_edited else stored.verified_value)
            ),
            source=answer.get("source"),
            verified_by=answer.get("verified_by"),
            bindings=bindings,
            status=str(answer["status"]) if answer.get("status") is not None else None,
            slots=answer.get("slots"),
            sample_bindings=answer.get("sample_bindings"),
            embedding=anchor_vec,
            # A re-authored answer is new SQL that was never executed: the old
            # dialect pin vouches for nothing and must not survive the edit.
            clear_verified_dialect=stored.question in reauthored
            and stored.verified_dialect is not None,
            verified_prose=prose_in,
            clear_verified_prose=clear_prose,
            # Re-authored SQL reads new names; everything else keeps the stored ones.
            terms=_certified_terms(effective_sql, model) if sql_edited else None,
        )
    created = 0
    if fresh:
        # Embedding and probing are orthogonal: keyless installs store
        # UNEMBEDDED + warn (a bulk import must not silently evaporate), but
        # --probe-certified still records verified values — the probe must
        # never end up silently gated behind the embedder.
        embedder = _embedder()
        vectors: list[list[float] | None]
        if embedder is None:
            vectors = [None] * len(fresh)
            result.warnings.append(
                _degraded(
                    f"{len(fresh)} certified answer(s) stored unembedded — no embedding "
                    "provider configured; they will not be served or matched until one is "
                    "added and `dst reindex` runs"
                )
            )
        else:
            try:
                embedding_meta.guard_write(session, embedder)
            except embedding_meta.EmbeddingMismatchError as exc:
                result.warnings.append(
                    _degraded(f"{len(fresh)} certified answer(s) skipped: {exc}")
                )
                fresh, vectors = [], []
            else:
                # Template rows embed their first sample-bound question
                # (the match anchor); plain pairs embed as before.
                try:
                    vectors = list(embedder.embed([_anchor_question(a) for a in fresh]))
                except Exception as exc:  # noqa: BLE001 — a BROKEN embedder
                    # A provider failure must not sink the whole apply — and it
                    # must not land a lying `active` either: the rows go in as
                    # pending_embedding, visibly.
                    vectors = [None] * len(fresh)
                    result.warnings.append(
                        _degraded(
                            f"{len(fresh)} certified answer(s) stored unembedded — the "
                            f"embedding provider failed ({exc}); they will not be served "
                            "or matched until it recovers and `dst reindex` runs"
                        )
                    )
        if fresh:
            connector = _probe_connector(session, source.config, org_id, result) if probe else None
            probed_count = 0
            for answer, vec in zip(fresh, vectors, strict=True):
                question = str(answer["question"]).strip()
                sql = str(answer.get("sql", ""))
                # Gates already passed — re-render for the probe + bindings.
                gated_fresh, _ = _template_gate_sql(answer, question, sql, model)
                exec_sql = gated_fresh or sql
                verified = answer.get("verified_value")
                probed_ok = False
                # A file can carry certify-time prose (round-trip of an export);
                # else the probe composes it once below. Templates never store
                # prose — their serve renders deterministically per binding.
                prose = str(answer["verified_prose"]) if answer.get("verified_prose") else None
                if connector is not None:
                    try:
                        # The whole result (to the prose cap, plus one row to
                        # see the cap bite): the prose is composed over every
                        # row it will describe, and the stored row count lets
                        # a serve tell a result that grew. Past the cap no
                        # prose is stored — a sentence over part of a result
                        # must never serve verbatim over the whole.
                        probed = connector.execute(
                            exec_sql, read_only=True, row_limit=_PROSE_ROWS + 1
                        )
                        whole = len(probed.rows) <= _PROSE_ROWS
                        verified = _value_summary(probed.columns, probed.rows, exact_count=whole)
                        probed_ok = True
                        probed_count += 1
                        if prose is None and not answer.get("slots") and whole:
                            prose = _certify_prose(source.config, model, question, sql, probed)
                    except ApplyStepTimeout:
                        raise  # the deadline is the apply's verdict, not this answer's
                    except Exception as exc:  # noqa: BLE001 — advisory: warn, store anyway
                        result.warnings.append(
                            _degraded(
                                f"certified answer '{question}' probe failed: {exc} "
                                "— stored unverified"
                            )
                        )
                certify_store.create(
                    session,
                    name,
                    question,
                    sql,
                    vec,
                    created_by="apply",
                    verified_value=verified,
                    source=answer.get("source"),
                    verified_by=answer.get("verified_by"),
                    bindings=certified_bindings(exec_sql, model),
                    status=str(answer.get("status") or "active"),
                    slots=answer.get("slots"),
                    sample_bindings=answer.get("sample_bindings"),
                    # The pin: a successful execution vouches for THIS dialect.
                    verified_dialect=model.dialect if probed_ok else None,
                    verified_prose=prose,
                    terms=_certified_terms(exec_sql, model),
                )
            created = len(fresh)
            if connector is not None:
                # Say what the probe DID, not only when it did nothing: the
                # operator reading one lens's row must see its answers executed.
                result.applied.append(
                    f"--probe-certified: probed {probed_count} of {len(fresh)} new "
                    "certified answer(s)"
                )
    if probe and not fresh and existing:
        result.warnings.append(_probe_noop(len(existing)))
    # Files win on deletion: a file-originated stored answer
    # the pushed certified_answers.yaml no longer carries deletes here —
    # apply used to leave it active forever while plan rendered the removal
    # diff on every run (a plan/apply contract break). Review-approved answers
    # (source review:*) are server-origin: file absence is their normal state,
    # never a removal. Rejected entries above still count as carried — their
    # errors abort the apply anyway (blue/green).
    kept_questions = {
        q for a in source.certified_answers if (q := str(a.get("question", "")).strip().lower())
    }
    deleted = 0
    for key, row in existing.items():
        if key in kept_questions or (row.source or "").startswith("review:"):
            continue
        certify_store.delete(session, row.id)
        deleted += 1
    # The gate is binding-SCOPED: it re-tests answers whose assets moved, so a
    # created or re-authored answer (fresh bindings, nothing stale) is never
    # tested by the gate of the apply that lands it. An oracle that can't
    # reproduce — a row-shaped SQL certified for a "how many" question, the
    # classic — would then sail through every green apply silently. The
    # self-test runs exactly those answers NOW; whatever it cannot test keeps
    # the sweep nudge (retired-on-arrival answers are never tested, never
    # nagged).
    landed = [str(a["question"]).strip() for a in fresh] + reauthored
    untested = (
        _certify_self_test(session, name, source, result, model, landed, org_id=org_id)
        if landed
        else 0
    )
    if untested:
        # (run-3 added an eval_gate: off hint here; the self-test is
        # UNCONDITIONAL now, so the only causes left — no smart-tier model, no
        # connector, suite failure — are already loudly named above.)
        result.warnings.append(
            f"{untested} certified answer(s) landed untested — run `dst test {name}`: "
            "the apply gate only re-tests answers whose assets changed, so a new "
            "answer's oracle is unproven until the full sweep runs it"
        )
    if created >= 10:
        # Bulk-import honesty: a corpus landing in one apply shifts router-matching
        # dynamics before anyone has evidence the pairs hold up here.
        result.warnings.append(
            f"{created} certified answers landed — run `dst reviews`/evals before "
            "relying on router matches; embeddings derived now (or at `dst reindex` "
            "if keyless)"
        )
    # The corpus-wide condition, not just this push's rows. The failure mode is
    # answers left unembedded by an EARLIER degraded apply — every apply after
    # it reports success while the corpus can never match.
    if unembedded := certify_store.count_unembedded(session, name):
        result.warnings.append(_degraded(certify_store.UNEMBEDDED_WARNING.format(n=unembedded)))
    deleted_part = f"deleted {deleted}, " if deleted else ""
    result.applied.append(
        f"certified answers: created {created}, updated {len(changed)}, "
        f"{deleted_part}unchanged {unchanged}"
    )
