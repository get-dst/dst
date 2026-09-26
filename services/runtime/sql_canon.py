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
qualifiers and quoting stripped, identifiers lowercased (string literals stay
exact: 'Active' and 'active' are different values), parentheses unwrapped (the
tree already holds the grouping they wrote), single precision widened, and a
count of every row read one way: ``COUNT(1)`` and ``COUNT(<column of the
entity's declared primary key>)`` are ``COUNT(*)``, because a key column is
never null. A count of any other column stays itself: it skips nulls. An
expression is carried when an equal subtree occurs; an AND (or OR) is carried
when every one of its operands occurs in one chain of the same connective, in
any order.
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


def _key_columns(tree: exp.Expr, keyspec: _KeySpec) -> dict[str, frozenset[str]]:
    """qualifier (lowercased; '' for a bare column) -> the key columns of the
    entity it denotes. An entity is named by its own name, by any alias of its
    source table in *tree*, and a bare column belongs to the one entity the
    statement reads, when it reads exactly one."""
    keys: dict[str, frozenset[str]] = {}
    for name, _table, pk in keyspec:
        if pk:
            keys[name.lower()] = frozenset(k.lower() for k in pk)
    read: set[str] = set()
    for tbl in tree.find_all(exp.Table):
        ref = ".".join(p for p in (tbl.catalog, tbl.db, tbl.name) if p)
        owners = [(name, pk) for name, table, pk in keyspec if _ref_matches(ref, table)]
        if len(owners) != 1:
            continue
        name, pk = owners[0]
        read.add(name)
        if pk:
            cols = frozenset(k.lower() for k in pk)
            keys[(tbl.alias or tbl.name).lower()] = cols
    if len(read) == 1:
        (only,) = read
        pk = next(pk for name, _t, pk in keyspec if name == only)
        if pk:
            keys[""] = frozenset(k.lower() for k in pk)
    return keys


@functools.lru_cache(maxsize=256)
def _canonical(text: str, dialect: str | None, keyspec: _KeySpec) -> exp.Expr | None:
    """The canonical tree of *text*, or None when it does not parse. Cached: one
    served answer is compared against every governed expression of its lens.
    Callers never mutate the result."""
    try:
        tree = sqlglot.parse_one(text, read=dialect or None)
    except Exception:  # noqa: BLE001 — unparseable text has no canonical form
        return None
    if tree is None:
        return None
    keys = _key_columns(tree, keyspec)
    for count in list(tree.find_all(exp.Count)):
        arg = count.this
        every_row = (isinstance(arg, exp.Literal) and not arg.is_string and arg.this == "1") or (
            isinstance(arg, exp.Column) and arg.name.lower() in keys.get(arg.table.lower(), ())
        )
        if every_row:
            count.set("this", exp.Star())
    for col in tree.find_all(exp.Column):
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


def _contains(have: exp.Expr, want: exp.Expr) -> bool:
    kinds: tuple[type[exp.Connector], ...] = (exp.And, exp.Or)
    for kind in kinds:
        if isinstance(want, kind):
            parts = _operands(want, kind)
            chains = [n for n in have.find_all(kind) if not isinstance(n.parent, kind)]
            return any(
                all(any(p == q for q in _operands(chain, kind)) for p in parts) for chain in chains
            )
    return any(node == want for node in have.find_all(type(want)))


def carries(sql: str, expr: str, model: SemanticModel | None = None) -> bool | None:
    """Does *sql* carry *expr*, compared canonically (see the module docstring)?
    None when either side does not parse: the caller decides what the text alone
    can say. *model* supplies the dialect and the declared keys.

    Compared twice: plainly, and with counts of a declared key read as COUNT(*).
    Either is a match. The second needs each side's key column resolved to its
    entity, which a fragment with no FROM (``COUNT(p.match_id)``) cannot do;
    the plain comparison still matches it against the same column counted
    under another qualifier."""
    dialect = model.dialect if model is not None else None
    parsed = False
    for spec in dict.fromkeys(((), _keyspec(model))):
        have = _canonical(sql, dialect, spec)
        want = _canonical(expr, dialect, spec)
        if have is None or want is None:
            continue
        parsed = True
        if _contains(have, want):
            return True
    return False if parsed else None
