"""Two deterministic rails over the lens's table scope — declared facts, no prose
heuristics.

``named_table_clarification``: a question that names a table the scope holds —
a declared entity, its source table, an exposed table, in this lens or in another
the caller may use — and whose generated SQL does not read that table becomes a
clarification instead of a figure. "How many events per day" on a lens with
``orders`` and no events table once served ``order_count`` as events: a silent
noun swap. The match is literal and word-bounded on author-declared names (the
``named_values`` discipline: case-insensitive, the bare name and its regular
plural, or the qualified name as written), so a table named after a common word
("orders" in "purchase orders") still names the table — in the normal case the
SQL reads it and nothing fires; only the mismatch clarifies. A table named
AFTER the named one counts as read — "staged orders" served from ``stg_orders``
names orders and reads a table whose name says so — by its ``_``/``.`` tokens,
another declared fact; so does a column the SQL reads that is named after it
("customers with more than one order" counted over ``number_of_orders``).

``scoped_suggestions``: a warehouse error that reaches the caller carries the
engine's own "Did you mean ...?" — and the engine suggests from everything it
can see, including tables outside the lens's grant. The suggestion is rewritten
to name only tables and columns in the allow-list (declared or exposed, any
topic), the closest by name to what was written; a suggestion with nothing close
in scope is removed. The rest of the error text stays verbatim.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping

import sqlglot
from sqlglot import exp

from services.contracts.response import ClarificationRequest
from services.contracts.semantic_model import SemanticModel
from services.runtime import sql_guard
from services.runtime.typed_resolver import _plural

# ── named table ──────────────────────────────────────────────────────────────


def _read_names(model: SemanticModel, sql: str) -> tuple[list[str], set[str]]:
    """The entities the SQL reads, by name, and every spelling that counts as
    reading them: each one's name, source table bare and qualified, the
    ``_``/``.`` tokens of the name, the bare table and every column the SQL
    reads (``stg_orders`` reads orders; so does ``number_of_orders``), and the
    regular plural of each."""
    reads = sql_guard.entities_read(sql, model)
    if not reads:
        return [], set()
    forms: set[str] = set()
    for e in reads:
        tail = e.source.table.rsplit(".", 1)[-1]
        forms |= {e.name, e.source.table, tail, *re.split(r"[._]", e.name + "_" + tail)}
    stmt = sqlglot.parse_one(sql, read=model.dialect)
    for column in stmt.find_all(exp.Column):
        forms.update(re.split(r"[._]", column.name))
    names = {f.lower() for f in forms if f}
    return [e.name for e in reads], names | {_plural(n) for n in names}


def _keys(name: str, table: str) -> set[str]:
    """The spellings a question uses for a table: the entity name, the source
    table bare and qualified, and the regular plurals of the bare forms."""
    tail = table.rsplit(".", 1)[-1].lower()
    keys = {name.lower(), table.lower(), tail}
    keys |= {_plural(k) for k in (name.lower(), tail)}
    return keys


def _names_said(question: str, keys: Mapping[str, tuple[str, str | None]]) -> list[str]:
    """The keys the question says, in the order it says them: word-bounded (a
    dot counts as a word character, so a bare name inside a qualified one is not
    the bare table), case-insensitive, longest key first so a shorter key inside
    a longer one is not said twice."""
    lowered = question.lower()
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, str]] = []
    for key in sorted(keys, key=lambda k: (-len(k), k)):
        if key not in lowered:
            continue
        spans = [
            m.span()
            for m in re.finditer(rf"(?<![\w.]){re.escape(key)}(?![\w.])", lowered)
            if not any(m.start() < end and start < m.end() for start, end in taken)
        ]
        if spans:
            taken += spans
            found.append((spans[0][0], key))
    return [key for _pos, key in sorted(found)]


def named_table_clarification(
    question: str,
    model: SemanticModel,
    sql: str,
    elsewhere: Mapping[str, str] | None = None,
) -> ClarificationRequest | None:
    """The clarification a question earns by naming a table the SQL does not
    read, or None when it names none, or names only tables the SQL reads.

    ``elsewhere`` maps a name (an entity name or source table, as another lens
    the caller may use declares it) to that lens; a name this lens also holds
    is this lens's. A table from elsewhere is never read here (the guard would
    refuse the SQL), so naming one always clarifies — with the lens that holds
    it, which is the only way the answer can say where the data lives. SQL
    that resolves to no entity is left alone: the rail fires on a mismatch it
    can show, never on a parse it cannot."""
    reads, read_names = _read_names(model, sql)
    if not reads:
        return None
    held: dict[str, tuple[str, str | None]] = {}
    for e in model.entities:
        for key in _keys(e.name, e.source.table):
            held.setdefault(key, (e.name, None))
    for name, holder in (elsewhere or {}).items():
        for key in _keys(name, name):
            held.setdefault(key, (name, holder))
    for key in _names_said(question, held):
        if key in read_names:
            continue
        label, lens = held[key]
        where = f" (lens `{lens}` holds it)" if lens else ""
        return ClarificationRequest(
            term=key,
            kind="named_table",
            options=[*reads, label],
            question=(
                f"you named `{key}`; this lens would answer from "
                f"{' and '.join(f'`{r}`' for r in reads)} — did you mean "
                f"{' or '.join(reads)}, or the `{key}` table{where}?"
            ),
        )
    return None


# ── scoped suggestions ───────────────────────────────────────────────────────

_QUOTED = r"""["'`][^"'`]+["'`]"""
_NAME = rf"(?:{_QUOTED}|[\w.]+)"
# The suggestion sentence each engine writes: DuckDB `Did you mean "x"?` and
# `Candidate bindings: "a", "b"`, Postgres `Perhaps you meant to reference the
# column "t.c".`, BigQuery `Did you mean x?` (unquoted). A list of names is
# rewritten name by name.
_SUGGESTION = re.compile(
    r"(?P<lead>(?:did you mean|perhaps you meant(?: to reference the \w+)?|candidate bindings:)"
    r"\s*)(?P<names>"
    rf"{_NAME}(?:\s*(?:,|or)\s*{_NAME})*)(?P<tail>[?.]?)",
    re.I,
)
_NAME_RE = re.compile(_NAME)
# What the caller wrote, where the engine's text says it — the reference the
# in-scope pick is closest to. Without one, the engine's own suggestion stands
# in for it (it is already close to what was written).
_WRITTEN = (
    re.compile(r"Table with name (\S+?) does not exist", re.I),
    re.compile(r"Referenced column \"([^\"]+)\"", re.I),
    re.compile(r"(?:relation|column|table) \"([^\"]+)\" does not exist", re.I),
    re.compile(r"Unrecognized name: ([\w.]+)", re.I),
    re.compile(r"Not found: Table ([\w.:-]+)", re.I),
    re.compile(r"invalid identifier '([^']+)'", re.I),
)


def _scope_names(model: SemanticModel) -> dict[str, str]:
    """lowercased -> as declared: every spelling of a table or column in the
    allow-list — a table qualified and bare, an entity name, a column bare and
    table-qualified."""
    out: dict[str, str] = {}
    for e in model.entities:
        table = e.source.table
        tail = table.rsplit(".", 1)[-1]
        for n in (table, tail, e.name):
            out.setdefault(n.lower(), n)
    for table, cols in model.allowed_columns().items():
        tail = table.rsplit(".", 1)[-1]
        for c in cols:
            for n in (c, f"{tail}.{c}", f"{table}.{c}"):
                out.setdefault(n.lower(), n)
    return out


def scoped_suggestions(text: str, model: SemanticModel) -> str:
    """*text* with every suggested identifier rewritten to one in the lens's
    allow-list: a name already in scope stays as written; one outside it becomes
    the closest in-scope name to what the caller wrote (difflib); a suggestion
    with nothing close is removed, sentence and all. An error about a function
    is left alone — a function name is no table and leaks none."""
    if re.search(r"\bfunction\b", text, re.I):
        return text
    scope = _scope_names(model)
    if not scope:
        return text
    written = next((m.group(1) for p in _WRITTEN if (m := p.search(text))), None)

    def rewrite(m: re.Match[str]) -> str:
        kept: list[str] = []
        for n in _NAME_RE.findall(m.group("names")):
            quote = n[0] if n[0] in "\"'`" else ""
            bare = n.strip("\"'`")
            if bare.lower() in scope:
                kept.append(n)
                continue
            close = difflib.get_close_matches((written or bare).lower(), list(scope), n=1)
            if close and (picked := f"{quote}{scope[close[0]]}{quote}") not in kept:
                kept.append(picked)
        if not kept:
            return ""
        return f"{m.group('lead')}{', '.join(kept)}{m.group('tail')}"

    out: list[str] = []
    pos = 0
    for m in _SUGGESTION.finditer(text):
        before = text[pos : m.start()]
        replacement = rewrite(m)
        if not replacement:
            # The sentence goes with what led into it: BigQuery's "; ", a
            # Postgres "HINT:" label, trailing blanks.
            before = re.sub(r"(?:HINT:)?[ \t;]*$", "", before)
        out += [before, replacement]
        pos = m.end()
    out.append(text[pos:])
    return re.sub(r"\n{3,}", "\n\n", "".join(out))
