"""Identifier quoting — the SQL mechanics dst charges the author for.

`order` is one of the most common table names in existence, and it is a keyword in
every dialect dst speaks. An entity named after one reached the warehouse bare
and every answer through it died on a parser error the author could do nothing
about: you cannot rename the ecommerce warehouse's `order` table.

Quote only what needs it. Quoting unconditionally would be simpler and is wrong:
Snowflake and BigQuery fold unquoted identifiers to a canonical case, so pinning
`"customers"` on a warehouse that stores `CUSTOMERS` breaks a lens that works
today. A name that is already safe is emitted byte for byte as it was before.

The keyword set is sqlglot's own tokenizer table for the target dialect, plus the
first word of every multi-word keyword — `GROUP BY` and `ORDER BY` live in that
table as phrases, so `group` and `order` are invisible to a plain membership test
while DuckDB rejects both bare. Even the phrase-head test has holes — `at`,
`cast` and `check` are words sqlglot's table does not carry at all — so a name
that trips one of those still reaches the warehouse bare.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sqlglot import TokenType
from sqlglot.dialects.dialect import Dialect

# Every dialect SemanticModel.dialect allows. MySQL and BigQuery delimit with
# backticks; the other three with double quotes.
DIALECTS = ("bigquery", "duckdb", "mysql", "postgres", "snowflake")
_BACKTICK = ("mysql", "bigquery")

# A name that needs no quoting anywhere: ASCII, no leading digit, no punctuation.
_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_keywords: dict[str, frozenset[str]] = {}


def _reserved(dialect: str) -> frozenset[str]:
    cached = _keywords.get(dialect)
    if cached is None:
        table = Dialect.get_or_raise(dialect).tokenizer_class.KEYWORDS
        cached = _keywords[dialect] = frozenset(
            word for phrase in table for word in (phrase, phrase.split(" ", 1)[0])
        )
    return cached


def is_reserved(name: str, dialect: str) -> bool:
    """Is this name a keyword of ``dialect`` — the reason an entity dies at serve time?"""
    return name.upper() in _reserved(dialect)


def reserved_in(name: str) -> list[str]:
    """The dialects that would misread this name bare — for the author, at apply
    time, when a shared asset has no one dialect yet."""
    return [d for d in DIALECTS if is_reserved(name, d)]


def quote_ident(name: str, dialect: str) -> str:
    """One identifier, delimited only when a bare emission would be misread.

    Embedded delimiters are doubled — the escape every one of these dialects uses.
    """
    if _PLAIN.match(name) and not is_reserved(name, dialect):
        return name
    q = "`" if dialect in _BACKTICK else '"'
    return f"{q}{name.replace(q, q * 2)}{q}"


def quote_table(table: str, dialect: str) -> str:
    """A possibly schema-qualified physical table name, part by part."""
    return ".".join(quote_ident(part, dialect) for part in table.split("."))


# Where an identifier can stand and a keyword cannot: right after FROM, JOIN, AS
# or a dot — plus, by lookahead, right in front of a dot.
_IDENT_AFTER = frozenset({TokenType.FROM, TokenType.JOIN, TokenType.ALIAS, TokenType.DOT})
# Tokens the tokenizer already read as text, not grammar: a delimited identifier
# (re-quoting would double it) and a string literal (`'select'` is data).
_ALREADY_TEXT = frozenset({TokenType.IDENTIFIER, TokenType.STRING, TokenType.VAR})


def quote_bare_keywords(sql: str, names: Iterable[str], dialect: str) -> str:
    """Delimit the lens's own keyword names where SQL handed to us wrote them bare.

    The other half of this module quotes on the way OUT — everything dst
    itself emits. This is the way IN, and it exists because quoting after the
    parse is too late for the worst case: a delimiter can only be added to an
    identifier that survived, and a keyword name often does not survive. sqlglot
    reads ``FROM select`` as ``From(this=Select())`` — no identifier left, no
    table for the allow-list to check, and the statement renders back out as
    ``FROM SELECT`` and dies in the warehouse. ``customers.select`` and
    ``FROM t AS select`` do not even parse.

    Only a name in ``names`` that is also a keyword of ``dialect`` is touched,
    and only where a keyword cannot legally stand. A genuine leading ``SELECT``,
    the ``INT`` in ``CAST(x AS INT)`` and the word inside a string literal are
    therefore left alone — as is every string containing no keyword name at all,
    which returns without tokenizing.
    """
    wanted = {n.lower() for n in names if is_reserved(n, dialect)}
    if not wanted:
        return sql
    try:
        tokens = Dialect.get_or_raise(dialect).tokenizer_class().tokenize(sql)
    except Exception:  # noqa: BLE001 — unparseable input is the guard's rejection, not ours
        return sql
    q = "`" if dialect in _BACKTICK else '"'
    out: list[str] = []
    cut = 0
    for i, tok in enumerate(tokens):
        if tok.token_type in _ALREADY_TEXT or tok.text.lower() not in wanted:
            continue
        before = tokens[i - 1].token_type if i else None
        after = tokens[i + 1].token_type if i + 1 < len(tokens) else None
        if before not in _IDENT_AFTER and after is not TokenType.DOT:
            continue
        out.append(sql[cut : tok.start])
        out.append(f"{q}{tok.text.replace(q, q * 2)}{q}")
        cut = tok.end + 1
    out.append(sql[cut:])
    return "".join(out)
