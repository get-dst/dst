"""The semantic model — the core artifact a lens is built around.

A curated, bounded description of what a lens can answer; the query generator is
grounded against it and `sql_guard` derives its allow-list from it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from typing import Annotated, Any, Literal, get_args

from pydantic import AliasChoices, BaseModel, BeforeValidator, field_validator, model_validator
from pydantic import Field as PField

from services.contracts.authoring import Authored


def _one_string_is_one_entry(v: object) -> object:
    """A bare string on a list-of-strings authoring field means ONE entry.

    Python iterates a string CHARACTER BY CHARACTER, so any code that loops a
    field typed `list[str]` without checking turns `use_when: some sentence`
    into ~300 one-character entries — accepted, silent, and (for use_when) in
    the routing path. Authors write the scalar form constantly because YAML
    makes it look right, so the scalar IS the single-element list here.

    Deliberately NOT applied to lists of MODELS (fields, metrics, entities,
    sample_queries): a bare string there has no obvious single-entry meaning,
    so pydantic's `list_type` rejection stands.
    """
    return [v] if isinstance(v, str) else v


# Every list-of-strings an author can type by hand. Use this, never a bare
# `list[str]`, on any field parsed from a project's YAML/frontmatter.
StrList = Annotated[list[str], BeforeValidator(_one_string_is_one_entry)]


def _source_needs_two_keys(v: object) -> object:
    """`source: warehouse` is a two-key mapping written as a scalar.

    Pydantic's own rejection names a class the author has never seen — "Input
    should be a valid dictionary or instance of `EntitySource`" — instead of the
    shape they need, and it is an easy mistake to repeat. Unlike a bare string on a
    list field, this one cannot be coerced: `warehouse` is a plausible connection
    name and `main.orders` a plausible table, so guessing which half was meant
    would silently author the wrong entity. Name the shape, echo what they wrote.
    """
    if isinstance(v, str):
        raise ValueError(
            "is two keys, not a string: `source: {connection: <a connections entry in "
            f"dst.yaml>, table: <physical table>}}` — you wrote `source: {v}`"
        )
    return v


FieldType = Literal["string", "number", "integer", "boolean", "timestamp", "date", "json"]
AggType = Literal["sum", "count", "count_distinct", "avg", "min", "max", "sum_distinct_on"]
# flow: the rows are events, and a period's figure aggregates every row in it.
# level: the rows are snapshots of a stock (ARR, headcount, a balance), and a
# period's figure is its LAST snapshot — summing snapshots across a quarter
# reports three months of the same balance as one number.
Additivity = Literal["flow", "level"]
ADDITIVITIES: tuple[str, ...] = get_args(Additivity)

FIELD_TYPES: tuple[str, ...] = get_args(FieldType)

# Every natural source of a column's type — the warehouse, `dst introspect`,
# dbt, a CREATE TABLE — speaks PHYSICAL types, and not one of them is valid here.
# An author who writes `type: BIGINT` plans clean and then has apply reject every
# file. So: the enum is spelled out in the field description (scaffold reference
# blocks render it), the error names the allowed values, and introspect prints
# the semantic type for each column.
_FIELD_TYPE_DESC = (
    "SEMANTIC type, not the warehouse type: " + " | ".join(FIELD_TYPES) + ". Warehouse "
    "types map in (BIGINT/INT64 -> integer, VARCHAR/TEXT -> string, NUMERIC/DOUBLE -> "
    "number, TIMESTAMP_NTZ -> timestamp, STRUCT/ARRAY -> json); `dst introspect` "
    "prints the semantic type for every column"
)

# First prefix wins — order matters (DATETIME before DATE, INTERVAL before INT).
_TYPE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("TIMESTAMP", "timestamp"),
    ("DATETIME", "timestamp"),
    ("DATE", "date"),
    ("INTERVAL", "string"),
    ("TIME", "string"),  # TIME / TIME WITH TIME ZONE — no semantic slot of its own
    ("BOOL", "boolean"),
    ("BIGINT", "integer"),
    ("SMALLINT", "integer"),
    ("TINYINT", "integer"),
    ("HUGEINT", "integer"),
    ("UBIGINT", "integer"),
    ("UINTEGER", "integer"),
    ("USMALLINT", "integer"),
    ("UTINYINT", "integer"),
    ("UHUGEINT", "integer"),
    ("SERIAL", "integer"),
    ("INTEGER", "integer"),
    ("INT", "integer"),  # INT, INT2/4/8, INT64
    ("DECIMAL", "number"),
    ("NUMERIC", "number"),
    ("NUMBER", "number"),
    ("BIGNUMERIC", "number"),
    ("FLOAT", "number"),
    ("DOUBLE", "number"),
    ("REAL", "number"),
    ("MONEY", "number"),
    ("JSON", "json"),
    ("STRUCT", "json"),
    ("ARRAY", "json"),
    ("MAP", "json"),
    ("LIST", "json"),
    ("VARIANT", "json"),
    ("OBJECT", "json"),
    ("RECORD", "json"),
    ("ROW", "json"),
    ("VARCHAR", "string"),
    ("NVARCHAR", "string"),
    ("CHARACTER", "string"),
    ("CHAR", "string"),
    ("TEXT", "string"),
    ("STRING", "string"),
    ("CLOB", "string"),
    ("UUID", "string"),
    ("ENUM", "string"),
    ("BLOB", "string"),
    ("BYTES", "string"),
    ("BINARY", "string"),
)


def warehouse_field_type(raw: str) -> str | None:
    """The `fields[].type` value for a warehouse column type, or None if it isn't
    one we recognise (so a typo is never dressed up as a mapping hint).

    Parameters and modifiers are ignored: VARCHAR(50), NUMERIC(10,2) and
    TIMESTAMP WITH TIME ZONE all resolve by their leading word — except a
    ``T[]`` list suffix, which decides the mapping on its own: DuckDB spells a
    list of strings ``VARCHAR[]``, and resolving that by its leading word gave
    `string`, so an author wrote ``tags = 't1'`` and got zero rows."""
    base = raw.strip().upper().split("(")[0].strip()
    if base.endswith("[]"):
        return "json"
    for prefix, mapped in _TYPE_PREFIXES:
        if base.startswith(prefix):
            return mapped
    return None


def _semantic_field_type(v: object) -> object:
    """Reject a warehouse type at the field it was typed into, naming the enum."""
    if isinstance(v, str) and v not in FIELD_TYPES:
        mapped = warehouse_field_type(v)
        hint = f" ('{v}' is a warehouse type — its semantic type is '{mapped}')" if mapped else ""
        raise ValueError(f"'{v}' is not one of {', '.join(FIELD_TYPES)}{hint}")
    return v


# `on:` unquoted is a YAML 1.1 BOOLEAN — safe_load resolves the KEY to True, so a
# join authored the obvious way arrives as {True: "a.id = b.id"} and `on` reports
# as missing ("joins.0.on Field required" — baffling, and the scaffold's own
# reference used to teach the unquoted form). Repair the key here so all three
# spellings author: bare `on:`, quoted `"on":`, and the `condition:` alias.
# (Write-side is already safe: yaml.safe_dump emits `'on':` quoted.)
_ON_ALIASES = AliasChoices("on", "condition")
_ON_DESC = (
    "SQL join condition, e.g. orders.customer_id = customers.id. NOTE: bare `on` is "
    'a YAML boolean — this key also authors as `"on":` (quoted) or `condition:`'
)


def _restore_on_key(data: object) -> object:
    """Map a join mapping's boolean True key back to `on` (see _ON_ALIASES)."""
    if isinstance(data, dict) and any(k is True for k in data):
        return {("on" if k is True else k): v for k, v in data.items()}
    return data


class Field(Authored):
    name: str = PField(
        description="the column name exactly as the warehouse holds it, e.g. order_id"
    )
    type: FieldType = PField(description=_FIELD_TYPE_DESC)
    description: str | None = None

    @field_validator("type", mode="before")
    @classmethod
    def _enum(cls, v: object) -> object:
        return _semantic_field_type(v)


class Dimension(Authored):
    name: str
    expr: str | None = None  # defaults to the field of the same name
    type: FieldType = PField(default="string", description=_FIELD_TYPE_DESC)
    description: str | None = None
    aliases: StrList = PField(
        default_factory=list,
        description="further phrasings that name this dimension word for word "
        "(e.g. ['region'] for store_region): a breakdown ('by region'), a ranking "
        "('which region') and a binding key read them exactly like the name",
    )

    @field_validator("type", mode="before")
    @classmethod
    def _enum(cls, v: object) -> object:
        return _semantic_field_type(v)


class Metric(Authored):
    name: str = PField(
        description="the metric's identity on this entity — what a question asks for, "
        "what a lens selects, and the column alias in generated SQL",
    )
    agg: AggType | None = PField(
        default=None, description="aggregation function; required for simple metrics"
    )
    expr: str | None = PField(
        default=None,
        description="simple: the SQL expression to aggregate (entity.column) — omit it "
        "with `agg: count` for a plain row count (COUNT(*)); derived: "
        "arithmetic over sibling metrics as {name} placeholders, e.g. "
        '"({revenue} - {cost}) / NULLIF({cost}, 0)"',
    )
    type: Literal["simple", "ratio", "derived"] = PField(
        default="simple",
        description="simple = one aggregate over expr; ratio = numerator / denominator "
        "(sibling metric names); derived = expr arithmetic over sibling metrics",
    )
    numerator: str | None = PField(
        default=None, description="ratio only: sibling metric name on the same entity"
    )
    denominator: str | None = PField(
        default=None, description="ratio only: sibling metric name on the same entity"
    )
    description: str | None = None
    # What people CALL the metric. The typed rails read a metric's name off the
    # question word for word (underscores as spaces, longest first); a metric
    # named `otd_rate` is never said that way, so "on-time delivery rate" named
    # nothing and the question went to a decider that could pick its count
    # sibling. Same discipline as a definition's aliases: literal word-boundary
    # matches on author-declared strings, zero model judgment.
    label: str | None = PField(
        default=None,
        description="the metric's business-English name, matched word for word like "
        "its identifier (e.g. 'on-time delivery rate' for otd_rate)",
    )
    aliases: StrList = PField(
        default_factory=list,
        description="further phrasings that name this metric word for word "
        "(e.g. ['OTD', 'on-time rate']) — the sole-metric door and the typed "
        "metric rail read them exactly like the name; no model involved",
    )
    filters: StrList = PField(
        default_factory=list,
        description="SQL boolean fragments ANDed into WHERE whenever this metric is "
        "computed, e.g. \"orders.status = 'completed'\" (one fragment may be written bare)",
    )
    format: Literal["number", "currency", "percent"] | None = PField(
        default=None, description="display hint for rendered answers"
    )
    currency: str | None = PField(
        default=None,
        description="ISO 4217 code (e.g. EUR, USD) for a monetary metric. The "
        "author's judgment about their own data — the product NEVER guesses one. "
        "Set, the composer states it and writes the amount in that currency; unset, "
        "the answer carries a bare number and asserts no currency at all.",
    )
    # One value per key, then summed: a monthly quota joined onto daily rows
    # repeats on every day of the month, and a plain SUM counts it thirty
    # times. The key is the author's fact about the data (which columns name
    # one copy of the value); the compiler keeps one row per key and sums those.
    distinct_on: StrList = PField(
        default_factory=list,
        description="sum_distinct_on only: the SQL expressions that identify one copy "
        "of the value (e.g. [quotas.rep_id, quotas.month]) — the sum counts one row "
        "per distinct key, so a value repeated across joined rows is added once",
    )
    agg_time_field: str | None = PField(
        default=None,
        description="field this metric aggregates over time by, when it differs "
        "from the entity's default_time_field",
    )
    better: Literal["higher", "lower"] | None = PField(
        default=None,
        description="which end of this metric is the better one: 'higher' for a win "
        "rate, 'lower' for deaths or churn. The author's judgment — the product never "
        "guesses one. Set, 'best' and 'worst' rank by it; unset, a question asking "
        "for the best or worst by this metric asks which end is meant.",
    )
    additivity: Additivity | None = PField(
        default=None,
        description="flow (the default): rows are events and a period's figure "
        "aggregates every row in it. level: rows are snapshots of a stock (ARR, "
        "headcount, a balance) and a period's figure is the LAST snapshot inside it, "
        "never a sum of snapshots; needs a time field (agg_time_field or the entity's "
        "default_time_field). The author's judgment — the product never guesses one.",
    )

    window_reading: Literal["snapshot", "any_in_window"] | None = PField(
        default=None,
        description="level only: how a stated period reads this level. snapshot (the "
        "default): one snapshot inside the period, its last. any_in_window: for a flag "
        "counted with count_distinct, the distinct things flagged at ANY snapshot inside "
        "the period (accounts overdue at some point in Q3). Asked over no period, or at "
        "a named edge of one ('at the end of Q3'), both read the snapshot. The "
        "author's judgment — the product never guesses one.",
    )

    period_weight: Literal["weeks", "days"] | None = PField(
        default=None,
        description="ratio only: lets a flow and a level meet in one ratio by weighting "
        "the level with the window's length — utilisation = scheduled appointments "
        "(a flow, summed over the period) / planned slots per week (a level, read at "
        "the period's latest snapshot) x weeks in the period. `weeks` multiplies the "
        "level by the window's days / 7, `days` by its days; the window must be bounded "
        "on both ends, and the compiler refuses an open or bucketed one. The author's "
        "judgment — the product never guesses a weight.",
    )

    @field_validator("additivity", mode="before")
    @classmethod
    def _additivity_enum(cls, v: object) -> object:
        # Named like `type:` is — the author sees the two words, not a class name.
        if isinstance(v, str) and v not in ADDITIVITIES:
            raise ValueError(f"'{v}' is not one of {', '.join(ADDITIVITIES)}")
        return v

    @field_validator("type", mode="before")
    @classmethod
    def _legacy_type(cls, v: object) -> object:
        # Pre-depth-1 bundles stored the output datatype here ("number", "integer", ...);
        # every such metric was a plain aggregate — coerce so they validate unchanged.
        if isinstance(v, str) and v in {
            "string",
            "number",
            "integer",
            "boolean",
            "timestamp",
            "date",
            "json",
        }:
            return "simple"
        return v

    @model_validator(mode="after")
    def _shape(self) -> Metric:
        # Every message names the exact YAML KEYS to write, with an example.
        # Naming only the constraint cost a benchmark driver three attempts on
        # the ratio shape alone (numerator_metric → numerator_metric_name →
        # numerator): an error that says what is wrong but not what to type
        # makes the author guess the schema.
        if self.type == "simple" and self.agg == "count" and self.expr is None:
            # COUNT(*): the one aggregate that needs no column. Stored as the
            # literal 1 — COUNT(1) IS COUNT(*) in every dialect, and it is the
            # only spelling that survives the filter path, which wraps expr as
            # COUNT(CASE WHEN … THEN expr END): a real engine rejects `*` there
            # ("STAR expression is only allowed as the root element").
            self.expr = "1"
        if self.period_weight is not None and self.type != "ratio":
            raise ValueError(
                f"metric '{self.name}' sets `period_weight:` but is not a ratio — it "
                "weights a ratio's level operand by the period's length: write "
                "type: ratio, numerator: <flow metric>, denominator: <level metric>, "
                "period_weight: weeks"
            )
        if self.agg == "sum_distinct_on" and not self.distinct_on:
            raise ValueError(
                f"metric '{self.name}' uses `agg: sum_distinct_on` but names no key — add "
                "`distinct_on:` with the expressions that identify one copy of the value, "
                "e.g. distinct_on: [quotas.rep_id, quotas.month]"
            )
        if self.distinct_on and self.agg != "sum_distinct_on":
            raise ValueError(
                f"metric '{self.name}' sets `distinct_on:` but its agg is "
                f"{self.agg or self.type} — the key applies only to agg: sum_distinct_on"
            )
        if self.type == "simple" and (self.agg is None or self.expr is None):
            raise ValueError(
                f"simple metric '{self.name}' needs keys `agg:` and `expr:` — "
                "e.g. agg: sum, expr: orders.amount (agg is one of sum/count/"
                "count_distinct/avg/min/max/sum_distinct_on; `agg: count` alone, with no expr, "
                "is a plain row count)"
            )
        if self.type == "ratio" and not (self.numerator and self.denominator):
            raise ValueError(
                f"ratio metric '{self.name}' needs keys `numerator:` and `denominator:`, "
                "each naming a SIBLING METRIC on the same entity — e.g. "
                "numerator: converted_sessions, denominator: session_count"
            )
        if self.type == "derived" and not self.expr:
            raise ValueError(
                f"derived metric '{self.name}' needs key `expr:` — arithmetic over sibling "
                'metrics as {name} placeholders, e.g. expr: "({revenue} - {cost}) / '
                'NULLIF({cost}, 0)"'
            )
        return self


class EntitySource(Authored):
    connection: str = PField(
        description="the dst.yaml connection name this table lives on",
    )
    table: str = PField(
        description="the physical table, qualified exactly as the warehouse holds it "
        "(schema.table); this IS the allow-list a lens over this entity may read",
    )


class HideRule(Authored):
    """A `hide` entry withheld from every caller except the named groups.

    Groups are the caller groups an admin attaches to a caller (or an identity
    provider asserts in a verified token), never anything a request says about
    itself. Every caller outside them reads the lens as if the entry were a
    plain pattern."""

    column: str = PField(
        description="the column pattern, fnmatch on the column name like a plain "
        "`hide` entry — bare ('contact_email', '*_email') or qualified"
    )
    except_groups: StrList = PField(
        description="the caller groups that may still read the column, each one "
        "declared under `groups:` in dst.yaml (e.g. [support, sales])"
    )

    @field_validator("column")
    @classmethod
    def _some_column(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("column must name a column pattern, e.g. contact_email")
        return v.strip()

    @field_validator("except_groups")
    @classmethod
    def _some_groups(cls, v: list[str]) -> list[str]:
        cleaned = [g.strip() for g in v if g and g.strip()]
        if not cleaned:
            raise ValueError(
                "except_groups needs at least one group; a column nobody may read "
                "is a plain `hide` entry"
            )
        return cleaned


HideList = Annotated[list[str | HideRule], BeforeValidator(_one_string_is_one_entry)]


class Entity(Authored):
    name: str = PField(
        description="the entity's identity, unique project-wide (folders are "
        "organization only) — what a lens selects and what generated SQL aliases "
        "the table to",
    )
    description: str | None = None
    grain: str | None = PField(
        default=None, description="what one row is, e.g. 'one row per sales order'"
    )
    use_cases: StrList = PField(
        default_factory=list,
        description="'Use when ...' / 'Avoid for ...' guidance for routing and generation",
    )
    common_questions: StrList = PField(
        default_factory=list,
        description="canonical questions this entity answers, natural language, no SQL",
    )
    source: Annotated[EntitySource, BeforeValidator(_source_needs_two_keys)]
    default_time_field: str | None = PField(
        default=None,
        description="canonical event-date field: trend / over-time questions bucket "
        "and group by it, e.g. 'created_at'",
    )
    primary_key: StrList = PField(default_factory=list)
    fields: list[Field] = PField(default_factory=list)
    dimensions: list[Dimension] = PField(default_factory=list)
    metrics: list[Metric] = PField(default_factory=list)
    # The population bound. A table holding a
    # SCOPED SUBSET (an enrolled cohort, a channel allow-list, a start date)
    # used to document that scope in definition prose — which constrains
    # nothing, so "what share of ALL X" was answered from the subset and
    # presented as the whole population at `verified`. `population` is the
    # prose that must travel with every answer; `population_filter` is the
    # enforcing half — a SQL predicate the metric compiler ANDs into every
    # query and the population_declared serve check requires. Both must ride
    # every rail `dialect` rides — a fact that reaches only one tier is not
    # enforced anywhere else.
    population: str | None = PField(
        default=None,
        description="the subset this table holds, as prose the answer carries "
        "(e.g. 'enrolled accounts only; channels A and B; from 2026-06-01')",
    )
    population_filter: str | None = PField(
        default=None,
        description="SQL predicate every query against this entity must include — "
        "the enforcing half of `population` (compiler ANDs it in; serving "
        "verifies it's present)",
    )
    # The aggregation-scope rail. "Never sum money columns across currencies"
    # written as description PROSE is honoured for one phrasing and violated for
    # the next, and the overstated cross-currency sum is served with no
    # disclosure; a structured predicate holds under the same pressure. So the
    # rule lives in code, not in prose: name the dimensions here and the
    # aggregation_scope serve check enforces them deterministically.
    pinned_dimensions: StrList = PField(
        default_factory=list,
        description="dimensions every aggregate over this entity must pin (filter "
        "to one value) or group by — e.g. ['currency']: a SUM across them is a "
        "number denominated in nothing",
    )
    # An entity resolved at apply from a lens's `expose:` grant, not authored:
    # the table's columns as the profile holds them, no metrics, no dimensions,
    # no meaning. The runtime treats it as ungoverned on every rail — never
    # offered to a typed decision, no router anchors, never a declared source
    # in the ledger, and every answer that reads it says EXPOSED. `exposed_whole`
    # is True when no `hide` pattern withheld a column; describe and compile read
    # it. The SQL guard does not key on it: a star over exposed tables is
    # rewritten into the allowed columns, hidden ones absent, whole or not.
    exposed: bool = False
    exposed_whole: bool = False
    # Columns the entity withholds from every door — generated SQL, the `sql`
    # tool, describe — the way an `expose:` grant's `hide` withholds a profiled
    # column. Patterns are fnmatch globs on the column name, bare or as
    # `entity.column` / `table.column`. Compile fails on a pattern that matches
    # no field and on a hidden column a dimension, metric or key still reads:
    # a hide that withholds nothing is a column the author believes is hidden
    # and is not.
    # An entry may also be a mapping, `{column: <pattern>, except_groups: [..]}`:
    # the column is withheld exactly like a plain pattern from every caller
    # outside those groups, and compiled into ``SemanticModel.group_columns`` so
    # a caller inside one reads it (``SemanticModel.for_groups``).
    hide: HideList = PField(
        default_factory=list,
        description="column patterns to withhold from every door, fnmatch on the "
        "column name — bare ('ssn', '*_email') or qualified ('patients.birth_date'). "
        "A hidden column is absent from the lens: naming it is out of scope. An "
        "entry `{column: contact_email, except_groups: [support]}` withholds the "
        "column from every caller outside those groups",
    )

    # Columns the lens keeps but never offers a question: a surrogate key a
    # listing, a join or a distinct count still reads (so `hide` cannot take
    # it) and nobody filters or groups by.
    internal: StrList = PField(
        default_factory=list,
        description="column patterns never offered as a filter, a breakdown or a "
        "ranked member, fnmatch on the column name like `hide` (e.g. ['member_sk']); "
        "unlike a hidden column it stays readable, so it may be a key",
    )

    def hidden_columns(self) -> set[str]:
        """The column names `hide` withholds, read from the fields and the
        passthrough dimensions (a bare dimension is the lens reading its
        column) — the compile keeps both off the entity, and the allow-list
        subtracts them again so a column an expression still names cannot come
        back through the guard."""
        names = {f.name for f in self.fields}
        names |= {d.name for d in self.dimensions if passthrough_dimension(d)}
        return {n for n in names if column_hidden(n, self)}

    def withhold_hidden(self) -> None:
        """Drop the fields and passthrough dimensions `hide` names, in place."""
        self.fields = [f for f in self.fields if not column_hidden(f.name, self)]
        self.dimensions = [
            d
            for d in self.dimensions
            if not (passthrough_dimension(d) and column_hidden(d.name, self))
        ]


def hide_patterns(entity: Entity, *, plain_only: bool = False) -> list[str]:
    """The column patterns of the entity's `hide` entries; a group entry's
    pattern included unless *plain_only*."""
    return [
        h if isinstance(h, str) else h.column
        for h in entity.hide
        if isinstance(h, str) or not plain_only
    ]


def column_hidden(column: str, entity: Entity) -> bool:
    """Does one of the entity's `hide` patterns name this column? Matched
    case-insensitively against the bare name, `entity.column`, and the source
    table's `table.column` (bare or qualified), so every spelling an author
    reaches for reads naturally. A group entry counts: the model a caller reads
    keeps only the entries that caller is not excepted from."""
    return _patterned(column, entity, hide_patterns(entity))


def predicate_hidden_columns(entity: Entity) -> set[str]:
    """The hidden columns the entity's own predicates read — its
    ``population_filter`` and its metrics' ``filters``. Matched by the `hide`
    patterns, not the fields, so the answer holds after the compile withheld
    the fields: these names still resolve inside those predicates, and
    nowhere else."""
    named = _referenced_columns(entity.population_filter, entity.name)
    for m in entity.metrics:
        for f_ in m.filters or []:
            named |= _referenced_columns(f_, entity.name)
    return {c for c in named if column_hidden(c, entity)}


def passthrough_dimension(dimension: Dimension) -> bool:
    """A dimension that IS its column: no expression, or one naming only its
    own name. Hiding the column withholds the dimension with it."""
    return not dimension.expr or dimension.expr.strip() == dimension.name


def _patterned(column: str, entity: Entity, patterns: list[str]) -> bool:
    if not patterns:
        return False
    table = entity.source.table
    forms = (
        column,
        f"{entity.name}.{column}",
        f"{table}.{column}",
        f"{table.rsplit('.', 1)[-1]}.{column}",
    )
    return any(fnmatchcase(f.lower(), p.lower()) for p in patterns for f in forms)


def column_internal(column: str, entity: Entity) -> bool:
    """Does one of the entity's `internal` patterns name this column? Matched
    like `hide`: case-insensitively, bare or qualified."""
    return _patterned(column, entity, entity.internal)


class Join(Authored):
    left: str
    right: str
    on: str = PField(validation_alias=_ON_ALIASES, description=_ON_DESC)
    type: Literal["inner", "left", "right", "full"] = "left"
    relationship: Literal["one_to_one", "many_to_one", "one_to_many"] | None = PField(
        default=None,
        description="row-count relationship left->right — guards fan-out/double-count bugs",
    )
    # Set when the join compiles from a role relationship: `right` is then the
    # role copy of the shared entity (<entity>_<role>), so two roles over one
    # pair join the same table twice under two aliases.
    role: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _on_key(cls, data: object) -> object:
        return _restore_on_key(data)


def _valid_zone(v: str | None) -> str | None:
    """An IANA zone or nothing: a clock declared in a zone no database knows is
    a false fact, and it fails at plan rather than at the first answer."""
    if v is None or not v.strip():
        return None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(v.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"'{v}' is not an IANA timezone (e.g. UTC, Europe/Helsinki)") from exc
    return v.strip()


class BlueprintVersions(Authored):
    """Rows that repeat once per sync of the same record."""

    sync_column: str = PField(
        description="the column that stamps each sync or load; a record has one row per "
        "value of it, e.g. _synced_at",
    )
    latest_only: StrList = PField(
        default_factory=list,
        description="columns that hold only on a record's latest version, e.g. a delete flag",
    )

    @model_validator(mode="after")
    def _sync_not_latest_only(self) -> BlueprintVersions:
        if self.sync_column in self.latest_only:
            raise ValueError(
                f"'{self.sync_column}' is the sync column and cannot also be latest_only"
            )
        return self


class BlueprintClock(Authored):
    """What one timestamp column records, and in which zone it is stored."""

    meaning: str | None = PField(
        default=None, description="what the column records, e.g. when the contact was created"
    )
    timezone: str | None = PField(
        default=None,
        description="the IANA zone the stored values are in, e.g. UTC or Europe/Helsinki",
    )

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str | None) -> str | None:
        return _valid_zone(v)


class BlueprintJoin(Authored):
    """A join from this table to another table on the same connection."""

    to: str = PField(description="the other table, named as its connection names it")
    on: str = PField(
        validation_alias=_ON_ALIASES,
        description="join condition with columns qualified by the bare table names, e.g. "
        'contacts.account_id = accounts.id (type the key as `"on":` or `condition:`)',
    )
    cardinality: Literal["one", "many"] = PField(
        description="rows of `to` per row of this table: one, or many (the join fans out)",
    )

    @model_validator(mode="before")
    @classmethod
    def _on_key(cls, data: object) -> object:
        return _restore_on_key(data)


class TableBlueprint(Authored):
    """Facts about one warehouse table, authored in semantic/tables/**.yaml.

    Project-wide, not per lens: every entity whose `source.table` is this table
    inherits `grain` and `key` when it declares none, and an exposed table
    carries the blueprint to describe and to the grain check on its answers."""

    connection: str = PField(description="the dst.yaml connection the table lives on")
    table: str = PField(
        description="the table, named exactly as its connection names it (schema.table)"
    )
    system: str | None = PField(
        default=None,
        description="the source system the table is loaded from, as the team names it, "
        "e.g. billing, or legacy CRM extract; plan never hints a join between tables "
        "of two different systems",
    )
    grain: str | None = PField(
        default=None,
        description="one line: what one row is, e.g. one row per contact per sync",
    )
    key: StrList = PField(
        default_factory=list,
        description="the columns one row is unique on; with `versions`, the sync "
        "column is part of it whether listed or not",
    )
    versions: BlueprintVersions | None = PField(
        default=None,
        description="set when rows repeat once per sync of the same record: "
        "{sync_column: <the column that stamps each sync>, latest_only: [<columns that "
        "hold on the latest version only, e.g. a delete flag>]}",
    )
    clocks: dict[str, BlueprintClock] = PField(
        default_factory=dict,
        description="timestamp columns by name: {<column>: {meaning: <what it records>, "
        "timezone: <IANA zone the values are stored in>}}",
    )
    joins: list[BlueprintJoin] = PField(
        default_factory=list, description="joins to other tables on the same connection"
    )
    caveats: StrList = PField(
        default_factory=list,
        description="what a reader of this table must know, one sentence each",
    )

    @field_validator("key")
    @classmethod
    def _distinct_key(cls, v: list[str]) -> list[str]:
        if len({c.lower() for c in v}) != len(v):
            raise ValueError(f"key names a column twice: {v}")
        return v

    @property
    def name(self) -> str:
        """The asset identity: the table as its connection names it. One
        blueprint per table name in a project, whichever connection holds it."""
        return self.table

    @property
    def full(self) -> bool:
        """Grain and key both declared: what the grain check needs."""
        return bool(self.grain and self.grain.strip() and self.key)

    def record_key(self) -> list[str]:
        """The key one record is unique on, the sync column left out."""
        sync = self.versions.sync_column.lower() if self.versions else None
        return [c for c in self.key if c.lower() != sync]

    def unique_on(self) -> list[str]:
        """The columns every row is unique on: the key, plus the sync column
        when rows repeat per sync."""
        if self.versions and self.versions.sync_column.lower() not in {c.lower() for c in self.key}:
            return [*self.key, self.versions.sync_column]
        return list(self.key)

    def own_columns(self) -> list[tuple[str, str]]:
        """``(where, column)`` for every column this blueprint names on its own
        table, join conditions excluded (those name two tables)."""
        out = [("key", c) for c in self.key]
        if self.versions:
            out.append(("versions.sync_column", self.versions.sync_column))
            out += [("versions.latest_only", c) for c in self.versions.latest_only]
        out += [("clocks", c) for c in self.clocks]
        return out


def blueprint_lines(blueprint: TableBlueprint | None, readable: set[str]) -> list[str]:
    """What a table's blueprint adds to a generation prompt, beyond `grain`
    (which the entity inherits): repeated rows per sync, the clocks, the
    caveats. Both prompts render the same lines (a fact that reaches one tier
    does not exist); a column the entity does not read is never named."""
    if blueprint is None:
        return []
    out: list[str] = []
    versions = blueprint.versions
    if versions is not None and versions.sync_column.lower() in readable:
        latest = [c for c in versions.latest_only if c.lower() in readable]
        out.append(
            f"rows repeat once per sync ({versions.sync_column}): a count or sum reads "
            "the latest version of each record"
            + (f"; {', '.join(latest)} holds on the latest version only" if latest else "")
        )
    for column, clock in blueprint.clocks.items():
        if column.lower() not in readable:
            continue
        said = ", ".join(
            x for x in (clock.meaning, clock.timezone and f"stored in {clock.timezone}") if x
        )
        if said:
            out.append(f"clock {column}: {said}")
    out += [f"caveat: {c}" for c in blueprint.caveats]
    return out


# What a blueprint's prose reads in place of a column the lens hides.
WITHHELD_COLUMN = "[withheld column]"


def withheld_columns(entity: Entity, columns: Iterable[str]) -> set[str]:
    """The columns of *entity*'s table the lens withholds, lowercased, out of
    *columns* (the table's names as profiled, or the fields before `hide`): on
    an exposed table every one it does not read, on a governed entity the ones
    its `hide` names."""
    if entity.exposed:
        readable = {f.name.lower() for f in entity.fields}
        return {c.lower() for c in columns if c.lower() not in readable}
    return {c.lower() for c in columns if column_hidden(c, entity)}


def hidden_names(table: str, hidden: Mapping[str, set[str]], readable: Iterable[str]) -> set[str]:
    """Every spelling of a hidden column that blueprint prose about *table*
    must not carry: its own hidden columns bare, any table's hidden column as
    `table.column`, and another table's hidden column bare when *table* has no
    readable column of that name. ``hidden`` maps a table to its withheld
    columns (``withheld_columns``)."""
    own = next((cs for t, cs in hidden.items() if t.lower() == table.lower()), set())
    keep = {c.lower() for c in readable}
    out = set(own)
    for t, columns in hidden.items():
        bare = t.rsplit(".", 1)[-1].lower()
        out |= {f"{bare}.{c}" for c in columns}
        out |= {c for c in columns if c not in keep}
    return out


def _mention(name: str) -> re.Pattern[str]:
    # Word-bounded, any qualifier in front, each part optionally quoted:
    # salary_eur, employees.salary_eur, hr.employees."salary_eur".
    q = r'["`]?'
    body = (q + r"\." + q).join(re.escape(part) for part in name.split("."))
    return re.compile(rf"(?<![\w$])(?:{q}[\w$]+{q}\.)*{q}{body}{q}(?![\w$])", re.IGNORECASE)


def redact_columns(text: str, names: Iterable[str]) -> str:
    """*text* with every mention of *names* (``hidden_names``) replaced by
    ``WITHHELD_COLUMN``."""
    for name in sorted(names, key=len, reverse=True):
        text = _mention(name).sub(WITHHELD_COLUMN, text)
    return text


def blueprint_mentions(blueprint: TableBlueprint, names: Iterable[str]) -> list[str]:
    """Where the blueprint's prose (grain, caveats, clock meanings, joins)
    names one of *names*, as ``"<slot> names <column>"``."""
    found = sorted(names, key=len, reverse=True)
    slots: list[tuple[str, str]] = [("grain", blueprint.grain or "")]
    slots += [(f"caveat {n}", c) for n, c in enumerate(blueprint.caveats, 1)]
    slots += [(f"clock {k}", c.meaning or "") for k, c in blueprint.clocks.items()]
    slots += [(f"join to {j.to}", j.on) for j in blueprint.joins]
    out: list[str] = []
    for slot, text in slots:
        hits = [n for n in found if _mention(n).search(text)]
        if hits:
            out.append(f"{slot} names {hits[0]}")
    return out


def withhold_hidden(blueprint: TableBlueprint, names: Iterable[str]) -> TableBlueprint:
    """The blueprint as a lens may serve it: every mention of a hidden column
    in its prose replaced by ``WITHHELD_COLUMN``, and a join whose condition
    names one dropped, with a caveat counting the drops. Structure the checks
    count with (key, versions, clock names) is kept: those doors withhold by
    column already."""
    names = set(names)
    if not names:
        return blueprint
    joins = [j for j in blueprint.joins if redact_columns(j.on, names) == j.on]
    caveats = [redact_columns(c, names) for c in blueprint.caveats]
    if dropped := len(blueprint.joins) - len(joins):
        caveats.append(
            f"{dropped} declared join(s) withheld: the condition names a {WITHHELD_COLUMN}"
        )
    return blueprint.model_copy(
        update={
            "grain": redact_columns(blueprint.grain, names) if blueprint.grain else None,
            "caveats": caveats,
            "clocks": {
                k: c.model_copy(
                    update={"meaning": redact_columns(c.meaning, names) if c.meaning else None}
                )
                for k, c in blueprint.clocks.items()
            },
            "joins": joins,
        }
    )


# The shapes a definition's `period` may take; the dates they span are read by
# services.runtime.verification.period_dates.
PERIOD_SHAPE = re.compile(r"\d{4}(-\d{2}(-\d{2}/\d{4}-\d{2}-\d{2})?)?")


class Definition(Authored):
    term: str = PField(
        description="the term this page defines, e.g. active_customer "
        "(the page body is its meaning)"
    )
    body: str
    about: str | None = PField(
        default=None,
        description="optional binding to the semantic object this term explains: "
        "'entity' or 'entity.member' (a field, dimension, or metric)",
    )
    sql_expr: str | None = None  # makes the definition enforceable in generated SQL
    # A rule on a PERIOD: the `sql:` predicate governs reads of the entity this
    # page is `about` over these dates (a duplicated billing export in one
    # month, a migration week). Certified SQL that reads the entity over a
    # window overlapping the period without the predicate serves below
    # `verified`, naming the term; outside the period the rule is silent.
    # No period = the predicate governs every read.
    period: str | None = PField(
        default=None,
        description="the dates the `sql:` rule governs: 'YYYY-MM', 'YYYY', or "
        "'YYYY-MM-DD/YYYY-MM-DD' (both ends inclusive)",
    )
    # "shared" = compiled from the project's shared semantic layer; "dbt" survives as an
    # inert legacy tag so pre-import bundles still validate.
    source: Literal["authored", "org_standard", "shared", "dbt"] = "authored"
    status: Literal["active", "ambiguous"] = PField(
        default="active",
        description="'ambiguous' terms make the system ask which meaning is intended "
        "instead of guessing (the body carries the curator's note)",
    )
    possible_mappings: StrList = PField(
        default_factory=list,
        description="for ambiguous terms: each entry 'meaning - where it lives'",
    )
    # The per-role rule, as a declared fact instead of page prose: prose like
    # "asked on behalf of Finance, revenue means net invoiced" cannot fire at
    # the deterministic pre-generation check, so the caller rail's identity
    # ("On behalf of the CFO: …") clarified anyway. Same discipline as aliases:
    # literal word-boundary matches on author-declared strings, zero model
    # judgment — a phrase resolves the term only when its value selects exactly
    # one possible_mapping and no other channel contests it.
    audiences: dict[str, str] = PField(
        default_factory=dict,
        description="for ambiguous terms: audience phrase -> the meaning it resolves "
        "to (e.g. {cfo: net invoiced revenue}) — a question naming the phrase serves "
        "that possible_mapping instead of clarifying, with the reading disclosed",
    )
    # The trigger LEXICON. The clarify rail is deterministic by contract — a
    # governance promise must not depend on a model's mood — but keyed to the
    # term's identifier alone it is unreachable for realistic phrasings: authors
    # test with the metric name (it's what they're editing) while users ask in
    # business English, so a declared ambiguity gets silently guessed instead.
    # Aliases widen the LEXICON, never the judgment: still a literal
    # word-boundary match, zero model involvement.
    aliases: StrList = PField(
        default_factory=list,
        description="business-English phrasings that trigger this term's rail "
        "(e.g. 'basket size' for order_value) — for ambiguous terms, these are "
        "what makes the clarification reachable for questions users actually type",
    )
    # The words people use for a stored VALUE that are not the value: "offlaner"
    # for 'offlane', "midlaner" for 'mid'. The typed rail binds a value the
    # question names word for word (and a lowercase value's regular plural);
    # these reach no rule, so the author maps them. Literal word-boundary
    # matches, bound only where the column's complete dictionary holds the value.
    value_aliases: dict[str, str] = PField(
        default_factory=dict,
        description="word people use -> the stored value of the column this "
        "definition is `about` (e.g. {offlaner: offlane}) — a question naming the "
        "word filters that column to the value; needs `about: entity.column`",
    )
    # The three certified-page keys that DO work: `render_context` puts summary,
    # grain and sources into the generation prompt for a page under
    # model.certified_dir. The identical page under semantic/definitions/ used to
    # parse them and drop them on the floor before Definition — same file format,
    # opposite outcome, decided only by which directory it sat in. They live here
    # now, so a definition page means the same thing wherever it is filed
    # (services/runtime/generator.py renders them).
    summary: str = PField(
        default="", description="one-line meaning, rendered beside the term in the prompt"
    )
    grain: str | None = PField(
        default=None,
        description="what one row of this term's result is, e.g. 'one row per invoice "
        "month' — rendered as an aggregate-at-this-grain instruction",
    )
    sources: StrList = PField(
        default_factory=list, description="underlying tables this term is computed from"
    )

    @field_validator("period")
    @classmethod
    def _period_shape(cls, v: str | None) -> str | None:
        if v is None or not str(v).strip():
            return None
        text = str(v).strip()
        if not PERIOD_SHAPE.fullmatch(text):
            raise ValueError(
                f"period {text!r} must be 'YYYY-MM', 'YYYY' or 'YYYY-MM-DD/YYYY-MM-DD'"
            )
        return text


class SampleQuery(Authored):
    question: str = PField(
        description="a natural-language ask this lens answers, "
        "e.g. how many orders shipped last week?"
    )
    sql: str = PField(description="the SQL that answers `question`, in this lens's dialect")


class SharedProvenance(BaseModel):
    """What a compiled model was built from: shared-layer assets by content-hash.

    A lens whose stored hashes differ from the current shared assets is stale —
    `dst plan` reports it and apply recompiles it."""

    compiled_at: str  # ISO-8601
    assets: dict[str, str] = PField(default_factory=dict)  # "entity/orders" -> content hash


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING_LITERAL = re.compile(r"'[^']*'")
# SQL words that show up in an expr or predicate and are never column names.
_SQL_WORDS = frozenset(
    """and or not in is null between like ilike case when then else end
    cast as distinct count sum avg min max coalesce nullif date interval
    current_date current_timestamp true false extract from where select
    on left right inner join group by order having asc desc limit""".split()
)


def _referenced_columns(expr: str | None, entity_name: str) -> set[str]:
    """Column names an authored expression references, best-effort.

    Deliberately lexical, not a SQL parse: this widens a guard allow-list, and
    a missed name only restores today's behaviour (a rejection), while a false
    name grants nothing — the guard still checks the column exists on an
    allowed TABLE. String literals are stripped first so a filter value like
    'Revenue (SE)' cannot smuggle in an identifier, and `entity.column` is
    reduced to `column` because the allow-list is keyed by physical table.
    """
    if not expr:
        return set()
    cleaned = _STRING_LITERAL.sub(" ", expr)
    out: set[str] = set()
    for m in _IDENT.finditer(cleaned):
        tok = m.group(0)
        after = cleaned[m.end() : m.end() + 1]
        if tok.lower() in _SQL_WORDS or after == "(":  # keywords and function calls
            continue
        if tok == entity_name:  # the qualifier itself, not a column
            continue
        out.add(tok)
    return out


class GroupColumn(BaseModel):
    """A column an entity's `hide` withholds from every caller outside
    ``groups`` — kept here, off the entity, so a model read without a caller
    (or by a caller outside the groups) never carries it, and
    ``SemanticModel.for_groups`` puts it back for a caller inside one."""

    entity: str
    column: str
    groups: list[str]
    field: Field | None = None
    dimensions: list[Dimension] = PField(default_factory=list)


class CubeStamp(BaseModel):
    """What apply recorded about a lens's Cube views: the model and overlay
    versions they compiled to, when, and the ``LensModel`` itself as JSON
    (``services.contracts.lens_model``), so a reader needs no second fetch."""

    model_version: str
    overlay_version: str
    stamped_at: str
    views: StrList = PField(default_factory=list)
    lens_model: dict[str, Any] = PField(default_factory=dict)


class SemanticModel(BaseModel):
    # No `version` field. There was one, defaulted to 1, written into every
    # stored bundle and every compiled.yaml, and read by nothing — a schema
    # version nobody branches on documents nothing. Bundles written before this
    # still carry the key and still load: this model is a STORAGE schema and
    # stays tolerant of extras (see services/contracts/authoring.py).
    lens: str
    dialect: Literal["bigquery", "duckdb", "postgres", "mysql", "snowflake"]
    # Compiled from LensConfig.timezone — rides next to dialect because it is the
    # same kind of fact: something every generated query must agree on. Empty =
    # the lens never declared one.
    timezone: str = ""
    # Compiled from LensConfig.stale_after_days: the declared freshness contract
    # (measured data_as_of older than this many days fails the serve-time
    # `freshness` check). None = undeclared.
    stale_after_days: int | None = None
    entities: list[Entity] = PField(default_factory=list)
    joins: list[Join] = PField(default_factory=list)
    definitions: list[Definition] = PField(default_factory=list)
    sample_queries: list[SampleQuery] = PField(default_factory=list)
    # "Use this lens when…" — natural-language example asks (no SQL) that say what this
    # lens is for. Seeded by the LLM at lens creation, curated by the data team, and fed
    # to the router as scoring anchors (services/router/profiles.py) so a question phrased
    # the way people actually ask hits a near-exact anchor and routes reliably.
    use_when: StrList = PField(default_factory=list)
    ai_instructions: str | None = None
    # Metric names the lens's selection deliberately DROPPED (per-entity metric
    # subsets at compile) and no selected entity still defines. The runtime
    # refuses questions that name one — an excluded metric is a curator's
    # boundary, not a gap for the model to reconstruct from raw columns.
    excluded_metrics: StrList = PField(default_factory=list)
    # Measures the lens declares it CANNOT compute: each entry is
    # {measure, aliases, route_to, reason}. The runtime refuses a question that
    # names one, and an answer whose result column is named after one — SQL
    # written for a paraphrase names its column after the measure it computed,
    # the deterministic hook no phrase list can give the question side.
    not_computable: list[dict[str, Any]] = PField(default_factory=list)
    # Asks the lens declares it does not answer, each {ask, sibling}
    # (LensConfig.not_for): refused before typing or generation when the
    # question says the ask as a phrase (services/runtime/not_for).
    not_for: list[dict[str, Any]] = PField(default_factory=list)
    # The dropped metrics' full definitions, keyed by selected entity name —
    # the name list refuses a question that ASKS for a dropped metric; these
    # shapes let the runtime's shape guard refuse SQL that REBUILDS one from
    # raw columns (e.g. a self-composed conversion rate). Compiled from the
    # same shared entities as excluded_metrics; empty on pre-shape bundles.
    excluded_metric_shapes: dict[str, list[Metric]] = PField(default_factory=dict)
    # Set when this model was compiled from the project's shared semantic layer:
    # which assets (at which content-hash) went in — the staleness signal.
    shared_provenance: SharedProvenance | None = None
    # The table blueprints (semantic/tables/) of the tables this model reads,
    # keyed by the entity's compiled source.table. Describe shows them for an
    # exposed table, and the grain check on an answer reads the declared key.
    blueprints: dict[str, TableBlueprint] = PField(default_factory=dict)
    # The columns a group `hide` entry withholds, compiled off their entities.
    # Only ``for_groups`` reads them: every door serves the entities, so a
    # door that never asked for a caller's view serves none of these.
    group_columns: list[GroupColumn] = PField(default_factory=list)
    # Set on a caller's view only: "entity.column (group)" for each column it
    # reads by a group exception — the receipt and the answer name them.
    column_grants: list[str] = PField(default_factory=list)
    # The Cube views the lens serves (LensConfig.views, or the overlay's), and
    # the versions apply stamped them at. None = not a Cube-backed lens.
    cube_views: StrList = PField(default_factory=list)
    cube: CubeStamp | None = None

    def for_groups(self, groups: Iterable[str]) -> SemanticModel:
        """The model as a caller in *groups* reads it.

        Each group `hide` entry naming one of *groups* is dropped from its
        entity and the columns it withheld come back (field and passthrough
        dimensions), unless another entry still hides them: deny wins. Every
        entity is then stripped of whatever its remaining `hide` names, so a
        model that reached storage without the compile's withholding serves
        nothing hidden either. ``group_columns`` is emptied on the view."""
        held = set(groups)
        if not self.group_columns and not any(
            isinstance(h, HideRule) for e in self.entities for h in e.hide
        ):
            return self
        view = self.model_copy(deep=True)
        by_entity: dict[str, list[GroupColumn]] = {}
        for gc in view.group_columns:
            by_entity.setdefault(gc.entity, []).append(gc)
        grants: list[str] = []
        for entity in view.entities:
            granted = [
                h for h in entity.hide if isinstance(h, HideRule) and held & set(h.except_groups)
            ]
            if granted:
                entity.hide = [h for h in entity.hide if not any(h is g for g in granted)]
                for gc in by_entity.get(entity.name, []):
                    if column_hidden(gc.column, entity):
                        continue
                    if gc.field is not None and gc.field.name not in {
                        f.name for f in entity.fields
                    }:
                        entity.fields.append(gc.field)
                    known = {d.name for d in entity.dimensions}
                    entity.dimensions += [d for d in gc.dimensions if d.name not in known]
                    via = sorted(held & set(gc.groups))
                    grants.append(f"{entity.name}.{gc.column} (group {', '.join(via)})")
            entity.withhold_hidden()
        view.group_columns = []
        view.column_grants = grants
        return view

    def every_exception(self) -> SemanticModel:
        """The view of a caller in every group a `hide` entry excepts — the
        widest any caller reads. What a certified answer is checked against
        when it is approved: it may serve a group-excepted column (only to
        callers in the group), never a column hidden from everyone."""
        return self.for_groups({g for gc in self.group_columns for g in gc.groups})

    def allowed_columns(self) -> dict[str, set[str]]:
        """Map physical table -> allowed column names. Consumed by sql_guard.

        A column is in scope if the model REFERENCES it, not only if it is
        listed under ``fields:``. Fields were the whole allow-list once, and
        that made three authoring shapes compile clean and then fail at query
        time with "column X is out of lens scope": a bare passthrough dimension, a
        column named only inside a metric's ``expr``, and a column named only
        by ``population_filter`` — the last one worse than a rejection, because
        a scalar question dropped the filter and served an undeduped number.

        The guard's job is to stop SQL from reaching columns the curator did
        not model. A dimension or population filter the curator wrote IS the
        curator modelling that column.
        """
        out: dict[str, set[str]] = {}
        for e in self.entities:
            cols = out.setdefault(e.source.table, set())
            cols.update(f.name for f in e.fields)
            # Dimensions: a bare one is the field of the same name; an expr one
            # names its columns inline.
            for d in e.dimensions:
                cols.add(d.name)
                cols.update(_referenced_columns(d.expr, e.name))
            for m in e.metrics:
                cols.update(_referenced_columns(m.expr, e.name))
                for f_ in m.filters or []:
                    cols.update(_referenced_columns(f_, e.name))
            cols.update(_referenced_columns(e.population_filter, e.name))
            cols.update(e.pinned_dimensions)
            if e.default_time_field:
                cols.add(e.default_time_field)
            cols.update(e.primary_key or [])
            # A hidden column is out of scope however it was named: a hide on
            # a column an expression still reads is a compile error, and this
            # subtraction is the guard's own copy of that promise. The one
            # read that survives is a compiler-owned predicate on the
            # compiled door (``predicate_columns``).
            cols.difference_update(c for c in list(cols) if column_hidden(c, e))
        return out

    def predicate_columns(self) -> dict[str, set[str]]:
        """Map physical table -> the hidden columns the compiler's own
        predicates read: an entity's ``population_filter`` and its metrics'
        ``filters``. Absent from ``allowed_columns`` (no caller may name
        them), they are what the guard admits on the compiled door, in
        predicate position only — the filter runs, the value never comes
        back."""
        out: dict[str, set[str]] = {}
        for e in self.entities:
            if hidden := predicate_hidden_columns(e):
                out.setdefault(e.source.table, set()).update(hidden)
        return out

    def allowed_tables(self) -> set[str]:
        return {e.source.table for e in self.entities}
