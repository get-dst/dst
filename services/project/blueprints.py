"""The checks on table blueprints (semantic/tables/), run by plan and re-run by apply.

A blueprint is a set of declared facts about one table. A false one is worse
than none, so every fact the probe or one live count can test is tested:

- errors (a false declaration; apply aborts on any): a key whose rows are not
  unique, a column the table does not have, a blueprint for a table no
  connection holds, a join whose `on` names a column that does not exist;
- warnings: an in-scope table (read by an entity or an `expose:` grant) with
  no declared grain and key, a declared key that could not be verified, with
  the reason, and blueprint text that names a column a lens hides (every door
  serves it withheld);
- hints: a likely join no blueprint declares, a join certified SQL uses that
  no blueprint declares, a likely sync or delete column with no `versions`.

A coverage line counts the in-scope tables with a full blueprint (grain and
key). A missing blueprint never fails a plan.

``check_blueprints`` is pure (profiles and a counter in, findings out);
``gather`` reads the store the way plan and apply both need it.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from services.contracts.profile import TableProfile, is_numeric_type, is_text_type
from services.contracts.semantic_model import (
    WITHHELD_COLUMN,
    TableBlueprint,
    blueprint_mentions,
    hidden_names,
    withheld_columns,
)
from services.project.lineage import same_table
from services.runtime.grain import KeyCount, key_label, probe_count

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from services.contracts.lens_config import LensConfig
    from services.contracts.semantic_model import Entity
    from services.contracts.shared_semantic import SharedEntity, SharedRelationship

# Counts a table's rows and distinct key tuples live: (connection, table,
# columns) -> KeyCount. None when no live count may run (describe).
LiveCounter = Callable[[str, str, Sequence[str]], KeyCount]


@dataclass(frozen=True)
class Verdict:
    """One check on one blueprint, as describe and plan show it."""

    check: Literal["table", "columns", "key", "joins"]
    status: Literal["pass", "fail", "unverified"]
    detail: str


@dataclass
class Findings:
    errors: dict[str, list[str]] = field(default_factory=dict)  # blueprint table -> errors
    # A stored blueprint this push did not add or change that no longer holds:
    # the warehouse moved under it. A warning, never an abort (it would block
    # every unrelated lens on a warehouse change); answers reading the table
    # carry it on their grain check.
    drifted: dict[str, list[str]] = field(default_factory=dict)
    verdicts: dict[str, list[Verdict]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    covered: int = 0
    in_scope: int = 0

    def coverage_line(self) -> str:
        return (
            f"table blueprints: {self.covered} of {self.in_scope} in-scope table(s) "
            "declare grain and key"
        )


@dataclass(frozen=True)
class ScopedTable:
    """A table something reads: an entity, or a lens's `expose:` grant."""

    connection: str
    table: str
    readers: tuple[str, ...]
    # The entity on it declares grain and primary_key itself.
    entity_declares: bool = False
    entity_key: tuple[str, ...] = ()
    # The columns a reader withholds on it (lowercased): a lens's `hide`.
    hidden: tuple[str, ...] = ()


def _find(profiles: Sequence[TableProfile], table: str) -> TableProfile | None:
    exact = next((p for p in profiles if p.table.lower() == table.lower()), None)
    return exact or next((p for p in profiles if same_table(p.table, table)), None)


def _columns(profile: TableProfile) -> dict[str, str]:
    return {c.name.lower(): c.name for c in profile.columns}


def _missing_column(
    where: str, column: str, profile: TableProfile, hidden: frozenset[str] = frozenset()
) -> str:
    # A hidden column is never offered as the near miss.
    names = [c for c in _columns(profile) if c not in hidden]
    near = difflib.get_close_matches(column.lower(), names, n=1)
    hint = f" — did you mean '{_columns(profile)[near[0]]}'?" if near else ""
    return f"{where} '{column}' is not a column of {profile.table} as last probed{hint}"


def on_columns(on: str) -> list[tuple[str | None, str]]:
    """``(qualifier, column)`` for every column a join condition names."""
    import sqlglot
    from sqlglot import exp

    try:
        parsed = sqlglot.parse_one(on)
    except Exception:  # noqa: BLE001 — an unparseable condition names nothing checkable
        return []
    return [(c.table or None, c.name) for c in parsed.find_all(exp.Column)]


def _bare(table: str) -> str:
    return table.rsplit(".", 1)[-1].lower()


def blueprint_verdicts(
    blueprint: TableBlueprint,
    profiles: Sequence[TableProfile],
    *,
    known_connection: bool,
    fresh: Callable[[TableProfile], bool],
    live: LiveCounter | None,
    hidden: frozenset[str] = frozenset(),
) -> tuple[list[Verdict], list[str], list[str]]:
    """``(verdicts, errors, unverified)`` for one blueprint against its
    connection's profiles. ``hidden`` columns are never named in a verdict."""
    verdicts: list[Verdict] = []
    errors: list[str] = []
    unverified: list[str] = []
    where = f"{blueprint.table} on '{blueprint.connection}'"
    if not known_connection:
        message = (
            f"blueprint for {blueprint.table} names connection '{blueprint.connection}', "
            "which dst.yaml does not declare"
        )
        errors.append(message)
        return [Verdict("table", "fail", message)], errors, unverified
    if not profiles:
        message = (
            f"could not verify {where}: the connection has no probe artifact — run "
            "`dst probe` and push it"
        )
        unverified.append(message)
        return [Verdict("table", "unverified", message)], errors, unverified
    profile = _find(profiles, blueprint.table)
    if profile is None:
        message = (
            f"blueprint for {blueprint.table}: connection '{blueprint.connection}' has no "
            "such table as last probed — fix the name (as `dst introspect` prints it) or "
            "re-run `dst probe`"
        )
        errors.append(message)
        return [Verdict("table", "fail", message)], errors, unverified
    verdicts.append(Verdict("table", "pass", f"{profile.table} exists"))

    columns = _columns(profile)
    missing = [
        _missing_column(label, column, profile, hidden)
        for label, column in blueprint.own_columns()
        if column.lower() not in columns
    ]
    errors += missing
    verdicts.append(
        Verdict("columns", "fail", "; ".join(missing))
        if missing
        else Verdict("columns", "pass", "every declared column exists")
    )

    join_errors: list[str] = []
    for join in blueprint.joins:
        target = _find(profiles, join.to)
        if target is None:
            join_errors.append(
                f"join to {join.to}: connection '{blueprint.connection}' has no such table"
            )
            continue
        mine, theirs = _columns(profile), _columns(target)
        for qualifier, column in on_columns(join.on):
            q = (qualifier or "").lower()
            if q and q in (_bare(blueprint.table), blueprint.table.lower()):
                pool = mine
            elif q and q in (_bare(join.to), join.to.lower()):
                pool = theirs
            elif q:
                join_errors.append(
                    f"join to {join.to}: `on` qualifies '{column}' with '{qualifier}', "
                    f"which is neither {_bare(blueprint.table)} nor {_bare(join.to)}"
                )
                continue
            else:
                pool = {**theirs, **mine}
            if column.lower() not in pool:
                join_errors.append(
                    f"join to {join.to}: `on` names '{column}', which is not a column of "
                    f"{qualifier or 'either table'}"
                )
    errors += join_errors
    if blueprint.joins:
        verdicts.append(
            Verdict("joins", "fail", "; ".join(join_errors))
            if join_errors
            else Verdict("joins", "pass", "every join's columns exist")
        )

    if blueprint.key and not missing:
        unique_on = blueprint.unique_on()
        label = key_label(unique_on, hidden)
        count = probe_count(profile, unique_on, fresh=fresh(profile))
        if not count.counted and live is not None:
            counted = live(blueprint.connection, profile.table, unique_on)
            count = (
                counted
                if counted.counted
                else KeyCount(skipped=f"{count.skipped}, and {counted.skipped}")
            )
        if not count.counted:
            message = f"could not verify that {label} is unique in {profile.table}: " + (
                str(count.skipped)
                if live is not None
                else f"{count.skipped}; plan and apply count it live"
            )
            unverified.append(message)
            verdicts.append(Verdict("key", "unverified", message))
        elif count.rows != count.distinct:
            source = "the probe" if count.source == "probe" else "a live count"
            message = (
                f"key [{label}] is not unique in {profile.table}: {count.rows:,} rows, "
                f"{count.distinct:,} distinct ({source})"
            )
            if blueprint.versions is None:
                message += " — if rows repeat once per sync, declare `versions: {sync_column: ...}`"
            errors.append(message)
            verdicts.append(Verdict("key", "fail", message))
        else:
            source = "the probe" if count.source == "probe" else "a live count"
            verdicts.append(
                Verdict(
                    "key",
                    "pass",
                    f"[{label}] is unique: {count.rows:,} rows, {count.distinct:,} distinct "
                    f"({source})",
                )
            )
    return verdicts, errors, unverified


# A column that flags a deleted record, and one that stamps a sync or load.
_DELETED = re.compile(r"(^|_)(is_)?deleted($|_)")
_SYNCED = re.compile(r"(^_|_)(synced|sync|loaded|extracted|batched|ingested)(_at|_on|_ts)?$")


def _is_bool(type_name: str) -> bool:
    return type_name.strip().upper() in ("BOOLEAN", "BOOL")


def _repeating_key(profile: TableProfile, key: Sequence[str]) -> str | None:
    """A candidate key whose exact distinct count is below the row count: the
    declared one-column key, else `id` / `<table>_id`."""
    if profile.row_count is None or profile.sampled_rows is not None:
        return None
    bare = _bare(profile.table)
    names = [key[0]] if len(key) == 1 else ["id", f"{bare}_id", f"{bare.rstrip('s')}_id"]
    for column in profile.columns:
        if column.name.lower() not in [n.lower() for n in names]:
            continue
        if (
            column.distinct_is_exact
            and column.distinct_count is not None
            and column.distinct_count < profile.row_count
        ):
            return column.name
    return None


def _same_family(a: str, b: str) -> bool:
    return (is_numeric_type(a) and is_numeric_type(b)) or (is_text_type(a) and is_text_type(b))


def check_blueprints(
    blueprints: Mapping[str, TableBlueprint],
    *,
    connections: Iterable[str],
    profiles: Mapping[str, Sequence[TableProfile]],
    scope: Sequence[ScopedTable],
    fresh: Callable[[TableProfile], bool],
    live: LiveCounter | None,
    declared_joins: Iterable[tuple[str, str]] = (),
    certified: Sequence[tuple[str, str, str]] = (),
    introduced: Iterable[str] | None = None,
) -> Findings:
    """Every check over the project's blueprints. ``profiles`` maps a connection
    to its table profiles (store with the push layered on); ``scope`` is every
    table something reads; ``declared_joins`` are table pairs a shared
    relationship already joins; ``certified`` is ``(connection, dialect, sql)``
    per certified answer. ``introduced`` names the blueprints this push adds or
    changes: only their failures are errors, any other's are ``drifted``
    (None: every blueprint is being introduced)."""
    new = None if introduced is None else set(introduced)
    known = set(connections)
    out = Findings()
    for name, blueprint in sorted(blueprints.items()):
        verdicts, errors, unverified = blueprint_verdicts(
            blueprint,
            list(profiles.get(blueprint.connection, ())),
            known_connection=blueprint.connection in known,
            fresh=fresh,
            live=live,
        )
        out.verdicts[name] = verdicts
        if errors and (new is None or name in new):
            out.errors[name] = errors
        elif errors:
            out.drifted[name] = errors
        out.warnings += unverified

    def blueprint_of(connection: str, table: str) -> TableBlueprint | None:
        return next(
            (
                b
                for b in blueprints.values()
                if b.connection == connection and same_table(b.table, table)
            ),
            None,
        )

    out.in_scope = len(scope)
    bare_tables: list[str] = []
    for item in scope:
        found = blueprint_of(item.connection, item.table)
        if found is not None and found.full:
            out.covered += 1
            continue
        if item.entity_declares:
            continue
        bare_tables.append(f"{item.table} ({', '.join(item.readers)})")
    if bare_tables:
        out.warnings.append(
            f"{len(bare_tables)} in-scope table(s) declare no grain and key: "
            + "; ".join(bare_tables)
            + " — add semantic/tables/<table>.yaml with `grain` and `key`; until then an "
            "answer that reads one cannot be checked for repeated rows"
        )

    out.warnings += _hidden_mention_warnings(blueprints, profiles, scope)
    out.hints += _sync_hints(scope, profiles, blueprint_of)
    out.hints += _join_hints(scope, profiles, blueprint_of, set(declared_joins))
    out.hints += _certified_join_hints(certified, blueprints)
    return out


def _hidden_mention_warnings(
    blueprints: Mapping[str, TableBlueprint],
    profiles: Mapping[str, Sequence[TableProfile]],
    scope: Sequence[ScopedTable],
) -> list[str]:
    """Blueprint text (grain, caveats, clock meanings, join conditions) that
    names a column some reader hides. Compile serves it withheld; the author
    learns here, by name, so the text can be reworded."""
    hidden: dict[str, set[str]] = {}
    for item in scope:
        if item.hidden:
            hidden.setdefault(item.table, set()).update(item.hidden)
    if not hidden:
        return []
    out: list[str] = []
    for _name, blueprint in sorted(blueprints.items()):
        profile = _find(list(profiles.get(blueprint.connection, ())), blueprint.table)
        own = next((cs for t, cs in hidden.items() if same_table(t, blueprint.table)), set())
        readable = [c.name for c in profile.columns if c.name.lower() not in own] if profile else []
        keyed = {
            (blueprint.table if same_table(t, blueprint.table) else t): cs
            for t, cs in hidden.items()
        }
        mentions = blueprint_mentions(blueprint, hidden_names(blueprint.table, keyed, readable))
        if mentions:
            out.append(
                f"blueprint for {blueprint.table} names a column a lens hides "
                f"({'; '.join(mentions)}): every door serves {WITHHELD_COLUMN} in its place — "
                "reword the text so it reads without the name"
            )
    return out


def _sync_hints(
    scope: Sequence[ScopedTable],
    profiles: Mapping[str, Sequence[TableProfile]],
    blueprint_of: Callable[[str, str], TableBlueprint | None],
) -> list[str]:
    out: list[str] = []
    for item in scope:
        blueprint = blueprint_of(item.connection, item.table)
        if blueprint is not None and blueprint.versions is not None:
            continue
        profile = _find(list(profiles.get(item.connection, ())), item.table)
        if profile is None:
            continue
        repeating = _repeating_key(profile, blueprint.key if blueprint else item.entity_key)
        if repeating is None:
            continue
        flags = [
            c.name
            for c in profile.columns
            if (_DELETED.search(c.name.lower()) and _is_bool(c.type))
            or _SYNCED.search(c.name.lower())
        ]
        if flags:
            out.append(
                f"{profile.table}: '{repeating}' repeats ({profile.row_count:,} rows) and "
                f"{', '.join(flags)} look like sync or delete columns — if rows repeat once "
                "per sync, declare `versions: {sync_column: ..., latest_only: [...]}` in its "
                "blueprint"
            )
    return out


# A source column's probed values that must be values of the key, when the
# key's dictionary is complete: a foreign key may carry a few orphans.
_CONTAINED = 0.95
# Join hints listed per table; the rest are counted, never dropped silently.
JOIN_HINTS_PER_TABLE = 3


def _system(blueprint: TableBlueprint | None) -> str | None:
    if blueprint is None or not blueprint.system:
        return None
    return " ".join(re.findall(r"[a-z0-9]+", blueprint.system.lower())) or None


def _join_hints(
    scope: Sequence[ScopedTable],
    profiles: Mapping[str, Sequence[TableProfile]],
    blueprint_of: Callable[[str, str], TableBlueprint | None],
    declared: set[tuple[str, str]],
) -> list[str]:
    """A column named like another in-scope table's one-column key, on a
    blueprinted table that declares no join to it, hinted only on evidence:
    the probe shows both tables non-empty and some of the column's values
    among the key's (all but a few of them when the key's dictionary is
    complete). Tables of two different declared systems are never paired.
    Candidates are ranked by how much of the column the key holds, the first
    ``JOIN_HINTS_PER_TABLE`` per table listed and the rest counted; a
    candidate the probe holds no values for is counted, never hinted."""
    keyed: list[tuple[ScopedTable, TableProfile, str]] = []
    for item in scope:
        blueprint = blueprint_of(item.connection, item.table)
        key = blueprint.record_key() if blueprint is not None else list(item.entity_key)
        profile = _find(list(profiles.get(item.connection, ())), item.table)
        if profile is not None and len(key) == 1:
            keyed.append((item, profile, key[0]))
    out: list[str] = []
    blind = empty = 0
    for item in scope:
        blueprint = blueprint_of(item.connection, item.table)
        if blueprint is None:
            continue
        profile = _find(list(profiles.get(item.connection, ())), item.table)
        if profile is None:
            continue
        joined = {_bare(j.to) for j in blueprint.joins}
        ranked: list[tuple[tuple[float, int, int], str]] = []
        for other, other_profile, other_key in keyed:
            if other.connection != item.connection or same_table(other.table, item.table):
                continue
            if _bare(other.table) in joined:
                continue
            other_blueprint = blueprint_of(other.connection, other.table)
            if other_blueprint is not None and _bare(item.table) in {
                _bare(j.to) for j in other_blueprint.joins
            }:
                continue
            if any(
                (same_table(a, item.table) and same_table(b, other.table))
                or (same_table(b, item.table) and same_table(a, other.table))
                for a, b in declared
            ):
                continue
            mine, theirs = _system(blueprint), _system(other_blueprint)
            if mine and theirs and mine != theirs:
                continue
            stem = _bare(other.table).rstrip("s")
            names = {other_key.lower(), f"{stem}_{other_key.lower()}"}
            key_column = next(
                (c for c in other_profile.columns if c.name.lower() == other_key.lower()), None
            )
            if key_column is None:
                continue
            for column in profile.columns:
                if column.name.lower() not in names or not _same_family(
                    column.type, key_column.type
                ):
                    continue
                if not profile.row_count or not other_profile.row_count:
                    empty += 1
                    continue
                values, keys = set(column.top_values or ()), set(key_column.top_values or ())
                if not values or not keys:
                    blind += 1
                    continue
                shared = len(values & keys)
                contained = shared / len(values)
                if not shared or (key_column.values_complete and contained < _CONTAINED):
                    continue  # the values say no
                exact = column.name.lower() == other_key.lower()
                ranked.append(
                    (
                        (contained, shared, int(exact)),
                        f"{profile.table}.{column.name} looks like a join to "
                        f"{other_profile.table}.{key_column.name} ({shared} of {len(values)} "
                        f"probed value(s) of it are values of the key) — declare it under "
                        f"`joins:` in {profile.table}'s blueprint, with its cardinality",
                    )
                )
        ranked.sort(key=lambda r: r[0], reverse=True)
        out += [line for _rank, line in ranked[:JOIN_HINTS_PER_TABLE]]
        if len(ranked) > JOIN_HINTS_PER_TABLE:
            out.append(
                f"{profile.table}: {len(ranked) - JOIN_HINTS_PER_TABLE} more likely join(s) "
                "with fewer of its values in the key are not listed — declare the ones that "
                "hold under `joins:`"
            )
    if blind or empty:
        out.append(
            f"join candidates not hinted: {blind} column(s) named like another table's key "
            f"whose values the probe does not hold on both sides, and {empty} where the "
            "probe shows one of the tables empty — a name alone is no evidence of a join; "
            "declare the joins you know under `joins:`"
        )
    return out


def _certified_join_hints(
    certified: Sequence[tuple[str, str, str]], blueprints: Mapping[str, TableBlueprint]
) -> list[str]:
    """A table pair certified SQL joins that no blueprint declares a join for."""
    import sqlglot
    from sqlglot import exp

    seen: set[tuple[str, str]] = set()
    out: list[str] = []
    for connection, dialect, sql in certified:
        try:
            statement = sqlglot.parse_one(sql, read=dialect)
        except Exception:  # noqa: BLE001 — an unparseable answer joins nothing checkable
            continue
        ctes = {c.alias_or_name.lower() for c in statement.find_all(exp.CTE)}
        for select in statement.find_all(exp.Select):
            names = {
                t.alias_or_name.lower(): ".".join(p for p in (t.catalog, t.db, t.name) if p)
                for t in select.find_all(exp.Table)
                if t.name.lower() not in ctes and t.find_ancestor(exp.Select) is select
            }
            source = select.args.get("from")
            first = (
                names.get(source.this.alias_or_name.lower())
                if source is not None and isinstance(source.this, exp.Table)
                else None
            )
            pairs: list[tuple[str, str]] = []
            for join in select.args.get("joins") or []:
                if not isinstance(join.this, exp.Table):
                    continue
                right = names.get(join.this.alias_or_name.lower())
                if right is None:
                    continue
                condition = join.args.get("on")
                lefts = {
                    names[c.table.lower()]
                    for c in (condition.find_all(exp.Column) if condition is not None else [])
                    if c.table and c.table.lower() in names
                } - {right}
                if not lefts and first is not None:
                    lefts = {first}
                pairs += [(left, right) for left in sorted(lefts)]
            for left, right in pairs:
                if same_table(left, right):
                    continue
                mine = [
                    b
                    for b in blueprints.values()
                    if b.connection == connection
                    and (same_table(b.table, left) or same_table(b.table, right))
                ]
                if not mine:
                    continue
                declared = any(
                    same_table(j.to, right if same_table(b.table, left) else left)
                    for b in mine
                    for j in b.joins
                )
                key = (min(_bare(left), _bare(right)), max(_bare(left), _bare(right)))
                if declared or key in seen:
                    continue
                seen.add(key)
                out.append(
                    f"certified SQL joins {left} and {right}, and no blueprint declares "
                    "that join — add it under `joins:` with its cardinality"
                )
    return out


# ── reading the project the way plan and apply both need it ──────────────────


@dataclass
class ProjectTables:
    blueprints: dict[str, TableBlueprint]
    connections: set[str]
    dialects: dict[str, str]
    profiles: dict[str, list[TableProfile]]
    scope: list[ScopedTable]
    declared_joins: list[tuple[str, str]]
    certified: list[tuple[str, str, str]]
    max_age: timedelta | None


_Entry = tuple[str, str, str, bool, tuple[str, ...], tuple[str, ...]]


def _scope(entries: Iterable[_Entry]) -> list[ScopedTable]:
    """Fold ``(connection, table, reader, entity_declares, key, hidden)`` rows
    into one row per table."""
    out: list[ScopedTable] = []
    for connection, table, reader, declares, key, hidden in entries:
        i = next(
            (
                n
                for n, t in enumerate(out)
                if t.connection == connection and same_table(t.table, table)
            ),
            None,
        )
        if i is None:
            out.append(ScopedTable(connection, table, (reader,), declares, key, hidden))
            continue
        old = out[i]
        out[i] = ScopedTable(
            old.connection,
            old.table,
            old.readers if reader in old.readers else (*old.readers, reader),
            old.entity_declares or declares,
            old.entity_key or key,
            tuple(sorted({*old.hidden, *hidden})),
        )
    return out


def gather(
    session: Session,
    *,
    incoming_entities: Mapping[str, SharedEntity],
    incoming_relationships: Mapping[str, SharedRelationship],
    incoming_blueprints: Mapping[str, TableBlueprint],
    incoming_configs: Mapping[str, LensConfig],
    incoming_certified: Mapping[str, list[str]],
    incoming_profiles: Mapping[str, Sequence[TableProfile]],
    incoming_connections: Mapping[str, str] | None,
) -> ProjectTables:
    """The stored project with the push layered on, exactly as apply lands it:
    connections, profiles, entities, relationships, blueprints, the lenses'
    expose grants and the certified SQL. ``incoming_connections`` maps a
    declared connection to its type; ``incoming_certified`` a pushed lens to
    the SQL of the certified answers its file carries."""
    from services.certify import store as certify_store
    from services.lenses import connection_store, profile_store, store
    from services.project import apply as apply_engine
    from services.project import probe_settings
    from services.project.expose import resolve_expose

    blueprints = {**apply_engine.stored_blueprints(session), **incoming_blueprints}
    types = {c.name: c.type for c in connection_store.list_connections(session)}
    types.update(incoming_connections or {})
    profiles: dict[str, list[TableProfile]] = {}
    for connection in types:
        stored = {
            p.profile.table: p.profile for p in profile_store.list_profiles(session, connection)
        }
        stored.update({p.table: p for p in incoming_profiles.get(connection, ())})
        profiles[connection] = list(stored.values())

    def withheld(e: Entity) -> tuple[str, ...]:
        profile = _find(profiles.get(e.source.connection, []), e.source.table)
        columns = {c.name for c in profile.columns} if profile else set()
        return tuple(sorted(withheld_columns(e, columns | {f.name for f in e.fields})))

    entities, _definitions, relationships = apply_engine._shared_layer(session)
    entities.update(incoming_entities)
    relationships.update(incoming_relationships)
    entries: list[_Entry] = [
        (
            e.source.connection,
            e.source.table,
            f"entity {e.name}",
            bool(e.grain and e.primary_key),
            tuple(e.primary_key),
            withheld(e),
        )
        for e in entities.values()
    ]
    published = {name: bundle for name, _dn, _desc, bundle in store.list_published(session)}
    for name, config in sorted(incoming_configs.items()):
        if not config.expose:
            continue
        grant_profiles, dropped = apply_engine._expose_profiles(
            session, config, incoming=incoming_profiles
        )
        exposed, _errors = resolve_expose(
            config,
            grant_profiles,
            declared=[p.name for p in config.select.entities],
            dropped=dropped,
        )
        entries += [
            (e.source.connection, e.source.table, f"lens {name} exposes", False, (), withheld(e))
            for e in exposed
        ]
    for name, bundle in sorted(published.items()):
        if name in incoming_configs:
            continue
        entries += [
            (e.source.connection, e.source.table, f"lens {name} exposes", False, (), withheld(e))
            for e in bundle.semantic_model.entities
            if e.exposed
        ]
    joins = [
        (entities[r.left].source.table, entities[r.right].source.table)
        for r in relationships.values()
        if r.left in entities and r.right in entities
    ]

    certified: list[tuple[str, str, str]] = []
    for name, sqls in incoming_certified.items():
        pushed = incoming_configs.get(name)
        connection = pushed.connections[0] if pushed and pushed.connections else ""
        certified += [(connection, types.get(connection, "duckdb"), sql) for sql in sqls]
    for name, bundle in published.items():
        if name in incoming_certified or not bundle.config.connections:
            continue
        connection = bundle.config.connections[0]
        certified += [
            (connection, bundle.semantic_model.dialect, a.sql)
            for a in certify_store.list_for_lens(session, name)
            if certify_store.is_active(a.status) and a.sql
        ]
    return ProjectTables(
        blueprints=blueprints,
        connections=set(types),
        dialects=types,
        profiles=profiles,
        scope=_scope(entries),
        declared_joins=joins,
        certified=certified,
        max_age=probe_settings.load(session).max_age_delta,
    )


def fresh_within(
    max_age: timedelta | None, now: datetime | None = None
) -> Callable[[TableProfile], bool]:
    """A profile is fresh within ``probe.max_age`` (always, with the rails off)."""
    moment = now or datetime.now(UTC)

    def fresh(profile: TableProfile) -> bool:
        if max_age is None:
            return True
        stamp = profile.profiled_at
        stamp = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
        return moment - stamp <= max_age

    return fresh


def live_counter(session: Session, applied: set[str]) -> LiveCounter:
    """A counter over the org's applied connections. A connection the push only
    declares is not built here (apply lands it first and counts there)."""
    from services.lenses.connections import resolve_connector
    from services.runtime.grain import live_count

    cache: dict[str, object] = {}

    def count(connection: str, table: str, columns: Sequence[str]) -> KeyCount:
        if connection not in applied:
            return KeyCount(
                skipped=f"connection '{connection}' is not applied yet, so apply counts it"
            )
        if connection not in cache:
            try:
                # On the caller's session: an apply sees the connection it staged.
                cache[connection] = resolve_connector(connection, session=session)
            except Exception as exc:  # noqa: BLE001 — an unbuildable connection is a skip, said
                cache[connection] = f"connection '{connection}' could not be opened: {exc}"
        connector = cache[connection]
        if isinstance(connector, str):
            return KeyCount(skipped=connector)
        return live_count(connector, table, columns, connection=connection)  # type: ignore[arg-type]

    return count


def run(
    session: Session,
    project: ProjectTables,
    *,
    introduced: Iterable[str],
    live: bool = True,
) -> Findings:
    """``check_blueprints`` over a gathered project, with live counts on the
    org's applied connections when ``live``. A drifted blueprint becomes a
    warning naming its table and the lenses reading it."""
    from services.lenses import connection_store

    applied = {c.name for c in connection_store.list_connections(session)}
    findings = check_blueprints(
        project.blueprints,
        connections=project.connections,
        profiles=project.profiles,
        scope=project.scope,
        fresh=fresh_within(project.max_age),
        live=live_counter(session, applied) if live else None,
        declared_joins=project.declared_joins,
        certified=project.certified,
        introduced=introduced,
    )
    for name, errors in sorted(findings.drifted.items()):
        findings.warnings.append(drift_warning(project.blueprints[name], errors, session))
    return findings


def lenses_reading(session: Session, table: str) -> list[str]:
    """Published lenses whose compiled model reads *table*."""
    from services.lenses import store

    return sorted(
        name
        for name, _dn, _desc, bundle in store.list_published(session)
        if any(same_table(e.source.table, table) for e in bundle.semantic_model.entities)
    )


def drift_warning(blueprint: TableBlueprint, errors: Sequence[str], session: Session) -> str:
    """The line for a stored blueprint the warehouse moved under."""
    readers = lenses_reading(session, blueprint.table)
    who = f"lenses reading it: {', '.join(readers)}" if readers else "no lens reads it"
    key_broke = any(" is not unique in " in e for e in errors)
    effect = (
        "answers that read it grade unverified on the grain check"
        if key_broke
        else "the grain check on answers that read it cannot run"
    )
    return (
        f"DRIFT: {blueprint.table}: its blueprint no longer holds ({'; '.join(errors)}); "
        f"{who}; {effect} until the blueprint or the data is fixed"
    )
