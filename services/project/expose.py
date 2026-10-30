"""Resolve a lens's `expose:` grants into entities — at apply, from profiles.

An exposed table is one the lens reads without definitions. The grant is a set
of patterns; the tables behind it are whatever the connection's stored profiles
(`dst probe` / `dst introspect`) say exist, so the entity carries the columns the
warehouse holds TODAY and is rebuilt on every apply instead of rotting in a
meaning-free file. Pure: grants + profiles in, entities + errors out. Every
error names the pattern and the fix, and compile fails loud on any of them — a
grant that silently matched nothing would be a lens that quietly stopped
answering.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase

from services.contracts.lens_config import LensConfig
from services.contracts.profile import TableProfile
from services.contracts.semantic_model import (
    Entity,
    EntitySource,
    Field,
    HideRule,
    warehouse_field_type,
)


def _matches(pattern: str, name: str) -> bool:
    """fnmatch, case-insensitive: warehouses disagree about identifier case and
    a pattern is typed by a human."""
    return fnmatchcase(name.lower(), pattern.lower())


def _hidden(column: str, table: str, hide: Sequence[str]) -> bool:
    """A hide pattern matches `table.column` with the table bare or as introspect
    names it, so `*.ssn` and `staging.customers.email` both read naturally."""
    bare = table.rsplit(".", 1)[-1]
    return any(_matches(p, f"{bare}.{column}") or _matches(p, f"{table}.{column}") for p in hide)


def _literal(name: str) -> str:
    """``name`` as an fnmatch pattern that matches only itself."""
    return "".join(f"[{c}]" if c in "*?[" else c for c in name)


def _entity(profile: TableProfile, hide: Sequence[str]) -> Entity:
    fields: list[Field] = []
    withheld: list[str | HideRule] = []
    bare = profile.table.rsplit(".", 1)[-1]
    for column in profile.columns:
        if _hidden(column.name, profile.table, hide):
            # Dropping the field keeps it out of the allow-list, which binds
            # only generated SQL; recorded as the entity's own `hide` (exactly
            # this column, whatever the grant's pattern also matched) it holds
            # on every door, trusted SQL included.
            withheld.append(f"{_literal(bare)}.{_literal(column.name)}")
            continue
        fields.append(
            Field(
                name=column.name,
                # A warehouse type the mapping does not know is read as text: the
                # column still has to be addressable, and a string compares
                # safely where a guessed number would not.
                type=warehouse_field_type(column.type) or "string",  # type: ignore[arg-type]
                description=column.description,
            )
        )
    return Entity(
        name=profile.table.rsplit(".", 1)[-1],
        description=profile.description,
        source=EntitySource(connection=profile.connection, table=profile.table),
        fields=fields,
        exposed=True,
        exposed_whole=not withheld,
        hide=withheld,
    )


def resolve_expose(
    config: LensConfig,
    profiles: Mapping[str, Sequence[TableProfile]],
    *,
    declared: Sequence[str] = (),
    dropped: Mapping[str, Sequence[str]] | None = None,
) -> tuple[list[Entity], list[str]]:
    """``(entities, errors)`` for the lens's grants. ``profiles`` maps a connection
    name to its stored table profiles; ``declared`` names the entities the lens
    selects, which an exposed table's bare name must not collide with; ``dropped``
    maps a connection to the tables a stored profile still describes but the
    pushed probe no longer lists — gone upstream, so a pattern that matches only
    one of them fails, naming the table, instead of exposing a stale profile."""
    entities: list[Entity] = []
    errors: list[str] = []
    seen: dict[str, str] = {}  # entity name -> table that claimed it
    taken = set(declared)
    # A bare name two granted tables share, or a selected entity holds, names
    # neither table alone: each such exposed table is named by its qualified
    # name instead (`legacy.customers` -> `legacy_customers`), so a bare
    # reference keeps meaning the selected entity and a qualified one the table.
    granted_tables = {
        p.table
        for grant in config.expose
        if grant.connection in config.connections
        for p in profiles.get(grant.connection, ())
        if any(_matches(t, p.table) for t in grant.tables)
    }
    bare_claims = Counter(t.rsplit(".", 1)[-1] for t in granted_tables)
    for grant in config.expose:
        if grant.connection not in config.connections:
            errors.append(
                f"lens '{config.name}' exposes tables on connection '{grant.connection}', "
                f"which is not in its connections {list(config.connections)} — add it to "
                f"lenses/{config.name}/lens.yaml"
            )
            continue
        known = list(profiles.get(grant.connection, ()))
        gone = list((dropped or {}).get(grant.connection, ()))
        for pattern in grant.tables:
            matched = [p for p in known if _matches(pattern, p.table)]
            if not matched:
                why = (
                    "no profiled table matches"
                    if known
                    else "the connection has no stored profiles"
                )
                if stale := sorted(t for t in gone if _matches(pattern, t)):
                    why += (
                        f" — {', '.join(stale)} was dropped upstream (the pushed probe no "
                        "longer lists it)"
                    )
                errors.append(
                    f"lens '{config.name}' exposes '{pattern}' on connection "
                    f"'{grant.connection}' but {why} — run `dst probe` (so the tables are "
                    "profiled and the artifact is pushed) or fix the pattern"
                )
                continue
            for profile in sorted(matched, key=lambda p: p.table):
                if any(e.source.table == profile.table for e in entities):
                    continue  # two patterns matched the same table: one entity
                entity = _entity(profile, grant.hide)
                if entity.name in taken or bare_claims[entity.name] > 1:
                    entity.name = profile.table.replace(".", "_")
                if entity.name in taken:
                    errors.append(
                        f"lens '{config.name}' exposes '{profile.table}' (pattern "
                        f"'{pattern}') as '{entity.name}', which collides with a selected "
                        "entity of the same lens — exclude it from the pattern, or drop the "
                        "entity from `select`"
                    )
                    continue
                if entity.name in seen:
                    errors.append(
                        f"lens '{config.name}' exposes '{profile.table}' and "
                        f"'{seen[entity.name]}', which share the name '{entity.name}' "
                        "even qualified — narrow the patterns so one lens exposes one "
                        "table under that name"
                    )
                    continue
                seen[entity.name] = profile.table
                entities.append(entity)
        granted = [p for p in known if any(_matches(t, p.table) for t in grant.tables)]
        for pattern in grant.hide:
            if granted and not any(
                _matches(pattern, f"{p.table.rsplit('.', 1)[-1]}.{c.name}")
                or _matches(pattern, f"{p.table}.{c.name}")
                for p in granted
                for c in p.columns
            ):
                # A hide that withholds nothing is a column the author believes
                # is hidden and is not — the same silent miss as a table pattern
                # matching nothing, and it fails the same way.
                errors.append(
                    f"lens '{config.name}' hides '{pattern}' on connection "
                    f"'{grant.connection}' but no column of the exposed tables "
                    f"({', '.join(p.table for p in granted)}) matches it — fix the "
                    "pattern (`table.column`, the table bare or as profiled) or drop it"
                )
    return entities, errors


def expose_summary(
    entities: Sequence[Entity], profiles: Mapping[str, Sequence[TableProfile]] | None = None
) -> str | None:
    """`exposes N tables (M columns)` — the plan's one-line count, or None when
    the lens exposes nothing. With the ``profiles`` the entities resolved from,
    the columns `hide` withheld are counted too: `(M columns, H hidden)`."""
    exposed = [e for e in entities if e.exposed]
    if not exposed:
        return None
    columns = sum(len(e.fields) for e in exposed)
    profiled = {
        (p.connection, p.table): len(p.columns)
        for conn, rows in (profiles or {}).items()
        for p in rows
    }
    hidden = sum(
        max(profiled.get((e.source.connection, e.source.table), len(e.fields)) - len(e.fields), 0)
        for e in exposed
    )
    counts = f"{columns} column{'s' if columns != 1 else ''}"
    if hidden:
        counts += f", {hidden} hidden"
    return f"exposes {len(exposed)} table{'s' if len(exposed) != 1 else ''} ({counts})"


UNTYPED_WARNING = (
    "`expose` is set with untyped_fallback: false — questions about exposed tables "
    "never type, so `query` cannot answer from them (the `sql` tool still can); set "
    "untyped_fallback: true to serve them; answers then carry an EXPOSED line (and an "
    "UNTYPED line only when a typed reading was bypassed)"
)
