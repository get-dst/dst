"""The typed resolver — a question either types or it doesn't.

Every slot of an answer is a closed-set decision over what dst already owns
(services/runtime/option_sets.py), made by the install's ``Decider`` with a
measured probability and turned into act / clarify / decline by the policy
(services/runtime/decision_policy.py). Nothing is extracted from prose:

- shape, entity, metrics, dimensions, grain, filter columns and dictionary
  values are DECISIONS (each a DecisionRecord on the answer's ledger);
- the time window is a deterministic parse (services/runtime/timewindow.py);
  a stated window the parser cannot resolve clarifies, never guesses;
- a stored value the question names verbatim (a complete dictionary's member,
  word-bounded, longest first) is matched, not decided: it fixes the filter's
  value, and a named value no filter uses clarifies rather than being dropped;
- numbers and dates in the question resolve only when unambiguous (exactly one
  standalone literal); anything else — and every open string value — comes
  from the caller's ``bindings`` or clarifies with ``unresolved_slot``, naming
  the column, its type grammar and the known values;
- order and limit are a small regex vocabulary.

The output is a ``QueryIntent`` the deterministic compiler turns into SQL — the
same closed sets the compiler already enforces, decided up front with a
probability instead of emitted as one JSON blob and rejected after the fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, cast

import sqlglot
from sqlglot import exp

from services.certify.lexical import breakdown_words
from services.contracts.profile import LOW_CARDINALITY_MAX
from services.contracts.protocols import (
    ContextChunk,
    Decider,
    Decision,
    GeneratedQuery,
    Option,
)
from services.contracts.query_intent import (
    FilterOp,
    IntentFilter,
    IntentOrder,
    QueryIntent,
    TimeGrain,
)
from services.contracts.resolution import DecisionRecord
from services.contracts.response import ClarificationRequest
from services.contracts.semantic_model import (
    Definition,
    Entity,
    Metric,
    SemanticModel,
    column_hidden,
)
from services.runtime import ambiguity, decision_policy, reading, timewindow, typed_health
from services.runtime.compiler import (
    CompileError,
    UnpinnedDimension,
    compile_intent,
    contradicted_literal,
    govern_literals,
    is_predicate,
    metric_sql,
    prefer_weighted_ratio,
)
from services.runtime.intent_generator import intent_term
from services.runtime.option_sets import option_sets

MAX_METRICS = 3
MAX_DIMENSIONS = 3
MAX_FILTERS = 3

_NONE = "none"
_ANOTHER_NONE = Option(_NONE, "no further one is asked for")
_SHAPES = [
    Option(
        "aggregate",
        "a figure: a total, count, average, rate, ratio, share or percentage (a retention "
        "rate, a conversion rate), or a breakdown of one",
    ),
    Option(
        "ranking",
        "which X has the highest / most / lowest / best / worst Y, or the top N X by Y: "
        "one figure per X, ordered by it",
    ),
    Option(
        "listing",
        "rows or one stored value: list / show / which records, with their fields, or "
        "what is the <thing> (the current patch, a team's region)",
    ),
]
# A ranking is an aggregate with a required breakdown and an order: it takes the
# aggregate's path everywhere a figure is resolved.
_FIGURES = ("aggregate", "ranking")
_ENGAGED = [Option("engaged", "the term's filter applies"), Option("not_engaged", "it does not")]
# Two different "none"s on a filter value: the question states NO value for
# the field (the column was not a filter after all — dropped, acted on) versus
# a value that is not in the dictionary (an unknown value — clarified). Under
# "act unless none wins" the first must never become a clarification.
_UNSTATED = "unstated"
_STATED = [
    Option("stated", "the question states a value for this field"),
    Option(_UNSTATED, "the question states no value for it — it is not a filter"),
]
_OPS = [
    Option("=", "equal to"),
    Option(">", "greater than / more than / above"),
    Option("<", "less than / below / under"),
    Option(">=", "at least / from"),
    Option("<=", "at most / up to"),
]
_TOP = re.compile(r"\b(?:top|first|largest|biggest|highest)\s+(\d{1,3})\b", re.I)
_BOTTOM = re.compile(r"\b(?:bottom|lowest|smallest|fewest)\s+(\d{1,3})\b", re.I)
_HIGHEST = re.compile(r"\b(?:highest|largest|biggest|most|top)\b", re.I)
_LOWEST = re.compile(r"\b(?:lowest|smallest|least|fewest|bottom)\b", re.I)
# A ranking whose direction depends on the measure (fewer deaths is the
# better record): it cannot be typed as desc or asc, so it is never typed.
_RANKED = re.compile(r"\b(?:best|worst)\b", re.I)
_NUMBER = re.compile(r"(?<![\d.-])-?\d+(?:\.\d+)?(?![\d.])")
_ISO_DATE = re.compile(r"\b((?:19|20)\d{2}-\d{2}-\d{2})\b")
_YEARISH = re.compile(r"\b(?:19|20)\d{2}\b")

# The ledger's provider for a value matched from the question's own text: a
# deterministic read, so no probability was measured.
QUESTION_TEXT = "question-text"
_LETTER = re.compile(r"[^\W\d_]")
_CODE = re.compile(r"^[A-Z0-9]{1,4}$")


def _plural(value: str) -> str:
    """The regular English plural of a lowercased value: carry → carries,
    hard support → hard supports, mid → mids, match → matches."""
    if value.endswith("y") and len(value) > 1 and value[-2] not in "aeiou":
        return value[:-1] + "ies"
    if value.endswith(("s", "x", "z", "ch", "sh")):
        return value + "es"
    return value + "s"


# Words a question uses for its grammar, not its subject: determiners,
# quantifiers, comparatives and superlatives, pronouns, prepositions,
# conjunctions, auxiliaries, question words, and the generic nouns of a
# question about teams and players. A stored value spelled like one ("Most", a
# team; "top", a lane) is far more often the word than the value.
_ORDINARY = frozenset(
    """
    a about above after again against all also an and any are as at be been before
    best better between biggest both bottom but by can could did do does down during
    each either else enough even ever every few fewer fewest first for from had has
    have he her here highest his how i if in into is it its just largest last least
    less like lowest many more most much my neither never next no none nor not now of
    off on once one only or other others our out over overall per player players same
    she should since smallest so some such than that the their them then there these
    they this those through to too top total team teams under until up us very was we
    were what when where which while who whom whose why will with within without worst
    would you your
    """.split()
)
_QUOTES = "'\"`‘’“”"


def _sentence_initial(text: str, start: int) -> bool:
    """Does the word at ``start`` open the question or a sentence of it?"""
    before = text[:start].rstrip(" \t\"'`‘“([")
    return not before or before[-1] in ".!?:;\n"


def _written_as_name(question: str, start: int, end: int, stored: set[str]) -> bool:
    """Is the occurrence at [start, end) marked as a name, not read as a word:
    possessive ("Most's"), quoted, or a capitalised stored value written as
    stored and not where every word is capitalised (the start of a sentence)."""
    if question[end : end + 2].lower() in ("'s", "’s"):
        return True
    if start > 0 and question[start - 1] in _QUOTES and question[end : end + 1] in _QUOTES:
        return True
    written = question[start:end]
    return (
        written in stored and written != written.lower() and not _sentence_initial(question, start)
    )


def named_values(
    question: str,
    domains: dict[str, list[str]],
    aliases: dict[str, dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Stored values the question names verbatim, in the order it names them:
    one entry per value, ``{column: the value as that column stores it}`` —
    "Lion" held by two columns is one entry with both.

    Case-insensitive and word-bounded (a possessive "Crystal Maiden's" names
    "Crystal Maiden"; "Lions" does not name "Lion"), longest value first, so a
    shorter value inside a longer one is not named twice. A value stored in
    lowercase is a category word, and its regular plural names it too —
    "which hard supports", "carries", "mids" — because a question about the
    members of a category says the stored word in the plural; a capitalised
    value is a name and has none; a value another column stores in the plural
    form wins the spelling. Two exceptions keep a match a fact rather than a
    coincidence: a value with no letter never matches (a number in a question
    is a limit or a threshold as often as a stored value), and a short
    all-capitals code matches only as written — the country code 'IN' is not
    the word "in", nor 'NO' the word "no".

    A one-word value spelled like a function or quantifier word (``_ORDINARY``:
    a team named 'Most', a lane stored as 'top') is named only where the
    question marks it as a name: possessive ("Most's record"), quoted ("'top'
    lane"), or — for a capitalised value — written as stored mid-sentence ("How
    did Most do this patch?"). Otherwise "Which country has the most pro
    players?" would restrict the count to one team. A sentence-initial capital
    is every word's, and a type noun in front is no signal either ("which team
    most often wins"). Multi-word values and words with no ordinary reading
    ("liquid", "team spirit") match in any case, as ever.

    ``aliases`` (column -> {word: stored value}, from a definition's
    ``value_aliases``) adds the words people use for a value that are neither
    it nor its plural — "offlaner" for 'offlane' — and their plurals. A word
    names its value only where the column's dictionary holds that value, and a
    stored spelling always wins over an alias."""
    groups: dict[str, dict[str, str]] = {}
    plurals: list[tuple[str, str, str]] = []
    for column, values in domains.items():
        for value in values:
            if not _LETTER.search(value):
                continue
            if _CODE.match(value):
                groups.setdefault(value, {}).setdefault(column, value)
                continue
            groups.setdefault(value.lower(), {}).setdefault(column, value)
            if value == value.lower():
                plurals.append((_plural(value), column, value))
    # Stored spellings first, so a value stored in the plural is itself, not
    # another value's plural.
    for key, column, value in plurals:
        groups.setdefault(key, {}).setdefault(column, value)
    for column, words in (aliases or {}).items():
        held = set(domains.get(column, ()))
        for word, value in words.items():
            if value in held and _LETTER.search(word):
                for key in (word.lower(), _plural(word.lower())):
                    groups.setdefault(key, {}).setdefault(column, value)
    lowered = question.lower()
    # The written form is read off the question only where lowercasing kept
    # every offset (it does for all but a few scripts); elsewhere an ordinary
    # word's value needs the possessive or quotes it would need anyway.
    as_written = question if len(lowered) == len(question) else lowered
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, dict[str, str]]] = []
    for key in sorted(groups, key=lambda k: (-len(k), k)):
        text = question if _CODE.match(key) else lowered
        if key not in text:
            # The regex below can only match where the key occurs; a dictionary
            # of thousands would otherwise compile a pattern per value per call.
            continue
        ordinary = key in _ORDINARY
        stored = set(groups[key].values())
        spans = [
            m.span()
            for m in re.finditer(rf"(?<!\w){re.escape(key)}(?!\w)", text)
            if not any(m.start() < end and start < m.end() for start, end in taken)
            and (not ordinary or _written_as_name(as_written, m.start(), m.end(), stored))
        ]
        if spans:
            taken += spans
            found.append((spans[0][0], groups[key]))
    return [by_column for _pos, by_column in sorted(found, key=lambda f: f[0])]


def _value_aliases(model: SemanticModel) -> dict[str, dict[str, str]]:
    """column (lowercased) -> {word: stored value}: each definition's
    ``value_aliases``, bound to the column its ``about: entity.column`` names
    (a definition about a table names no column and binds nothing — validate
    warns at plan/apply)."""
    out: dict[str, dict[str, str]] = {}
    for d in model.definitions:
        head, dot, member = (d.about or "").rpartition(".")
        if d.value_aliases and dot and head.strip() and member.strip():
            out.setdefault(member.strip().lower(), {}).update(d.value_aliases)
    return out


def _uses(named: dict[str, str], filters: list[IntentFilter]) -> bool:
    """Does some filter restrict one of the named value's columns to it?"""
    return any(f.op == "=" and f.field in named and f.value == named[f.field] for f in filters)


GRAMMAR = {
    "date_range": "YYYY | YYYY-Qn | YYYY-MM | YYYY-MM-DD/YYYY-MM-DD",
    "date": "YYYY-MM-DD",
    "number": "a plain number",
    "string": "the exact stored value",
}


@dataclass
class TypedResolution:
    intent: QueryIntent | None
    decisions: list[DecisionRecord] = field(default_factory=list)
    clarification: ClarificationRequest | None = None
    decline: str | None = None
    # Filter columns whose value came from the caller's bindings.
    supplied: list[str] = field(default_factory=list)
    # Filter columns whose value the question named verbatim (named_values).
    matched: list[str] = field(default_factory=list)
    # Asks the resolution left out instead of clarifying: a further field or
    # dimension the decider could not place once the first was settled.
    declined: list[str] = field(default_factory=list)

    @property
    def typed(self) -> bool:
        return self.intent is not None


class _Clarify(Exception):
    def __init__(self, request: ClarificationRequest) -> None:
        self.request = request


class _Decline(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


_MONTH_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# A breakdown over time is ASKED in words — by month, weekly, daily trend,
# over time. A stated period ("in August 2026") is a window, never a grain,
# and a date-shaped literal (a batch id) is not a daily series. Only a
# question carrying one of these poses the grain question at all.
_BREAKDOWN_OVER_TIME = re.compile(
    r"\b(?:by|per|each|every)\s+(?:hour|day|week|month|quarter|year)\b"
    r"|\b(?:hourly|daily|weekly|monthly|quarterly|yearly|annually)\b"
    r"|\bover time\b|\btrend\b|\btime series\b"
    r"|\b(?:day|week|month|quarter|year)[- ](?:over|on|by)[- ](?:day|week|month|quarter|year)\b",
    re.IGNORECASE,
)


def asks_breakdown_over_time(question: str) -> bool:
    return _BREAKDOWN_OVER_TIME.search(question) is not None


def period_field(entity: Entity, domains: dict[str, list[str]]) -> str | None:
    """A string field that IS a month period ("YYYY-MM"), read off the column
    profile: every profiled value matches the form. The deterministic window
    then compiles to a range on it (`month >= '2026-01' AND month <= '2026-08'`)
    instead of asking the agent to bind the stored value — the profile turns
    an open value into a typed slot. Declared time fields win."""
    for f in entity.fields:
        if f.type != "string":
            continue
        values = domains.get(f.name.lower())
        if values and all(_MONTH_PERIOD.match(v) for v in values):
            return f.name
    return None


def _expr_columns(metric: Metric) -> set[str]:
    cols: set[str] = set()
    for sql in [metric.expr or "", *metric.filters]:
        if not sql.strip():
            continue
        try:
            cols |= {c.name.lower() for c in sqlglot.parse_one(sql).find_all(exp.Column)}
        except sqlglot.errors.ParseError:
            continue
    return cols


_WORD = re.compile(r"[a-z0-9]+")


def _as_words(text: str) -> str:
    """The text as lowercased words: underscores, punctuation and runs of
    space read as one space, so `store_region` and "store region" are the
    same spelling."""
    return " ".join(_WORD.findall(text.lower()))


def named_options(question: str, options: list[Option]) -> list[str]:
    """The declared names the question says word for word, in the order it
    says them — the ``named_values`` discipline over an option set: a literal
    word-boundary match of each option's name read as words (a qualified
    `entity.metric` by its bare name) or of one of its aliases, longest first,
    so `puppy_revenue` inside "puppy revenue" is named once and `revenue` is
    not named off its tail. A spelling two options share (siblings each
    declaring `win_rate`) names neither: which one is the decider's question,
    not the text's."""
    return [name for _pos, name, _surface in _named_spans(question, options)]


def _named_spans(question: str, options: list[Option]) -> list[tuple[int, str, str]]:
    """``(position, option name, the surface matched)`` per option the question
    names — the body of ``named_options``, kept for the callers that must blank
    the matched words out of the text."""
    text = _as_words(question)
    by_surface: dict[str, set[str]] = {}
    for o in options:
        for spelling in (o.name.rsplit(".", 1)[-1], *o.aliases):
            surface = _as_words(spelling)
            if surface:
                by_surface.setdefault(surface, set()).add(o.name)
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, str, str]] = []
    for surface in sorted(by_surface, key=lambda k: (-len(k), k)):
        if surface not in text or len(by_surface[surface]) > 1:
            continue
        spans = [
            m.span()
            for m in re.finditer(rf"(?<!\w){re.escape(surface)}(?!\w)", text)
            if not any(m.start() < end and start < m.end() for start, end in taken)
        ]
        if spans:
            taken += spans
            found.append((spans[0][0], next(iter(by_surface[surface])), surface))
    return sorted(found)


# The words a question may add around a metric's name and still ask for
# nothing but that figure: interrogative frame, politeness, "total". Any other
# word — a period, a place, a dimension, a limit — is a slot, and the question
# leaves this path for a decision.
_FRAME_WORDS = frozenset(
    "what is the our total overall how much many show me give tell please number figure "
    "value of all and whats s were are was do we have i want need know can you it".split()
)
# The prepositions a period rides in on: "in September", "for Q3 2026",
# "during last month", "on 28 August". Admitted only beside a window the text
# states.
_WINDOW_FRAME_WORDS = frozenset("in for during over within on".split())


def window_filters(
    question: str,
    entity: Entity,
    metrics: list[str],
    *,
    period_field: str | None,
    today: date,
) -> list[IntentFilter]:
    """The filters a question's stated window compiles to on this entity's
    time field — the one window reading every structured door shares. Raises
    ``_Clarify`` when the window cannot be placed (an unplaced month, no time
    field, two periods)."""
    terms = timewindow.temporal_terms(question)
    # A vague part of a month ("early August", "mid-August") is never served
    # as the month and never guessed: ask, offering the days each reading uses.
    if vague := timewindow.vague_days(question, today):
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term="window",
                question="Which days exactly? "
                + "; ".join(
                    f"'{phrase}' would read as {a.isoformat()} to {b.isoformat()}"
                    for phrase, a, b in vague
                )
                + f" — supply the period as a binding ({GRAMMAR['date_range']}).",
                options=[f"{a.isoformat()}/{b.isoformat()}" for _p, a, b in vague],
            )
        )
    # A day-level qualifier beside a month that the parser did not read as
    # days ("the first few days of August"): the month is a wider window.
    if unplaced_days := timewindow.unplaced_days(question, today):
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term="window",
                question="Which days exactly? The question asks about "
                + ", ".join(f"'{phrase}'" for phrase, _a, _b in unplaced_days)
                + ", part of a month the window could not place, and the whole month would "
                f"be a wider answer — supply the days as a binding ({GRAMMAR['date_range']}).",
                options=[GRAMMAR["date_range"]],
            )
        )
    if any(m.name in metrics and m.additivity == "level" for m in entity.metrics):
        # A level metric at a month's end is the month's last snapshot.
        terms = timewindow.month_end_as_month(terms)
    # A month the question names that no window term carries would be
    # served as the OTHER months alone (or as all time) — a silently wrong
    # period. Ask for the period instead; never drop a stated month.
    if unplaced := timewindow.unplaced_months(question, terms):
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term="window",
                question="Which period exactly? The question names "
                f"{', '.join(unplaced)} in a way the window could not place — "
                f"supply the period as a binding ({GRAMMAR['date_range']}).",
                options=[GRAMMAR["date_range"]],
            )
        )
    if not terms:
        return []
    field_name = entity.default_time_field
    for m in entity.metrics:
        if m.name in metrics and m.agg_time_field:
            field_name = m.agg_time_field
    period = None if field_name else period_field
    if period is not None:
        field_name = period
    if not field_name:
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term="window",
                question="The question states a time window, but this entity declares no "
                "time field to apply it to.",
                options=[GRAMMAR["date_range"]],
            )
        )
    ranges = timewindow.window_ranges(terms, today)
    if len(ranges) != 1:
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term=field_name,
                question="Which period exactly? Supply it as a binding for "
                f"'{field_name}' ({GRAMMAR['date_range']}).",
                options=[GRAMMAR["date_range"]],
            )
        )
    start, end = ranges[0]
    if period is not None and timewindow.is_day_window(terms):
        # A month-form period field holds whole months: a day or a span of
        # days bound to it would answer for the month it falls in.
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term=field_name,
                question=f"The question asks about days, and '{field_name}' holds months, so "
                "the answer would cover the whole month. Ask for the month, or for a lens "
                "whose time field holds days.",
                options=[GRAMMAR["date_range"]],
            )
        )
    if start <= today < end and any(t.startswith("this ") for t in terms):
        # The current period is read to date on the lens clock: "this month"
        # is the first of the month through today.
        end = today
    if period is not None:
        # The profiled month form: one month is an equality (the SQL a
        # human certifies reads `month = '2026-08'`), a span is bound
        # inclusively on both ends.
        first, last = start.strftime("%Y-%m"), end.strftime("%Y-%m")
        if first == last:
            return [IntentFilter(field=field_name, op="=", value=first)]
        return [
            IntentFilter(field=field_name, op=">=", value=first),
            IntentFilter(field=field_name, op="<=", value=last),
        ]
    if _field_type(entity, field_name) == "timestamp":
        # `<= '2026-09-30'` on a timestamp is midnight: the last day's rows
        # after 00:00 fall out. The bound is the first day after the window.
        return [
            IntentFilter(field=field_name, op=">=", value=start.isoformat()),
            IntentFilter(field=field_name, op="<", value=(end + timedelta(days=1)).isoformat()),
        ]
    return [
        IntentFilter(field=field_name, op=">=", value=start.isoformat()),
        IntentFilter(field=field_name, op="<=", value=end.isoformat()),
    ]


def sole_metric_intent(
    question: str,
    model: SemanticModel,
    *,
    period_fields: dict[str, str] | None = None,
    domains: dict[str, list[str]] | None = None,
    today: date | None = None,
) -> QueryIntent | None:
    """The structured intent for a question that names exactly one governed
    metric word for word and asks for nothing else: no breakdown, no filter,
    no period, no limit. Read off the text alone (``named_options``), with
    the rest of the question held to a frame vocabulary, so a question that
    does carry a slot never lands here — it goes to a decision, or to
    generation. None otherwise.

    A question this plain is the structured door's own case: the compiler
    computes it deterministically, and a generated reading of it can only be
    equal or wrong.

    A stated period is the one slot the text settles on its own: "in
    September", "for Q3 2026", "last month" compile through the same window
    reading typed serving uses (``window_filters``), on the entity's time
    field. A bare month is the most recent one on or before ``today``."""
    governed = model.model_copy(update={"entities": [e for e in model.entities if not e.exposed]})
    if not governed.entities:
        return None
    sets = option_sets(governed, [], domains={})
    metrics = sets["metric"]
    named = _named_spans(question, metrics)
    if len(named) != 1:
        return None
    _pos, name, surface = named[0]
    if asks_breakdown_over_time(question):
        return None
    text = _as_words(question)
    residue = re.sub(rf"(?<!\w){re.escape(surface)}(?!\w)", " ", text, count=1)
    windowed = timewindow.place_bare_month(residue, today or date.today())
    spans = timewindow.temporal_spans(windowed)
    for a, b in reversed(spans):
        windowed = f"{windowed[:a]} {windowed[b:]}"
    frame = _FRAME_WORDS | _WINDOW_FRAME_WORDS if spans else _FRAME_WORDS
    if any(word not in frame for word in windowed.split()):
        return None
    bare = name.rsplit(".", 1)[-1]
    owners = [e for e in governed.entities if any(m.name == bare for m in e.metrics)]
    entity = (
        next((e for e in governed.entities if e.name == name.rsplit(".", 1)[0]), None)
        if "." in name
        else (owners[0] if len(owners) == 1 else None)
    )
    if entity is None:
        return None
    filters: list[IntentFilter] = []
    if spans:
        period = (period_fields or {}).get(entity.name) or period_field(entity, domains or {})
        try:
            filters = window_filters(
                timewindow.place_bare_month(question, today or date.today()),
                entity,
                [bare],
                period_field=period,
                today=today or date.today(),
            )
        except _Clarify:
            return None  # the window did not settle; the usual path asks or reads it
        if not filters:
            return None
    return QueryIntent(entity=entity.name, metrics=[name], filters=filters)


def by_dimension(question: str, dims: list[Option]) -> tuple[str | None, str]:
    """A breakdown the question asks for in words — "by <dimension>" / "per
    <dimension>" with an offered dimension's name right after — is a declared
    fact, read deterministically. ``(the dimension, the question with that
    phrase blanked)`` on exactly one match; ``(None, the question)`` otherwise
    (no match, or two — which one is then the decider's question). The blanked
    text is what the metric rounds read, so "by household plan" cannot name the
    `households` metric through its first word."""
    text = _as_words(question)
    hits: list[tuple[str, tuple[int, int]]] = []
    for o in dims:
        surface = _as_words(o.name.rsplit(".", 1)[-1])
        if not surface:
            continue
        m = re.search(rf"(?<!\w)(?:by|per) {re.escape(surface)}(?!\w)", text)
        if m is not None:
            hits.append((o.name, m.span()))
    if len(hits) != 1:
        return None, question
    name, (start, end) = hits[0]
    return name, f"{text[:start]} {text[end:]}"


def _says(question: str, name: str) -> bool:
    """Does the question say a declared name word for word (underscores as
    spaces, the last word also in its plural)? A literal match on an
    author-declared string — the ``named_options`` discipline for one name."""
    words = _as_words(name.rsplit(".", 1)[-1])
    if not words:
        return False
    text = _as_words(question)
    head, _sp, last = words.rpartition(" ")
    forms = {words, f"{head} {_plural(last)}".strip()}
    return any(re.search(rf"(?<!\w){re.escape(f)}(?!\w)", text) for f in forms)


def asks_about_time(question: str) -> bool:
    """A stated window or a breakdown over time — the question is temporal."""
    return bool(timewindow.temporal_terms(question)) or asks_breakdown_over_time(question)


def time_grouping_asked(question: str, entity: Entity, name: str) -> bool:
    """May a date or timestamp member group this answer? Only when the
    question, read beside its window phrases, asks over time ("per day",
    "daily"), says a time unit ("which day", "what date") or names the
    member. A window is a filter, never a grouping: "per station in the
    first 2 days of October" sums the two days per station. Any other
    member: always."""
    if _field_type(entity, name) not in ("date", "timestamp"):
        return True
    beside = timewindow.beside_windows(question)
    units = set(_TIME_UNITS) | {_plural(u) for u in _TIME_UNITS} | {"when"}
    return (
        asks_breakdown_over_time(beside)
        or bool(units & set(_WORD.findall(beside.lower())))
        or _says(beside, name)
    )


def _keyish(entity: Entity, name: str) -> bool:
    """A key column: the declared primary key, a column named ``*_id`` /
    ``*_key`` / ``id`` / ``key`` / ``unique_key``, or a hidden one. Its values
    identify rows; no question restricts or groups by one without saying it."""
    low = name.rsplit(".", 1)[-1].lower()
    return (
        low in {k.lower() for k in entity.primary_key}
        or low in ("id", "key", "unique_key", "uuid")
        or low.endswith(("_id", "_key", "_uuid"))
        or column_hidden(name, entity)
    )


def _key_part(entity: Entity, name: str) -> bool:
    """A label column that is one part of a composite primary key —
    `driver_name` in `[day, driver_name]`. The key identifies a row only together;
    the label alone is what a per-driver question groups by. A surrogate id
    (`*_id`, `*_key`) and a single-column key stay keys."""
    low = name.rsplit(".", 1)[-1].lower()
    keys = {k.lower() for k in entity.primary_key}
    return (
        len(keys) > 1
        and low in keys
        and _field_type(entity, name) == "string"
        and low not in ("id", "key", "unique_key", "uuid")
        and not low.endswith(("_id", "_key", "_uuid"))
        and not column_hidden(name, entity)
    )


def groupable(question: str, entity: Entity, shape: str, domains: dict[str, list[str]]) -> set[str]:
    """The members a breakdown may group by: the declared `dimensions:` when
    the entity declares any. Otherwise, when the question asks a breakdown
    (a ranking, or "per SDR" / "by source" / "for each account" in words),
    its string, boolean, date and timestamp fields, and the integer ones
    whose profile holds a complete value dictionary (a low-cardinality code,
    never a measure or an id) — the members the metric catalog reports. A
    question asking none is not offered a grouping to add."""
    if entity.dimensions:
        return {d.name for d in entity.dimensions}
    units = set(_TIME_UNITS) | {_plural(u) for u in _TIME_UNITS}
    if shape != "ranking" and not any(w - units for w in breakdown_words(question)):
        return set()
    return {
        f.name
        for f in entity.fields
        if not column_hidden(f.name, entity)
        and (
            f.type in ("string", "boolean", "date", "timestamp")
            or (f.type == "integer" and f.name.lower() in domains)
        )
    }


def offered(question: str, entity: Entity, options: list[Option]) -> list[Option]:
    """The members a field, dimension or filter decision may offer: no key
    column and, unless the question is about time, no date or timestamp — a
    candidate list of `row_key, store_id, sale_date` is no question
    anyone can answer. A label inside a composite key is not a key
    (``_key_part``). A member the question names word for word is offered
    whatever it is."""
    temporal = asks_about_time(question)

    def keyed(name: str) -> bool:
        # "order 7": the key's own noun beside the one number of its type.
        bare = name.rsplit(".", 1)[-1]
        stem = re.sub(r"_(?:id|key|uuid)$", "", bare, flags=re.I)
        return (
            stem != bare
            and _says(question, stem)
            and TypedResolver._literal_in_question(question, _field_type(entity, name)) is not None
        )

    return [
        o
        for o in options
        if _says(question, o.name)
        or not (
            (_keyish(entity, o.name) and not keyed(o.name) and not _key_part(entity, o.name))
            or (not temporal and _field_type(entity, o.name) in ("date", "timestamp"))
        )
    ]


# The tokens a metric's name carries for a parameter of the same measure: a
# window size (5m, 24h, 30d), a number, a period word. Two siblings that differ
# by one of these alone are one measure at two settings.
_PERIOD_FLAG_WORDS = frozenset(
    "day week month quarter year date latest current period today mtd qtd ytd wtd".split()
)
_PARAMETER_WORDS = frozenset(
    "hour hours day days week weeks month months quarter quarters year years daily weekly "
    "monthly quarterly yearly annual mtd qtd ytd wtd ltm ttm".split()
)


def _parameter(token: str) -> bool:
    return any(c.isdigit() for c in token) or token in _PARAMETER_WORDS


def parameter_siblings(question: str, chosen: str, options: list[Option]) -> list[str]:
    """The metrics that differ from ``chosen`` by one parameter token only —
    `served_within_5m` beside `served_within_15m` / `_1h` / `_24h` — when the
    question states no such token. ``[chosen, *siblings]`` then, else ``[]``.
    Which setting is meant is not in the text, so no probability settles it.
    A question saying the token ("within 15 minutes": the number 15 that only
    one sibling carries) or naming the metric word for word picks it."""
    tokens = _WORD.findall(chosen.rsplit(".", 1)[-1].lower())
    by_name = {o.name: o for o in options}
    if chosen in by_name and chosen in named_options(question, list(by_name.values())):
        return []
    siblings: list[tuple[str, str]] = []
    position: int | None = None
    for o in options:
        if o.name == chosen:
            continue
        other = _WORD.findall(o.name.rsplit(".", 1)[-1].lower())
        if len(other) != len(tokens):
            continue
        diff = [i for i, (a, b) in enumerate(zip(tokens, other, strict=True)) if a != b]
        if len(diff) != 1 or not (_parameter(tokens[diff[0]]) and _parameter(other[diff[0]])):
            continue
        if position not in (None, diff[0]):
            continue
        position = diff[0]
        siblings.append((o.name, other[diff[0]]))
    if position is None:
        return []
    words = set(_as_words(question).split())
    mine = tokens[position]
    if mine in words:
        return []
    digits = "".join(c for c in mine if c.isdigit())
    if (
        digits
        and digits in words
        and all("".join(c for c in t if c.isdigit()) != digits for _n, t in siblings)
    ):
        return []
    return [chosen, *(n for n, _t in siblings)]


_COUNT_CUE = re.compile(r"\b(?:count|counts|how many|number of)\b", re.I)
_ROW_WORDS = ("row", "record")


def _row_count(metric: Metric) -> bool:
    """A bare COUNT(*) whose name says nothing it counts (`count`,
    `row_count`) — the shape a modelling tool adds to every view."""
    words = set(_WORD.findall(metric.name.lower()))
    return (
        metric.type == "simple"
        and metric.agg == "count"
        and (metric.expr or "").strip() in ("1", "*")
        and words <= {"count", "row", "rows", "record", "records", "n"}
    )


def _grain_nouns(entity: Entity) -> set[str]:
    """What one row of the entity is, as words: the entity's name and the noun
    of its declared grain ("one row per ARR movement")."""
    nouns = set(_WORD.findall(entity.name.lower()))
    grain = (entity.grain or "").lower()
    if " per " in f" {grain} ":
        nouns |= set(_WORD.findall(grain.split("per", 1)[1].split(" per ")[0]))
    else:
        nouns |= set(_WORD.findall(grain))
    return {n for n in nouns if n not in _ORDINARY and len(n) > 1} - {"one", "row", "rows"}


def row_count_asked(question: str, entity: Entity) -> bool:
    """Does the question ask for a count of the entity's own rows — "how many
    records", "number of ARR movements"? A bare row count answers only that:
    "customer counts" on a table of movements counts movements, not
    customers."""
    if not _COUNT_CUE.search(question):
        return False
    words = set(_as_words(question).split())
    nouns = {*_ROW_WORDS, *_grain_nouns(entity)}
    return any(n in words or _plural(n) in words for n in nouns)


# Words a numeric field's name carries for its shape, not for what it measures.
_MEASURE_SHAPE_WORDS = frozenset(
    "delta amount value values total count sum avg net change number num qty quantity pct "
    "percent rate ratio raw local flag is has share level balance".split()
)


def undeclared_measure(question: str, entity: Entity, model: SemanticModel) -> str | None:
    """A measure the question names that the entity holds as a column (or the
    lens as a measure definition) but declares no metric for: "lost revenue" on
    a table whose metrics all count crates, beside a `revenue_delta` field.
    Acting on the nearest metric would answer in a different unit. None when
    the question names a metric of the entity word for word, or names no such
    measure."""
    metric_opts = [
        Option(m.name, "", tuple(a for a in (m.label, *m.aliases) if a)) for m in entity.metrics
    ]
    if named_options(question, metric_opts):
        return None
    declared: set[str] = set()
    for m in entity.metrics:
        for text in (m.name, m.label or "", *m.aliases):
            declared |= set(_WORD.findall(text.lower()))
    candidates: set[str] = set()
    for f in entity.fields:
        if f.type in ("number", "integer") and not _keyish(entity, f.name):
            candidates |= set(_WORD.findall(f.name.lower()))
    candidates = {
        c
        for c in candidates
        if len(c) > 1 and c not in _MEASURE_SHAPE_WORDS and c not in _ORDINARY and not c.isdigit()
    }
    words = set(_as_words(question).split())
    declared |= {_plural(w) for w in declared} | {w[:-1] for w in declared if w.endswith("s")}
    for c in sorted(candidates):
        if (c in words or _plural(c) in words) and c not in declared:
            return c
    for d in model.definitions:
        expr = (d.sql_expr or "").strip()
        if not expr or is_predicate(expr, model.dialect):
            continue
        term = _as_words(d.term)
        if term and _says(question, d.term) and not set(term.split()) <= declared:
            return d.term
    return None


# "customer counts", "count of accounts": the word count itself asks for a
# COUNT. "how many units", "number of hours" may also be a quantity column's sum.
_COUNT_OF = re.compile(r"\bcounts?\s+of\s+((?:[a-z][a-z-]*\s*){1,3})", re.I)
_X_COUNTS = re.compile(r"\b((?:[a-z][a-z-]*\s+(?:or|and)\s+)*)([a-z][a-z-]*)\s+counts?\b", re.I)
_HOW_MANY = re.compile(r"\b(?:how\s+many|number\s+of)\s+((?:[a-z][a-z-]*\s*){1,3})", re.I)
_COUNT_SKIP = (
    _ORDINARY
    | _MEASURE_SHAPE_WORDS
    | {
        "distinct",
        "unique",
        "different",
        "s",
        "times",
    }
)


def _phrase(text: str) -> list[str]:
    """The counted phrase's words, from its first content word up to the next
    ordinary one: "ARR movements were there" -> ["arr", "movements"]."""
    kept: list[str] = []
    for w in _WORD.findall(text.lower()):
        if w in _ORDINARY:
            if kept:
                break
            continue
        kept.append(w)
    return [w for w in kept if w not in _COUNT_SKIP]


def _counted(question: str) -> list[tuple[list[str], bool]]:
    """``(phrase words, strict)`` for every count the question asks: strict
    where it says the word count ("crate counts", "count of stores"), which
    only a COUNT answers. Coordinated nouns before "counts" ("store or crate
    counts") are one count each."""
    out: list[tuple[list[str], bool]] = []
    for m in _X_COUNTS.finditer(question):
        for w in [*_WORD.findall(m.group(1).lower()), m.group(2).lower()]:
            if w not in ("or", "and") and w not in _COUNT_SKIP:
                out.append(([w], True))
    for rx, strict in ((_COUNT_OF, True), (_HOW_MANY, False)):
        for m in rx.finditer(question):
            if words := _phrase(m.group(1)):
                out.append((words, strict))
    return out


def _forms(word: str) -> set[str]:
    return {word, _plural(word), word[:-1] if word.endswith("s") else word}


def uncounted_noun(question: str, entity: Entity, metrics: list[str]) -> str | None:
    """A thing the question counts that none of the answer's metrics counts —
    "store or crate counts" served as a sum of crates. Where the
    question says the word count ("crate counts", "count of stores") only a
    COUNT or COUNT DISTINCT answers, one whose name, label, aliases or column
    name the thing (a row count counts the entity's own rows). "how many X" /
    "number of X" is held to that only where X names a key column of the
    entity ("how many accounts" beside `account_id`): elsewhere a sum is often
    the count ("how many leads were called" over a weekly `called` column)."""
    asked = _counted(question)
    if not asked:
        return None
    by_name = {m.name: m for m in entity.metrics}
    chosen = [by_name[n] for n in (x.rsplit(".", 1)[-1] for x in metrics) if n in by_name]
    rows = {*_ROW_WORDS, *_grain_nouns(entity), *_WORD.findall(entity.name.lower())}
    keyed = {
        w
        for f in entity.fields
        if _keyish(entity, f.name)
        for w in _WORD.findall(f.name.lower())
        if w not in ("id", "key", "uuid", "unique")
    }
    for phrase, strict in asked:
        forms = {f for w in phrase for f in _forms(w)}
        keyed_words = [w for w in phrase if _forms(w) & keyed]
        if not strict and not keyed_words:
            continue
        head = (keyed_words or phrase)[0]
        answered = False
        for m in chosen:
            if m.type != "simple" or m.agg not in ("count", "count_distinct"):
                continue
            columns = [c.name.lower() for c in _expr_columns_of(m)]
            subject = {w for c in columns for w in _WORD.findall(c)}
            for text in (m.name, m.label or "", *m.aliases):
                subject |= set(_WORD.findall(text.lower()))
            if (m.expr or "").strip() in ("1", "*") or any(
                c in {k.lower() for k in entity.primary_key} for c in columns
            ):
                subject |= rows
            if forms & subject:
                answered = True
                break
        if not answered:
            return head
    return None


def _expr_columns_of(metric: Metric) -> list[exp.Column]:
    try:
        return list(sqlglot.parse_one(metric.expr or "").find_all(exp.Column))
    except sqlglot.errors.ParseError:
        return []


# The figure a question asks for: the words after "what was", "how much",
# "total", "sum of" ("what was GRV in March"), or the opening noun phrase before
# by/per/in ("GRV by station"). Counts ride _counted.
_FIGURE_CUE = re.compile(
    r"\b(?:what\s+(?:was|were|is|are|s)|whats|how\s+much|sum\s+of|totals?(?:\s+of)?)\s+"
    r"((?:[a-z0-9][a-z0-9-]*\s*){1,5})",
    re.I,
)
_LEADING_FIGURE = re.compile(r"^\s*((?:[a-z][a-z0-9-]*\s+){1,4}?)(?:by|per|in)\b", re.I)
_FIGURE_LEAD = frozenset(
    "the a an our my your their its total totals overall average avg mean median "
    "show give tell me us get find".split()
)


def _figure_phrase(text: str) -> list[str]:
    """The figure's words: past the leading determiners, up to the first
    grammar word, number or time unit ("the total GRV in March" -> ["grv"])."""
    words = _WORD.findall(text.lower())
    while words and words[0] in _FIGURE_LEAD:
        words = words[1:]
    kept: list[str] = []
    for w in words:
        if w in _ORDINARY or w.isdigit() or w in _TIME_UNITS:
            break
        kept.append(w)
    return kept


def _initialisms(text: str) -> set[str]:
    """Every acronym a run of the text's words spells: "gross rental value"
    -> grv (also over its content words alone: "return on ad spend" -> roas, ras)."""
    out: set[str] = set()
    for words in (_WORD.findall(text.lower()), _content_words(text)):
        for i in range(len(words)):
            for n in range(2, 7):
                if i + n <= len(words):
                    out.add("".join(w[0] for w in words[i : i + n]))
    return out


def _content_words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _ORDINARY]


def _spelled_out(acronym: str, texts: list[str]) -> list[list[str]]:
    """The word runs of ``texts`` whose initials spell ``acronym`` — where a
    description or a definition writes it out ("GRV: gross rental value")."""
    runs: list[list[str]] = []
    n = len(acronym)
    for text in texts:
        for words in (_WORD.findall(text.lower()), _content_words(text)):
            for i in range(len(words) - n + 1):
                run = words[i : i + n]
                if "".join(w[0] for w in run) == acronym:
                    runs.append(run)
    return runs


def _metric_texts(entity: Entity, metric: Metric) -> list[str]:
    """What a metric says it measures: its name, label, aliases, description,
    and the names and descriptions of the columns its expression reads."""
    by_field = {f.name.lower(): f for f in entity.fields}
    texts = [metric.name, metric.label or "", *metric.aliases, metric.description or ""]
    for column in sorted(_expr_columns(metric)):
        texts.append(column)
        if f := by_field.get(column):
            texts.append(f.description or "")
    return [t for t in texts if t]


def unconsumed_quantity(
    question: str,
    entity: Entity,
    metrics: list[str],
    model: SemanticModel,
    domains: dict[str, list[str]],
) -> str | None:
    """The quantity word the question asks the figure of, when none of the
    chosen metrics measures it: "what was GRV" on an entity whose one metric
    counts rentals. The word maps to a metric through its name, label,
    aliases, description or expression columns (their names and descriptions),
    an acronym those spell, or an acronym a definition spells out. A count
    ("how many helmets") asked of a COUNT must be what it counts (a row count
    counts the entity's own rows). Words that name a dimension, a non-numeric
    field, a stored value or a definition the question triggers are not
    quantity words, and a figure's word is checked only where it reads as a
    measure (see ``measure_word``). None when the figure is measured, or the
    question names a metric of the entity word for word."""
    by_name = {m.name: m for m in entity.metrics}
    chosen = [by_name[n] for n in (x.rsplit(".", 1)[-1] for x in metrics) if n in by_name]
    if not chosen:
        return None
    metric_opts = [
        Option(m.name, "", tuple(a for a in (m.label, *m.aliases) if a)) for m in entity.metrics
    ]
    if named_options(question, metric_opts):
        return None
    text = re.sub(r"[\'’]", "", question)
    asked: list[tuple[list[str], bool]] = [
        (_figure_phrase(m.group(1)), False) for m in _FIGURE_CUE.finditer(text)
    ]
    if lead := _LEADING_FIGURE.match(text):
        asked.append((_figure_phrase(lead.group(1)), False))
    # Count words map to count metrics: a COUNT must count the thing asked.
    # A sum answering "how many" is uncounted_noun's to judge.
    if any(m.type == "simple" and m.agg in ("count", "count_distinct") for m in chosen):
        asked += [(words, True) for words, strict in _counted(question) if not strict]
    naming: set[str] = set()
    for f in entity.fields:
        if f.type not in ("number", "integer"):
            naming |= set(_WORD.findall(f.name.lower()))
    for dim in entity.dimensions:
        naming |= set(_WORD.findall(dim.name.lower()))
    for values in domains.values():
        for v in values:
            naming |= set(_WORD.findall(str(v).lower()))
    rows = {*_ROW_WORDS, *_grain_nouns(entity)}
    entity_words = set(_WORD.findall(entity.name.lower()))
    keys = {k.lower() for k in entity.primary_key}

    def vocab(m: Metric, counting: bool) -> set[str]:
        words = {w for t in _metric_texts(entity, m) for w in _WORD.findall(t.lower())}
        words |= set(_WORD.findall((m.expr or "").lower()))  # `rentals.amount` reads rentals
        if counting and m.type == "simple" and m.agg in ("count", "count_distinct"):
            if (m.expr or "").strip() in ("1", "*") or _expr_columns(m) & keys:
                words |= rows | entity_words  # a row count counts the entity's rows
        elif counting:
            words |= entity_words
        return {f for w in words for f in _forms(w)}

    measures = {m.name: vocab(m, False) for m in chosen}
    counts = {m.name: vocab(m, True) for m in chosen}
    chosen_names = set(measures)
    for d in model.definitions:
        if not ambiguity.triggered(question, d):
            continue
        # A filter's term, a term bound to a chosen metric (`about:`), or one
        # whose meaning reads a chosen metric's words (a pinned reading) has
        # its meaning governed already. A term its body spells out as an
        # acronym is checked through that spelling below instead.
        term = _as_words(d.term).replace(" ", "")
        meaning = {
            f
            for w in _content_words(f"{d.body} {d.sql_expr or ''}")
            if w not in _MEASURE_SHAPE_WORDS and w not in _as_words(d.term).split()
            for f in _forms(w)
        }
        if (
            is_predicate(d.sql_expr or "", model.dialect)
            or (d.about or "").rpartition(".")[2] in chosen_names
            or (not _spelled_out(term, [d.body]) and any(meaning & v for v in measures.values()))
        ):
            naming |= set(_WORD.findall(d.term.lower()))
            for a in d.aliases:
                naming |= set(_WORD.findall(a.lower()))
    spelled_in = [t for d in model.definitions for t in (d.term, d.body, *d.aliases) if t]
    spelled_in += [t for m in entity.metrics for t in _metric_texts(entity, m)]
    spelled_in += [f.description for f in entity.fields if f.description]

    # A figure's word is held to the metric only where the lens or the text
    # marks it as a measure: an acronym (written in capitals, or spelled out by
    # the lens), a word of a numeric column, of another metric, or of a measure
    # definition. "busiest", "difference" or a synonym the lens never declares
    # stays the decider's reading.
    capitals = {w.lower() for w in re.findall(r"\b[A-Z][A-Z0-9]{1,5}\b", question)}
    measure_terms: set[str] = set()
    for f in entity.fields:
        if f.type in ("number", "integer") and not _keyish(entity, f.name):
            measure_terms |= set(_WORD.findall(f.name.lower()))
    for other in entity.metrics:
        for t in (other.name, other.label or "", *other.aliases):
            measure_terms |= set(_WORD.findall(t.lower()))
    for d in model.definitions:
        if d.sql_expr and not is_predicate(d.sql_expr, model.dialect):
            measure_terms |= set(_WORD.findall(d.term.lower()))
    measure_terms = {f for w in measure_terms for f in _forms(w)}

    def measure_word(word: str) -> bool:
        return (
            word in capitals
            or bool(_forms(word) & measure_terms)
            or bool(_spelled_out(word, spelled_in))
        )

    def measured(word: str, counting: bool) -> bool:
        for m in chosen:
            v = counts[m.name] if counting else measures[m.name]
            if _forms(word) & v:
                return True
            if counting:
                continue  # a count's noun is a thing, never an acronym of a measure
            if any(word in _initialisms(t) for t in _metric_texts(entity, m)):
                return True
            for run in _spelled_out(word, spelled_in):
                content = {w for w in run if w not in _MEASURE_SHAPE_WORDS}
                if content and all(_forms(w) & v for w in content):
                    return True
        return False

    for phrase, counting in asked:
        words = [
            w
            for w in phrase
            if w not in _MEASURE_SHAPE_WORDS
            and w not in _COUNT_SKIP
            and not (_forms(w) & naming)
            and len(w) > 1
        ]
        if not words or not (counting or all(measure_word(w) for w in words)):
            continue
        if not any(measured(w, counting) for w in words):
            return " ".join(words)
    return None


# A listing is the answer to "list", "which", "show me the rows"; a question
# naming a measure without one of these asks for a figure.
_LISTING_WORDS = re.compile(r"\b(?:list|listing|rows?|records?|which|who)\b", re.I)
_MEASURE_WORDS = frozenset("level levels total totals sum".split())
_HOW_MUCH = re.compile(r"\bhow\s+much\b", re.I)


def measure_noun(question: str, model: SemanticModel) -> str | None:
    """The measure a question names ("levels", "total", "how much", or a word
    of a numeric column, a one-word metric name, or a metric's label or
    aliases — "ARR", "revenue") when it says no listing word; None otherwise.
    A list of raw rows never answers "Show me stock levels". A longer metric
    name's other words are its qualifiers ("new", "churned"), not measures."""
    if _LISTING_WORDS.search(question):
        return None
    if hit := _HOW_MUCH.search(question):
        return hit.group(0).lower()
    words = set(_as_words(question).split())
    for w in sorted(_MEASURE_WORDS):
        if w in words:
            return w
    measures: set[str] = set()
    for e in model.entities:
        for f in e.fields:
            if f.type in ("number", "integer") and not _keyish(e, f.name):
                measures |= set(_WORD.findall(f.name.lower()))
        for m in e.metrics:
            for text in (m.label or "", *m.aliases):
                measures |= set(_WORD.findall(text.lower()))
            if len(name := _WORD.findall(m.name.lower())) == 1:
                measures |= set(name)
    measures = {
        w
        for w in measures
        if len(w) > 1 and w not in _MEASURE_SHAPE_WORDS and w not in _ORDINARY and not w.isdigit()
    }
    for w in sorted(measures):
        if w in words:
            return w
    return None


# A period is the grain's ("per month"); a date ordering is not a breakdown.
_TIME_UNITS = frozenset("hour day week month quarter year date time period".split())


def unplaced_breakdown(
    question: str,
    entity: Entity,
    metrics: list[str],
    dimensions: list[str],
    grain: str | None,
    filters: list[IntentFilter],
) -> str | None:
    """A breakdown the question asks in words ("per SDR", "by region", "for
    each account") that the answer does not group by, or None. Serving the
    total for a per-SDR ask drops the breakdown in silence. A word the answer
    already carries is not missing: a grouped dimension's, a chosen metric's
    name ("revenue per customer" as a metric), a filter's value ("booked by
    Aino" names a member, not a breakdown); a period ("per month") is the
    grain's, checked where the grain is."""
    by_name = {m.name: m for m in entity.metrics}
    covered: set[str] = set(_TIME_UNITS) | {_plural(u) for u in _TIME_UNITS}
    for d in dimensions:
        covered |= set(_WORD.findall(d.rsplit(".", 1)[-1].lower()))
    for name in metrics:
        m = by_name.get(name.rsplit(".", 1)[-1])
        for text in (name, *(((m.label or ""), *m.aliases) if m else ())):
            covered |= set(_WORD.findall(text.lower()))
    for f in filters:
        values = f.value if isinstance(f.value, list) else [f.value]
        for v in values:
            covered |= set(_WORD.findall(str(v).lower()))
    if grain:
        covered |= set(_WORD.findall((entity.default_time_field or "").lower()))
    for words in breakdown_words(question):
        forms = {f for w in words for f in _forms(w)}
        if not forms & covered:
            return " ".join(sorted(words))
    return None


# A short all-capitals code after a preposition — "for LATAM", "in APAC" — is
# how a question names a value. Read only in that literal form.
_CODE_VALUE = re.compile(r"\b(?:for|in|from|within|across)\s+(?:the\s+)?([A-Z][A-Z0-9]{1,5})\b")


def _declared_words(model: SemanticModel) -> set[str]:
    """Every word of every name the lens declares, lowercased."""
    out: set[str] = set()
    texts: list[str] = [model.lens]
    for e in model.entities:
        texts += [e.name, *(f.name for f in e.fields), *(d.name for d in e.dimensions)]
        for m in e.metrics:
            texts += [m.name, m.label or "", *m.aliases, m.currency or ""]
    for d in model.definitions:
        texts += [d.term, *d.aliases, *d.value_aliases]
    for t in texts:
        out |= set(_WORD.findall(t.lower()))
    return out


def unheld_code(
    question: str, entity: Entity, model: SemanticModel, domains: dict[str, list[str]]
) -> str | None:
    """A value the question names in code form ("for APAC") that no column of
    the entity can take: not a declared name, not a period, not a stored value
    of any dictionary, and every non-key text column's dictionary is complete
    (one it is not complete for might hold it — that is a decision). Serving
    without it answers a different question, so it is asked about."""
    declared = _declared_words(model)
    stored = {v.lower() for values in domains.values() for v in values}
    for m in _CODE_VALUE.finditer(question):
        code = m.group(1)
        low = code.lower()
        if low in declared or low in stored or low in _ORDINARY:
            continue
        if timewindow.temporal_terms(code):
            continue
        open_text = [
            f.name
            for f in entity.fields
            if f.type == "string" and not _keyish(entity, f.name) and f.name.lower() not in domains
        ]
        if open_text:
            return None
        return code
    return None


_USE_PREFIX = re.compile(r"^\s*(?:use(?:\s+it)?\s+(?:when|for|to)\b\s*:?\s*)", re.I)
_CONTENT_SKIP = _ORDINARY | _FRAME_WORDS


def anchored(question: str, model: SemanticModel) -> tuple[str, str] | None:
    """``(entity, line)`` when the question is one an entity declares it
    answers — a ``common_questions`` line or a "Use when" ``use_cases`` line,
    equal as words, or with every content word of the line in the question.
    "Avoid for" lines never anchor."""
    text = _as_words(question)
    words = set(text.split())
    for e in model.entities:
        lines = [*e.common_questions]
        for u in e.use_cases:
            if re.match(r"^\s*avoid\b", u, re.I):
                continue
            lines.append(_USE_PREFIX.sub("", u))
        for line in lines:
            norm = _as_words(line)
            if not norm:
                continue
            content = {w for w in norm.split() if w not in _CONTENT_SKIP and len(w) > 1}
            if norm == text or (len(content) >= 2 and content <= words):
                return e.name, line.strip()
    return None


@dataclass(frozen=True)
class _Carried:
    """What the answer's metrics already say about the rows they count, read
    off their own SQL: the words of each metric's name and of every column its
    expansion reads, and the (column, value) pairs it selects rows by."""

    words: tuple[tuple[str, ...], ...] = ()
    selects: frozenset[tuple[str, str]] = frozenset()

    def carries(self, value: str) -> bool:
        """Is a stored value the question names the metric's own subject —
        'radiant' of radiant_win_rate, 'pick' of hero_first_picks, 'drawn' of a
        rate over lanes_drawn or over `lane_result = 'drawn'`? Such a value is
        not a restriction the answer lacks."""
        v = value.lower()
        if any(val == v for _col, val in self.selects):
            return True
        want = _WORD.findall(v)
        if not want:
            return False
        forms = (want, [*want[:-1], _plural(want[-1])])
        n = len(want)
        return any(
            list(ws[i : i + n]) in forms for ws in self.words for i in range(len(ws) - n + 1)
        )


def _carried(entity: Entity, metrics: list[str], dialect: str | None) -> _Carried:
    by_name = {m.name: m for m in entity.metrics}
    words: list[tuple[str, ...]] = []
    selects: set[tuple[str, str]] = set()
    for name in metrics:
        metric = by_name.get(name.rsplit(".", 1)[-1])
        if metric is None:
            continue
        words.append(tuple(_WORD.findall(metric.name.lower())))
        try:
            tree = sqlglot.parse_one(metric_sql(metric, entity), read=dialect or None)
        except (CompileError, sqlglot.errors.ParseError):
            continue
        words += [tuple(_WORD.findall(c.name.lower())) for c in tree.find_all(exp.Column)]
        for eq in tree.find_all(exp.EQ):
            if isinstance(eq.parent, exp.Not):
                continue  # `NOT lane_result = 'drawn'` counts the other rows
            col, lit = eq.this, eq.expression
            if isinstance(lit, exp.Column):
                col, lit = lit, col
            if isinstance(col, exp.Column) and isinstance(lit, exp.Literal) and lit.is_string:
                selects.add((col.name.lower(), lit.this.lower()))
    return _Carried(tuple(words), frozenset(selects))


def _refuse_self_defining_filters(
    entity: Entity, metrics: list[str], filters: list[IntentFilter]
) -> None:
    """A filter on a column only a ratio's NUMERATOR reads decides the ratio:
    `win_rate WHERE is_win` is 1.0 for every row, `WHERE NOT is_win` is 0.0 — a
    ranking of identical values served as an answer. Ask instead. A simple
    metric filtered on its own column (SUM(amount) WHERE amount > 1000) is an
    ordinary question and is untouched."""
    by_name = {m.name: m for m in entity.metrics}
    for name in metrics:
        ratio = by_name.get(name)
        if ratio is None or ratio.type != "ratio" or not ratio.numerator or not ratio.denominator:
            continue
        num, den = by_name.get(ratio.numerator), by_name.get(ratio.denominator)
        if num is None or den is None:
            continue
        defining = _expr_columns(num) - _expr_columns(den)
        for f in filters:
            if f.field.lower() in defining:
                raise _Clarify(
                    ClarificationRequest(
                        kind="unresolved_slot",
                        term=f.field,
                        question=f"Filtering on '{f.field}' would decide '{name}' itself "
                        f"(its numerator counts '{f.field}'), so every row would read the "
                        "same — what should the answer be restricted by instead?",
                    )
                )


def _population_conflict(
    entity: Entity, filters: list[IntentFilter], dialect: str | None
) -> str | None:
    """A filter the entity's declared population rules out, or None.

    `game_type = 'turbo'` on a table counted over ranked All Pick only compiles to
    a WHERE no row satisfies — an empty answer to a question the table never
    covered. Equality and IN constraints in ``population_filter`` are read
    statically, so the contradiction declines before any query runs."""
    if not (entity.population_filter or "").strip() or not filters:
        return None
    try:
        tree = sqlglot.parse_one(entity.population_filter or "", read=dialect or None)
    except sqlglot.errors.ParseError:
        return None
    allowed: dict[str, set[str]] = {}
    for node in tree.find_all(exp.EQ, exp.In):
        column = node.this if isinstance(node.this, exp.Column) else None
        if column is None:
            continue
        values = [node.expression] if isinstance(node, exp.EQ) else list(node.expressions)
        if not all(isinstance(v, exp.Literal) for v in values):
            continue
        allowed.setdefault(column.name.lower(), set()).update(v.this for v in values)
    for f in filters:
        permitted = allowed.get(f.field.lower())
        if f.op == "=" and permitted is not None and str(f.value) not in permitted:
            scope = entity.population or f"{entity.name}: {entity.population_filter}"
            return (
                f"'{f.value}' is outside what this table counts ({scope.strip()}) — "
                f"{f.field} is always {', '.join(sorted(permitted))} here"
            )
    return None


def _field_type(entity: Entity, name: str) -> str | None:
    for f in entity.fields:
        if f.name == name:
            return f.type
    for d in entity.dimensions:
        if d.name == name:
            return d.type
    return None


def _grammar_for(ftype: str | None) -> str:
    if ftype in ("date", "timestamp"):
        return GRAMMAR["date"]
    if ftype in ("number", "integer"):
        return GRAMMAR["number"]
    return GRAMMAR["string"]


class TypedResolver:
    def __init__(
        self,
        decider: Decider,
        *,
        domains: dict[str, list[str]] | None = None,
        entity_domains: dict[str, dict[str, list[str]]] | None = None,
        period_fields: dict[str, str] | None = None,
        bindings: dict[str, str] | None = None,
        today: date | None = None,
        policies: dict[str, decision_policy.Policy] | None = None,
    ) -> None:
        self._decider = decider
        self._domains = domains or {}
        self._entity_domains = entity_domains or {}
        self._period_fields = period_fields or {}
        self._bindings = {k.rsplit(".", 1)[-1].lower(): str(v) for k, v in (bindings or {}).items()}
        self._today = today or date.today()
        self._policies = policies
        self._value_aliases: dict[str, dict[str, str]] = {}

    def _period_field(self, entity: Entity) -> str | None:
        """The entity's month-period field: from the profile's sample when
        assembly read one (any sample proves the form), else from a complete
        dictionary the resolver holds."""
        declared = self._period_fields.get(entity.name)
        return declared or period_field(entity, self._domains_for(entity))

    def _domains_for(self, entity: Entity) -> dict[str, list[str]]:
        """The dictionaries this entity's decisions read: its own profile's
        first, the model-wide ones beneath (value_guard.value_domains drops a
        column name whose dictionaries differ across entities; the entity's
        own view never has to)."""
        return {**self._domains, **self._entity_domains.get(entity.name, {})}

    # ── one decision, recorded ───────────────────────────────────────────────

    def _record(
        self, slot: str, d: Decision, records: list[DecisionRecord]
    ) -> tuple[Decision, str, int]:
        verdict = decision_policy.verdict(d, slot, self._policies)
        records.append(
            DecisionRecord(
                slot=slot,
                chosen=d.chosen,
                p=d.p,
                runner_up=d.runner_up,
                margin=d.margin,
                votes=d.votes,
                k=d.k,
                p_lower=d.p_lower,
                verdict=verdict,
                provider=d.provider,
                usage=d.usage.as_dict() if d.usage is not None else None,
            )
        )
        return d, verdict, len(records) - 1

    def _decide(
        self,
        slot: str,
        question: str,
        context: str,
        options: list[Option],
        *,
        allow_none: bool,
        records: list[DecisionRecord],
        key: str | None = None,
    ) -> tuple[Decision, str, int]:
        """One decision — served from the round's fan-out when it was asked
        there (``key``), else asked now."""
        if key is not None and key in self._prefetched:
            return self._prefetched.pop(key)
        d = self._decider.decide(
            question=question, context=context, options=options, allow_none=allow_none
        )
        return self._record(slot, d, records)

    def _fan_out(
        self,
        question: str,
        specs: dict[str, tuple[str, str, list[Option], bool]],
        records: list[DecisionRecord],
    ) -> None:
        """Ask every independent question of a round in ONE call when the
        decider batches (``decide_many``); a decider without it answers each
        lazily, in the same order, when the slot is reached. Records land in
        spec order either way."""
        decide_many = getattr(self._decider, "decide_many", None)
        specs = {k: v for k, v in specs.items() if v[2]}  # nothing to ask over no options
        if decide_many is None or not specs:
            return
        answers = decide_many(
            question,
            {k: (ctx, opts, allow_none) for k, (_slot, ctx, opts, allow_none) in specs.items()},
        )
        for key, (slot, _ctx, _opts, _allow_none) in specs.items():
            self._prefetched[key] = self._record(slot, answers[key], records)

    def _peek(self, key: str) -> str | None:
        """The acted choice of a prefetched decision, or None — read, not
        consumed, so the next round can be built on it before the slot logic
        runs; the pick itself still comes from the prefetch, recorded once."""
        hit = self._prefetched.get(key)
        if hit is None:
            return None
        d, verdict, _idx = hit
        return d.chosen if verdict == "act" else None

    @staticmethod
    def _another(context: str, chosen: list[str]) -> str:
        return f"{context} Already chosen: {', '.join(chosen)}. Is another one asked for?"

    @staticmethod
    def _candidates(d: Decision, options: list[Option]) -> list[str]:
        ranked = sorted((d.probs or {}).items(), key=lambda kv: -kv[1])
        names = {o.name for o in options}
        named = [k for k, _v in ranked if k != _NONE and k in names][:4]
        return named or [o.name for o in options[:4]]

    def _pick(
        self,
        slot: str,
        question: str,
        context: str,
        options: list[Option],
        *,
        allow_none: bool,
        records: list[DecisionRecord],
        clarify_term: str,
        clarify_question: str,
        decline_reason: str | None = None,
        key: str | None = None,
    ) -> str | None:
        """One single-choice slot under the policy: the chosen name on act,
        None on an acted `none`, a clarification or decline otherwise."""
        if not options:
            return None
        d, verdict, idx = self._decide(
            slot, question, context, options, allow_none=allow_none, records=records, key=key
        )
        if verdict == "act":
            return d.chosen
        if verdict == "decline":
            # A confident `none`: where the slot is the whole question (shape,
            # entity) that is a decline; elsewhere it is the honest "no more" —
            # acted on, and recorded as acted on.
            if decline_reason is not None:
                raise _Decline(decline_reason)
            records[idx] = records[idx].model_copy(update={"verdict": "act"})
            return None
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term=clarify_term,
                question=clarify_question,
                options=self._candidates(d, options),
            )
        )

    def _pick_many(
        self,
        slot: str,
        question: str,
        context: str,
        options: list[Option],
        *,
        cap: int,
        records: list[DecisionRecord],
        clarify_term: str,
        first_required: bool,
        key_prefix: str | None = None,
        initial: list[str] | None = None,
    ) -> list[str]:
        """A multi-valued slot as repeated single choices: the first pick, then
        'another, or none' until none or the cap — every step recorded. Each
        pick is keyed ``<prefix>:<index>`` so a fan-out round may have asked
        it already (round two asks the first, round three the second).
        ``initial``: picks already made (the first metric, decided over every
        metric of the lens); the loop continues from there."""
        chosen: list[str] = list(initial or [])
        remaining = [o for o in options if o.name not in chosen]
        while remaining and len(chosen) < cap:
            first = not chosen
            opts = remaining if (first and first_required) else [*remaining, _ANOTHER_NONE]
            ctx = context if first else self._another(context, chosen)
            try:
                pick = self._pick(
                    slot,
                    question,
                    ctx,
                    opts,
                    allow_none=not (first and first_required),
                    records=records,
                    clarify_term=clarify_term,
                    clarify_question=(
                        f"Which {clarify_term} does the question mean? "
                        f"Candidates: {', '.join(o.name for o in remaining[:6])}."
                    ),
                    key=f"{key_prefix}:{len(chosen)}" if key_prefix else None,
                )
            except _Clarify:
                # A further field or dimension the decider cannot place, once
                # the first is settled, is not the whole question: the rest
                # serves and the answer says what it leaves out, instead of a
                # clarification that refuses every part. No further decision.
                if first or clarify_term not in ("field", "dimension"):
                    raise
                self._declined.append(f"a further {clarify_term}: not in this lens")
                break
            if pick is None:
                break
            chosen.append(pick)
            remaining = [o for o in remaining if o.name != pick]
        return chosen

    # ── the slots ────────────────────────────────────────────────────────────

    def resolve(
        self, question: str, model: SemanticModel, notes: list[str] | None = None
    ) -> TypedResolution:
        """``notes``: governed facts settled before resolution (a pinned
        reading of an ambiguous term) — they ride the entity and metric
        decisions' instructions, the way they ride the JSON generator's prompt."""
        records: list[DecisionRecord] = []
        self._prefetched: dict[str, tuple[Decision, str, int]] = {}
        self._matched: list[str] = []
        self._declined: list[str] = []
        self._notes = " ".join(notes or [])
        self._value_aliases = _value_aliases(model)
        # An exposed table is never a typed option: it declares no metric, no
        # dimension and no meaning, so a decision over it would be a closed-set
        # choice among ungoverned columns. The resolver sees the governed
        # model only; a question about an exposed table does not type and
        # follows the lens's untyped rule.
        model = model.model_copy(update={"entities": [e for e in model.entities if not e.exposed]})
        try:
            if not model.entities:
                raise _Decline(
                    "no governed entity of this lens answers the question "
                    "(its tables are exposed without definitions)"
                )
            intent, supplied = self._resolve(question, model, records)
        except _Clarify as c:
            return TypedResolution(
                intent=None, decisions=self._consumed(records), clarification=c.request
            )
        except _Decline as d:
            return TypedResolution(intent=None, decisions=self._consumed(records), decline=d.reason)
        return TypedResolution(
            intent=intent,
            decisions=self._consumed(records),
            supplied=supplied,
            matched=list(self._matched),
            declined=list(self._declined),
        )

    def _consumed(self, records: list[DecisionRecord]) -> list[DecisionRecord]:
        """The ledger carries the decisions the resolution USED. A fan-out asks
        ahead; when an earlier slot clarified or declined, the answers still
        waiting were never part of this resolution and their raw policy verdicts
        would read as decisions taken (an optional slot's confident `none` as
        a decline). They are dropped, not relabelled."""
        unused = {idx for _d, _v, idx in self._prefetched.values()}
        self._prefetched = {}
        return [r for i, r in enumerate(records) if i not in unused]

    def _readings(
        self, question: str, model: SemanticModel, records: list[DecisionRecord]
    ) -> SemanticModel:
        """Every ambiguous term the question triggers, settled before any slot
        is decided: the lexical rail first (a question-named or audience-
        resolved reading pins for free), then one typed decision over the
        declared readings (reading.py) for the residue — act pins, none or a
        stray name clarifies. The pinned reading rides the entity and metric
        decisions as a note. The pipeline pins the same way before the
        generator; a pinned definition is active here and is left alone."""
        settled = ambiguity.resolve(question, model)
        pinned = list(settled)
        for d in model.definitions:
            if d.status != "ambiguous" or len(d.possible_mappings) < 2:
                continue
            if not ambiguity.triggered(question, d) or any(r.term == d.term for r in settled):
                continue
            picked, record = reading.decide_reading(self._decider, question, d, self._policies)
            records.append(record)
            if picked is None:
                raise _Clarify(
                    ClarificationRequest(
                        kind="ambiguous_term",
                        term=d.term,
                        question=f"'{d.term}' has more than one meaning here — which do you mean? "
                        + "; ".join(d.possible_mappings),
                        options=list(d.possible_mappings),
                    )
                )
            pinned.append(picked)
        if not pinned:
            return model
        # Word for word the pipeline's context chunk: the same fact must read
        # the same on every rail, or the decisions it steers drift between them.
        notes = " ".join(
            f"GOVERNED RESOLUTION — the ambiguous term '{r.term}' is pinned for this "
            f"question to: {r.mapping} ({r.via}). Compute that reading; do not ask for "
            "clarification."
            for r in pinned
        )
        self._notes = f"{self._notes} {notes}".strip()
        return ambiguity.pin(model, pinned)

    def _resolve(
        self, question: str, model: SemanticModel, records: list[DecisionRecord]
    ) -> tuple[QueryIntent, list[str]]:
        model = self._readings(question, model, records)
        sets = option_sets(model, [], domains=self._domains)
        # A bare row count is offered only to a question counting the entity's
        # own rows: "customer counts" served as the number of movement rows is
        # an answer in the wrong unit, not a count of customers.
        unasked = {
            name
            for e in model.entities
            if not row_count_asked(question, e)
            for m in e.metrics
            if _row_count(m)
            for name in (m.name, f"{e.name}.{m.name}")
        }
        if unasked:
            sets["metric"] = [o for o in sets["metric"] if o.name not in unasked]
        if barred := self._contradicted(question, model):
            kept = [o for o in sets["metric"] if o.name not in barred]
            if sets["metric"] and not kept:
                reason = next(iter(barred.values()))
                raise _Clarify(
                    ClarificationRequest(
                        kind="unresolved_slot",
                        term="metric",
                        question=f"{reason}, and every metric of this lens counts only other "
                        "values of it. Is the question meant for another lens?",
                        options=sorted({n.rsplit(".", 1)[-1] for n in barred}),
                    )
                )
            sets["metric"] = kept
        entity_options = [
            Option(e.name, " ".join(p for p in (e.description or "", e.grain or "") if p))
            for e in model.entities
        ]

        shape_ctx = "What kind of answer does the question ask for?"
        entity_ctx = self._noted(
            "Which entity (table of the semantic model) is the question about?"
        )
        metric_ctx = self._noted("Which governed metric does the question ask for?")
        # The entity a metric option belongs to: qualified names say it, a bare
        # name has one owner in the lens.
        owner_of: dict[str, str] = {}
        for e in model.entities:
            for m in e.metrics:
                owner_of.setdefault(m.name, e.name)
                owner_of[f"{e.name}.{m.name}"] = e.name
        # Round 1: what is asked, WHICH METRIC over every metric of the lens
        # (the metric names the entity — deciding the entity first lost to a
        # sibling entity on every distinctive metric name), and the entity for
        # a listing — independent, one call.
        self._fan_out(
            question,
            {
                "shape": ("shape", shape_ctx, _SHAPES, True),
                **(
                    {"metric:0": ("metric", metric_ctx, sets["metric"], False)}
                    if sets["metric"]
                    else {}
                ),
                **(
                    {"entity": ("entity", entity_ctx, entity_options, True)}
                    if len(model.entities) > 1
                    else {}
                ),
            },
            records,
        )
        try:
            shape = self._pick(
                "shape",
                question,
                shape_ctx,
                _SHAPES,
                allow_none=True,
                records=records,
                clarify_term="shape",
                clarify_question="Is this asking for a figure or for a list of records?",
                decline_reason="not a question this lens's data answers (no figure, no listing)",
                key="shape",
            )
        except _Decline:
            shape = self._shape_after_none(question, model, sets["metric"], records)
        if shape is None:
            raise _Decline("not a question this lens's data answers")
        if shape not in _FIGURES and sets["metric"] and (noun := measure_noun(question, model)):
            records.append(
                DecisionRecord(slot="shape", chosen=None, verdict="clarify", provider=QUESTION_TEXT)
            )
            names = [o.name for o in sets["metric"][:6]]
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term="metric",
                    question=f"The question names a measure ('{noun}'), so a list of rows would "
                    "not answer it. Which figure does it ask for? " + ", ".join(names) + ".",
                    options=names,
                )
            )

        first_metric: str | None = None
        # Metrics the question names word for word (named_options): when the
        # first pick cannot settle between them, they ARE the answer's metrics.
        named_metrics: list[str] = []
        if shape in _FIGURES:
            if not sets["metric"]:
                raise _Decline("no governed metric of this lens answers the question")
            try:
                first_metric = self._pick(
                    "metric",
                    question,
                    metric_ctx,
                    sets["metric"],
                    allow_none=False,
                    records=records,
                    clarify_term="metric",
                    clarify_question="Which metric does the question mean? Candidates: "
                    + ", ".join(o.name for o in sets["metric"][:6])
                    + ".",
                    key="metric:0",
                )
            except _Clarify as unsure:
                # "The split between puppy revenue and senior revenue": the
                # votes divide between two metrics because the question asks
                # for BOTH. Two or more governed names said word for word are
                # a declared fact (the named_values discipline), not a
                # guess — they are the metrics, read off the text and recorded
                # as such, never clarified. One QueryIntent reads one entity,
                # so names on different entities keep the clarification, and
                # it says why.
                named_metrics = named_options(question, sets["metric"])[:MAX_METRICS]
                if len(named_metrics) < 2:
                    raise
                owners = sorted({owner_of.get(m, "") for m in named_metrics})
                if len(owners) > 1:
                    raise _Clarify(
                        ClarificationRequest(
                            kind="unresolved_slot",
                            term="metric",
                            question="The question names "
                            + ", ".join(named_metrics)
                            + ", which live on different entities ("
                            + ", ".join(owners)
                            + ") — one answer reads one entity. Which metric does the "
                            "question mean?",
                            options=named_metrics,
                        )
                    ) from unsure
                for name in named_metrics:
                    records.append(
                        DecisionRecord(
                            slot="metric", chosen=name, verdict="act", provider=QUESTION_TEXT
                        )
                    )
                first_metric = named_metrics[0]
            if first_metric is None:
                raise _Decline("no governed metric of this lens answers the question")
            if not named_metrics:
                owner = owner_of.get(first_metric)
                if siblings := parameter_siblings(
                    question,
                    first_metric,
                    [o for o in sets["metric"] if owner_of.get(o.name) == owner],
                ):
                    records.append(
                        DecisionRecord(
                            slot="parameter", chosen=None, verdict="clarify", provider=QUESTION_TEXT
                        )
                    )
                    raise _Clarify(
                        ClarificationRequest(
                            kind="unresolved_slot",
                            term="metric",
                            question="These metrics measure the same thing at different "
                            "settings, and the question names none: "
                            + ", ".join(siblings)
                            + ". Which one?",
                            options=siblings,
                        )
                    )
            entity_name: str | None = owner_of.get(first_metric)
        else:
            entity_name = (
                model.entities[0].name
                if len(model.entities) == 1
                else self._pick(
                    "entity",
                    question,
                    entity_ctx,
                    entity_options,
                    allow_none=True,
                    records=records,
                    clarify_term="entity",
                    clarify_question="Which entity is this about? "
                    + ", ".join(e.name for e in model.entities),
                    decline_reason="no entity of this lens holds what the question asks about",
                    key="entity",
                )
            )
        if entity_name is None:
            raise _Decline("no entity of this lens holds what the question asks about")
        entity = next(e for e in model.entities if e.name == entity_name)
        entity, first_metric = self._holds_the_named(
            question, model, entity, shape, first_metric, sets["metric"], owner_of, records
        )
        # By owner, not by bare name: two siblings declaring `win_rate` each
        # have the other's qualified option match the bare name, and "another
        # metric?" then offers the sibling's figure as this entity's own.
        own_metrics = [o for o in sets["metric"] if owner_of.get(o.name) == entity.name]
        own_members = [
            *(Option(d.name, d.description or "") for d in entity.dimensions),
            *(
                Option(f.name, f.description or "")
                for f in entity.fields
                if f.name not in {d.name for d in entity.dimensions}
            ),
        ]

        # Round 2: every pick that depends only on the entity — the second
        # metric (or the first listing field), the first dimension, the grain,
        # each definition, the first filter column — one call; round three and
        # the sequential steps follow.
        fields_ctx = "Which fields should each listed row show?"
        # A dimension the question names a stored value of is a FILTER, never a
        # grouping: "which hard supports place the most wards" ranks heroes among
        # the hard supports, and a breakdown by position_name would be one group
        # wearing a ranking's shape. Such a column is not offered as a dimension
        # at all — the named value binds it below (`_filters`), deterministically.
        named_columns = {c for named in self._named(question, entity) for c in named}
        can_group = groupable(question, entity, shape, self._domains_for(entity))
        dims = [
            o
            for o in offered(question, entity, own_members)
            if o.name in can_group
            and o.name not in named_columns
            and time_grouping_asked(question, entity, o.name)
        ]
        # What a breakdown clarification offers when no dimension fits: the
        # entity's fields, so the question it asks names something to pick.
        fallback = [o.name for o in dims] or [
            f.name for f in entity.fields if not column_hidden(f.name, entity)
        ]
        # The fields a listing may show: no key column, no timestamp the
        # question does not ask about. With none left the listing is not one
        # this entity's members answer — ask which figure is meant instead.
        listed = offered(question, entity, own_members)
        if shape not in _FIGURES and not listed:
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term="metric",
                    question="Which figure does the question ask for? "
                    + ", ".join(o.name for o in own_metrics[:6]),
                    options=[o.name for o in own_metrics],
                )
            )
        dim_ctx = "Is the figure broken down by a dimension (per X / by Y)?"
        # "by <dimension>" said in words is read, not decided (by_dimension):
        # it is the first dimension, recorded off the text, and its words
        # are blanked from what the metric rounds read.
        named_dim, metric_text = (
            by_dimension(question, dims) if shape in _FIGURES else (None, question)
        )
        dim_initial: list[str] = []
        if named_dim is not None:
            records.append(
                DecisionRecord(
                    slot="dimension", chosen=named_dim, verdict="act", provider=QUESTION_TEXT
                )
            )
            dim_initial = [named_dim]
        # The metrics this answer starts with: the first pick, plus every
        # further metric the question named on this entity.
        initial_metrics = [first_metric] if first_metric is not None else []
        initial_metrics += [
            m for m in named_metrics if m not in initial_metrics and owner_of.get(m) == entity.name
        ]
        # "Another metric?" holds a stricter bar than the first pick. The first
        # is argmax over every metric — the question asks for SOMETHING — but
        # a further one is offered only among metrics whose name the question
        # says word for word, outside the "by <dimension>" phrase: "revenue by
        # household plan" pulled the `households` metric through the word
        # "household", and a sibling of revenue then won over none. Nothing
        # named → the round is not asked at all.
        spoken = set(named_options(metric_text, own_metrics))
        rest_metrics = [
            o for o in own_metrics if o.name in spoken and o.name not in initial_metrics
        ]
        metric_options = [o for o in own_metrics if o.name in initial_metrics or o.name in spoken]
        grain_ctx = (
            "The question asks for the figure broken down over time. Which period is "
            "each row — hour, day, week, month, quarter or year? A stated period such "
            "as 'in August 2026' is the window, not the breakdown."
        )
        over_time = asks_breakdown_over_time(question)
        grain_opts = [*sets["grain"], Option(_NONE, "no time series is asked for")]
        # A level metric read over a window is settled by the window and the
        # snapshot rule: the last snapshot inside it. No restricting field is
        # asked for — a named value still binds its column (`_filters`).
        snapshot = self._level_windowed(question, entity, initial_metrics)
        # The column a "by <dimension>" phrase groups by is not also a
        # restriction: "sales by store name" names the breakdown, not a
        # value of store_name to filter on.
        filter_members = [o for o in own_members if o.name != named_dim]
        filter_candidates = (
            [] if snapshot else self._filter_candidates(question, entity, filter_members)
        )
        round_two: dict[str, tuple[str, str, list[Option], bool]] = {}
        if shape in _FIGURES:
            if rest_metrics and len(initial_metrics) < MAX_METRICS and initial_metrics:
                round_two[f"metric:{len(initial_metrics)}"] = (
                    "metric",
                    self._another(metric_ctx, initial_metrics),
                    [*rest_metrics, _ANOTHER_NONE],
                    True,
                )
            rest_dims = [o for o in dims if o.name not in dim_initial]
            if dim_initial:
                if rest_dims and MAX_DIMENSIONS > 1:
                    round_two["dimension:1"] = (
                        "dimension",
                        self._another(dim_ctx, dim_initial),
                        [*rest_dims, _ANOTHER_NONE],
                        True,
                    )
            else:
                round_two["dimension:0"] = (
                    ("dimension", dim_ctx, dims, False)
                    if shape == "ranking"
                    else ("dimension", dim_ctx, [*dims, _ANOTHER_NONE], True)
                )
            if entity.default_time_field and over_time:
                round_two["grain"] = ("grain", grain_ctx, grain_opts, True)
        else:
            round_two["field:0"] = ("dimension", fields_ctx, listed, False)
        # A definition is asked ("does the question engage it?") only when
        # the question carries its surface words (term or aliases): under
        # act-unless-none an "engaged" on a question that never named the
        # term would apply a filter nobody asked for — a silent-wrong on every
        # question for every definition. Aliases are how authoring widens it.
        for d in model.definitions:
            if ambiguity.triggered(question, d) and is_predicate(d.sql_expr or "", model.dialect):
                round_two[f"definition:{d.term}"] = (
                    "definition",
                    self._definition_ctx(d),
                    _ENGAGED,
                    False,
                )
        if filter_candidates:
            round_two["filter:0"] = (
                "filter",
                self._filter_ctx([]),
                [*filter_candidates, Option(_NONE, "no further restriction is asked for")],
                True,
            )
        self._fan_out(question, round_two, records)

        # Round 3: everything that depends only on the first picks — the
        # second metric / dimension / field ("another, or none"), the first
        # filter column's value (or whether a value is stated at all), the
        # second filter column — one call. A question that needs a third of
        # anything falls to the sequential steps; most never do.
        round_three: dict[str, tuple[str, str, list[Option], bool]] = {}
        next_metric = self._peek(f"metric:{len(initial_metrics)}") if initial_metrics else None
        if next_metric is not None:
            picked = [*initial_metrics, next_metric]
            rest = [o for o in rest_metrics if o.name != next_metric]
            if rest and len(picked) < MAX_METRICS:
                round_three[f"metric:{len(picked)}"] = (
                    "metric",
                    self._another(metric_ctx, picked),
                    [*rest, _ANOTHER_NONE],
                    True,
                )
        next_dim = self._peek(f"dimension:{len(dim_initial)}")
        if next_dim is not None:
            picked = [*dim_initial, next_dim]
            rest = [o for o in dims if o.name not in picked]
            if rest and len(picked) < MAX_DIMENSIONS:
                round_three[f"dimension:{len(picked)}"] = (
                    "dimension",
                    self._another(dim_ctx, picked),
                    [*rest, _ANOTHER_NONE],
                    True,
                )
        first_field = self._peek("field:0")
        if first_field is not None:
            rest = [o for o in listed if o.name != first_field]
            if rest:
                round_three["field:1"] = (
                    "dimension",
                    self._another(fields_ctx, [first_field]),
                    [*rest, _ANOTHER_NONE],
                    True,
                )
        first_filter = self._peek("filter:0")
        if first_filter is not None:
            spec = self._value_spec(question, entity, first_filter)
            if spec is not None:
                round_three[spec[0]] = spec[1]
            rest = [o for o in filter_candidates if o.name != first_filter]
            if rest and MAX_FILTERS > 1:
                round_three["filter:1"] = (
                    "filter",
                    self._filter_ctx([first_filter]),
                    [*rest, Option(_NONE, "no further restriction is asked for")],
                    True,
                )
        self._fan_out(question, round_three, records)

        metrics: list[str] = []
        fields: list[str] = []
        if shape in _FIGURES:
            metrics = [
                m.rsplit(".", 1)[-1]
                for m in self._pick_many(
                    "metric",
                    question,
                    metric_ctx,
                    metric_options,
                    cap=MAX_METRICS,
                    records=records,
                    clarify_term="metric",
                    first_required=True,
                    key_prefix="metric",
                    initial=initial_metrics or None,
                )
            ]
            if not metrics:
                raise _Decline("no governed metric of this lens answers the question")
        else:
            fields = self._pick_many(
                "dimension",
                question,
                fields_ctx,
                listed,
                cap=6,
                records=records,
                clarify_term="field",
                first_required=True,
                key_prefix="field",
            )
            if not fields:
                raise _Decline("no field of this lens matches what the listing asks for")

        dimensions: list[str] = []
        if shape in _FIGURES:
            dimensions = self._pick_many(
                "dimension",
                question,
                dim_ctx,
                dims,
                cap=MAX_DIMENSIONS,
                records=records,
                clarify_term="dimension",
                # a ranking ranks SOMETHING: one figure per X needs its X
                first_required=shape == "ranking",
                key_prefix="dimension",
                initial=dim_initial or None,
            )

        if shape == "ranking" and not dimensions:
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term="dimension",
                    question="Ranked across what? "
                    + (
                        ", ".join(o.name for o in dims[:6]) + "."
                        if dims
                        else f"No field of {entity.name} groups this question; its fields "
                        f"are {', '.join(fallback)}."
                    ),
                    options=fallback,
                )
            )

        grain: TimeGrain | None = None
        if shape in _FIGURES and entity.default_time_field and over_time:
            g = self._pick(
                "grain",
                question,
                grain_ctx,
                grain_opts,
                allow_none=True,
                records=records,
                clarify_term="grain",
                clarify_question="Over which period should the figure be broken down — "
                "hour, day, week, month, quarter, year, or none?",
                key="grain",
            )
            grain = g if g in {o.name for o in sets["grain"]} else None  # type: ignore[assignment]

        definitions = self._definitions(question, model, records)
        if shape in _FIGURES and (noun := undeclared_measure(question, entity, model)):
            unit = ", ".join(metrics)
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term=noun,
                    question=f"This lens measures {unit} here; '{noun}' is not declared as a "
                    f"metric of {entity.name}. Which of its metrics does the question mean, "
                    "or is it for another lens?",
                    options=[m.name for m in entity.metrics],
                )
            )
        if shape in _FIGURES and (counted := uncounted_noun(question, entity, metrics)):
            counts = [
                m.name
                for m in entity.metrics
                if m.type == "simple" and m.agg in ("count", "count_distinct")
            ]
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term=counted,
                    question=f"The question counts {_plural(counted)}, and {', '.join(metrics)} "
                    f"does not count them. Which metric of {entity.name} does it mean, or is "
                    "it for another lens?",
                    options=counts or [m.name for m in entity.metrics],
                )
            )
        if shape in _FIGURES and (
            word := unconsumed_quantity(question, entity, metrics, model, self._domains_for(entity))
        ):
            # One confident option is not a reading of the question: a count
            # served for "what was GRV" answers in another unit.
            labels = ", ".join(m.label or m.name for m in entity.metrics)
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term=word,
                    question=f"This lens has no metric for '{word}'; it measures {labels}. "
                    "Which of them does the question mean, or is it for another lens?",
                    options=[m.name for m in entity.metrics],
                )
            )
        filters, supplied = self._filters(
            question,
            entity,
            filter_members,
            _carried(entity, metrics, model.dialect),
            records,
            snapshot=self._level_windowed(question, entity, metrics),
        )
        if code := unheld_code(question, entity, model, self._domains_for(entity)):
            if not any(str(f.value).lower() == code.lower() for f in filters):
                raise _Clarify(
                    ClarificationRequest(
                        kind="unresolved_slot",
                        term=code,
                        question=f"This lens has no field for {code}: no column of "
                        f"{entity.name} holds it. Is the question meant without it, or for "
                        "another lens?",
                        options=[entity.name],
                    )
                )
        if conflict := _population_conflict(entity, filters, model.dialect):
            raise _Decline(conflict)
        _refuse_self_defining_filters(entity, metrics, filters)
        filters += self._window(question, entity, metrics, records)
        # A column an equality filter pins to one value is a constant: grouping
        # by it adds a column and no information, and whether a decider added it
        # was the only thing that varied between runs of one question. A named
        # value is kept out of the dimensions up front; a decided or bound one
        # is known only now, so it is dropped here — the same shape every run.
        pinned = {f.field for f in filters if f.op == "="}
        if pinned and set(dimensions) & pinned:
            dimensions = [d for d in dimensions if d not in pinned]
            if shape == "ranking" and not dimensions:
                raise _Clarify(
                    ClarificationRequest(
                        kind="unresolved_slot",
                        term="dimension",
                        question="Ranked across what? The field it would rank is fixed to "
                        "one value by the question.",
                        options=[o for o in fallback if o not in pinned] or fallback,
                    )
                )
        if shape in _FIGURES and (
            missing := unplaced_breakdown(question, entity, metrics, dimensions, grain, filters)
        ):
            records.append(
                DecisionRecord(
                    slot="dimension", chosen=None, verdict="clarify", provider=QUESTION_TEXT
                )
            )
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term="dimension",
                    question=f"The question asks a breakdown by {missing}, and the answer "
                    f"would group by nothing that is one. Which field of {entity.name} should "
                    f"it be broken down by? {', '.join(fallback)}",
                    options=fallback,
                )
            )
        order_by, limit = self._order(question, metrics, fields, self._better(entity, metrics))
        if shape == "ranking" and not order_by:
            # Read as a ranking with no direction the question states: which end
            # is the answer is not the resolver's to guess.
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term="order",
                    question="Ranked by which end — highest or lowest first?",
                    options=["highest first", "lowest first"],
                )
            )

        intent = QueryIntent(
            entity=entity.name,
            metrics=metrics,
            dimensions=dimensions,
            fields=fields,
            definitions=definitions,
            grain=grain,
            filters=filters,
            order_by=order_by,
            limit=limit,
        )
        return intent, supplied

    def _contradicted(self, question: str, model: SemanticModel) -> dict[str, str]:
        """Metric option name (bare and qualified) -> why it can never answer
        this question: its own filter pins a column to values the question
        names another stored value of. "the one-way ride minutes" names
        'one-way' of trip_type word for word; a metric counting only
        `trip_type = 'overnight'` cannot answer it whatever a decider prefers, so
        it is not offered — the named value then binds as a filter on a metric
        that can. Deterministic: a dictionary read and the metric's own SQL."""
        out: dict[str, str] = {}
        for e in model.entities:
            named = self._named(question, e)
            if not named:
                continue
            for m in e.metrics:
                for n in named:
                    for column, value in n.items():
                        if contradicted_literal(m, column, [value], model.dialect):
                            why = f"The question names '{value}' of {e.name}.{column}"
                            out.setdefault(m.name, why)
                            out.setdefault(f"{e.name}.{m.name}", why)
        return out

    def _holds_the_named(
        self,
        question: str,
        model: SemanticModel,
        entity: Entity,
        shape: str,
        first_metric: str | None,
        metric_options: list[Option],
        owner_of: dict[str, str],
        records: list[DecisionRecord],
    ) -> tuple[Entity, str | None]:
        """A value the question names (a dictionary member, or a word a
        definition's ``value_aliases`` maps to one) is a restriction the answer
        must carry — and the entity the metric or the listing chose may have no
        column that holds it. "Which carry heroes have the highest win rate on
        the current patch?" typed onto a per-patch table with no position and
        served every hero, with a note that no position filter applied: the
        filter the question stated was dropped because the chosen table could
        not apply it, while its sibling could. A stated filter the chosen entity
        cannot apply changes the entity or asks; it is never dropped.

        Among the entities of the lens that hold a column for EVERY such value,
        the figure's metric (or the listing's entity) is decided again,
        constrained to them — the metric names the entity, so the same decision
        that chose the first table chooses the right one. No holder with a
        figure to offer, or no confident pick among them, is a clarification
        naming both sides."""
        here = {v for named in self._named(question, entity) for v in named.values()}
        # A value the chosen metric is named for (radiant_win_rate's 'radiant')
        # is what it counts, not a restriction the entity fails to hold.
        carried = _carried(entity, [first_metric] if first_metric else [], model.dialect)
        # value -> the columns holding it -> the entities with that column
        missing: dict[str, dict[str, set[str]]] = {}
        for e in model.entities:
            if e.name == entity.name:
                continue
            for named in self._named(question, e):
                for column, value in named.items():
                    if value not in here and not carried.carries(value):
                        missing.setdefault(value, {}).setdefault(column, set()).add(e.name)
        if not missing:
            return entity, first_metric
        holding = set.intersection(
            *(
                {e for holders in by_column.values() for e in holders}
                for by_column in missing.values()
            )
        )
        # Model order throughout, so the options and the question read the same
        # on every run of the same question.
        holders = [e.name for e in model.entities if e.name in holding]
        stated = "; ".join(
            f"'{value}' is a {' / '.join(sorted(by_column))} value"
            for value, by_column in missing.items()
        )
        where = ", ".join(holders) or "no entity of this lens"
        if shape in _FIGURES:
            slot = "metric"
            options = [o for o in metric_options if owner_of.get(o.name) in holding]
            ask = "Which governed metric of those does the question ask for?"
        else:
            slot = "entity"
            options = [
                Option(e.name, " ".join(p for p in (e.description or "", e.grain or "") if p))
                for e in model.entities
                if e.name in holding
            ]
            ask = "Which of those is the question about?"
        if slot == "entity" and len(options) == 1:
            # The one table that holds the value the listing is restricted by.
            return next(e for e in model.entities if e.name == options[0].name), None
        if options:
            d, verdict, _idx = self._decide(
                slot,
                question,
                f"{stated} — a field {where} holds and {entity.name} does not, and the "
                f"question restricts by it. {ask}",
                [*options, Option(_NONE, "none of these answers the question")],
                allow_none=True,
                records=records,
            )
            if verdict == "act" and d.chosen is not None:
                if slot == "metric":
                    owner = owner_of[d.chosen]
                    return next(e for e in model.entities if e.name == owner), d.chosen
                return next(e for e in model.entities if e.name == d.chosen), None
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term=next(iter(missing)),
                question=f"{stated}: {where} has it; {entity.name} does not — which should "
                "answer, or is the question meant without it?",
                options=[*holders, entity.name],
            )
        )

    def _noted(self, context: str) -> str:
        return f"{context} {self._notes}".strip() if getattr(self, "_notes", "") else context

    @staticmethod
    def _definition_ctx(d: Definition) -> str:
        return (
            f"The governed term '{d.term}' means: {d.body}. Does the question engage "
            "that meaning (should its filter apply)?"
        )

    def _filter_candidates(
        self, question: str, entity: Entity, members: list[Option], *, decided: bool = True
    ) -> list[Option]:
        """The fields a filter may restrict by: never the entity's time field,
        and when the question states a window no date-typed field and no
        profiled month-period field either — the deterministic window owns
        time (rule 5), so "in August 2026" is a range on the time field, not
        a `due_date` or `month` value the agent must bind."""
        time_field = (entity.default_time_field or "").lower()
        windowed = bool(timewindow.temporal_terms(question))
        period = (self._period_field(entity) or "").lower() if windowed else ""
        out: list[Option] = []
        for m in members:
            if m.name.lower() in (time_field, period) and m.name.lower():
                continue
            # A numeric column restricts only by a number the question states:
            # offered otherwise, an id column (ally_hero_id beside ally_hero_name)
            # wins the pick and the filter then asks for a number nobody gave.
            ftype = _field_type(entity, m.name)
            if (
                ftype in ("integer", "number")
                and self._literal_in_question(question, ftype) is None
            ):
                continue
            if windowed and _field_type(entity, m.name) in ("date", "timestamp"):
                continue
            # A period flag (`is_current_month`, `latest_complete_week`) beside
            # a stated window would ask again what the window already says.
            if (
                decided
                and windowed
                and not _says(question, m.name)
                and set(_WORD.findall(m.name.lower())) & _PERIOD_FLAG_WORDS
            ):
                continue
            out.append(m)
        # A decided filter is never on a key column or an unasked timestamp; a
        # stored value the question names binds its column whatever it is.
        return offered(question, entity, out) if decided else out

    @staticmethod
    def _filter_ctx(columns: list[str]) -> str:
        return (
            "Does the question restrict the rows by a field's value (a status, a "
            "region, a customer, an amount)? Pick the field, or none."
            + (f" Already applied: {', '.join(columns)}." if columns else "")
        )

    def _definitions(
        self, question: str, model: SemanticModel, records: list[DecisionRecord]
    ) -> list[str]:
        out: list[str] = []
        resolved = {r.term for r in ambiguity.resolve(question, model)}
        for d in model.definitions:
            if not ambiguity.triggered(question, d):
                continue
            if d.status == "ambiguous" and len(d.possible_mappings) >= 2 and d.term not in resolved:
                raise _Clarify(
                    ClarificationRequest(
                        kind="ambiguous_term",
                        term=d.term,
                        question=f"'{d.term}' has more than one meaning here — which do you mean? "
                        + "; ".join(d.possible_mappings),
                        options=list(d.possible_mappings),
                    )
                )
            if not is_predicate(d.sql_expr or "", model.dialect):
                continue
            pick = self._pick(
                "definition",
                question,
                self._definition_ctx(d),
                _ENGAGED,
                allow_none=False,
                records=records,
                clarify_term=d.term,
                clarify_question=f"Should the governed meaning of '{d.term}' apply here?",
                key=f"definition:{d.term}",
            )
            if pick == "engaged":
                out.append(d.term)
        return out

    def _filters(
        self,
        question: str,
        entity: Entity,
        members: list[Option],
        carried: _Carried,
        records: list[DecisionRecord],
        *,
        snapshot: bool = False,
    ) -> tuple[list[IntentFilter], list[str]]:
        filters: list[IntentFilter] = []
        supplied: list[str] = []
        candidates = [] if snapshot else self._filter_candidates(question, entity, members)
        # Column, then its value, then "another filter or none" — one filter at a
        # time, so the value decision follows the column it belongs to.
        columns: list[str] = []
        while candidates and len(columns) < MAX_FILTERS:
            column = self._pick(
                "filter",
                question,
                self._filter_ctx(columns),
                [*candidates, Option(_NONE, "no further restriction is asked for")],
                allow_none=True,
                records=records,
                clarify_term="filter",
                clarify_question="Which field should the answer be restricted by? "
                + ", ".join(o.name for o in candidates[:6]),
                key=f"filter:{len(columns)}",
            )
            if column is None:
                break
            columns.append(column)
            candidates = [o for o in candidates if o.name != column]
            self._value(question, entity, column, filters, supplied, records)
        # A value the question names is a restriction it asked for: serving
        # without it answers a different question. The general "any restriction?"
        # decision can miss it (the name read as a field to show), so each
        # unused one gets a focused decision among the columns holding it; a
        # "none" there is still never a silent drop — the caller is asked.
        for named in self._named(question, entity):
            if _uses(named, filters) or any(carried.carries(v) for v in named.values()):
                continue
            value = next(iter(named.values()))
            free = [c for c in sorted(named) if c not in columns]
            column = (
                self._pick(
                    "filter",
                    question,
                    f"The question names '{value}', a stored value of "
                    f"{', '.join(free)}. Which field does it restrict the answer by?",
                    [
                        *(Option(c) for c in free),
                        Option(_NONE, "it restricts nothing; the name is incidental"),
                    ],
                    allow_none=True,
                    records=records,
                    clarify_term="filter",
                    clarify_question=f"Which field should '{value}' restrict? " + ", ".join(free),
                    key=f"named:{value}",
                )
                if free and len(columns) < MAX_FILTERS
                else None
            )
            if column is not None:
                columns.append(column)
                filters.append(IntentFilter(field=column, op="=", value=named[column]))
                continue
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term=value,
                    question=f"The question names '{value}', a stored value of "
                    f"{', '.join(sorted(named))}, but no filter of the answer uses it. "
                    "Which field should it restrict? Supply it as a binding.",
                    options=sorted(named),
                )
            )
        # A filter to the value the metric selects its rows by decides it: the
        # draw rate over `lane_result = 'drawn'`, filtered to drawn lanes, is
        # 1.0 on every row. The question's 'drawn' is the metric's already.
        decided = [
            f
            for f in filters
            if f.op == "="
            and f.field not in supplied
            and (f.field.lower(), str(f.value).lower()) in carried.selects
        ]
        for f in decided:
            filters.remove(f)
            if f.field in self._matched:
                self._matched.remove(f.field)
        return filters, supplied

    def _named(self, question: str, entity: Entity) -> list[dict[str, str]]:
        """The stored values the question names among the entity's filter
        candidates — text fields with a complete dictionary (named_values)."""
        members = [
            *(Option(d.name) for d in entity.dimensions),
            *(Option(f.name) for f in entity.fields),
        ]
        domains = self._domains_for(entity)
        text: dict[str, list[str]] = {}
        for o in self._filter_candidates(question, entity, members, decided=False):
            values = domains.get(o.name.lower())
            if values and _field_type(entity, o.name) in (None, "string"):
                text[o.name] = values
        aliases = {c: self._value_aliases.get(c.lower(), {}) for c in text}
        return named_values(question, text, aliases)

    def _named_for(
        self, question: str, entity: Entity, column: str, filters: list[IntentFilter]
    ) -> list[str]:
        """The named values this column holds that no filter uses yet."""
        return [
            n[column]
            for n in self._named(question, entity)
            if column in n and not _uses(n, filters)
        ]

    def _decidable_domain(self, entity: Entity, column: str) -> list[str] | None:
        """The dictionary a model may pick a filter value from: text and boolean only.
        A number (a bracket id, a year, an amount) is a fact the question states
        or it is not — picking '3' off a list of ids for 'Legend' is a guess, so
        numeric values come only from a literal in the question or a binding."""
        if _field_type(entity, column) not in (None, "string", "boolean"):
            return None
        return self._domains_for(entity).get(column.lower())

    @staticmethod
    def _named_ctx(column: str) -> str:
        return (
            f"The question names more than one stored value of '{column}'. Which is its '{column}'?"
        )

    @staticmethod
    def _value_options(domain: list[str]) -> list[Option]:
        return [
            *(Option(v) for v in domain),
            Option(_UNSTATED, "the question states no value for this field"),
            Option(_NONE, "the question states a value, but not one of these"),
        ]

    def _value_spec(
        self, question: str, entity: Entity, column: str
    ) -> tuple[str, tuple[str, str, list[Option], bool]] | None:
        """The value decision a filter column needs, as a fan-out spec — the
        same question ``_value`` asks, so a round may ask it early. None when
        no decision is needed (a binding, a literal in the question)."""
        ftype = _field_type(entity, column)
        domain = self._decidable_domain(entity, column)
        bound = self._bindings.get(column.lower())
        if domain and bound is not None and bound in domain:
            return None
        named = self._named_for(question, entity, column, [])
        if len(named) > 1:
            return (
                f"filter_value:{column}",
                ("filter_value", self._named_ctx(column), [Option(v) for v in named], False),
            )
        if named:
            return None
        if domain and len(domain) <= LOW_CARDINALITY_MAX:
            return (
                f"filter_value:{column}",
                (
                    "filter_value",
                    f"Which stored value of '{column}' does the question mean?",
                    self._value_options(domain),
                    True,
                ),
            )
        if bound is not None or self._literal_in_question(question, ftype) is not None:
            return None
        return (
            f"stated:{column}",
            (
                "filter_value",
                f"Does the question state a value for '{column}' to filter on?",
                _STATED,
                False,
            ),
        )

    def _value(
        self,
        question: str,
        entity: Entity,
        column: str,
        filters: list[IntentFilter],
        supplied: list[str],
        records: list[DecisionRecord],
    ) -> None:
        ftype = _field_type(entity, column)
        domain = self._decidable_domain(entity, column)
        bound = self._bindings.get(column.lower())
        if domain and bound is not None and bound in domain:
            # The caller bound it: the value is theirs, no decision to make.
            filters.append(IntentFilter(field=column, op="=", value=bound))
            supplied.append(column)
            return
        named = self._named_for(question, entity, column, filters)
        if named:
            # The question names the value: one named value is the value, read
            # off the text and recorded as such; two named values of the same
            # column ("Crystal Maiden's win rate with Lion") decide only which
            # is this column's — the other is the next filter's.
            if len(named) == 1:
                value: str | None = named[0]
                records.append(
                    DecisionRecord(
                        slot="filter_value", chosen=value, verdict="act", provider=QUESTION_TEXT
                    )
                )
            else:
                value = self._pick(
                    "filter_value",
                    question,
                    self._named_ctx(column),
                    [Option(v) for v in named],
                    allow_none=False,
                    records=records,
                    clarify_term=column,
                    clarify_question=f"Which of {', '.join(repr(v) for v in named)} "
                    f"is the question's '{column}'?",
                    key=f"filter_value:{column}",
                )
            if value is None:
                raise _Clarify(
                    ClarificationRequest(
                        kind="unresolved_slot",
                        term=column,
                        question=f"Which of {', '.join(repr(v) for v in named)} "
                        f"is the question's '{column}'?",
                        options=named,
                    )
                )
            filters.append(IntentFilter(field=column, op="=", value=value))
            self._matched.append(column)
            return
        if domain and len(domain) <= LOW_CARDINALITY_MAX:
            value = self._pick(
                "filter_value",
                question,
                f"Which stored value of '{column}' does the question mean?",
                self._value_options(domain),
                allow_none=True,
                records=records,
                clarify_term=column,
                clarify_question=f"Which value of '{column}'? It holds: {', '.join(domain)}.",
                key=f"filter_value:{column}",
            )
            if value == _UNSTATED:
                return  # not a filter after all — dropped, on the record
            if value is None:
                raise _Clarify(
                    ClarificationRequest(
                        kind="unknown_value",
                        term=column,
                        question=f"The question's value for '{column}' is not one it holds. "
                        f"It holds: {', '.join(domain)}.",
                        options=list(domain),
                    )
                )
            filters.append(IntentFilter(field=column, op="=", value=value))
            return
        if bound is not None:
            value_obj = self._typed_binding(column, ftype, bound)
            op = (
                self._op(question, column, records)
                if ftype in ("number", "integer", "date", "timestamp")
                else "="
            )
            filters.append(IntentFilter(field=column, op=op, value=value_obj))
            supplied.append(column)
            return
        literal = self._literal_in_question(question, ftype)
        if literal is not None:
            # The question states this number once: it restricts one column. A
            # second column reading it ('15 minutes' as minute = 15 AND
            # gold_lead = 15) filters on a value nobody gave — ask which it is.
            taken = next(
                (f.field for f in filters if f.field != column and f.value == literal), None
            )
            if taken is not None:
                raise _Clarify(
                    ClarificationRequest(
                        kind="unresolved_slot",
                        term=column,
                        question=f"The question states {literal} once, and both '{taken}' "
                        f"and '{column}' would read it — which field does it restrict?",
                        options=[taken, column],
                    )
                )
            op = self._op(question, column, records)
            filters.append(IntentFilter(field=column, op=op, value=literal))
            return
        # No option set (no dictionary, or one too long to offer), no named
        # value, no binding, no literal: one more decision before asking the
        # agent to bind — does the question state a value at all? A column
        # picked at low p on a question that names no value is not a filter.
        stated = self._pick(
            "filter_value",
            question,
            f"Does the question state a value for '{column}' to filter on?",
            _STATED,
            allow_none=False,
            records=records,
            clarify_term=column,
            clarify_question=f"Which value of '{column}' should the answer filter on?",
            key=f"stated:{column}",
        )
        if stated == _UNSTATED:
            return
        raise _Clarify(
            ClarificationRequest(
                kind="unresolved_slot",
                term=column,
                question=f"Which value of '{column}' should the answer filter on? "
                f"Supply it as a binding ({_grammar_for(ftype)}).",
                options=[_grammar_for(ftype)],
            )
        )

    def _op(self, question: str, column: str, records: list[DecisionRecord]) -> FilterOp:
        pick = self._pick(
            "filter_op",
            question,
            f"How does the question compare '{column}' to the value?",
            _OPS,
            allow_none=False,
            records=records,
            clarify_term=column,
            clarify_question=f"Should '{column}' be equal to, above, or below the value?",
        )
        return cast(FilterOp, pick or "=")

    @staticmethod
    def _typed_binding(column: str, ftype: str | None, raw: str) -> object:
        raw = raw.strip()
        if ftype in ("number", "integer"):
            if not re.fullmatch(r"-?\d+(\.\d+)?", raw):
                raise _Clarify(
                    ClarificationRequest(
                        kind="unresolved_slot",
                        term=column,
                        question=f"The binding for '{column}' must be a number, got {raw!r}.",
                        options=[GRAMMAR["number"]],
                    )
                )
            return float(raw) if "." in raw else int(raw)
        if ftype in ("date", "timestamp"):
            try:
                date.fromisoformat(raw)
            except ValueError as exc:
                raise _Clarify(
                    ClarificationRequest(
                        kind="unresolved_slot",
                        term=column,
                        question=f"The binding for '{column}' must be YYYY-MM-DD, got {raw!r}.",
                        options=[GRAMMAR["date"]],
                    )
                ) from exc
            return raw
        return raw

    @staticmethod
    def _literal_in_question(question: str, ftype: str | None) -> object | None:
        """A number or ISO date resolves from the question only when exactly one
        standalone literal of that type appears — one is a fact, two is a guess."""
        if ftype in ("number", "integer"):
            stripped = _ISO_DATE.sub(" ", _YEARISH.sub(" ", question))
            nums = _NUMBER.findall(stripped)
            if len(nums) == 1:
                return float(nums[0]) if "." in nums[0] else int(nums[0])
            return None
        if ftype in ("date", "timestamp"):
            dates = _ISO_DATE.findall(question)
            return dates[0] if len(dates) == 1 else None
        return None

    def _window(
        self, question: str, entity: Entity, metrics: list[str], records: list[DecisionRecord]
    ) -> list[IntentFilter]:
        filters = window_filters(
            question, entity, metrics, period_field=self._period_field(entity), today=self._today
        )
        current = sorted(t for t in timewindow.temporal_terms(question) if t.startswith("this "))
        spans = timewindow.window_ranges(frozenset(current), self._today) if current else []
        if filters and spans:
            # "this month", "today": the calendar period to date on the lens
            # clock, never asked back — and said, so the answer reads as the
            # partial period it is.
            start, end = spans[0]
            records.append(
                DecisionRecord(
                    slot="window",
                    chosen=f"{current[0]}: {start.isoformat()} to "
                    f"{min(end, self._today).isoformat()} (to date, lens clock)",
                    verdict="act",
                    provider=QUESTION_TEXT,
                )
            )
        return filters

    def _level_windowed(self, question: str, entity: Entity, metrics: list[str]) -> bool:
        """Is a level metric (a snapshot figure) read over a stated window?"""
        bare = {m.rsplit(".", 1)[-1] for m in metrics}
        return bool(timewindow.temporal_terms(question)) and any(
            m.additivity == "level" for m in entity.metrics if m.name in bare
        )

    def _shape_after_none(
        self,
        question: str,
        model: SemanticModel,
        metric_options: list[Option],
        records: list[DecisionRecord],
    ) -> str:
        """The shape decision chose `none` confidently. Two declared facts
        overrule a decline: a governed metric the question names word for
        word is a figure asked for, and a question an entity declares it
        answers (``anchored``) is one the lens answers — asked about, never
        refused."""
        if named_options(question, metric_options):
            beside = timewindow.beside_windows(question)
            ranks = any(r.search(beside) for r in (_TOP, _BOTTOM, _HIGHEST, _LOWEST))
            if not ranks:
                records.append(
                    DecisionRecord(
                        slot="shape", chosen="aggregate", verdict="act", provider=QUESTION_TEXT
                    )
                )
                return "aggregate"
        if (hit := anchored(question, model)) is not None or named_options(
            question, metric_options
        ):
            why = (
                f"It matches what {hit[0]} is declared for ('{hit[1]}')"
                if hit
                else "It names a governed metric"
            )
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term="shape",
                    question=f"{why} — is it asking for a figure, a ranking, or a list of records?",
                    options=[o.name for o in _SHAPES],
                )
            )
        raise _Decline("not a question this lens's data answers (no figure, no listing)")

    @staticmethod
    def _better(entity: Entity, metrics: list[str]) -> str | None:
        """The declared better end of the metric a ranking orders by, if any."""
        if not metrics:
            return None
        return next((m.better for m in entity.metrics if m.name == metrics[0]), None)

    @staticmethod
    def _order(
        question: str, metrics: list[str], fields: list[str], better: str | None = None
    ) -> tuple[list[IntentOrder], int | None]:
        limit: int | None = None
        direction: Literal["asc", "desc"] | None = None
        # A number inside a window phrase counts days, never ranks: "the
        # first 2 days of October" is no top 2.
        question = timewindow.beside_windows(question)
        if m := _TOP.search(question):
            limit, direction = int(m.group(1)), "desc"
        elif m := _BOTTOM.search(question):
            limit, direction = int(m.group(1)), "asc"
        elif _HIGHEST.search(question):
            direction = "desc"
        elif _LOWEST.search(question):
            direction = "asc"
        if direction is None and (ranked := _RANKED.search(question)) and metrics and better:
            # 'best' is the declared better end; 'worst' the other one.
            best = ranked.group(0).lower() == "best"
            direction = "desc" if best == (better == "higher") else "asc"
        if direction is None and _RANKED.search(question):
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term="order",
                    question="The question ranks by 'best' or 'worst' — by which figure, "
                    "and is higher or lower better?",
                )
            )
        if direction is None:
            return [], limit
        if not metrics:
            # The question ranks ('highest', 'most', 'top 5') but the reading
            # carries no measure to rank by: dropping the order would serve an
            # unordered listing as the answer. Ask instead — with escalation on,
            # raw-SQL generation takes the question.
            raise _Clarify(
                ClarificationRequest(
                    kind="unresolved_slot",
                    term="order",
                    question="The question ranks by a measure, but this reading found "
                    "none to rank on — which figure should the answer be ordered by?",
                )
            )
        return [IntentOrder(field=metrics[0], dir=direction)], limit


class TypedIntentGenerator:
    """The generator seam over the typed resolver: act on every slot → compiled
    SQL; a clarify → the clarification; a decline → the named gap. Consumes no
    generation tokens of its own — the decisions are the cost. Repair feedback
    is ignored on purpose: the same question decides the same way."""

    def __init__(
        self, resolver: TypedResolver, *, column_types: dict[str, dict[str, str]] | None = None
    ) -> None:
        self._resolver = resolver
        self._column_types = column_types
        self.model = "typed"

    def generate(
        self,
        *,
        question: str,
        semantic_model: SemanticModel,
        prose_context: list[ContextChunk],
        dialect: str,
        feedback: str | None = None,
    ) -> GeneratedQuery:
        notes = [c.text for c in prose_context if c.source.startswith("ambiguity-resolution:")]
        try:
            res = self._resolver.resolve(question, semantic_model, notes=notes)
        except Exception as exc:
            # The provider failing is not the question failing to type: the
            # pipeline serves it untyped where that is accepted, or refuses it
            # with the failure named. Anything else is a defect and propagates.
            what = typed_health.provider_failure(exc)
            if what is None:
                raise
            typed_health.failed(what)
            raise typed_health.TypedUnavailable(what) from exc
        typed_health.succeeded()
        if res.clarification is not None:
            return GeneratedQuery(sql="", clarification=res.clarification, decisions=res.decisions)
        if res.intent is None:
            return GeneratedQuery(
                sql="", no_answer_reason=res.decline or "did not type", decisions=res.decisions
            )
        try:
            res.intent = govern_literals(
                prefer_weighted_ratio(res.intent, semantic_model), semantic_model
            )
            entity = next((e for e in semantic_model.entities if e.name == res.intent.entity), None)
            sql = compile_intent(
                res.intent,
                semantic_model,
                domains=self._resolver._domains_for(entity) if entity else self._resolver._domains,
                column_types=self._column_types,
            )
        except UnpinnedDimension as exc:
            # The reading is complete but sums kinds of record that do not add:
            # which kind is the caller's to say, so ask rather than decline.
            return GeneratedQuery(
                sql="", clarification=exc.clarification(), decisions=res.decisions
            )
        except CompileError as exc:
            return GeneratedQuery(
                sql="",
                no_answer_reason=f"the typed intent did not compile: {exc}",
                decisions=res.decisions,
            )
        return GeneratedQuery(
            sql=sql,
            definition_used=intent_term(res.intent),
            intent=res.intent,
            decisions=res.decisions,
            supplied=res.supplied,
            typed=True,
            declined=res.declined,
        )
