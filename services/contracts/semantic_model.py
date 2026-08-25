"""The semantic model — the core artifact a lens is built around.

A curated, bounded description of what a lens can answer; the query generator is
grounded against it and `sql_guard` derives its allow-list from it.
"""

from __future__ import annotations

from typing import Annotated, Literal, get_args

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
AggType = Literal["sum", "count", "count_distinct", "avg", "min", "max"]

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
    agg_time_field: str | None = PField(
        default=None,
        description="field this metric aggregates over time by, when it differs "
        "from the entity's default_time_field",
    )

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
        if self.type == "simple" and (self.agg is None or self.expr is None):
            raise ValueError(
                f"simple metric '{self.name}' needs keys `agg:` and `expr:` — "
                "e.g. agg: sum, expr: orders.amount (agg is one of sum/count/"
                "count_distinct/avg/min/max; `agg: count` alone, with no expr, "
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


class Join(Authored):
    left: str
    right: str
    on: str = PField(validation_alias=_ON_ALIASES, description=_ON_DESC)
    type: Literal["inner", "left", "right", "full"] = "left"
    relationship: Literal["one_to_one", "many_to_one", "one_to_many"] | None = PField(
        default=None,
        description="row-count relationship left->right — guards fan-out/double-count bugs",
    )

    @model_validator(mode="before")
    @classmethod
    def _on_key(cls, data: object) -> object:
        return _restore_on_key(data)


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
    # The dropped metrics' full definitions, keyed by selected entity name —
    # the name list refuses a question that ASKS for a dropped metric; these
    # shapes let the runtime's shape guard refuse SQL that REBUILDS one from
    # raw columns (e.g. a self-composed conversion rate). Compiled from the
    # same shared entities as excluded_metrics; empty on pre-shape bundles.
    excluded_metric_shapes: dict[str, list[Metric]] = PField(default_factory=dict)
    # Set when this model was compiled from the project's shared semantic layer:
    # which assets (at which content-hash) went in — the staleness signal.
    shared_provenance: SharedProvenance | None = None

    def allowed_columns(self) -> dict[str, set[str]]:
        """Map physical table -> allowed column names. Consumed by sql_guard."""
        out: dict[str, set[str]] = {}
        for e in self.entities:
            out.setdefault(e.source.table, set()).update(f.name for f in e.fields)
        return out

    def allowed_tables(self) -> set[str]:
        return {e.source.table for e in self.entities}
