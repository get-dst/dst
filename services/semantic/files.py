"""Shared-layer file I/O: `semantic/` on disk ⇄ SharedEntity / Definition.

Entities render one YAML file each (`semantic/entities/<name>.yaml`, full
model_dump); definitions render the same frontmatter pages lenses use
(`semantic/definitions/<slug>.md`), so the whole project speaks one page
format. Parse is the pure inverse — malformed files raise ValueError naming
the path (machine-actionable, per the era principle).
"""

from __future__ import annotations

import re

import yaml

from services.certdefs import (
    CERTIFIED_DIR_ONLY,
    METADATA_ONLY,
    CertifiedDefinition,
    parse_definition_page,
    render_definition_page,
)
from services.contracts.authoring import parse_authored
from services.contracts.semantic_model import Definition
from services.contracts.shared_semantic import SharedEntity

ENTITY_DIR = "semantic/entities"
DEFINITION_DIR = "semantic/definitions"


def slug(term: str) -> str:
    """The filesystem-safe stem an asset's page is written under."""
    return re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-") or "untitled"


def definition_to_page(d: Definition) -> str:
    return render_definition_page(
        CertifiedDefinition(
            metric=d.term,
            about=d.about,
            sql=d.sql_expr,
            body=d.body,
            status=d.status,
            possible_mappings=d.possible_mappings,
            aliases=d.aliases,
            summary=d.summary,
            grain=d.grain,
            sources=d.sources,
        ),
        minimal=True,
    )


def page_to_definition(
    text: str,
    *,
    source: str = "authored",
    path: str | None = None,
    notes: list[str] | None = None,
) -> Definition:
    """One definition page -> its Definition. ``path`` switches on authoring
    strictness (unknown frontmatter key = error naming the file).

    summary/grain/sources ride through: they are what makes a page under
    ``model.certified_dir`` steer generation, and the identical file under
    ``semantic/definitions/`` used to drop them here. The certified-dir-only
    keys can't ride through — there is no per-question selector on this path —
    so they are NOTED rather than silently dropped."""
    page = parse_definition_page(text, path=path, notes=notes)
    if notes is not None and path is not None:
        _note_certified_only(page, path, notes)
    return Definition(
        term=page.metric,
        body=page.body,
        about=page.about,
        sql_expr=page.sql,
        source=source,  # type: ignore[arg-type]
        status=page.status,
        possible_mappings=page.possible_mappings,
        aliases=page.aliases,
        summary=page.summary,
        grain=page.grain,
        sources=page.sources,
    )


def _note_certified_only(page: CertifiedDefinition, path: str, notes: list[str]) -> None:
    """Say which frontmatter keys this directory does not act on.

    The page format is shared with ``model.certified_dir``, where a per-question
    selector reads question/usage_mode/verified_value. A definition page is
    compiled into the lens and injected unconditionally, so those three do
    nothing here — and owner/source_of_truth do nothing anywhere. Naming the
    directory that WOULD honour them is the actionable half."""
    fields = type(page).model_fields
    set_here = [k for k in CERTIFIED_DIR_ONLY if getattr(page, k) != fields[k].default]
    if set_here:
        notes.append(
            f"{path}: {', '.join(f'`{k}`' for k in set_here)} steer the per-question "
            "selector, which only runs for pages in a lens's `model.certified_dir` — "
            "a definition page is always injected, so they change nothing here"
        )
    meta = [k for k in METADATA_ONLY if getattr(page, k) != fields[k].default]
    if meta:
        notes.append(
            f"{path}: {', '.join(f'`{k}`' for k in meta)} is documentation on the page — "
            "no part of the product reads it"
        )


def render_semantic_files(
    entities: list[SharedEntity], definitions: list[Definition]
) -> dict[str, str]:
    files: dict[str, str] = {}
    for e in entities:
        files[f"{ENTITY_DIR}/{slug(e.name)}.yaml"] = yaml.safe_dump(
            e.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
        )
    for d in definitions:
        files[f"{DEFINITION_DIR}/{slug(d.term)}.md"] = definition_to_page(d)
    return files


def parse_semantic_file(
    path: str, content: str, *, notes: list[str] | None = None
) -> SharedEntity | Definition | None:
    """One `semantic/**` file → its asset, or None when the path isn't one.

    The single validation seam: plan and apply both go through it, so plan can
    never render a clean create-diff for a file apply will reject —
    and so an unknown key is rejected in exactly one place. Raises ValueError
    naming the path; ``notes`` collects the inert-key findings."""
    is_entity = path.startswith(ENTITY_DIR + "/") and path.endswith((".yaml", ".yml"))
    is_definition = path.startswith(DEFINITION_DIR + "/") and path.endswith(".md")
    if not (is_entity or is_definition):
        return None
    try:
        if not is_entity:
            return page_to_definition(content, path=path, notes=notes)
        data = yaml.safe_load(content) or {}
    except ValueError:
        raise  # the authoring seam already names the path
    except Exception as exc:  # YAMLError incl. — not a ValueError, so name the file here
        raise ValueError(f"{path}: {exc}") from exc
    return parse_authored(SharedEntity, data, path, notes=notes)


def validate_semantic_files(files: dict[str, str]) -> dict[str, str]:
    """{path: error} for EVERY invalid file, not just the first.

    Apply rejected 8 entity files with 10 pydantic errors each after plan
    rendered them all as clean creates; a plan that stops at the first failure
    still doesn't predict apply."""
    errors: dict[str, str] = {}
    for path, content in sorted(files.items()):
        try:
            parse_semantic_file(path, content)
        except ValueError as exc:
            errors[path] = str(exc)
    return errors


def parse_semantic_files(
    files: dict[str, str], *, notes: list[str] | None = None
) -> tuple[dict[str, SharedEntity], dict[str, Definition]]:
    """`semantic/**` paths → (entities by name, definitions by term)."""
    entities: dict[str, SharedEntity] = {}
    definitions: dict[str, Definition] = {}
    entity_paths: dict[str, str] = {}
    definition_paths: dict[str, str] = {}
    for path, content in sorted(files.items()):
        asset = parse_semantic_file(path, content, notes=notes)
        # Silent last-file-wins here would be exactly the drift the shared layer
        # exists to kill — two teams, one name, two meanings.
        if isinstance(asset, SharedEntity):
            if asset.name in entity_paths:
                raise ValueError(
                    f"entity '{asset.name}' is defined in both "
                    f"{entity_paths[asset.name]} and {path}"
                )
            entity_paths[asset.name] = path
            entities[asset.name] = asset
        elif isinstance(asset, Definition):
            if asset.term in definition_paths:
                raise ValueError(
                    f"definition '{asset.term}' is defined in both "
                    f"{definition_paths[asset.term]} and {path}"
                )
            definition_paths[asset.term] = path
            definitions[asset.term] = asset
    return entities, definitions
