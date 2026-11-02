"""Driver mode: the caller is an agent doing its own reading, so dst calls no model.

dst spends model tokens only where no driver agent is present. When the
caller is an agent (the MCP server marks every request ``X-DST-Driver: 1``;
an admin can mark a caller a driver) and the lens serves through Cube, the
agent reads the question and writes the Cube query and the word ledger;
dst routes on coverage alone, runs every check on what the agent submits,
compiles through Cube, executes through its own connection and signs the
receipt. A failing check comes
back with its mode and what to change, so the agent can retry. Lenses on
the typed core are untouched by driver mode: they answer as they always do.

This module holds the pure parts: which words of a question each lens's
members and values carry (the ``why`` of a route), and what a driver changes
after a check stops it. The doors are ``services/api/route.py`` (route),
``services/api/driver.py`` (query_cube, values, data_end, grain).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field

from services.contracts.failure_modes import FailureMode
from services.contracts.lens_model import LensModel
from services.contracts.response import QueryResponse
from services.router.coverage import asked_words, vocabulary, word_keys

# Targets listed per matched word: enough to say why, bounded so a word met in
# a long dictionary does not flood the route.
TARGETS_PER_WORD = 5


class WordMatch(BaseModel):
    """One word of the question and what in the lens carries it."""

    word: str
    targets: list[str] = Field(
        default_factory=list,
        description="the members (`view.member`), values (`view.member = value`), "
        "segments and definitions that carry the word; empty when the lens holds the "
        "word but dst cannot name where (a lens not served through Cube)",
    )


class DriverCandidate(BaseModel):
    lens: str
    no_model: bool = Field(
        description="true: served through Cube, so dst calls no model for a driver here; "
        "false: the typed core, where dst reads the question with a model"
    )
    covers: bool = Field(description="the lens holds every word of the question some lens declares")
    serving: str = Field(description="cube (write a Cube query: query_cube) or typed")
    why: str
    matched: list[WordMatch] = Field(default_factory=list)
    uncovered: list[str] = Field(
        default_factory=list, description="the question's words this lens has nothing for"
    )
    score: int = Field(
        default=0,
        description="how many of the question's words the lens names (a member, title, "
        "alias or value, not prose): candidates come highest first",
    )


class ExploreTable(BaseModel):
    """An exposed table the caller may read with the sql tool: no definitions,
    only its blueprint's facts."""

    lens: str
    table: str
    grain: str | None = None
    key: list[str] | str | None = None
    versions: dict[str, object] | None = None
    rows: int | None = None
    caveats: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    matched: list[str] = Field(
        default_factory=list,
        description="the question's words the table carries (name, columns, descriptions, "
        "grain): the kit lists the tables carrying most first",
    )


def table_words(question: str, texts: Sequence[str]) -> list[str]:
    """The question's content words an exposed table carries in ``texts``
    (its name, columns, their descriptions, its blueprint's grain): what
    ranks it in the explore kit."""
    vocab = vocabulary(texts)
    return [w for w in asked_words(question) if word_keys(w) & vocab]


class ExploreKit(BaseModel):
    """What an agent needs to explore when no governed lens covers the
    question: the exposed tables in its scope and the tools to read them."""

    tables: list[ExploreTable] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    note: str = ""
    elided: int = Field(
        default=0,
        description="exposed tables left out of this list: those carrying fewest of the "
        "question's words; describe_lens(name, table) reads any of them",
    )


class DriverRoute(BaseModel):
    """route_query in driver mode over the lenses served through Cube: which
    cover the question and why, never an answer. ``choose``: several cover it equally and the driver
    picks. With none covering, ``nearest`` says what each lacks and
    ``explore_kit`` what is left to read."""

    driver: bool = True
    covered: bool
    request_id: str
    reason: str
    choose: bool = False
    candidates: list[DriverCandidate] = Field(default_factory=list)
    nearest: list[DriverCandidate] = Field(default_factory=list)
    # A lens the caller may use refuses this question by declaration (a
    # not-computable term, or an ask it says is another lens's with no lens
    # to take it): that refusal is the answer.
    refusal: QueryResponse | None = None
    explore_kit: ExploreKit | None = None
    mode: FailureMode | None = None
    no_model: dict[str, bool] = Field(
        default_factory=dict,
        description="per lens the caller may use: true where dst calls no model for a "
        "driver (served through Cube), false where it reads questions with one (typed core)",
    )


EXPLORE_TOOLS = [
    "describe_lens(name, table) — one exposed table in full: columns, blueprint, checks, facts",
    "grain(table) — the table's declared grain and key, and rows against distinct key",
    "values(lens, member, prefix) — the observed values of a column or member",
    "data_end(lens, member) — where a table's or member's data ends",
    "sql(lens, sql) — read-only SQL over the lens's tables; answers carry GRAIN and "
    "DATA END lines and say what could not be verified",
]
EXPLORE_NOTE = (
    "no governed lens covers this question. These tables are exposed without "
    "definitions: anything read from them is ungoverned. Before stating a number, check "
    "rows against distinct key (grain), where the data ends (data_end), that every word "
    "of the question is carried by a column or value you read, and whether an empty "
    "result means zero or no data; say plainly what you could not verify"
)
EXPLORE_BESIDE_NOTE = (
    "beside the lenses covering this question, these exposed tables carry as many of its "
    "words as the best lens names: read one when the lenses cover the words but do not "
    "hold what is asked. They are exposed without definitions: anything read from them is "
    "ungoverned. Before stating a number, check rows against distinct key (grain), where "
    "the data ends (data_end), and that every word of the question is carried by a column "
    "or value you read; say plainly what you could not verify"
)


def word_targets(
    question: str,
    model: LensModel,
    dictionaries: Mapping[str, Sequence[str]],
    groups: Sequence[str] = (),
) -> list[WordMatch]:
    """For each content word of ``question``, what in the lens carries it:
    a member (by name, title or alias), a value of a dimension (its observed
    values and value aliases), a segment, a definition, a company name. A
    member hidden from this caller carries nothing."""
    out: list[WordMatch] = []
    shown = [m for m in model.members if not model.hidden(m, tuple(groups))]
    for word in asked_words(question):
        keys = word_keys(word)
        found: list[str] = []
        for m in shown:
            aliases = list(getattr(m.dst, "aliases", None) or [])
            if keys & vocabulary([m.member, m.title or "", m.short_title or "", *aliases]):
                found.append(m.name)
            if m.segment is not None and keys & vocabulary(
                [m.segment.term or "", *m.segment.aliases]
            ):
                found.append(m.name)
            if m.kind != "dimension":
                continue
            found += [
                f"{m.name} = {value}"
                for value in dictionaries.get(m.base) or []
                if keys & vocabulary([str(value)])
            ]
            if m.dimension is not None:
                found += [
                    f"{m.name} = {value}"
                    for value, words in m.dimension.value_aliases.items()
                    if keys & vocabulary([value, *words])
                ]
        found += [
            f"definition {d.metric}"
            for d in [*model.definitions, *model.ambiguous]
            if keys & vocabulary([d.metric, *d.aliases])
        ]
        for company, words in model.scope.names.items():
            plain = [str(w) for w in words if not str(w).startswith("dictionary:")]
            if keys & vocabulary([company, *plain]):
                found.append(f"scope {company}")
        targets = list(dict.fromkeys(found))[:TARGETS_PER_WORD]
        if targets:
            out.append(WordMatch(word=word, targets=targets))
    return out


# What a driver changes after a check stops its query, by check name (two
# checks share a mode: a declared not-computable term and an undeclared
# member are both ``not_declared``).
CHANGE: dict[str, str] = {
    "not_computable": "nothing to retry: the lens declares this cannot be computed; relay "
    "the reason and where it says to ask instead",
    "not_for": "ask the lens the detail names (route_query lists it)",
    "scope": "use only members of this lens's views (describe_lens lists them)",
    "hidden": "drop the withheld member; it is not readable by this caller",
    "declared": "use members describe_lens lists, spelled exactly",
    "ambiguous": "the term has several readings that give different numbers: ask the user "
    "which one, or use the members of the reading they named",
    "words": "bind every listed word in the ledger to a member, value, period or segment the "
    "query uses, add what carries it to the query, or ask the user",
    "unasked_scope": "drop the filter the question did not ask for, or bind the word that "
    "asks for it in the ledger",
    "values": "use a value values(lens, member, prefix) lists, spelled exactly",
    "window": "set the dateRange to the dates the detail states",
    "period": "write every dateRange as absolute ISO dates [start, end]",
    "label_key": "group by the key the label belongs to as well (the detail names it)",
    "freshness": "the data ends before the window: narrow it to data_end, or relay the gap",
    "partial_period": "the period is partly loaded, or complete with its values still "
    "maturing (the detail says which): relay that, or end the window at the last full period",
    "empty_result": "no rows match and the zero could not be verified (the detail says "
    "what): check the filter values (values) and end the window at the data end "
    "(data_end) before reporting zero",
    "loaded": "nothing is loaded for the window (data_end names where the data ends): end "
    "the window at the data end, or relay the gap; 0 rows is not a measured zero",
    "snapshot_pin": "the probe's data end for this stock names a day with no snapshot: run "
    "`dst monitor`, or window the ask on days that hold snapshots",
    "execute": "the detail says what Cube or the warehouse refused; fix the query it names",
}


class Retry(BaseModel):
    """Why a driver's query did not serve, and what to change."""

    check: str
    mode: FailureMode | None = None
    detail: str
    change: str


def retry(response: QueryResponse) -> Retry | None:
    """The stopping check of a non-answer, with what the driver changes;
    None for an answer."""
    if response.status == "ok":
        return None
    failed = [
        c
        for c in (response.verification.checks if response.verification else [])
        if c.status == "fail"
    ]
    check = failed[-1].name if failed else "query"
    return Retry(
        check=check,
        mode=response.mode,
        detail=response.answer,
        change=CHANGE.get(check, "read the detail, change the query, and submit it again"),
    )
