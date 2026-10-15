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
    returned SQL nor the rows ever carry a column the lens withholds.

Returns a GuardResult; the pipeline executes only `result.sql` when `ok`.
NOTE: a column a CTE star carries out of a DECLARED entity's table is the one
remaining gap of the column rule (inner stars over declared entities are admitted
and not expanded); table-scope + read-only execution are the backstop there.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from services.contracts.semantic_model import Entity, SemanticModel
from services.contracts.warehouse import VALIDATION_SCHEMA
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
_ROW_GENERATOR_NAMES = [
    "GenerateSeries",
    "ExplodingGenerateSeries",
    "GenerateDateArray",
    "GenerateTimestampArray",
    "Unnest",
    "Explode",
    "Posexplode",
]
_ROW_GENERATORS = tuple(getattr(exp, n) for n in _ROW_GENERATOR_NAMES if hasattr(exp, n))
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


def _star_columns(src: object, columns_of: dict[str, list[str]]) -> list[str] | None:
    """What a star over ``src`` stands for: an exposed table's allowed columns,
    or a derived source's projection names once every one of them is explicit
    and named. None when they are not knowable, which keeps the star."""
    if isinstance(src, exp.Table):
        return columns_of.get(src.name.lower())
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
        e.source.table.split(".")[-1].lower(): [f.name for f in e.fields]
        for e in model.entities
        if e.exposed
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
    """A bare `*` / `t.*` in what the CALLER receives. Scanning the whole
    statement instead refused the CTE-staging idiom — a star inside a
    CTE/subquery under an explicit outer projection leaks nothing, because the
    outer projection is what comes back. An unclassifiable root falls back to
    scanning everything, so an unknown shape fails closed."""
    projections = _output_projections(stmt)
    for subtree in [stmt] if projections is None else projections:
        for star in subtree.find_all(exp.Star):
            if isinstance(star.parent, exp.Select | exp.Column):
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
    entity_tables: dict[str, str | None] = {}
    for e in model.entities:
        for ref in {e.name.lower(), e.source.table.split(".")[-1].lower()}:
            claimed = entity_tables.get(ref, e.source.table)
            entity_tables[ref] = e.source.table if claimed == e.source.table else None

    alias_to_table: dict[str, str] = {}
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
        alias_to_table[name] = name
        if tbl.alias:
            alias_to_table[tbl.alias.lower()] = name

    # Every table is an exposed entity: a star is made explicit here, in every
    # scope, so what comes back is the table's allowed columns and nothing the
    # lens hides. Runs after the table walk so the aliases it set are the
    # qualifiers the expansion uses. A shape whose scopes cannot be built
    # keeps the ban — the one way a star could still reach the caller.
    if all_exposed and (not _expand_exposed_stars(stmt, model) or _has_outer_star(stmt)):
        return GuardResult(False, reason="SELECT * is not allowed; select explicit columns")

    if not trust_tables:
        allowed_cols = {
            t.split(".")[-1].lower(): {c.lower() for c in cols}
            for t, cols in model.allowed_columns().items()
        }
        bad = _out_of_scope_column(stmt, allowed_cols, alias_to_table)
        if bad:
            return GuardResult(False, reason=f"column '{bad}' is out of lens scope")

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
    stmt: exp.Expression, allowed_cols: dict[str, set[str]], alias_to_table: dict[str, str]
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

    resolved: dict[int, str | None] = {}  # id(column) -> physical table, None = derived
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
                        resolved[id(col)] = src.name.lower() if isinstance(src, exp.Table) else None
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
        phys = resolved.get(id(col), alias_to_table.get(tref))
        if phys and phys in allowed_cols and col.name.lower() not in allowed_cols[phys]:
            return f"{tref}.{col.name}"
    return bare_bad


def _bare_column_in_scope(col: exp.Column, scope: Scope, allowed_cols: dict[str, set[str]]) -> bool:
    """Whether an unqualified column is admissible in the scope it sits in —
    the rule `_out_of_scope_column` states. Under-detection is the accepted
    bias: any source in sight that could legitimately carry the name admits it."""
    name = col.name.lower()
    physical_in_sight = False
    current: Scope | None = scope
    while current is not None:
        select = current.expression
        if isinstance(select, exp.Select) and name in {s.lower() for s in select.named_selects}:
            # A projection alias read from outside the projection list
            # (`GROUP BY total`, an engine that admits aliases in WHERE); a
            # column INSIDE the list is never its own alias.
            if not any(col is c for p in select.expressions for c in p.find_all(exp.Column)):
                return True
        for src in current.sources.values():
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
                table = src.name.lower()
                if table in allowed_cols:
                    physical_in_sight = True
                    if name in allowed_cols[table]:
                        return True
        current = current.parent
    return not physical_in_sight


def _is_star(projection: exp.Expr) -> bool:
    """`*` or `t.*` as a projection."""
    inner = projection.unalias()
    return isinstance(inner, exp.Star) or (
        isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)
    )
