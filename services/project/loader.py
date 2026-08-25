"""The inverse of render_lens_repo: a lens file tree → its declarable parts.

Pure (``{path: content}`` in, no filesystem) so the CLI, the mgmt endpoints, and
tests all share one implementation. Managed paths: lens.yaml, queries.yaml,
definitions/*.md (lens-LOCAL terms), certified_answers.yaml, evals/cases.yaml.
README.md, compiled.yaml, audit/*, and certified/* (certified-definition pages —
export-only in v1) are runtime outputs the loader ignores.

The embedded SemanticModel is no longer read from files — it is born at apply,
when the lens's ``select`` compiles against the shared layer
(services/project/compile.py). Shared assets live at project scope
(``semantic/``), split out by ``split_semantic``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from services.contracts.authoring import check_entry_keys, parse_authored
from services.contracts.lens_config import LensConfig
from services.contracts.semantic_model import Definition, SampleQuery
from services.semantic.files import DEFINITION_DIR, ENTITY_DIR, page_to_definition

MANAGED_EXACT = ("lens.yaml", "queries.yaml", "certified_answers.yaml", "evals/cases.yaml")

# queries.yaml has no model of its own (its two keys land on different objects),
# so its top level gets the unknown-key rule by hand.
_QUERIES_KEYS = frozenset({"use_when", "sample_queries"})

# certified_answers.yaml entries stay plain dicts through apply (the gates are
# hand-rolled), so they get the rule by hand too. These are exactly the keys
# render_lens_repo writes back, plus the authored-only ones apply reads.
_CERTIFIED_ANSWER_KEYS = frozenset(
    {
        "question",
        "sql",
        "slots",
        "sample_bindings",
        "created_by",
        "created_at",
        "source",
        "verified_by",
        "verified_value",
        "verified_prose",
        "status",
    }
)


def parse_yaml(text: str, path: str) -> Any:
    """The one YAML parse seam for managed project files: malformed content
    raises ValueError naming the file and position (machine-actionable) —
    yaml's own YAMLError is not a ValueError, so it would otherwise sail past
    the plan/apply catches and surface as a raw 500."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "unparseable"
        # The recurring authoring papercut: an unquoted
        # `: ` or `,` inside a plain-scalar description is valid YAML *syntax*
        # meaning something else entirely — name the likely fix, not just the
        # grammar rule.
        hint = ""
        if "mapping values are not allowed" in str(problem):
            hint = " (an unquoted `: ` inside a description? quote the whole string)"
        raise ValueError(f"{path}: invalid YAML{where} — {problem}{hint}") from exc


def is_managed(path: str) -> bool:
    return path in MANAGED_EXACT or (path.startswith("definitions/") and path.endswith(".md"))


def asset_dir(path: str) -> str | None:
    """The authored-asset directory a PROJECT path lives under, or None when it
    authors no asset (README.md, compiled.yaml, queries.yaml, fixtures/...).

    Prefix, at any depth: a folder under an asset directory is organization, so
    ``semantic/entities/sales/orders.yaml`` is as much an entity page as
    ``semantic/entities/orders.yaml``. That is already the rule
    ``split_semantic`` and ``load_lens_source`` load by — stated once here so
    nothing has to restate it and drift."""
    if path.startswith(ENTITY_DIR + "/") and path.endswith((".yaml", ".yml")):
        return ENTITY_DIR
    if path.startswith(DEFINITION_DIR + "/") and path.endswith(".md"):
        return DEFINITION_DIR
    parts = path.split("/")
    if len(parts) > 3 and parts[0] == "lenses" and parts[2] == "definitions":
        return "/".join(parts[:3]) if path.endswith(".md") else None
    return None


def asset_key(path: str, content: str) -> tuple[str, str] | None:
    """``(asset directory, asset name)`` — WHICH authored asset a project file is.

    The one identity rule, and the one every writer defers to: the DIRECTORY says
    what kind of page to read the file as, and the name INSIDE the page is the
    identity — never the filename. Two writers used to decide that for
    themselves, and both landed a SECOND page for one asset: ``patches approve``
    looked for the term's page in a single directory while the loader loads a
    whole subtree (so the scaffold's own ``semantic/definitions/examples/`` page
    was invisible to it), and ``dst export`` wrote every asset under its slug
    (``customer_nodes`` → ``customer-nodes.yaml``) beside the author's own file.
    Both leave a tree the next ``dst plan`` rejects, on a layout the product
    itself produced.

    None when the path authors nothing, or when the content is too malformed to
    name an asset — an unreadable neighbour is never the caller's problem."""
    directory = asset_dir(path)
    if directory is None:
        return None
    try:
        if path.endswith(".md"):
            name = page_to_definition(content).term
        else:
            data = parse_yaml(content, path)
            name = data.get("name", "") if isinstance(data, dict) else ""
    except Exception:  # noqa: BLE001 — an unreadable neighbour resolves to nothing
        return None
    name = name.strip().lower() if isinstance(name, str) else ""
    return (directory, name) if name else None


def existing_asset_paths(project: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    """``{incoming path: where THIS tree already authors that same asset}``, for
    every incoming file that would otherwise land a second page for one asset.

    The server always renders an asset to its canonical path
    (``semantic/definitions/<slug>.md``) — it cannot know the tree foldered it
    under ``examples/`` or named it after the term. Writers ask here where the
    bytes actually go; paths already in agreement are simply absent from the
    result. A tree that already carries two pages for one asset is broken (plan
    says so, by name) — resolve to the first in sorted order, so the answer is at
    least deterministic."""
    here: dict[tuple[str, str], str] = {}
    for path in sorted(project):
        key = asset_key(path, project[path])
        if key is not None:
            here.setdefault(key, path)
    moved: dict[str, str] = {}
    for path, content in incoming.items():
        key = asset_key(path, content)
        found = here.get(key) if key is not None else None
        if found is not None and found != path:
            moved[path] = found
    return moved


@dataclass
class LensSource:
    config: LensConfig
    local_definitions: list[Definition] = field(default_factory=list)
    use_when: list[str] = field(default_factory=list)
    sample_queries: list[SampleQuery] = field(default_factory=list)
    # None = the tree carries no certified_answers.yaml (surface unmanaged this
    # push — apply must not delete); [] = the file is present but empty (files
    # win: file-originated answers absent from it delete on apply).
    certified_answers: list[dict[str, Any]] | None = None
    eval_cases: list[dict[str, Any]] = field(default_factory=list)
    # Keys that parsed but are read by nothing — apply surfaces these as
    # warnings. A real key that does nothing is not an error (projects author
    # them, the server's own render emits some), but it must not be silent.
    notes: list[str] = field(default_factory=list)


def load_lens_source(files: dict[str, str]) -> LensSource:
    """Parse one lens's file tree. Raises ValueError on a malformed tree."""
    if "lens.yaml" not in files:
        raise ValueError("a lens tree needs lens.yaml")
    notes: list[str] = []
    config = parse_authored(
        LensConfig, parse_yaml(files["lens.yaml"], "lens.yaml") or {}, "lens.yaml", notes=notes
    )
    local_definitions = []
    for path in sorted(files):
        if path.startswith("definitions/") and path.endswith(".md"):
            try:
                local_definitions.append(page_to_definition(files[path], path=path, notes=notes))
            except ValueError:
                raise
            except Exception as exc:  # frontmatter YAMLError incl. — name the file
                raise ValueError(f"{path}: {exc}") from exc
    use_when, sample_queries = _parse_queries(files.get("queries.yaml"))
    certified_answers = (
        _yaml_list(files["certified_answers.yaml"], "certified_answers.yaml")
        if "certified_answers.yaml" in files
        else None
    )
    for entry in certified_answers or []:
        error = check_entry_keys(
            entry,
            _CERTIFIED_ANSWER_KEYS,
            "certified_answers.yaml",
            f"answer '{str(entry.get('question', '?'))[:60]}'",
        )
        if error:
            raise ValueError(error)
    return LensSource(
        config=config,
        local_definitions=local_definitions,
        use_when=use_when,
        sample_queries=sample_queries,
        certified_answers=certified_answers,
        eval_cases=_yaml_list(files.get("evals/cases.yaml"), "evals/cases.yaml"),
        notes=notes,
    )


def split_by_lens(files: dict[str, str]) -> dict[str, dict[str, str]]:
    """A project's ``lenses/<name>/...`` map → per-lens trees (prefix stripped).
    Paths outside lenses/ (dst.yaml itself, docs) are simply not lens files."""
    out: dict[str, dict[str, str]] = {}
    for path, content in files.items():
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "lenses":
            out.setdefault(parts[1], {})["/".join(parts[2:])] = content
    return out


def split_semantic(files: dict[str, str]) -> dict[str, str]:
    """The project-level shared layer: ``semantic/entities/*.yaml`` +
    ``semantic/definitions/*.md`` (never under lenses/)."""
    return {
        path: content
        for path, content in files.items()
        if (path.startswith(ENTITY_DIR + "/") and path.endswith((".yaml", ".yml")))
        or (path.startswith(DEFINITION_DIR + "/") and path.endswith(".md"))
    }


def _parse_queries(text: str | None) -> tuple[list[str], list[SampleQuery]]:
    """queries.yaml → (use_when, sample_queries). Both optional, both lens-local."""
    if not text:
        return [], []
    data = parse_yaml(text, "queries.yaml") or {}
    if not isinstance(data, dict):
        raise ValueError("queries.yaml must be a mapping {use_when, sample_queries}")
    unknown = check_entry_keys(data, _QUERIES_KEYS, "queries.yaml", "top level")
    if unknown:
        raise ValueError(unknown)
    # `use_when: some sentence` (no `- `) is the constant authoring slip, and
    # iterating it made ~300 one-character router anchors — accepted, silent,
    # and wrong. A bare string is ONE entry, exactly as the contract's StrList
    # reads it everywhere else.
    raw_use_when = data.get("use_when") or []
    if isinstance(raw_use_when, str):
        raw_use_when = [raw_use_when]
    if not isinstance(raw_use_when, list):
        raise ValueError(
            "queries.yaml: use_when must be a list of strings (or one bare string), "
            f"not {type(raw_use_when).__name__} — write:\n"
            "  use_when:\n    - how did commissions land last quarter?"
        )
    use_when = [str(u) for u in raw_use_when]
    # sample_queries are MAPPINGS ({question, sql}); a bare string has no
    # single-entry meaning, so it is rejected by shape rather than iterated.
    raw_samples = data.get("sample_queries") or []
    if not isinstance(raw_samples, list):
        raise ValueError(
            "queries.yaml: sample_queries must be a list of {question, sql} mappings, "
            f"not {type(raw_samples).__name__} — write:\n"
            "  sample_queries:\n    - question: what is ARR?\n      sql: SELECT ..."
        )
    sample_queries = [
        parse_authored(SampleQuery, q, f"queries.yaml: sample_queries[{i}]")
        for i, q in enumerate(raw_samples)
    ]
    return use_when, sample_queries


def _yaml_list(text: str | None, path: str = "file") -> list[dict[str, Any]]:
    if not text:
        return []
    # A comment-only file (the scaffold shape) parses to None → [] — present-
    # but-empty, so the files-win surface stays managed; absent stays distinct
    # (load_lens_source returns None for a tree without the file).
    data = parse_yaml(text, path) or []
    if not isinstance(data, list):
        raise ValueError(
            f"{path}: expected a top-level YAML list (entries like "
            "`- question: ...` / `  sql: ...`), not a mapping — no wrapper key"
        )
    return [dict(item) for item in data]
