"""One comparison for "does this SQL carry that expression?".

A definition's ``sql:`` and the SQL that applies it are written by different
hands (an author, the compiler, a model) and agree on meaning while differing
in form: a table qualifier or alias, quoting, identifier case, the parentheses
the compiler puts around every expanded metric (``(COUNT(t.match_id)) >= 20``),
the order of ANDed conditions, ``COUNT(*)`` against ``COUNT(1)`` or a count of
the key. Compared as text, a correct answer read as a departure from its own
definition, and a different one could read as applied: ``name = 'x'`` is a
substring of ``team_name = 'x'``.

So both sides are parsed and compared as trees, after one canonical pass:
qualifiers and quoting stripped (a column still never matches the same name on
another declared entity), identifiers lowercased (string literals stay
exact: 'Active' and 'active' are different values), parentheses unwrapped (the
tree already holds the grouping they wrote), single precision widened, and a
count of every row of an entity read one way: ``COUNT(*)`` and ``COUNT(1)`` over
the entity a SELECT reads FROM, and ``COUNT(<column of the entity's declared
primary key>)``, are one count, because a key column is never null. The entity
is part of that count: ``COUNT(*)`` over a fact joined to a dimension counts the
fact's rows, never the dimension's. A count of any other column stays itself:
it skips nulls. An expression is carried when an equal subtree occurs; an AND
(or OR) is carried when every one of its operands occurs in one chain of the
same connective, in any order.
"""

from __future__ import annotations

import functools

import sqlglot
from sqlglot import exp

from services.certify.bindings import _ref_matches
from services.contracts.semantic_model import SemanticModel
from services.runtime import sql_guard

# (entity name, source table, primary key) per entity: the hashable part of a
# model this comparison reads.
_KeySpec = tuple[tuple[str, str, tuple[str, ...]], ...]


def _keyspec(model: SemanticModel | None) -> _KeySpec:
    if model is None:
        return ()
    return tuple((e.name, e.source.table, tuple(e.primary_key)) for e in model.entities)


def _table_entity(tbl: exp.Table, keyspec: _KeySpec) -> str | None:
    """The entity (lowercased) whose source table *tbl* names, when exactly one does."""
    ref = ".".join(p for p in (tbl.catalog, tbl.db, tbl.name) if p)
    owners = [name for name, table, _pk in keyspec if _ref_matches(ref, table)]
    return owners[0].lower() if len(owners) == 1 else None


def _qualifiers(tree: exp.Expr, keyspec: _KeySpec) -> dict[str, str]:
    """qualifier (lowercased; '' for a bare column) -> the entity it denotes. An
    entity is named by its own name, by any alias of its source table in *tree*,
    and a bare column belongs to the one entity the statement reads, when it
    reads exactly one."""
    owner = {name.lower(): name.lower() for name, _table, _pk in keyspec}
    read: set[str] = set()
    for tbl in tree.find_all(exp.Table):
        entity = _table_entity(tbl, keyspec)
        if entity is not None:
            read.add(entity)
            owner[(tbl.alias or tbl.name).lower()] = entity
    if len(read) == 1:
        owner[""] = next(iter(read))
    return owner


def _rows_of(count: exp.Count, owner: dict[str, str], keyspec: _KeySpec) -> str | None:
    """The entity whose every row *count* counts, or None when it counts
    something else or the entity cannot be told. COUNT(*) and COUNT(1) count
    the rows of the query's grain: the entity its SELECT reads FROM, never a
    table joined to it. A fragment with no SELECT (a metric's or definition's
    expression) counts the rows of the one entity it names. A count of a
    declared key column counts the rows of that column's entity."""
    arg = count.this
    if isinstance(arg, exp.Star) or (
        isinstance(arg, exp.Literal) and not arg.is_string and arg.this == "1"
    ):
        select = count.find_ancestor(exp.Select)
        if select is not None:
            frm = select.args.get("from_") or select.args.get("from")
            source = frm.this if frm is not None else None
            return _table_entity(source, keyspec) if isinstance(source, exp.Table) else None
        named = {owner.get(c.table.lower()) for c in count.root().find_all(exp.Column)}
        named.discard(None)
        return named.pop() if len(named) == 1 else None
    if isinstance(arg, exp.Column):
        entity = owner.get(arg.table.lower())
        pk = next((pk for name, _t, pk in keyspec if name.lower() == entity), ())
        if arg.name.lower() in {k.lower() for k in pk}:
            return entity
    return None


@functools.lru_cache(maxsize=256)
def _canonical(text: str, dialect: str | None, keyspec: _KeySpec, rows: bool) -> exp.Expr | None:
    """The canonical tree of *text*, or None when it does not parse. With *rows*,
    every count of an entity's rows is spelled one way. Each column remembers the
    entity its qualifier denoted (``meta["entity"]``), which equality ignores and
    ``_same`` reads. Cached: one served answer is compared against every governed
    expression of its lens. Callers never mutate the result."""
    try:
        tree = sqlglot.parse_one(text, read=dialect or None)
    except Exception:  # noqa: BLE001 — unparseable text has no canonical form
        return None
    if tree is None:
        return None
    owner = _qualifiers(tree, keyspec)
    counts = [(c, _rows_of(c, owner, keyspec) if rows else None) for c in tree.find_all(exp.Count)]
    for count, entity in counts:
        arg = count.this
        if entity is not None:
            # One spelling per entity's row count: COUNT(*) over its rows and a
            # count of its key are one count; another entity's rows are not.
            count.set("this", exp.column(f"*rows of {entity}", quoted=True))
        elif isinstance(arg, exp.Literal) and not arg.is_string and arg.this == "1":
            count.set("this", exp.Star())
    for col in tree.find_all(exp.Column):
        col.meta["entity"] = owner.get(col.table.lower())
        col.set("table", None)
        col.set("db", None)
        col.set("catalog", None)
    for ident in tree.find_all(exp.Identifier):
        ident.set("quoted", False)
        ident.set("this", ident.this.lower())
    for paren in list(tree.find_all(exp.Paren)):
        if paren.parent is not None:
            paren.replace(paren.this)
    while isinstance(tree, exp.Paren):
        tree = tree.this
    sql_guard.widen_single_precision(tree)
    return tree


def _operands(node: exp.Expr, kind: type[exp.Connector]) -> list[exp.Expr]:
    """The operands of a chain of one connective (a AND b AND c), flattened."""
    if isinstance(node, kind):
        return [*_operands(node.left, kind), *_operands(node.right, kind)]
    return [node]


def _same(a: exp.Expr, b: exp.Expr) -> bool:
    """Equal trees whose columns do not name two different entities: a column
    whose entity is unknown on either side (an alias no table declares, a bare
    name in a join) matches the same name, but ``orders.customer_id`` is not
    ``customers.customer_id``. Equal trees walk in the same order."""
    if a != b:
        return False
    return all(
        x.meta.get("entity") is None
        or y.meta.get("entity") is None
        or x.meta["entity"] == y.meta["entity"]
        for x, y in zip(a.find_all(exp.Column), b.find_all(exp.Column), strict=False)
    )


def _contains(have: exp.Expr, want: exp.Expr) -> bool:
    kinds: tuple[type[exp.Connector], ...] = (exp.And, exp.Or)
    for kind in kinds:
        if isinstance(want, kind):
            parts = _operands(want, kind)
            chains = [n for n in have.find_all(kind) if not isinstance(n.parent, kind)]
            return any(
                all(any(_same(p, q) for q in _operands(chain, kind)) for p in parts)
                for chain in chains
            )
    return any(_same(node, want) for node in have.find_all(type(want)))


def carries(sql: str, expr: str, model: SemanticModel | None = None) -> bool | None:
    """Does *sql* carry *expr*, compared canonically (see the module docstring)?
    None when either side does not parse: the caller decides what the text alone
    can say. *model* supplies the dialect and the declared entities.

    Compared twice: plainly, and with every count of an entity's rows spelled
    one way. Either is a match. The second needs each side's count resolved to
    its entity, which a fragment with no FROM (``COUNT(p.match_id)``) cannot do;
    the plain comparison still matches it against the same column counted under
    another qualifier."""
    dialect = model.dialect if model is not None else None
    spec = _keyspec(model)
    parsed = False
    for rows in (False, True) if spec else (False,):
        have = _canonical(sql, dialect, spec, rows)
        want = _canonical(expr, dialect, spec, rows)
        if have is None or want is None:
            continue
        parsed = True
        if _contains(have, want):
            return True
    return False if parsed else None
