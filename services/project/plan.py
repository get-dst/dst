"""Terraform-style plan: incoming files vs the DB's rendered state.

Pure and side-effect-free — callers render the DB side (render_lens_repo /
render_semantic_files) and pass both maps. Only managed paths are compared
(README/compiled.yaml/audit are runtime outputs; certified/* pages are
export-only in v1). Anything present in the DB but absent from the files is
untouched — deletion stays an explicit act — except certified answers, which a
pushed certified_answers.yaml owns: file-originated entries absent from it
diff as removals (apply deletes them), review-approved ones (source review:*)
are server-origin and never diff as removals, and a push without the file
leaves the surface undiffed (apply leaves it untouched). ``stale_lenses`` is
the shared-layer staleness signal: published lenses whose compiled provenance
no longer matches the (current + incoming) asset hashes, recompiled on apply.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from services.project.loader import is_managed


@dataclass
class FileDiff:
    path: str
    diff: str


@dataclass
class LensPlan:
    lens: str
    status: Literal["create", "update", "unchanged"]
    diffs: list[FileDiff] = field(default_factory=list)


@dataclass
class SemanticPlan:
    path: str
    status: Literal["create", "update", "unchanged"]
    diff: str = ""


def _canonical_lens_yaml(content: str) -> str:
    """Re-render an incoming lens.yaml canonically (parse → LensConfig → the
    same dump render_lens_repo uses), so scaffold comment blocks and cosmetic
    ordering never produce a phantom, permanently-diffing plan row — the first
    plan a new user runs after their first apply must be clean (probe FAIL-2).
    Unparseable content passes through raw; apply rejects it with the real
    error."""
    import yaml as _yaml

    from services.contracts.lens_config import LensConfig

    try:
        config = LensConfig.model_validate(_yaml.safe_load(content) or {})
        return str(
            _yaml.safe_dump(
                config.model_dump(mode="json", exclude_none=True),
                sort_keys=False,
                allow_unicode=True,
            )
        )
    except Exception:
        return content


# Runtime provenance the server stamps onto certified pairs (render_lens_repo);
# a user never authors these, so a diff must never present them as their edit.
_SERVER_STAMPED = ("created_by", "created_at")

# Defaults the DB render elides (render_lens_repo emits `status` only when
# retired) but the scaffold teaches users to author explicitly — the pair drops
# from BOTH sides so `status: active` never phantom-diffs after it lands.
# A non-default value (`status: retired`) still compares.
# `source: authored` is the server's stamp on hand-authored eval cases —
# without the elision, landing any case leaves plan dirty until the author
# copies the stamp into the file.
_DEFAULT_ELIDED: tuple[tuple[str, object], ...] = (
    ("status", "active"),
    ("source", "authored"),
    ("tags", []),
    # An eval case's stored default status, and the oracle half a behavioral
    # case never carries (rendered as null).
    ("status", "approved"),
    ("expected_sql", None),
    ("expected_answer", None),
)


def _canonical_yaml_list(content: str) -> str:
    """Comment headers and formatting on list files (certified_answers.yaml,
    evals/cases.yaml) are authoring sugar — compare the parsed list, so a
    scaffolded commented-but-empty file never phantom-diffs. Server-stamped
    provenance and render-elided defaults drop from BOTH sides: the DB render
    carries the former and omits the latter, so an authored file showed
    `-created_by: apply` removals and `+status: active` additions the user
    never meant as edits."""
    import yaml as _yaml

    try:
        data = _yaml.safe_load(content)
        if data is None:
            data = []
        if not isinstance(data, list):
            return content
        data = [
            {
                k: v
                for k, v in entry.items()
                if k not in _SERVER_STAMPED and (k, v) not in _DEFAULT_ELIDED
            }
            if isinstance(entry, dict)
            else entry
            for entry in data
        ]
        # Entry ORDER is authoring sugar too: the DB serializes newest-first
        # while files keep author order, so a by-the-book retire would leave
        # an order-only diff forever. Entries key by question — compare
        # them sorted (_dump_list); the file keeps whatever order the author
        # likes. KEY order inside an entry is sugar as well (sort_keys=True).
        return _dump_list(data)
    except Exception:
        return content


def _dump_list(data: list[Any]) -> str:
    """The one canonical dump of a list file (entries sorted by question)."""
    import yaml as _yaml

    class _NoAlias(_yaml.SafeDumper):  # type: ignore[misc]  # yaml ships untyped
        # Anchors/aliases (&id001/*id001) are dump sugar for shared objects —
        # a canonical form must never carry them, or an authored alias (or a
        # round-tripped shared value) phantom-diffs against plain literals.
        def ignore_aliases(self, data: object) -> bool:
            return True

    data.sort(key=lambda e: str(e.get("question", "")) if isinstance(e, dict) else str(e))
    return str(_yaml.dump(data, Dumper=_NoAlias, sort_keys=True, allow_unicode=True))


# Values the server records onto a list entry at apply (a probe's verified
# value and prose, a row count, attribution terms, embeddings, timestamps).
# A file that never carried one is not asking to remove it: the DB side drops
# the key when the pushed entry lacks it, so `dst plan` right after `dst apply`
# is clean. A pushed entry that DOES carry the key still compares — an
# authored edit to it stays visible.
_STAMPED_IF_UNAUTHORED = frozenset(
    {
        "verified_value",
        "verified_prose",
        "row_count",
        "terms",
        "embedding",
        "embeddings",
        "updated_at",
        "verified_at",
        "retired_at",
    }
)

# Eval cases the server holds that a pushed cases.yaml need not carry: staged
# candidates (proposals awaiting review) and cases already retired.
_SERVER_ONLY_CASE_STATUSES = frozenset({"candidate", "retired"})


def _reconcile_db_list(path: str, db_content: str, incoming_content: str) -> str:
    """The DB side of a list file, minus what the push cannot be expected to
    carry: server-stamped values its entries lack, and (cases) server-only
    entries it does not name. Both sides arrive canonical; any parse failure
    passes the DB content through."""
    import yaml as _yaml

    try:
        db = _yaml.safe_load(db_content) or []
        incoming = _yaml.safe_load(incoming_content) or []
        if not isinstance(db, list) or not isinstance(incoming, list):
            return db_content
        pushed = {
            str(e.get("question", "")).strip().lower(): e for e in incoming if isinstance(e, dict)
        }
        kept: list[Any] = []
        changed = False
        for entry in db:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            mine = pushed.get(str(entry.get("question", "")).strip().lower())
            if (
                mine is None
                and path == _CASES_FILE
                and entry.get("status") in _SERVER_ONLY_CASE_STATUSES
            ):
                changed = True
                continue
            trimmed = {
                k: v
                for k, v in entry.items()
                if k not in _STAMPED_IF_UNAUTHORED or (mine is not None and k in mine)
            }
            changed = changed or len(trimmed) != len(entry)
            kept.append(trimmed)
        return _dump_list(kept) if changed else db_content
    except Exception:
        return db_content


def _canonical_queries_yaml(content: str) -> str:
    """The DB round-trips queries.yaml in whatever style its emitter picked;
    files carry the author's (or an exporter's) style — sequence indent, wrap
    column, folded scalars. Same data, three cosmetic deltas, and every lens
    planned as `update` forever. Parse and re-dump both
    sides through ONE emitter so display style can never drive the change
    decision. Unparseable content passes through raw; apply rejects it with
    the real error."""
    import yaml as _yaml

    from services.contracts.semantic_model import SampleQuery

    try:
        data = _yaml.safe_load(content) or {}
        if not isinstance(data, dict):
            return content
        canon = {
            "use_when": data.get("use_when") or [],
            "sample_queries": [
                SampleQuery.model_validate(q).model_dump() for q in data.get("sample_queries") or []
            ],
        }
        return str(_yaml.safe_dump(canon, sort_keys=False, allow_unicode=True))
    except Exception:
        return content


_LIST_FILES = ("certified_answers.yaml", "evals/cases.yaml")

_CERTIFIED_FILE = "certified_answers.yaml"

_CASES_FILE = "evals/cases.yaml"


def _canonical_managed(path: str, content: str) -> str:
    if path == "lens.yaml":
        return _canonical_lens_yaml(content)
    if path == "queries.yaml":
        return _canonical_queries_yaml(content)
    if path in _LIST_FILES:
        return _canonical_yaml_list(content)
    return content


def _drop_unpushed_review_answers(db_content: str, incoming_content: str) -> str:
    """Certified answers the review flow approved (source ``review:*``) are
    server-origin — the server-only idiom: their absence from a pushed
    certified_answers.yaml is the normal state, not a removal (apply keeps
    them), so the plan must not diff them as deletions. File-originated entries
    absent from the push DO diff — apply deletes them (files win). Both sides
    arrive canonicalized (parsed-list dumps), so parse failures just pass the
    content through."""
    import yaml as _yaml

    try:
        db = _yaml.safe_load(db_content) or []
        incoming = _yaml.safe_load(incoming_content) or []
        if not isinstance(db, list) or not isinstance(incoming, list):
            return db_content
        pushed = {
            str(e.get("question", "")).strip().lower() for e in incoming if isinstance(e, dict)
        }
        kept = [
            e
            for e in db
            if not (
                isinstance(e, dict)
                and str(e.get("source") or "").startswith("review:")
                and str(e.get("question", "")).strip().lower() not in pushed
            )
        ]
        if len(kept) == len(db):
            return db_content
        return _dump_list(kept)
    except Exception:
        return db_content


def plan_lenses(
    db_trees: dict[str, dict[str, str]], incoming_trees: dict[str, dict[str, str]]
) -> list[LensPlan]:
    plans: list[LensPlan] = []
    for lens, incoming in sorted(incoming_trees.items()):
        db_tree = db_trees.get(lens)
        managed_incoming = {
            p: _canonical_managed(p, c) for p, c in incoming.items() if is_managed(p)
        }
        if db_tree is None:
            diffs = [
                FileDiff(path, _diff(path, "", content))
                for path, content in managed_incoming.items()
            ]
            plans.append(LensPlan(lens, "create", diffs))
            continue
        # The DB side canonicalizes too — its render stamps runtime provenance
        # (created_by/created_at) the incoming side can never carry.
        managed_db = {p: _canonical_managed(p, c) for p, c in db_tree.items() if is_managed(p)}
        diffs = []
        for path in sorted(managed_incoming.keys() | managed_db.keys()):
            old, new = managed_db.get(path, ""), managed_incoming.get(path, "")
            if path == _CERTIFIED_FILE:
                if path not in managed_incoming:
                    # No certified_answers.yaml in the push = the surface is
                    # unmanaged this push (apply touches nothing) — a removal
                    # diff here would promise a deletion apply won't do.
                    continue
                old = _drop_unpushed_review_answers(old, new)
            if path in _LIST_FILES and path in managed_incoming:
                old = _reconcile_db_list(path, old, new)
            if path == "queries.yaml" and path not in managed_incoming:
                # The loader reads a missing queries.yaml as an empty one (no
                # anchors, no samples) and the DB renders that state as
                # `use_when: []`; compared raw, the two never match and a lens
                # that never wrote the file plans as `update` forever.
                new = _canonical_managed(path, "")
            if path == _CASES_FILE and path not in managed_incoming:
                # No cases.yaml in the push leaves the stored cases untouched
                # (apply retires absent cases only when the file is pushed),
                # so there is nothing to diff.
                continue
            if old != new:
                diffs.append(FileDiff(path, _diff(path, old, new)))
        plans.append(LensPlan(lens, "update" if diffs else "unchanged", diffs))
    return plans


def _canonical_semantic(path: str, content: str) -> tuple[str, str]:
    """Re-render an incoming semantic file canonically → (canonical path,
    canonical content), so cosmetic differences (omitted defaulted fields, key
    order, comment headers) never produce phantom plan diffs against the DB's
    canonical render — a plan output people learn to ignore is worthless.
    The canonical PATH is how foldered files (semantic/entities/sales/x.yaml —
    folders are organization, the asset name is identity) still match their DB
    counterpart, which always renders flat. Unparseable content passes through
    raw (apply rejects it with the real error)."""
    from services.semantic.files import parse_semantic_files, render_semantic_files

    try:
        entities, definitions, relationships = parse_semantic_files({path: content})
        rendered = render_semantic_files(
            list(entities.values()), list(definitions.values()), list(relationships.values())
        )
        return next(iter(rendered.items()), (path, content))
    except Exception:
        return path, content


def plan_semantic(db_files: dict[str, str], incoming_files: dict[str, str]) -> list[SemanticPlan]:
    """Incoming ``semantic/**`` files vs the DB's rendered assets, matched by
    asset identity (canonical path), reported under the author's path. Both
    sides compare in canonical form (hash-consistent with apply's no-op skip)."""
    plans: list[SemanticPlan] = []
    for path, content in sorted(incoming_files.items()):
        canon_path, new = _canonical_semantic(path, content)
        old = db_files.get(path)
        if old is None:
            old = db_files.get(canon_path)
        if old is None:
            plans.append(SemanticPlan(path, "create", _diff(path, "", new)))
        elif old != new:
            plans.append(SemanticPlan(path, "update", _diff(path, old, new)))
        else:
            plans.append(SemanticPlan(path, "unchanged"))
    return plans


def semantic_orphans(db_keys: Iterable[str], incoming_keys: Iterable[str]) -> list[str]:
    """DB assets (``kind/name`` keys) that no pushed semantic file mentions —
    advisory only, never auto-deleted (deletion stays an explicit act: `dst
    semantic rm`). Callers scope this to pushes that carry at least one
    semantic/ file, so a lens-only push never spams orphans."""
    return sorted(set(db_keys) - set(incoming_keys))


def server_only(db_names: Iterable[str], incoming_names: Iterable[str]) -> list[str]:
    """DB objects (plain lens/connection names) the pushed files don't carry —
    API-created, cloud-authored, or legacy. The semantic_orphans of object
    state: advisory only, adopt or leave for its owner, never auto-deleted
    (deletion stays an explicit act: `dst lens rm`). Callers scope it the
    same way — lens names only when the push carries lenses/ files, connections
    only when it carries dst.yaml."""
    return sorted(set(db_names) - set(incoming_names))


def stale_asset_keys(
    assets: dict[str, str],
    effective_hashes: dict[str, str],
    relationship_pairs: dict[str, tuple[str, str]],
) -> list[str]:
    """The asset keys that make one compiled provenance stale: every consumed
    asset whose effective hash moved (deletion included — an absent asset reads
    as ""), PLUS any relationship that exists NOW between two entities the lens
    compiled with but was absent at compile time. Without that second clause a
    brand-new relationship never appears in any provenance, no lens is flagged,
    and the published layer keeps refusing a join the project files declare —
    plan/apply reporting success the whole time. ``relationship_pairs`` maps the
    effective ``relationship/<name>`` keys to their (left, right) endpoints."""
    changed = [
        key for key, digest in sorted(assets.items()) if effective_hashes.get(key, "") != digest
    ]
    selected = {key.removeprefix("entity/") for key in assets if key.startswith("entity/")}
    changed += sorted(
        key
        for key, (left, right) in relationship_pairs.items()
        if key not in assets and left in selected and right in selected
    )
    return changed


def stale_lenses(
    provenances: dict[str, dict[str, str]],
    effective_hashes: dict[str, str],
    relationship_pairs: dict[str, tuple[str, str]],
) -> dict[str, list[str]]:
    """{lens: changed asset keys} for every published lens whose compiled
    provenance no longer matches ``effective_hashes`` (current DB hashes with the
    push's incoming assets layered on). Lenses absent from a push still appear —
    apply recompiles them."""
    out: dict[str, list[str]] = {}
    for lens, assets in provenances.items():
        changed = stale_asset_keys(assets, effective_hashes, relationship_pairs)
        if changed:
            out[lens] = changed
    return out


def stale_certified(
    answers: Iterable[tuple[str, dict[str, str] | None]], effective_hashes: dict[str, str]
) -> tuple[list[str], list[str]]:
    """(questions, changed asset keys) over one lens's certified answers — the
    stale_lenses idiom applied to each answer's derived bindings. A flagged
    answer means the shared assets its SQL touches changed since it was verified:
    re-verify is a human act, nothing is auto-disabled or recompiled. Answers
    without bindings (pre-CB rows; backfilled on next apply) never flag."""
    questions: list[str] = []
    changed_assets: set[str] = set()
    for question, bindings in answers:
        changed = [k for k, h in (bindings or {}).items() if effective_hashes.get(k, "") != h]
        if changed:
            questions.append(question)
            changed_assets.update(changed)
    return questions, sorted(changed_assets)


def _diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"db/{path}",
            tofile=f"files/{path}",
        )
    )


# ── selection: `--select` over the project graph (SQLMesh grammar) ───────────

SCOPES = ("all", "semantic", "certified", "cases", "config")
_SELECTABLE = ("lens", "entity", "definition")
_SCOPED_LENS_FILE = {"certified": _CERTIFIED_FILE, "cases": _CASES_FILE}


@dataclass
class Selection:
    """What a ``--select``/``--scope`` push resolved to: the selected nodes and
    the filtered file map that replaces the push. ``lenses`` are the only lenses
    validated, pushed and gated; ``assets`` the only shared files pushed."""

    selectors: list[str]
    scope: str
    lenses: list[str]
    assets: list[str]
    files: dict[str, str]
    excluded: int = 0
    notes: list[str] = field(default_factory=list)


def _file_keys(path: str, content: str) -> set[str]:
    """``kind/name`` keys one semantic file defines (relationships included)."""
    from services.project.loader import asset_key
    from services.semantic.files import parse_semantic_files

    try:
        entities, definitions, relationships = parse_semantic_files({path: content})
    except Exception:  # noqa: BLE001 — an unparseable file keys by the loader's rule
        found = asset_key(path, content)
        if found is None:
            return set()
        kind = {"entities": "entity", "definitions": "definition"}.get(
            found[0].rsplit("/", 1)[-1], "relationship"
        )
        return {f"{kind}/{found[1]}"}
    return (
        {f"entity/{n}" for n in entities}
        | {f"definition/{t}" for t in definitions}
        | {
            f"relationship/{n}|{r.left}|{r.right}"  # endpoints ride for the filter
            for n, r in relationships.items()
        }
    )


def _lens_selects(tree: dict[str, str]) -> tuple[set[str], set[str]]:
    """(entities, definitions) one pushed lens.yaml selects; '*' kept as is."""
    import yaml as _yaml

    from services.contracts.lens_config import LensConfig

    try:
        config = LensConfig.model_validate(_yaml.safe_load(tree.get("lens.yaml", "")) or {})
    except Exception:  # noqa: BLE001 — apply reports the real error on the lens row
        return set(), set()
    return {e.name for e in config.select.entities}, set(config.select.definitions)


def resolve_selection(
    files: dict[str, str],
    selectors: list[str],
    scope: str,
    *,
    db_semantic: dict[str, str],
    db_trees: dict[str, dict[str, str]],
) -> Selection:
    """Resolve ``--select`` selectors against the push's own graph and filter
    the push down to them. Grammar: ``kind/name`` (lens, entity, definition),
    shell globs on the name (``lens/finance_*``), ``+X`` (X and everything
    upstream — a lens's selected entities and definitions), ``X+`` (X and
    everything downstream — the pushed lenses that select it), and ``@changed``
    (every node whose files differ from the server: the plan's own diff). A
    selector that matches nothing is an error. No selectors selects everything,
    so ``--scope`` alone narrows the kinds of files only.

    ``scope`` narrows what kind of file is pushed: ``semantic`` (semantic/**),
    ``config`` (dst.yaml and the other project-level files), ``certified`` /
    ``cases`` (one lens file each, overlaid on the server's own render of that
    lens so nothing else about it changes — a lens not on the server yet is
    left out, and said)."""
    import fnmatch
    import re

    from services.project.loader import split_by_lens, split_semantic

    if scope not in SCOPES:
        raise ValueError(f"--scope must be one of {', '.join(SCOPES)}, not '{scope}'")
    semantic = split_semantic(files)
    trees = split_by_lens(files)
    keys_by_file = {p: _file_keys(p, c) for p, c in semantic.items()}
    assets = {
        k for keys in keys_by_file.values() for k in keys if not k.startswith("relationship/")
    }
    selects = {lens: _lens_selects(tree) for lens, tree in trees.items()}
    nodes = {f"lens/{n}" for n in trees} | assets

    def upstream(node: str) -> set[str]:
        if not node.startswith("lens/"):
            return set()
        entities, definitions = selects.get(node.removeprefix("lens/"), (set(), set()))
        return {
            a
            for a in assets
            if (a.startswith("entity/") and ({"*", a.removeprefix("entity/")} & entities))
            or (
                a.startswith("definition/") and ({"*", a.removeprefix("definition/")} & definitions)
            )
        }

    def downstream(node: str) -> set[str]:
        kind, _, name = node.partition("/")
        if kind == "lens":
            return set()
        index = 0 if kind == "entity" else 1
        return {f"lens/{lens}" for lens, sel in selects.items() if {"*", name} & sel[index]}

    picked: set[str] = set()
    for raw in selectors:
        selector = raw.strip()
        if selector == "@changed":
            picked |= {
                k
                for p in plan_semantic(db_semantic, semantic)
                if p.status != "unchanged"
                for k in keys_by_file.get(p.path, set())
                if not k.startswith("relationship/")
            }
            picked |= {
                f"lens/{p.lens}" for p in plan_lenses(db_trees, trees) if p.status != "unchanged"
            }
            continue
        m = re.fullmatch(r"(\+?)(lens|entity|definition)/([^+\s]+)(\+?)", selector)
        if m is None:
            raise ValueError(
                f"selector '{selector}' is not kind/name (kinds: {', '.join(_SELECTABLE)}), "
                "a glob on the name, +X / X+, or @changed"
            )
        up, kind, pattern, down = m.groups()
        matched = {
            n
            for n in nodes
            if n.startswith(f"{kind}/") and fnmatch.fnmatchcase(n.partition("/")[2], pattern)
        }
        if not matched:
            raise ValueError(f"selector '{selector}' matches nothing in the project")
        for node in matched:
            picked.add(node)
            if up:
                picked |= upstream(node)
            if down:
                picked |= downstream(node)
    if not selectors:
        picked = set(nodes)
    lenses = sorted(n.removeprefix("lens/") for n in picked if n.startswith("lens/"))
    chosen_assets = sorted(n for n in picked if not n.startswith("lens/"))
    chosen_entities = {a.removeprefix("entity/") for a in chosen_assets if a.startswith("entity/")}

    def keep_semantic(path: str) -> bool:
        for key in keys_by_file.get(path, set()):
            if key.startswith("relationship/"):
                _name, left, right = key.split("|")
                if {left, right} <= chosen_entities:
                    return True
            elif key in picked:
                return True
        return False

    out: dict[str, str] = {}
    notes: list[str] = []
    if scope in ("all", "semantic"):
        out.update({p: c for p, c in semantic.items() if keep_semantic(p)})
    if scope in ("all", "config"):
        out.update(
            {p: c for p, c in files.items() if p not in semantic and not p.startswith("lenses/")}
        )
    if scope == "all":
        for lens in lenses:
            out.update({f"lenses/{lens}/{p}": c for p, c in trees[lens].items()})
    elif scope in _SCOPED_LENS_FILE:
        only_file = _SCOPED_LENS_FILE[scope]
        for lens in lenses:
            if lens not in db_trees:
                notes.append(f"{lens}: not on the server yet — --scope {scope} cannot create it")
                continue
            tree = {p: c for p, c in db_trees[lens].items() if is_managed(p)}
            if only_file in trees[lens]:
                tree[only_file] = trees[lens][only_file]
            out.update({f"lenses/{lens}/{p}": c for p, c in tree.items()})
    return Selection(
        selectors=[s.strip() for s in selectors],
        scope=scope,
        lenses=lenses,
        assets=chosen_assets,
        files=out,
        excluded=sum(1 for p in files if p not in out),
        notes=notes,
    )
