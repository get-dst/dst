"""Shared-layer file I/O: `semantic/` on disk ⇄ SharedEntity / Definition /
SharedRelationship / TableBlueprint.

Entities, relationships and table blueprints render one YAML file each
(`semantic/entities/<name>.yaml`, `semantic/relationships/<left>__<right>.yaml`,
`semantic/tables/<table>.yaml`, full model_dump); definitions render the same
frontmatter pages lenses use
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
from services.contracts.semantic_model import Definition, TableBlueprint
from services.contracts.shared_semantic import SharedEntity, SharedRelationship

ENTITY_DIR = "semantic/entities"
DEFINITION_DIR = "semantic/definitions"
RELATIONSHIP_DIR = "semantic/relationships"
TABLE_DIR = "semantic/tables"


def is_yaml_under(path: str, directory: str) -> bool:
    return path.startswith(directory + "/") and path.endswith((".yaml", ".yml"))


def table_slug(table: str) -> str:
    """A blueprint's file stem: the table name as its connection names it,
    dots kept (`raw_crm.contacts.yaml`), anything a path cannot hold dashed."""
    return re.sub(r"[^a-z0-9._-]+", "-", table.lower()).strip("-.") or "untitled"


def slug(term: str) -> str:
    """The filesystem-safe stem an asset's page is written under."""
    return re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-") or "untitled"


def definition_to_page(d: Definition) -> str:
    return render_definition_page(
        CertifiedDefinition(
            metric=d.term,
            about=d.about,
            sql=d.sql_expr,
            period=d.period,
            body=d.body,
            status=d.status,
            possible_mappings=d.possible_mappings,
            aliases=d.aliases,
            audiences=d.audiences,
            value_aliases=d.value_aliases,
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
        period=page.period,
        source=source,  # type: ignore[arg-type]
        status=page.status,
        possible_mappings=page.possible_mappings,
        aliases=page.aliases,
        audiences=page.audiences,
        value_aliases=page.value_aliases,
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
    entities: list[SharedEntity],
    definitions: list[Definition],
    relationships: list[SharedRelationship],
    blueprints: list[TableBlueprint] | None = None,
) -> dict[str, str]:
    files: dict[str, str] = {}
    for e in entities:
        data = e.model_dump(mode="json", exclude_none=True)
        if not data.get("primary_key"):
            # No key column is "none declared", not a declared empty key; the
            # parser defaults it the same way, and `[]` reads as a mistake.
            data.pop("primary_key", None)
        files[f"{ENTITY_DIR}/{slug(e.name)}.yaml"] = yaml.safe_dump(
            data, sort_keys=False, allow_unicode=True
        )
    for d in definitions:
        files[f"{DEFINITION_DIR}/{slug(d.term)}.md"] = definition_to_page(d)
    for r in relationships:
        files[f"{RELATIONSHIP_DIR}/{slug(r.name)}.yaml"] = yaml.safe_dump(
            r.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
        )
    for b in blueprints or []:
        files[f"{TABLE_DIR}/{table_slug(b.table)}.yaml"] = render_blueprint(b)
    return files


def render_blueprint(blueprint: TableBlueprint) -> str:
    """One blueprint as its file: the authored fields only, empty ones left out."""
    data = blueprint.model_dump(mode="json", exclude_defaults=True)
    data = {"connection": blueprint.connection, "table": blueprint.table, **data}
    return str(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def parse_semantic_file(
    path: str, content: str, *, notes: list[str] | None = None
) -> SharedEntity | Definition | SharedRelationship | TableBlueprint | None:
    """One `semantic/**` file → its asset, or None when the path isn't one.

    The single validation seam: plan and apply both go through it, so plan can
    never render a clean create-diff for a file apply will reject —
    and so an unknown key is rejected in exactly one place. Raises ValueError
    naming the path; ``notes`` collects the inert-key findings."""
    is_entity = path.startswith(ENTITY_DIR + "/") and path.endswith((".yaml", ".yml"))
    is_relationship = path.startswith(RELATIONSHIP_DIR + "/") and path.endswith((".yaml", ".yml"))
    is_definition = path.startswith(DEFINITION_DIR + "/") and path.endswith(".md")
    is_table = is_yaml_under(path, TABLE_DIR)
    if not (is_entity or is_relationship or is_definition or is_table):
        return None
    try:
        if is_definition:
            return page_to_definition(content, path=path, notes=notes)
        data = yaml.safe_load(content) or {}
    except ValueError:
        raise  # the authoring seam already names the path
    except Exception as exc:  # YAMLError incl. — not a ValueError, so name the file here
        raise ValueError(f"{path}: {exc}") from exc
    if is_relationship:
        return parse_authored(SharedRelationship, data, path, notes=notes)
    if is_table:
        return parse_authored(TableBlueprint, data, path, notes=notes)
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
) -> tuple[dict[str, SharedEntity], dict[str, Definition], dict[str, SharedRelationship]]:
    """`semantic/**` paths → (entities by name, definitions by term,
    relationships by derived name). Table blueprints are read by
    ``parse_blueprint_files``; here they are validated and skipped."""
    entities: dict[str, SharedEntity] = {}
    definitions: dict[str, Definition] = {}
    relationships: dict[str, SharedRelationship] = {}
    entity_paths: dict[str, str] = {}
    definition_paths: dict[str, str] = {}
    pair_paths: dict[frozenset[str], str] = {}
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
        elif isinstance(asset, SharedRelationship):
            # The pair is the identity, unordered: a second file for the same
            # two entities — reversed included — is a competing claim, not a
            # second relationship.
            pair = frozenset((asset.left, asset.right))
            if pair in pair_paths:
                raise ValueError(
                    f"the {asset.left} <-> {asset.right} relationship is declared in "
                    f"both {pair_paths[pair]} and {path} — a pair has one relationship"
                )
            pair_paths[pair] = path
            relationships[asset.name] = asset
        elif isinstance(asset, Definition):
            if asset.term in definition_paths:
                raise ValueError(
                    f"definition '{asset.term}' is defined in both "
                    f"{definition_paths[asset.term]} and {path}"
                )
            definition_paths[asset.term] = path
            definitions[asset.term] = asset
    return entities, definitions, relationships


def parse_blueprint_files(
    files: dict[str, str], *, notes: list[str] | None = None
) -> dict[str, TableBlueprint]:
    """`semantic/tables/**` paths → blueprints by table name. Two files for one
    table are a competing claim, named — the same rule as two entity files for
    one entity. Tables match when one name is the trailing parts of the other
    on the same connection (`raw.contacts` and `proj.raw.contacts`)."""
    from services.project.lineage import same_table

    out: dict[str, TableBlueprint] = {}
    paths: dict[str, str] = {}
    for path, content in sorted(files.items()):
        if not is_yaml_under(path, TABLE_DIR):
            continue
        blueprint = parse_semantic_file(path, content, notes=notes)
        assert isinstance(blueprint, TableBlueprint)
        twin = next(
            (
                name
                for name, other in out.items()
                if same_table(other.table, blueprint.table)
                and (other.connection == blueprint.connection or other.table == blueprint.table)
            ),
            None,
        )
        if twin is not None:
            raise ValueError(
                f"table '{blueprint.table}' has a blueprint in both {paths[twin]} and {path} "
                "— a table has one blueprint"
            )
        out[blueprint.name] = blueprint
        paths[blueprint.name] = path
    return out
