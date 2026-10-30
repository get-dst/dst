"""sql_guard — validate LLM-generated SQL against a lens's semantic model.

Security boundary (first line; the read-only warehouse credential is the backstop).
Enforces, via sqlglot:
  - a single statement, SELECT-only (no DML/DDL anywhere, incl. inside CTEs);
  - every physical table referenced is in the lens's allowed tables; a bare
    entity-name reference is equally canonical and is resolved to
    `<source.table> AS <entity>` in the returned SQL — and so is a bare TABLE
    name whose source is schema-qualified: any unqualified relation
    target that unambiguously names exactly one in-scope source is rewritten
    source-qualified, instead of reaching the warehouse as a name its catalog
    cannot find. CTE references, derived-relation aliases, already-qualified
    tables, and names two sources claim are never rewritten;
  - every column referencing an allowed table is in its allowed columns —
    resolved per SCOPE, so a CTE's computed projection is not mistaken for a
    physical column of a same-named table. A qualified column resolves through
    its qualifier; an UNQUALIFIED one through the physical tables its scope (and
    the scopes above it) can see — in scope when at least one of them lists the
    name, out of scope when none does, and left alone only when no physical
    table is in sight (a pure CTE/derived scope). Bare was the door a hidden or
    unmodeled column walked through: `SELECT ssn FROM customers` passed while
    `SELECT c.ssn FROM customers c` was refused;
  - a FROM target that is not a table name passes only when it reads nothing:
    a derived relation (LATERAL/subquery) or a row generator. A data-source
    function (read_csv, read_parquet, dblink) is a table the lens never modeled;
  - no bare ``SELECT *`` / ``t.*`` in the OUTERMOST projection (every branch of a
    set operation counts) — that is what the caller receives. A star confined to a
    CTE/subquery — bare or table-qualified — is allowed: the outer projection
    names the columns that come back, and the inner scope's tables are
    allow-listed like any other. The one lens-scoped exception: a statement
    whose every table is an EXPOSED entity. There a star is not banned but
    REWRITTEN — in every scope — into the explicit allowed columns of the
    table it reads (its fields as profiled, minus `hide`), so neither the
    returned SQL nor the rows ever carry a column the lens withholds. DuckDB
    `* LIKE '...'`, `COLUMNS('regex')` and a positional `#3` are stars too;
  - no column `hide` withholds is read, on every door, trusted or not
    (`hidden_reads`): every reference is resolved through CTEs, derived
    tables, set operations, laterals and correlation to the base column it
    reads, so `WITH x AS (SELECT * FROM t) SELECT x.hidden FROM x` is the
    hidden column; a star, `COLUMNS(...)`, positional or whole-row read whose
    columns reach the caller or are consumed by position is refused over any
    entity that hides one.

Returns a GuardResult; the pipeline executes only `result.sql` when `ok`.
NOTE: a column a CTE star carries out of a DECLARED entity's table that the
entity neither models nor hides is the one remaining gap of the column rule
(inner stars over declared entities are admitted and not expanded); `hide` is
how a column is withheld, and table scope + read-only execution are the
backstop for the rest.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, ScopeType, build_scope, walk_in_scope

from services.contracts.failure_modes import FailureMode
from services.contracts.semantic_model import Entity, SemanticModel, column_hidden
from services.contracts.warehouse import VALIDATION_SCHEMA
from services.runtime import external_access
from services.runtime.identifiers import is_reserved, quote_bare_keywords

_FORBIDDEN_NAMES = [
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "TruncateTable",
    "Command",
    "Set",
    "Pragma",
    "Copy",
    "Use",
]
_FORBIDDEN = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))

# A FROM/JOIN target is not always a table NAME: sqlglot also wraps a function
# call in exp.Table (`FROM generate_series(...) AS d(m)` — a date spine — parses
# to Table(this=GenerateSeries)). Such a node's `.name` is the empty string, and
# the allow-list used to reject it as "table '' is out of lens scope".
# These generators materialize rows out of values already IN the query, so they
# read nothing and the allow-list has no jurisdiction over them. The membership
# is by sqlglot CLASS, never by function name: an unrecognized function parses to
# exp.Anonymous, and a UDTF is free to call itself `generate_series`.
# Everything else that arrives as a function IS a data source — duckdb's
# read_csv / read_parquet, postgres dblink, snowflake IDENTIFIER() — and stays
# rejected. Widening this set widens what a lens can read.
_ROW_GENERATORS = external_access.ROW_GENERATORS
# ...as is a subquery / LATERAL / VALUES target: derived, and every physical
# table inside it is walked separately, so nothing escapes the allow-list.
_DERIVED_TARGETS = (exp.Subquery, exp.Lateral, exp.Values) + _ROW_GENERATORS


@dataclass
class GuardResult:
    ok: bool
    sql: str | None = None
    reason: str | None = None
    # What KIND of failure: "policy" = a governance verdict (out-of-scope
    # table, forbidden operation) — a governed outcome; "syntax" = dst emitted
    # SQL that is not valid SQL — a dst FAULT. Sharing one status for both
    # books dst defects as governed outcomes: the health metric inverts (a
    # generation regression reads as governance being appropriately cautious),
    # the repair loop tells the model it violated a policy when it broke syntax,
    # and callers are told their in-scope question was out of scope.
    kind: str = "policy"
    # The failure mode a policy verdict carries (contracts/failure_modes.py):
    # the scope the SQL left, the column the lens hides, or the reach past the
    # warehouse. A syntax verdict is a fault and carries none.
    mode: FailureMode | None = None

    def __post_init__(self) -> None:
        if not self.ok and self.kind == "policy" and self.mode is None:
            self.mode = FailureMode.scope_violation


def _non_table_target(target: exp.Expression) -> str | None:
    """Why to reject a FROM/JOIN target that is not a table NAME — or None when
    it is a derived relation the allow-list does not govern (a LATERAL/subquery
    target, a row generator): any physical table inside one is walked separately,
    so nothing escapes the allow-list. Everything else fails closed."""
    if isinstance(target, _DERIVED_TARGETS):
        return None
    if isinstance(target, exp.Func):
        name = target.name if isinstance(target, exp.Anonymous) else target.sql_name()
        return f"table function '{name}' is not allowed"
    return f"unsupported FROM target ({type(target).__name__})"


def _output_projections(node: exp.Expression | exp.Query) -> list[exp.Expression] | None:
    """The projections that reach the caller: the outermost SELECT's, and — for a
    set operation — every branch's, since each contributes rows to the result.

    A CTE's or subquery's own projections are deliberately NOT here: they feed the
    outermost projection rather than being it, so a star among them exposes
    nothing (their tables are still walked against the allow-list). Returns None
    for a root shape this walk does not recognize; the caller then scans the whole
    statement, so an unknown shape fails closed.

    The walk is iterative on purpose: set operations chain left-deep, and a
    recursive version raised RecursionError out of check() at ~1000 UNION
    branches (measured) — a security boundary answers, it does not crash."""
    out: list[exp.Expression] = []
    stack: list[exp.Expression | exp.Query] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, exp.Subquery):
            stack.append(current.this)
        elif isinstance(current, exp.SetOperation):
            stack.extend((current.left, current.right))
        elif isinstance(current, exp.Select):
            out.extend(current.expressions)
        else:
            return None
    return out


def widen_single_precision(stmt: exp.Expr) -> None:
    """Rewrite every cast to a 4-byte float into the dialect's 8-byte one, in place.

    `REAL` (and DuckDB/MySQL/BigQuery `FLOAT`) is single-precision, so the same ratio
    computes to a different number than `DOUBLE`: 2000/2465 is 0.8113590263691683 at
    8 bytes and 0.8113590478897095 at 4. That is enough to fork otherwise identical
    runs: some answer with the float32 value because the generated SQL happened to
    say `CAST(... AS REAL)`, the rest with `DOUBLE`.

    Rewritten rather than rejected on purpose. A rejection would cost a repair
    round-trip and still depend on the model taking the hint, whereas widening is
    deterministic, total, and never loses information: DOUBLE represents every REAL
    exactly, so no query means something different afterwards. sqlglot renders the
    right spelling per dialect (DOUBLE PRECISION on postgres, FLOAT64 on bigquery),
    and because this sits in the guard every served statement passes through it —
    certified SQL (trust_tables) included, where a human approved a cast that forks
    just as readily.
    """
    for cast in stmt.find_all(exp.Cast):  # exp.TryCast subclasses exp.Cast
        if cast.to.this is exp.DataType.Type.FLOAT:
            cast.set("to", exp.DataType.build("DOUBLE"))


def _names_table(written: str, allowed: str) -> bool:
    """Does a written table reference denote the allowed (source) table?

    A reference may OMIT leading qualifiers the allowed table has — `orders`
    names `analytics.orders`, `analytics.orders` names `acme.analytics.orders`
    (the compiler stamps the catalog a connection pins, so the shorter authored
    form still binds) — and may never CARRY a qualifier the allowed table does
    not have: `staging.orders` is not `orders`, and `other.analytics.orders` is
    not `acme.analytics.orders`. Plain tail matching let a written reference
    ADD a schema, so a table the lens never modeled was served whenever its
    bare name happened to match an unqualified source."""
    a = written.lower().split(".")
    b = allowed.lower().split(".")
    return len(a) <= len(b) and b[-len(a) :] == a


def _owners(written: str, model: SemanticModel) -> list[Entity]:
    """The entities a written table reference denotes: by source table, or by
    the entity's own name when the reference is bare."""
    return [
        e
        for e in model.entities
        if _names_table(written, e.source.table)
        or ("." not in written and written == e.name.lower())
    ]


def _physical_refs(stmt: exp.Expr, cte_names: set[str]) -> list[str]:
    """Every physical table reference in the statement, as written and
    lower-cased; CTE references and non-name targets are not tables."""
    out: list[str] = []
    for tbl in stmt.find_all(exp.Table):
        if not isinstance(tbl.this, exp.Identifier):
            continue
        if tbl.name.lower() in cte_names and not tbl.db and not tbl.catalog:
            continue
        out.append(".".join(p.lower() for p in (tbl.catalog, tbl.db, tbl.name) if p))
    return out


def _all_exposed(stmt: exp.Expression, model: SemanticModel, cte_names: set[str]) -> bool:
    """True when every table the statement reads is an exposed entity — the
    case where a star is rewritten into the table's allowed columns instead of
    banned. Any other table (a declared entity, an unknown name) keeps the star
    ban; so does a statement reading no table at all."""
    refs = _physical_refs(stmt, cte_names)
    if not refs:
        return False
    for written in refs:
        owners = _owners(written, model)
        if not owners or not all(e.exposed for e in owners):
            return False
    return True


def _written(tbl: exp.Table) -> str:
    """A table reference as written, every qualifier it carries, lower-cased."""
    return ".".join(p.lower() for p in (tbl.catalog, tbl.db, tbl.name) if p)


def _table_keys(tbl: exp.Table, tables: Iterable[str]) -> list[str]:
    """The allowed source tables (``tables``, lower-cased and qualified as the
    sources name them) a physical reference denotes. Columns are scoped per
    source table, never per bare name: two tables sharing one (`sales.targets`,
    `raw.targets`) each keep their own columns, and a reference that could mean
    either (a bare `targets`) is held to both."""
    if not isinstance(tbl.this, exp.Identifier):
        return []
    written = _written(tbl)
    return [t for t in tables if _names_table(written, t)]


def _star_columns(src: object, columns_of: dict[str, list[str]]) -> list[str] | None:
    """What a star over ``src`` stands for: an exposed table's allowed columns,
    or a derived source's projection names once every one of them is explicit
    and named. None when they are not knowable — a reference two exposed
    tables answer to among them — which keeps the star."""
    if isinstance(src, exp.Table):
        keys = _table_keys(src, columns_of)
        return columns_of[keys[0]] if len(keys) == 1 else None
    if isinstance(src, Scope) and isinstance(src.expression, exp.Query):
        names = [e.alias_or_name for e in src.expression.selects]
        if names and all(names) and not any(_is_star(e) for e in src.expression.selects):
            return names
    return None


def _expand_exposed_stars(stmt: exp.Expression, model: SemanticModel) -> bool:
    """Rewrite every `*` / `t.*` into the columns it stands for, in place, in
    every scope: an exposed table's allowed columns, or — for a CTE/subquery
    source — the names its projection lists, which the same walk made explicit
    first (scopes are visited innermost first). A star it cannot resolve stays,
    and the caller refuses the statement if one is left in the outer projection.
    Returns False when scopes cannot be built, so the caller fails closed."""
    columns_of = {
        e.source.table.lower(): [f.name for f in e.fields] for e in model.entities if e.exposed
    }
    try:
        root = build_scope(stmt)
    except Exception:  # noqa: BLE001 — an unresolvable shape keeps the ban
        return False
    if root is None:
        return False
    for scope in root.traverse():
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        sources = scope.selected_sources
        # In FROM/JOIN order, which is the order a bare star hands columns back
        # (`selected_sources` groups by kind).
        from_ = select.args.get("from_")
        relations = [from_.this] if from_ is not None else []
        relations += [join.this for join in select.args.get("joins") or []]
        ordered = [r.alias_or_name for r in relations if r.alias_or_name in sources]
        ordered += [name for name in sources if name not in ordered]
        projections: list[exp.Expression] = []
        changed = False
        for proj in select.expressions:
            if isinstance(proj, exp.Star):
                targets = [(name, sources[name][1]) for name in ordered]
                qualify = len(targets) > 1
                bare_star = True
            elif isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                qualifier = proj.table
                targets = [(qualifier, sources[qualifier][1])] if qualifier in sources else []
                qualify = True
                bare_star = False
            else:
                projections.append(proj)
                continue
            if not targets:
                projections.append(proj)
                continue
            for name, src in targets:
                cols = _star_columns(src, columns_of)
                if cols:
                    projections.extend(
                        exp.column(col, table=name if qualify else None) for col in cols
                    )
                    changed = True
                elif bare_star and len(targets) > 1:
                    projections.append(exp.column(exp.Star(), table=name))
                    changed = True
                else:
                    projections.append(proj)
        if changed:
            select.set("expressions", projections)
    return True


def exposed_tables(sql: str | None, model: SemanticModel) -> list[str]:
    """The exposed tables the served SQL reads, as their sources name them —
    what the EXPOSED disclosure lists. Resolution is the guard's own (a
    reference denotes a table when it omits no more than the source's leading
    qualifiers, or names the entity); unparseable SQL reads nothing, and says
    nothing."""
    if not sql or not sql.strip():
        return []
    try:
        stmt = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001 — a disclosure never raises
        return []
    cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    out: list[str] = []
    for written in _physical_refs(stmt, cte_names):
        for e in _owners(written, model):
            if e.exposed and e.source.table not in out:
                out.append(e.source.table)
    return sorted(out)


def entities_read(sql: str | None, model: SemanticModel) -> list[Entity]:
    """The entities the SQL reads, in source order, by the guard's own
    resolution (a reference denotes a table when it omits no more than the
    source's leading qualifiers, or names the entity); unparseable SQL reads
    nothing."""
    if not sql or not sql.strip():
        return []
    try:
        stmt = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001 — a read of the SQL never raises
        return []
    cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    out: list[Entity] = []
    for written in _physical_refs(stmt, cte_names):
        for e in _owners(written, model):
            if all(e.name != o.name for o in out):
                out.append(e)
    return out


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_QUOTED = re.compile(r"'(?:[^']|'')*'")


def _columns_read(
    stmt: exp.Expr | None,
    sql: str,
    model: SemanticModel,
    pred: Callable[[str, Entity], bool],
) -> list[str]:
    """``entity.column`` for every column reference the statement makes that
    *pred* holds for, on an entity the statement reads.

    Resolution leans toward a hit: a column qualified by an entity's table or
    alias is judged on that entity, any other column (unqualified, or
    qualified by a CTE or derived table) on every entity the statement reads.
    A statement that does not parse is read word by word — a certified
    template's ``{slot}`` placeholders are the usual reason — so a column the
    lens withholds is never missed because the SQL could not be walked."""
    if stmt is None:
        cleaned = _QUOTED.sub(" ", sql)
        words = {w.lower() for w in _WORD.findall(cleaned)}
        read = [
            e
            for e in model.entities
            if {e.name.lower(), e.source.table.rsplit(".", 1)[-1].lower()} & words
        ]
        return sorted({f"{e.name}.{w}" for e in read for w in words if pred(w, e)})
    cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    by_ref: dict[str, list[Entity]] = {}
    read_entities: list[Entity] = []
    for tbl in stmt.find_all(exp.Table):
        if not isinstance(tbl.this, exp.Identifier):
            continue
        if tbl.name.lower() in cte_names and not tbl.db and not tbl.catalog:
            continue
        written = ".".join(p.lower() for p in (tbl.catalog, tbl.db, tbl.name) if p)
        for e in _owners(written, model):
            if all(e.name != o.name for o in read_entities):
                read_entities.append(e)
            for ref in {tbl.name.lower(), tbl.alias.lower()} - {""}:
                by_ref.setdefault(ref, []).append(e)
    hits: set[str] = set()
    for col in stmt.find_all(exp.Column):
        name = col.name
        if not name:
            continue
        qualifier = col.table.lower() if col.table else ""
        candidates = by_ref.get(qualifier) or read_entities
        hits |= {f"{e.name}.{name}" for e in candidates if pred(name, e)}
    return sorted(hits)


def _parsed(sql: str, model: SemanticModel) -> exp.Expr | None:
    try:
        return sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001 — the word reading takes over
        return None


def withheld_reads(sql: str | None, model: SemanticModel) -> list[str]:
    """``entity.column`` for each column the SQL reads that the model's `hide`
    withholds — on a caller's view (``SemanticModel.for_groups``), the ones
    withheld from that caller. What keeps pre-approved SQL (a certified
    answer, whose tables the guard trusts) from serving a hidden column."""
    if not sql or not sql.strip() or not any(_withholds(e) for e in model.entities):
        return []
    stmt = _parsed(sql, model)
    if stmt is None:
        return _columns_read(None, sql, model, _withheld)
    return hidden_reads(stmt, model)


def granted_reads(sql: str | None, model: SemanticModel) -> list[str]:
    """The view's ``column_grants`` entries whose column the SQL reads — what
    an answer's receipt names as shown by a group exception."""
    if not sql or not model.column_grants:
        return []
    by_column = {g.split(" ", 1)[0].lower(): g for g in model.column_grants}
    read = _columns_read(
        _parsed(sql, model),
        sql,
        model,
        lambda column, e: f"{e.name}.{column}".lower() in by_column,
    )
    return sorted({by_column[r.lower()] for r in read})


def _withholds(entity: Entity) -> bool:
    """Does the entity withhold any column? Its `hide`, or — on an exposed
    table compiled before a grant's `hide` was recorded on the entity — the
    `exposed_whole` flag saying the grant withheld something."""
    return bool(entity.hide) or (entity.exposed and not entity.exposed_whole)


def _withheld(column: str, entity: Entity) -> bool:
    """Is ``column`` withheld? On an exposed table with a withholding grant
    and no recorded `hide` (a lens applied by an earlier release), every
    column it does not list is: the grant's patterns did not survive the
    compile, and failing closed beats serving what one of them named."""
    if column_hidden(column, entity):
        return True
    return (
        entity.exposed
        and not entity.exposed_whole
        and not entity.hide
        and column.lower() not in {f.name.lower() for f in entity.fields}
    )


def _hide_owners(tbl: exp.Table, model: SemanticModel) -> list[Entity]:
    """The hiding entities a physical table reference may denote. Looser than
    the allow-list's `_owners` on purpose: trusted SQL is not held to the
    lens's spelling of a table, so a reference that ADDS a qualifier the
    source lacks (`warehouse.bakery.patrons` over `bakery.patrons`) still
    carries the entity's `hide`."""
    if not isinstance(tbl.this, exp.Identifier):
        return []
    written = ".".join(p.lower() for p in (tbl.catalog, tbl.db, tbl.name) if p)
    return [
        e
        for e in model.entities
        if _withholds(e)
        and (
            _names_table(written, e.source.table)
            or _names_table(e.source.table, written)
            or ("." not in written and written == e.name.lower())
        )
    ]


# A projection that stands for columns it does not name: a star (bare, `t.*`,
# DuckDB `* LIKE '...'`), DuckDB `COLUMNS('regex')`, a positional `#3` / `$3`.
_IMPLICIT = (exp.Star, exp.Columns, exp.PositionalColumn)


def _implicit(node: exp.Expr) -> bool:
    return isinstance(node, _IMPLICIT) or (
        isinstance(node, exp.Parameter) and isinstance(node.this, exp.Literal)
    )


class _HiddenReads:
    """Every column reference resolved to the base-table columns it reads,
    through CTEs, derived tables, set operations and laterals, and checked
    against `hide` — the one place both doors ask (`hidden_reads`).

    An explicit reference is checked where it is written: a CTE that projects
    `town` has its `town` checked inside the CTE. What needs resolution is a
    name a derived source carries without writing it — through a star — and a
    reference that takes columns without naming them: a star or a whole-row
    reference (`TO_JSON(p)`) whose output reaches the caller or is consumed
    by position. The first is followed down to the tables the star reads; the
    second reads every column of its sources, so any hiding entity among them
    is a hit, recorded as ``entity.*``."""

    def __init__(self, model: SemanticModel) -> None:
        self.model = model
        self.hits: set[str] = set()
        self.seen: set[tuple[int, str]] = set()
        self.branch: dict[int, tuple[Scope, int]] = {}

    # -- sources -------------------------------------------------------------
    def table(self, tbl: exp.Table, column: str | None) -> None:
        for e in _hide_owners(tbl, self.model):
            if column is None:
                self.hits.add(f"{e.name}.*")
            elif _withheld(column, e):
                self.hits.add(f"{e.name}.{column}")

    def carry(self, src: object, column: str) -> None:
        """``column`` read off ``src`` by name."""
        if isinstance(src, exp.Table):
            self.table(src, column)
            return
        if not isinstance(src, Scope) or (id(src), column.lower()) in self.seen:
            return
        self.seen.add((id(src), column.lower()))
        if isinstance(src.expression, exp.SetOperation):
            # A set operation's names are its first branch's; the other
            # branches feed by position, which `implicit_star` rules on.
            self.carry(_branches(src)[0], column)
            return
        select = src.expression
        if not isinstance(select, exp.Select):
            for inner in src.sources.values():  # a lateral / UDTF
                if isinstance(inner, Scope):
                    self.carry(inner, column)
            return
        if _renamed(src):
            return  # positional: `implicit_star` reads its stars whole
        if any(p.alias_or_name.lower() == column.lower() for p in select.expressions):
            return  # explicit: checked inside its own scope
        implicit = [
            node
            for proj in select.expressions
            for node in proj.find_all(*_IMPLICIT, exp.Parameter)
            if _implicit(node)
        ]
        if not implicit:
            # Named by nothing the source projects (a star the guard already
            # made explicit, a typo): the statement cannot run, and reading the
            # name off the source's tables names the column if it is hidden.
            for target in self.everything(src):
                self.carry(target, column)
        for node in implicit:
            if isinstance(node, exp.Star) and _star_drops(node, column):
                continue
            for target in self.targets(src, node):
                self.carry(target, column)

    def whole(self, src: object) -> None:
        """Every column of ``src`` read."""
        if isinstance(src, exp.Table):
            self.table(src, None)
            return
        if not isinstance(src, Scope) or (id(src), "*") in self.seen:
            return
        self.seen.add((id(src), "*"))
        if isinstance(src.expression, exp.SetOperation):
            for branch in _branches(src):
                self.whole(branch)
            return
        select = src.expression
        if not isinstance(select, exp.Select):
            for inner in src.sources.values():
                if isinstance(inner, Scope):
                    self.whole(inner)
            return
        for proj in select.expressions:
            for node in proj.find_all(*_IMPLICIT, exp.Parameter):
                if _implicit(node):
                    for target in self.targets(src, node):
                        self.whole(target)

    def targets(self, scope: Scope, node: exp.Expr) -> list[object]:
        """The sources an implicit projection reads: `t.*` its qualifier's,
        anything else every source its SELECT reads from."""
        qualifier = node.parent.table if isinstance(node.parent, exp.Column) else ""
        if isinstance(node, exp.Star) and qualifier:
            src = _lookup(scope, qualifier)
            return [src] if src is not None else self.everything(scope)
        return self.everything(scope)

    def everything(self, scope: Scope) -> list[object]:
        try:
            return [src for _, src in scope.selected_sources.values()]
        except Exception:  # noqa: BLE001 — an alias used twice: every source
            return list(scope.sources.values())

    # -- references ----------------------------------------------------------
    def column(self, scope: Scope, col: exp.Column) -> None:
        parts = [p.name for p in col.parts]
        # `a.b.c` is `table.column.field` when `a` names a source, `schema.table.
        # column` when `b` does, and a struct path off column `a` when none does.
        for i in range(len(parts) - 2, -1, -1):
            src = _lookup(scope, parts[i])
            if src is not None:
                self.carry(src, parts[i + 1])
                return
        # No part names a source: a struct path off column `a`, or a qualifier
        # nothing binds — every part is read as a column, so none is missed.
        for part in parts:
            self.bare(scope, part)

    def bare(self, scope: Scope, name: str) -> None:
        """An unqualified name: carried by whatever source in sight could hold
        it — this scope's, then (correlated) each scope above it until one
        names it explicitly; and a whole-row reference when it names a source
        (`TO_JSON(p)`, BigQuery `SELECT p FROM patrons p`)."""
        current: Scope | None = scope
        while current is not None:
            sources = self.everything(current)
            for src in sources:
                self.carry(src, name)
            row = _lookup(current, name, here_only=True)
            if row is not None and not _provides(row, name, self.model):
                self.whole(row)
            if any(_provides(src, name, self.model) for src in sources):
                return
            current = current.parent

    def implicit_star(self, scope: Scope, node: exp.Expr) -> None:
        """A star (or `COLUMNS`) read whole unless its SELECT feeds a source
        consumed by name — a CTE, derived table or lateral under no column
        rename, not a later set-operation branch — or an EXISTS, or it is
        `COUNT(*)`. A positional read is always read whole."""
        if isinstance(node.parent, exp.Count):
            return
        projection = node
        while projection.parent is not None and projection.parent is not scope.expression:
            projection = projection.parent
        in_projection = projection.arg_key == "expressions" and (
            node is projection.unalias()
            or (isinstance(node.parent, exp.Column) and node.parent is projection.unalias())
            or (isinstance(node.parent, exp.Like) and node.parent is projection.unalias())
        )
        # A positional read (`#3`, `$3`) takes a column by number, never by
        # name, so no consumer by name can tell what it carries.
        named = isinstance(node, exp.Star | exp.Columns)
        if named and in_projection and self.by_name(scope):
            return
        for target in self.targets(scope, node):
            self.whole(target)

    def by_name(self, scope: Scope) -> bool:
        current = scope
        while id(current) in self.branch:
            union, index = self.branch[id(current)]
            if index:
                return False
            current = union
        if current.is_root or _renamed(current):
            return False
        if current.is_subquery:
            parent = current.expression.parent
            if isinstance(parent, exp.Subquery):
                parent = parent.parent
            # EXISTS ignores what its subquery projects; a LATERAL subquery
            # is a source read by name like a derived table. Any other
            # subquery (IN, scalar) is consumed by position.
            return isinstance(parent, exp.Exists | exp.Lateral)
        return True

    def run(self, root: Scope) -> None:
        scopes = list(root.traverse())
        for scope in scopes:
            if isinstance(scope.expression, exp.SetOperation):
                branches = _branches(scope)
                if len(branches) != 2:
                    # A sqlglot whose scopes this walk cannot read: the caller
                    # falls back to reading names, which fails closed.
                    raise ValueError("set operation without two branch scopes")
                for i, branch in enumerate(branches):
                    self.branch[id(branch)] = (scope, i)
        for scope in scopes:
            for node in walk_in_scope(scope.expression):
                if isinstance(node, exp.Column):
                    if isinstance(node.this, exp.Star):
                        continue
                    if node.find_ancestor(exp.Star) is not None and node.arg_key == "except_":
                        continue  # `* EXCLUDE (c)` drops c, it does not read it
                    self.column(scope, node)
                elif _implicit(node):
                    self.implicit_star(scope, node)
                elif isinstance(node, exp.Join):
                    for ident in node.args.get("using") or []:
                        self.bare(scope, ident.name)
                    if (node.args.get("method") or "").upper() == "NATURAL":
                        # Joined on every shared column, named or not.
                        for src in self.everything(scope):
                            self.whole(src)


def _lookup(scope: Scope, name: str, *, here_only: bool = False) -> object | None:
    """The source ``name`` denotes from ``scope`` — its own, then (correlated)
    the scopes above it."""
    current: Scope | None = scope
    while current is not None:
        for key, src in current.sources.items():
            if key.lower() == name.lower():
                return src
        if here_only:
            return None
        current = current.parent
    return None


def _branches(scope: Scope) -> list[Scope]:
    """A set operation scope's two branch scopes (`union_scopes` until
    sqlglot renamed it `set_operation_scopes`)."""
    branches: list[Scope] = getattr(scope, "set_operation_scopes", None) or getattr(
        scope, "union_scopes", []
    )
    return branches


def _renamed(scope: Scope) -> bool:
    """A CTE or derived table whose alias renames its columns by position
    (`WITH x(a, b) AS ...`, `(...) AS d(a, b)`)."""
    parent = scope.expression.parent
    alias = parent.args.get("alias") if isinstance(parent, exp.CTE | exp.Subquery) else None
    return bool(alias and alias.columns)


def _star_drops(star: exp.Star, column: str) -> bool:
    """Does `* EXCLUDE (c)` / `* REPLACE (expr AS c)` take ``column`` off the star?"""
    excluded = [c.name for c in star.args.get("except_") or []]
    replaced = [a.alias for a in star.args.get("replace") or []]
    return column.lower() in {n.lower() for n in excluded + replaced}


def _provides(src: object, name: str, model: SemanticModel) -> bool:
    """Does ``src`` name ``name`` as a column it carries — a modelled field or
    dimension of the table, or an explicit projection of a derived source?"""
    lowered = name.lower()
    if isinstance(src, exp.Table):
        return any(
            lowered in {f.name.lower() for f in e.fields} | {d.name.lower() for d in e.dimensions}
            for e in _owners(
                ".".join(p.lower() for p in (src.catalog, src.db, src.name) if p), model
            )
        )
    if isinstance(src, Scope) and isinstance(src.expression, exp.Select):
        return lowered in {p.alias_or_name.lower() for p in src.expression.expressions}
    return False


def hidden_reads(stmt: exp.Expr, model: SemanticModel) -> list[str]:
    """``entity.column`` for every column ``stmt`` reads that the model's
    `hide` withholds, resolved through CTEs, derived tables, set operations,
    laterals and correlation to the base table it comes from; ``entity.*`` for
    a star, `COLUMNS(...)`, positional or whole-row read that takes every
    column of a hiding entity. A shape whose scopes cannot be built or walked is
    read by name on every entity the statement reads, and any star in it reads
    every hiding entity whole, so nothing is missed."""
    if not any(_withholds(e) for e in model.entities):
        return []
    reads = _HiddenReads(model)
    try:
        root = build_scope(stmt)
        if root is not None:
            reads.run(root)
    except Exception:  # noqa: BLE001 — the name reading takes over
        root = None
    if root is None:
        hiding = {
            ref: e
            for tbl in stmt.find_all(exp.Table)
            for e in _hide_owners(tbl, model)
            for ref in {tbl.name.lower(), tbl.alias.lower()} - {""}
        }
        whole = any(
            _implicit(n) and not isinstance(n.parent, exp.Count) for n in stmt.walk()
        ) or any(not c.table and c.name.lower() in hiding for c in stmt.find_all(exp.Column))
        return _columns_read(stmt, "", model, _withheld) or (
            sorted({f"{e.name}.*" for e in hiding.values()}) if whole else []
        )
    return sorted(reads.hits)


def _as_words(name: str) -> str:
    return " ".join(name.lower().replace("_", " ").replace("-", " ").split())


def not_computable_hit(sql: str, model: SemanticModel) -> dict[str, Any] | None:
    """The `not_computable` entry a caller-written statement runs into, or None.

    The governed door refuses a QUESTION that names a declared not-computable
    measure; this is the same rail for a statement, where the only names are
    columns. Two shapes hit: a column the entry names — its measure or an
    alias, underscores read as spaces, so `birth_date` is 'birth date' —
    anywhere in the statement (projected or filtered: a filter on it is a
    lookup); and, for an entry declared ``row_level``, a key column in the
    outer projection outside any aggregate — one row per key IS the listing
    the entry refuses, whatever the columns beside it are called. The keys are
    the entry's own or the lens's entity primary keys. Unparseable SQL hits
    nothing: the guard proper refuses it first."""
    if not model.not_computable:
        return None
    try:
        stmt = sqlglot.parse_one(sql, read=model.dialect)
    except Exception:  # noqa: BLE001 — the guard's parse names the fault
        return None
    named = {
        _as_words(c.name) for c in stmt.find_all(exp.Column) if not isinstance(c.this, exp.Star)
    }
    projections: list[Any] = _output_projections(stmt) or []  # type: ignore[arg-type]
    row_keys = {
        c.name.lower()
        for proj in projections
        for c in proj.find_all(exp.Column)
        if not isinstance(c.this, exp.Star) and not _under_aggregate(c, proj)
    }
    entity_keys = {k.lower() for e in model.entities for k in e.primary_key}
    for entry in model.not_computable:
        aliases = entry.get("aliases")
        surfaces = {
            _as_words(str(s))
            for s in [entry.get("measure") or "", *(aliases if isinstance(aliases, list) else [])]
            if str(s).strip()
        }
        if surfaces & named:
            return entry
        if entry.get("row_level"):
            keys = {str(k).lower() for k in entry.get("keys") or []} or entity_keys
            if keys & row_keys:
                return entry
    return None


def _under_aggregate(col: exp.Column, projection: exp.Expression) -> bool:
    """Is the column inside an aggregate within this projection? `COUNT(DISTINCT
    patient_id)` is a count, not a listing; bare `patient_id` is the listing."""
    node: Any = col.parent
    while node is not None and node is not projection.parent:
        if isinstance(node, exp.AggFunc):
            return True
        node = node.parent
    return False


def _lens_names(model: SemanticModel) -> set[str]:
    """Every identifier the lens owns — exactly what `serialize_model` shows the
    generator and so exactly what it can copy back at us, bare."""
    names: set[str] = set()
    for e in model.entities:
        names.add(e.name)
        names.update(e.source.table.split("."))
        names.update(f.name for f in e.fields)
        names.update(d.name for d in e.dimensions)
        names.update(m.name for m in e.metrics)
    return names


# dialect -> {FUNCTION: the fix to name in the guard message}. Only functions
# the engine will 400 on belong here — sqlglot either parses them as Anonymous
# (DATE_FORMAT, GETDATE, CURDATE, NOW) or as typed nodes it re-renders
# UNCHANGED for the dialect (Year/Month/Day/Quarter), so no parse-based check
# sees them, and the dry-run failure reads like a data-team problem rather
# than a generation one. BigQuery is the case that bites in practice. DATEDIFF
# is deliberately absent: sqlglot transpiles it correctly to DATE_DIFF.
_DIALECT_DENYLIST: dict[str, dict[str, str]] = {
    "bigquery": {
        "YEAR": "EXTRACT(YEAR FROM …)",
        "MONTH": "EXTRACT(MONTH FROM …)",
        "DAY": "EXTRACT(DAY FROM …)",
        "QUARTER": "EXTRACT(QUARTER FROM …)",
        "DATE_FORMAT": "FORMAT_DATE(format, …)",
        "GETDATE": "CURRENT_DATE",
        "CURDATE": "CURRENT_DATE",
        "NOW": "CURRENT_TIMESTAMP",
    },
}
# The typed-node halves of the deny-list: sqlglot recognizes these, then
# renders them verbatim into a dialect that does not have them.
_DENY_TYPED: dict[str, tuple[type[exp.Expression], ...]] = {
    "bigquery": (exp.Year, exp.Month, exp.Day, exp.Quarter),
}


def _has_outer_star(stmt: exp.Expression) -> bool:
    """A bare `*` / `t.*` in what the CALLER receives — or what stands for one:
    DuckDB `* LIKE '...'`, `COLUMNS('regex')`, a positional `#3`. Scanning the
    whole statement instead refused the CTE-staging idiom — a star inside a
    CTE/subquery under an explicit outer projection leaks nothing, because the
    outer projection is what comes back. An unclassifiable root falls back to
    scanning everything, so an unknown shape fails closed."""
    projections = _output_projections(stmt)
    for subtree in [stmt] if projections is None else projections:
        for node in subtree.find_all(*_IMPLICIT, exp.Parameter):
            if isinstance(node, exp.Star):
                if isinstance(node.parent, exp.Select | exp.Column | exp.Like):
                    return True
            elif _implicit(node):
                return True
    return False


def check(
    sql: str, model: SemanticModel, *, trust_tables: bool = False, allow_star: bool = False
) -> GuardResult:
    """Guard a SQL string against the lens.

    ``trust_tables`` relaxes ONLY the table/column allow-list — for pre-approved
    SQL (a certified answer, a certified-definition page's authored query) a human has already
    blessed the exact tables, so the model never chose them. Every other check
    still applies, including the reserved-schema backstop: the generation path
    stays constrained to the modeled (conformed) tables, while a trusted artifact
    may reach raw tables no entity exposes.

    ``allow_star`` relaxes ONLY the outer-projection star ban, and only for the
    authoring probe over a whole CONNECTION (`dst sql --connection`): there
    the caller is the org admin whose own credential dst holds, `SELECT *
    LIMIT 5` is the entire point of the verb, and a star hides nothing from
    someone who can already `introspect --profile` every column. Never set it on
    a lens-scoped path — there the star IS how a column the lens deliberately
    does not expose comes back."""
    dialect = model.dialect
    # Delimit the lens's own keyword names BEFORE the parse. The backstop at the
    # bottom of this function is the same fix applied after it, and after is too
    # late for the worst shape: `FROM select` parses to `From(this=Select())`,
    # leaving no identifier to delimit, no table for the allow-list to see, and a
    # statement that renders back out as `FROM SELECT`. The biting shape is a
    # reserved-word table in the DEFAULT schema, where the entity name IS the
    # keyword; a qualified variant escapes only because its name is not one.
    sql = quote_bare_keywords(sql, _lens_names(model), dialect)
    # Unbalanced identifier quote, named PRECISELY: a stray
    # backtick opens a runaway quoted identifier that consumes the rest of the
    # statement, so the tokenizer's error quotes text hundreds of chars from
    # the defect — actively misdirecting both the human reading it and the
    # repair model fixing it. Cheapest possible invariant, checked first.
    if dialect in ("bigquery", "mysql") and sql.count("`") % 2:
        return GuardResult(
            False,
            reason=(
                "unbalanced backtick (`) identifier quote — the statement has an odd "
                "number of backticks, so one identifier never closes; re-emit with "
                "every quoted identifier opened and closed (this is a syntax defect, "
                "not a scope problem)"
            ),
            kind="syntax",
        )
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except Exception as exc:
        return GuardResult(False, reason=f"parse error: {exc}", kind="syntax")

    if not statements:
        # `parse("")` returns [] — the generator emitted NOTHING (a reasoning model
        # that spent its budget on thinking, a stripped fence). Saying "exactly one
        # statement is allowed" here blames the SQL for a query that was never
        # produced, which hides a total-failure bug behind a plausible message.
        return GuardResult(
            False, reason="the generator produced no SQL (empty response)", kind="syntax"
        )
    if len(statements) != 1:
        return GuardResult(False, reason="exactly one statement is allowed")
    stmt = statements[0]

    # Cross-dialect functions that PARSE but do not EXIST on the target engine:
    # sqlglot reads YEAR(d) in the bigquery dialect as an anonymous function
    # and re-renders it unchanged, so no parse-based check can see it — the
    # dry run fails, and the failure reads like a warehouse problem rather
    # than a generation one. Caught here instead, with the fix in the message so
    # one repair lands it; only Anonymous nodes are checked (a function
    # sqlglot recognizes gets transpiled correctly at render).
    dialect_key = (model.dialect or "").lower()
    deny = _DIALECT_DENYLIST.get(dialect_key, {})
    if deny:
        hits: list[str] = []
        for node in stmt.find_all(exp.Anonymous):
            name = (node.name or "").upper()
            if name in deny:
                hits.append(name)
        for typed in (
            stmt.find_all(*_DENY_TYPED.get(dialect_key, ())) if dialect_key in _DENY_TYPED else ()
        ):
            hits.append(type(typed).__name__.upper())
        for name in hits:
            return GuardResult(
                False,
                reason=(
                    f"{name}() does not exist in {model.dialect} — use {deny[name]} "
                    "(this is a syntax defect for this dialect, not a scope problem)"
                ),
                kind="syntax",
            )

    # No DML/DDL anywhere (covers hidden statements inside CTEs).
    for node in stmt.find_all(*_FORBIDDEN):
        return GuardResult(
            False, reason=f"non-SELECT operation not allowed ({type(node).__name__})"
        )

    # Nothing that reaches past the warehouse's tables (a file, a URL, another
    # database, SQL passed as a string), on every door: trust_tables relaxes
    # which TABLES may be read, never whether a file may be.
    if reason := external_access.refusal_in(stmt, dialect or ""):
        return GuardResult(False, reason=reason, mode=FailureMode.external_access)

    if not isinstance(stmt, exp.Select | exp.Union | exp.Subquery | exp.With):
        return GuardResult(False, reason="only SELECT queries are allowed")

    cte_names = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}

    # No bare star projections (SELECT * / t.*) in what the CALLER receives.
    # Scanning the whole statement instead refused the CTE-staging idiom — a star
    # inside a CTE/subquery under an explicit outer projection leaks nothing,
    # because the outer projection is what comes back. An unclassifiable root
    # falls back to scanning everything, so an unknown shape fails closed.
    # Exposed-only statements are the exception named in the module doc: their
    # stars are rewritten into the allowed columns after the table walk below,
    # which is also where an unknown table among them is refused.
    all_exposed = not allow_star and _all_exposed(stmt, model, cte_names)
    if not allow_star and not all_exposed and _has_outer_star(stmt):
        return GuardResult(False, reason="SELECT * is not allowed; select explicit columns")

    allowed_bare = {t.split(".")[-1].lower() for t in model.allowed_tables()}
    # Entity aliases are canonical in-scope references — the compiled
    # model presents every table AS its entity name, so generated SQL may say
    # `FROM sessions` for an entity over `web_sessions`. The same rule widened
    # resolution to QUALIFICATION: `FROM prices` over source
    # `analytics.prices` names the right entity, but reaching the warehouse
    # unqualified dies with a catalog error.
    # So every bare name an entity answers to — its name, its
    # source's bare table — maps to the source-qualified table; None marks a
    # name that two different sources claim: ambiguous, never rewritten.
    # An entity's own name outranks another source's bare table name: an
    # exposed table sharing a selected entity's bare name is exposed under
    # its qualified name, and `FROM orders` keeps meaning the entity.
    entity_tables: dict[str, str | None] = {}
    named: dict[str, str | None] = {}
    for e in model.entities:
        for ref, claims in (
            (e.source.table.split(".")[-1].lower(), entity_tables),
            (e.name.lower(), named),
        ):
            claimed = claims.get(ref, e.source.table)
            claims[ref] = e.source.table if claimed == e.source.table else None
    entity_tables.update(named)

    allowed_cols = {
        t.lower(): {c.lower() for c in cols} for t, cols in model.allowed_columns().items()
    }
    alias_to_table: dict[str, list[str]] = {}
    for tbl in stmt.find_all(exp.Table):
        # Hard backstop: dst's in-warehouse validation plane is never readable by a
        # lens, regardless of allow-list (defense in depth — see architecture.md posture).
        if VALIDATION_SCHEMA in (tbl.db.lower(), tbl.catalog.lower()):
            return GuardResult(
                False, reason=f"schema '{VALIDATION_SCHEMA}' is reserved and not queryable"
            )
        if not isinstance(tbl.this, exp.Identifier):
            reason = _non_table_target(tbl.this)
            if reason and not trust_tables:
                return GuardResult(False, reason=reason)
            continue
        name = tbl.name.lower()
        # A CTE shadows only unqualified references — a schema-qualified name
        # always denotes the physical table (CTEs can't be qualified), so a
        # same-named CTE must not smuggle it past the scope check.
        if name in cte_names and not tbl.db and not tbl.catalog:
            continue
        if not trust_tables and not (tbl.db or tbl.catalog):
            # Unqualified reference → resolve to the source-qualified table,
            # aliased back to the written name, so the returned SQL executes
            # against the physical schema wherever it lives. Fires for an
            # entity-name reference and for a bare table name whose
            # source is qualified; the identity case is left as written.
            # Unresolvable keeps the old behavior: an ambiguous in-scope name
            # passes through untouched, an unknown one is out of scope.
            # Certified SQL (trust_tables) is physical by contract — never
            # translated.
            physical = entity_tables.get(name)
            if physical is None and name not in allowed_bare:
                return GuardResult(False, reason=f"table '{tbl.name}' is out of lens scope")
            if physical is not None and physical.lower() != name:
                resolved = exp.to_table(physical, dialect=dialect)
                if not tbl.args.get("alias"):
                    tbl.set("alias", exp.TableAlias(this=exp.to_identifier(tbl.name)))
                tbl.set("this", resolved.this)
                tbl.set("db", resolved.args.get("db"))
                tbl.set("catalog", resolved.args.get("catalog"))
                name = resolved.name.lower()
        elif not trust_tables:
            # A QUALIFIED reference is checked as written, not by its last
            # segment. Comparing bare names let `other-project.marts.orders`
            # through on a lens that models `acme-prod.marts.orders`: same table
            # name, different project, and the rewritten SQL kept the foreign
            # path — so a credential with access to both (the ordinary BigQuery
            # service account) read another project's data through a governed
            # lens, wearing the lens's badge.
            #
            # A reference that OMITS leading qualifiers still names the table;
            # one that CONTRADICTS them never does — and neither does one that
            # ADDS a qualifier the source lacks: `staging.orders` is not the
            # lens's unqualified `orders`, however the bare names agree
            # (`_names_table`).
            written = ".".join(p.lower() for p in (tbl.catalog, tbl.db, tbl.name) if p)
            if not any(_names_table(written, t) for t in model.allowed_tables()):
                return GuardResult(False, reason=f"table '{written}' is out of lens scope")
        keys = _table_keys(tbl, allowed_cols)
        alias_to_table[name] = [*alias_to_table.get(name, []), *keys]
        if tbl.alias:
            alias_to_table[tbl.alias.lower()] = keys

    # Every table is an exposed entity: a star is made explicit here, in every
    # scope, so what comes back is the table's allowed columns and nothing the
    # lens hides. Runs after the table walk so the aliases it set are the
    # qualifiers the expansion uses. A shape whose scopes cannot be built
    # keeps the ban — the one way a star could still reach the caller.
    if all_exposed and (not _expand_exposed_stars(stmt, model) or _has_outer_star(stmt)):
        return GuardResult(False, reason="SELECT * is not allowed; select explicit columns")

    if not trust_tables:
        bad = _out_of_scope_column(stmt, allowed_cols, alias_to_table)
        if bad:
            return GuardResult(False, reason=f"column '{bad}' is out of lens scope")

    # `hide` holds on every door, trusted or not: a trusted statement skips the
    # allow-list above, never `hide` — a certified answer reading a hidden
    # column would serve it to every caller the hide withholds it from (on a
    # caller's view, that caller's hide). And the allow-list cannot see a
    # hidden column a star carries out of a CTE or derived table, so here every
    # reference is resolved to the base column it reads.
    if withheld := hidden_reads(stmt, model):
        entity, _, column = withheld[0].partition(".")
        if column == "*":
            reason = (
                f"a star or whole-row read over '{entity}' would carry a column the "
                "lens hides; select explicit columns"
            )
        elif trust_tables:
            reason = f"column '{withheld[0]}' is hidden by the lens and cannot be read"
        else:
            reason = f"column '{withheld[0]}' is out of lens scope"
        return GuardResult(False, reason=reason, mode=FailureMode.hidden_column)

    # Last stop before the warehouse. sqlglot's generator quotes the keywords IT
    # knows about, and its set is neither complete nor the same across dialects
    # (duckdb quotes `order` and not `group`; postgres and snowflake quote
    # neither). A model that wrote a keyword name bare still parsed to a
    # real identifier here, so it can still be delimited — and a name that is
    # already safe is left exactly as written, byte for byte.
    for ident in stmt.find_all(exp.Identifier):
        if not ident.quoted and is_reserved(ident.this, dialect):
            ident.set("quoted", True)
    widen_single_precision(stmt)
    return GuardResult(True, sql=stmt.sql(dialect=dialect))


def _out_of_scope_column(
    stmt: exp.Expression, allowed_cols: dict[str, set[str]], alias_to_table: dict[str, list[str]]
) -> str | None:
    """First column that is provably a non-modeled column of an allowed PHYSICAL
    table — as "<qualifier>.<column>" when qualified, "<column>" when not — or
    None.

    Resolution is per-scope: `cohort.min_month` where `cohort` is a CTE or a
    derived table is a COMPUTED projection, not a physical column of anything.
    The flat alias map this replaced was global to the statement, so a name bound
    to a physical table in one scope was applied to a same-named CTE in another —
    it rejected legitimate SQL that aggregated inside a CTE and then read the
    aggregate back out. Shadowing stays honest because sqlglot resolves each name
    in its own scope: `WITH customers AS (...) SELECT customers.ssn FROM
    main.customers` still resolves `customers` to the physical table, and the
    unmodeled column is still rejected.

    An UNQUALIFIED column resolves through the sources its scope can see — its
    own and, for a correlated reference, every scope above it. It is in scope
    when a derived source projects the name (or projects a star, so its names
    are unknown), when it names a projection alias of its own SELECT from
    outside the projection list (`GROUP BY total`), or when at least one
    physical table in sight lists it among its allowed columns. With physical
    tables in sight and none of those holding, it is out of scope — the bare
    door a hidden column used to walk through. With no physical table in sight
    (a pure CTE/derived scope) it is left alone.

    A check is only ever DROPPED when a scope proves the qualifier derived; a
    qualifier no scope binds — and every column when sqlglot cannot build scopes
    at all — still goes through the flat map."""
    try:
        root = build_scope(stmt)
    except Exception:  # an unresolvable shape falls back, it never skips the check
        root = None

    resolved: dict[int, list[str]] = {}  # id(column) -> source tables, [] = derived
    bare_bad: str | None = None
    if root is not None:
        claimed: set[int] = set()  # a correlated column is checked where it sits
        for scope in root.traverse():
            sources = {name.lower(): src for name, src in scope.sources.items()}
            for col in scope.columns:
                if isinstance(col.this, exp.Star):
                    continue
                if col.table:
                    src = sources.get(col.table.lower())
                    if src is not None:
                        resolved[id(col)] = (
                            _table_keys(src, allowed_cols) if isinstance(src, exp.Table) else []
                        )
                    continue
                if id(col) in claimed:
                    continue
                claimed.add(id(col))
                if bare_bad is None and not _bare_column_in_scope(col, scope, allowed_cols):
                    bare_bad = col.name

    for col in stmt.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            # A qualified star (`bt.*`) parses as Column(this=Star): a projection
            # SHAPE, not a column NAME. The star rule owns it — and already
            # rejected it in the outermost projection before this walk ran.
            # Confined to an inner scope it is the admitted star idiom with a
            # table prefix, and the table under the prefix still goes through
            # the allow-list walk. Stringifying it here instead rejects
            # in-scope SQL as "column 'bt.*' is out of lens scope".
            continue
        tref = col.table.lower()
        if not tref:
            continue
        keys = resolved.get(id(col), alias_to_table.get(tref, []))
        if any(col.name.lower() not in allowed_cols[k] for k in keys):
            return f"{tref}.{col.name}"
    return bare_bad


def _bare_column_in_scope(col: exp.Column, scope: Scope, allowed_cols: dict[str, set[str]]) -> bool:
    """Whether an unqualified column is admissible in the scope it sits in —
    the rule `_out_of_scope_column` states. Under-detection is the accepted
    bias: any source in sight that could legitimately carry the name admits it.

    In sight means this scope's sources and, only where a reference can be
    correlated (from a subquery, a lateral, a set-operation branch), the
    scopes above it — never the scope a CTE or derived table defines itself
    for: `WITH x AS (SELECT ssn FROM customers)` is not admitted by `x`
    projecting `ssn`, which is the very read being checked."""
    name = col.name.lower()
    physical_in_sight = False
    current: Scope | None = scope
    came_from: Scope | None = None
    while current is not None:
        select = current.expression
        if isinstance(select, exp.Select) and name in {s.lower() for s in select.named_selects}:
            # A projection alias read from outside the projection list
            # (`GROUP BY total`, an engine that admits aliases in WHERE); a
            # column INSIDE the list is never its own alias.
            if not any(col is c for p in select.expressions for c in p.find_all(exp.Column)):
                return True
        for src in current.sources.values():
            if src is came_from:
                continue
            if isinstance(src, Scope):
                projected = src.expression.selects if isinstance(src.expression, exp.Query) else []
                if any(_is_star(e) for e in projected):
                    return True  # a star projection: the derived names are unknown
                if name in {e.alias_or_name.lower() for e in projected}:
                    return True
            elif isinstance(src, exp.Table) and not isinstance(src.this, exp.Identifier):
                # A row generator / table function: its columns are the alias's
                # column list when written, unknown otherwise.
                names = [c.lower() for c in src.alias_column_names]
                if not names or name in names:
                    return True
            elif isinstance(src, exp.Table):
                keys = _table_keys(src, allowed_cols)
                if keys:
                    physical_in_sight = True
                    if all(name in allowed_cols[k] for k in keys):
                        return True
        if not (_is_set_branch(current) or _correlatable(current)):
            break
        came_from, current = current, current.parent
    return not physical_in_sight


def _is_set_branch(scope: Scope) -> bool:
    """A branch of a set operation (`UNION`, `INTERSECT`, `EXCEPT`). Read off
    the parent's expression: sqlglot renamed the scope's own flag between the
    versions this package allows."""
    return scope.parent is not None and isinstance(scope.parent.expression, exp.SetOperation)


def _correlatable(scope: Scope) -> bool:
    """Can a reference in ``scope`` name a column of a scope above it? From a
    subquery or a lateral/table function, or anything nested in one."""
    current: Scope | None = scope
    while current is not None:
        if current.scope_type in (ScopeType.SUBQUERY, ScopeType.UDTF):
            return True
        current = current.parent
    return False


def _is_star(projection: exp.Expr) -> bool:
    """`*` or `t.*` as a projection."""
    inner = projection.unalias()
    return isinstance(inner, exp.Star) or (
        isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)
    )
