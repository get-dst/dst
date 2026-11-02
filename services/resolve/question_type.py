"""The question-type step: what kind of thing was asked, before any binding.

A fifth of real questions ask for no number: what a term means, whether the
data holds something, the SQL behind a figure, which report to open, the
caller's own log, or nothing about data at all. Every door used to read each
of them as a data question and stop on the ledger at a function word
("means", "want", "where"), or route a term nobody wanted a figure for.

So every door runs this first. The deterministic rules decide most asks:
``definition`` ("what does X mean", "difference between A and B", "how is X
calculated"), ``capability`` ("do we have X", "is it possible to"),
``sql`` ("give me the SQL"), ``locate`` ("what report shows", "where can I
find"), ``admin`` ("my last request"), ``not_a_data_question`` (a greeting,
an errand, a question with no word any lens holds) and ``query`` (a data
shape: a count, a period, a breakdown). What they cannot decide is left
open; a model is asked only on the door with no driver agent, never in
driver mode, and with no model the open case is a query, as before.

Only ``query`` binds. The other types are answered here, from the lens
model and the project file, running nothing; each carries its own mode so
``dst test`` and ``dst observe`` count them apart from refusals.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from services.contracts.failure_modes import FailureMode
from services.contracts.lens_model import LensModel
from services.contracts.protocols import CacheableBlock, Message
from services.contracts.response import Citation, QueryResponse
from services.contracts.semantic_model import SemanticModel
from services.contracts.trace import TraceLog
from services.llm.registry import ResolvedModel
from services.observability import usage as ai_usage
from services.router.coverage import vocabulary, word_keys
from services.verify.words import PERIOD, STOP

log = logging.getLogger("dst.question_type")

QuestionType = Literal[
    "query", "definition", "capability", "sql", "locate", "admin", "not_a_data_question"
]
TYPES: tuple[QuestionType, ...] = (
    "query",
    "definition",
    "capability",
    "sql",
    "locate",
    "admin",
    "not_a_data_question",
)
MODE: dict[str, FailureMode] = {
    "definition": FailureMode.definition,
    "capability": FailureMode.capability,
    "sql": FailureMode.sql_request,
    "locate": FailureMode.hand_off,
    "admin": FailureMode.admin,
    "not_a_data_question": FailureMode.not_a_data_question,
}
SERVING = "question_type"


@dataclass
class Typed:
    """What the step decided. ``type`` None: the rules could not tell (a
    door with no driver may ask a model; every other door reads a query).
    ``subject``: the words the type is about (the term to define, the thing
    asked for, the data question behind a SQL ask); ``second``: the other
    side of a "difference between". ``rule``: which rule decided, for the
    receipt and the tests. ``in_dst``: a locate ask about dst itself."""

    type: QuestionType | None
    subject: str = ""
    second: str = ""
    rule: str = ""
    in_dst: bool = False
    n: int = 1
    by_model: bool = False
    # The question as asked: a sql ask carries the data question in
    # ``subject`` and the row is written under the original.
    original: str = ""
    # The language whose rule decided (the table's entry): en, fi or sv.
    lang: str = "en"


# ── the rules ───────────────────────────────────────────────────────────────
#
# One table entry per language, every rule in each: the phrasings are data,
# and a language is added by adding an entry. The group names are shared
# across languages (``a``..``h`` the subject, ``rest*`` a sql ask's data
# question, ``n`` a count): ``classify`` reads the first group that captured.


@dataclass(frozen=True)
class Rules:
    """One language's phrasings of each question type."""

    lang: str
    admin: re.Pattern[str]
    sql: re.Pattern[str]
    chat: re.Pattern[str]
    difference: re.Pattern[str]
    definition: re.Pattern[str]
    help: re.Pattern[str]
    locate: re.Pattern[str]
    capability: re.Pattern[str]
    errand: re.Pattern[str]
    # A locate ask about dst itself, and the question words a bare in-dst
    # ask starts with.
    in_dst: re.Pattern[str]
    where: re.Pattern[str]
    # Words that only a question about data uses, and period words that name
    # a period on their own: beside any vocabulary hit they decide ``query``.
    shape: frozenset[str]
    period: frozenset[str]
    # Leading determiners a subject is never about.
    articles: str = ""

    def patterns(self) -> dict[str, re.Pattern[str]]:
        return {
            k: v
            for k, v in self.__dict__.items()
            if isinstance(v, re.Pattern) and k not in ("in_dst", "where")
        }


_NUMBER_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "ten": 10,
    "kaksi": 2,
    "kolme": 3,
    "neljä": 4,
    "viisi": 5,
    "kymmenen": 10,
    "två": 2,
    "tre": 3,
    "fyra": 4,
    "fem": 5,
    "tio": 10,
}

# ── English ─────────────────────────────────────────────────────────────────

_LEAD = r"(?:please |pls |can you |could you |would you |hey |hi |ok |so )*"
_ART = r"(?:the |a |an |our |my |your |this |that |its |their )?"

EN = Rules(
    lang="en",
    articles="the |a |an |our |my |your |this |that |its |their ",
    admin=re.compile(
        rf"^{_LEAD}(?:show|list|what|whats|what's|give|get|see|repeat|find)?\b.*?"
        r"\b(?:my|our) (?:last|latest|previous|recent|earlier|own) "
        r"(?:(?P<n>\d+|two|three|four|five|ten) )?"
        r"(?:requests?|questions?|quer(?:y|ies)|asks?|answers?)\b"
        r"|^what did (?:i|we) (?:just |last )?ask"
        r"|^(?:show|list|see)\b.*\b(?:my|our) (?:request|query|question) (?:log|history)"
    ),
    sql=re.compile(
        rf"^{_LEAD}(?:give|show|send|write|print|get|return|generate|produce|see|want|need|"
        rf"what is|whats|what's)(?: me| us)? {_ART}(?:compiled |underlying |raw |cube |full )?"
        r"(?:sql|query|cube query|cube json|code)\b(?: statement| text| you (?:ran|used|would "
        r"run))?(?:\s*(?:for|behind|of|that|:|-|to)\s*(?P<rest>.+))?$"
        r"|^(?P<rest2>.+?)[,:]? (?:as|in) (?:sql|a query|cube json|cube query)$"
        r"|^(?:what|which) (?:sql|query) (?:would|do|does|did|will) (?:you|dst|it) "
        r"(?:run|use|generate|write)(?: for (?P<rest3>.+))?$"
    ),
    chat=re.compile(
        r"^(?:hi|hello|hey|yo|thanks|thank you|thx|ty|ok|okay|cool|great|"
        r"nice|good|bye|goodbye|cheers|lol|haha|test|testing|ping|hmm|huh|wow|sorry|"
        r"good (?:morning|afternoon|evening|night))\b[\s!.,]*(?:\w+[\s!.,]*){0,3}$"
        r"|\b(?:meaning of life|tell me a joke|a joke|a poem|write me|who are you|what are you|"
        r"how are you|are you (?:a |an )?(?:ai|bot|robot|human|gpt|claude|real)|what model are "
        r"you|who made you|who built you|the weather|what time is it|what day is it|what's the "
        r"date|what is the date|love you|i'm bored|im bored|are you there|you there)\b"
    ),
    difference=re.compile(
        rf"^{_LEAD}(?:(?:what|whats|what's|show me|explain|tell me) (?:is |are )?{_ART})?"
        r"(?:difference|diff|distinction) between (?P<a>.+?) (?:and|vs\.?|versus) (?P<b>.+)"
        rf"|^{_LEAD}how (?:is|are|do|does) (?P<c>.+?) differ(?:s|ent)? from (?P<d>.+)"
        rf"|^{_LEAD}(?:is|are) (?P<e>.+?) the same (?:thing )?as (?P<f>.+)"
    ),
    definition=re.compile(
        rf"^{_LEAD}(?:what|wat|whats|what's) (?:does|do|did|would) {_ART}(?:term |word |metric |"
        rf"phrase )?(?P<a>.+?) (?:mean|means|stand for|refer to|signify)\b"
        rf"|^{_LEAD}(?:what|whats|what's) (?:is|are) {_ART}(?:meaning|definition|formula|"
        r"calculation|logic) (?:of|for|behind) (?P<b>.+)"
        rf"|^{_LEAD}(?:define|definition of|meaning of|explain|describe) {_ART}(?:term |metric )?"
        r"(?P<c>.+)"
        rf"|^{_LEAD}(?:how|hows|how's) (?:is|are|do we|do you|does dst|does it|does the system|"
        rf"did you|was|were|would) {_ART}(?P<d>.+?) (?:calculated|computed|defined|derived|"
        r"measured|counted|determined|worked out|built|composed)\b"
        rf"|^{_LEAD}how (?:do|does|did|would) (?:we|you|dst|it) (?:calculate|compute|define|"
        rf"measure|count|derive|determine|work out) {_ART}(?P<e>.+)"
        rf"|^{_LEAD}(?:what|which) (?:counts|qualifies|is counted|is included|gets counted) "
        rf"as {_ART}(?P<f>.+)"
        rf"|^{_LEAD}(?:what|whats|what's) (?:is|are) (?:a|an) (?P<g>.+)"
    ),
    help=re.compile(
        rf"^{_LEAD}(?:help|what can (?:you|i|we|dst) (?:do|ask|answer|query)|what (?:data|"
        r"metrics|lenses|questions) (?:do you have|can i ask|are there|are available|do you "
        r"know|exist)|what do you know|what (?:is|are) (?:this|dst|you)|what can this answer|"
        r"what (?:is|are) (?:in |on )?(?:here|scope)|what topics)\b"
    ),
    locate=re.compile(
        rf"^{_LEAD}(?:what|which|whats|what's|is there a|is there any) {_ART}(?:report|reports|"
        r"dashboard|dashboards|page|view|tab|chart|graph|sheet|workbook|looker|tableau|"
        r"metabase|power bi|powerbi|bi tool|tool)\b"
        rf"|^{_LEAD}where (?:can|could|do|does|would|should|might) (?:i|we|one|you|someone) "
        r"(?:find|see|get|look|check|open|view|access|go|read|download|pull)\b"
        rf"|^{_LEAD}where (?:is|are) {_ART}.*\b(?:report|dashboard|page|view|chart|graph|number|"
        r"figure|file|sheet|data|metric|defined|documented|shown|kept|stored|tracked|"
        r"published)\b"
        rf"|^{_LEAD}(?:which|what) (?:lens|lenses|member|members|metric|metrics|table|tables|"
        r"entity|entities|cube|cubes) (?:has|have|holds|hold|covers|cover|contains|contain|"
        r"owns|own|carries|carry|shows|show|is|are)\b"
        rf"|^{_LEAD}(?:is|are) {_ART}.+ (?:on|in) (?:a|the|any) (?:report|dashboard|page|view)\b"
    ),
    capability=re.compile(
        rf"^{_LEAD}(?:do|does|did) (?:we|you|dst|it|this|the lens|the data|the system|the model) "
        rf"(?:even |also |already )?(?:have|hold|track|store|capture|record|know|keep|contain|"
        rf"support|cover|include|measure|log|collect)(?: any| the| a| an| some)? "
        r"(?P<a>.+?)(?: data| field| metric| dimension| table| column| info| information| "
        r"anywhere| at all| available| in dst| in the data| in the lens| here)?$"
        rf"|^{_LEAD}(?:is|are) there (?:any |an? |some )?(?P<b>.+?)"
        r"(?: data| field| metric| dimension| table| column| info| information| available| "
        r"anywhere| in dst| in the data| in the lens| here| at all)*$"
        rf"|^{_LEAD}(?:is it|would it be|is it even) possible to (?P<c>.+)"
        rf"|^{_LEAD}(?:is|are) {_ART}(?P<e>.+?) (?:available|tracked|captured|recorded|stored|"
        r"covered|included|measured|known|in the data|in dst|in the lens)(?: anywhere| here)?$"
    ),
    errand=re.compile(
        rf"^{_LEAD}(?:email|e-mail|mail|send|forward|share|schedule|export|download|print|post|"
        r"slack|message|dm|ping|remind|notify|book|create|open|file|raise|log|save|publish|"
        r"upload|attach|put|add|set up|setup|subscribe|alert|text|call) "
        r"(?:this|that|it|me|us|them|him|her|the (?:result|results|answer|report|number|"
        r"figure|table|data|chart|query|dashboard)|these|those|a (?:ticket|reminder|meeting|"
        r"report|dashboard|chart|schedule|note|task|jira|alert)|an (?:alert|email|issue)|"
        r"to |with |as (?:a )?(?:csv|pdf|excel|xlsx|png|image|chart|file)|on |every |each |"
        r"daily|weekly|monthly|my |our |your |the |over )"
        rf"|^{_LEAD}how (?:do|can|could|would|should) (?:i|we|you|one) (?:export|download|share|"
        r"schedule|email|send|print|save|subscribe|get this (?:into|to|in)|open this|"
        r"put this|embed|connect|install|log ?in|sign ?in|get access|use (?:this|dst|the tool)|"
        r"add a|change my|reset)\b"
        rf"|^{_LEAD}(?:make|build|create|draw|plot|generate|render) (?:me |us )?(?:a |an |the )?"
        r"(?:chart|graph|plot|visuali[sz]ation|dashboard|report|deck|slide|presentation|"
        r"pdf|csv|excel|spreadsheet|powerpoint|image|picture)\b"
    ),
    in_dst=re.compile(
        r"\b(?:in dst|which lens|what lens|which member|which metric|which table|which cube|"
        r"which entity|in the lens|in which lens|in the model)\b"
    ),
    where=re.compile(rf"^{_LEAD}(?:where|which|what)\b"),
    shape=frozenset(
        """how many much count counts total totals sum average avg mean median number numbers
figure figures amount top bottom highest lowest most least best worst biggest largest
smallest more less fewer per vs versus compared compare comparison trend trends rate rates
share percentage percent ratio growth grew grow fall fell drop dropped change changed
increase decrease breakdown split by list rank ranking revenue sales orders customers
cost costs margin volume""".split()
    ),
    # Period words that name a period on their own; "this", "last", "since"
    # do not ("this page", "last request").
    period=frozenset(PERIOD)
    - frozenset(
        "this last next previous prior before after ago so-far since date dates day days".split()
    ),
)

RULES: tuple[Rules, ...] = (EN,)

_WORD = re.compile(r"[^\W_][\w'’\-]*")

# Words a subject is never about: the glossing and function words of the
# ledger's stop-list, and what the rules themselves consumed.
_SUBJECT_STOP = STOP | frozenset(
    """exactly actually here really even mean means meant define defined defines
    definition""".split()
)
_ARTICLES = re.compile("^(?:" + "|".join(r.articles for r in RULES if r.articles) + ")+")
_ABOUT = re.compile(
    r"^(?:any |some )?(?:data|info|information|anything|something|numbers?|figures?) "
    r"(?:on|about|for|regarding) "
)


def _clean(question: str) -> str:
    text = " ".join(question.strip().lower().split())
    text = re.sub(r"[\"“”‘’`]", "'", text)
    return text.strip(" ?!.,;:'")


def _subject(text: str) -> str:
    """The words of a captured span the type is about: the span with its
    leading determiners, trailing question marks and quoting stripped."""
    t = _ARTICLES.sub("", text.strip())
    t = re.sub(r"\s*(?:\?|!|\.|,|;|:)+$", "", t)
    t = _ABOUT.sub("", t)
    t = re.sub(r"^'|'$", "", t).strip()
    return t


def _toks(text: str) -> list[str]:
    return [t.strip("'’") for t in _WORD.findall(text)]


_SHAPE = frozenset().union(*(r.shape for r in RULES))
_PERIOD_ALONE = frozenset().union(*(r.period for r in RULES))


def _shaped(text: str) -> bool:
    """The text carries a data shape: a count, ranking, comparison or
    operator word, a period word, or a number."""
    return any(t in _SHAPE or t in _PERIOD_ALONE or t.isdigit() for t in _toks(text))


def _content(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            t for t in _toks(text) if t and t not in STOP and t not in PERIOD and not t.isdigit()
        )
    )


def _first(name: str, text: str) -> tuple[Rules, re.Match[str]] | None:
    """The first language whose rule ``name`` matches, in table order."""
    for rules in RULES:
        pattern: re.Pattern[str] = getattr(rules, name)
        m = pattern.search(text) if name in ("admin", "chat") else pattern.match(text)
        if m:
            return rules, m
    return None


def _span(m: re.Match[str]) -> str:
    return next((v for k, v in m.groupdict().items() if v and not k.startswith("n")), "")


def classify(question: str, *, held: frozenset[str] | None = None) -> Typed:
    """The deterministic rules, in order of how sure each is, each tried in
    every language of the table. ``held``: the stems every lens the caller
    may use declares (``router.coverage.held_by`` or ``vocabulary``); a
    question holding one of them is a query. A question holding none is
    left open, never declared off-topic: the word may be a column of an
    exposed table or a term no catalogue lists, and "not about data" said
    of a data question is the misattribution the step exists to stop.
    Returns ``type=None`` when no rule decides."""
    text = _clean(question)
    if not text:
        return Typed("not_a_data_question", rule="empty")
    if hit := _first("admin", text):
        rules, m = hit
        n = m.groupdict().get("n") or m.groupdict().get("n2")
        return Typed(
            "admin",
            rule="admin",
            n=int(n) if n and n.isdigit() else _NUMBER_WORDS.get(n or "", 1),
            lang=rules.lang,
        )
    if hit := _first("sql", text):
        rules, m = hit
        rest = m.group("rest") or m.group("rest2") or m.group("rest3") or ""
        return Typed("sql", subject=_subject(rest), rule="sql", lang=rules.lang)
    if hit := _first("chat", text):
        return Typed("not_a_data_question", rule="chat", lang=hit[0].lang)
    if hit := _first("difference", text):
        rules, m = hit
        a = m.group("a") or m.group("c") or m.group("e") or ""
        b = m.group("b") or m.group("d") or m.group("f") or ""
        return Typed(
            "definition",
            subject=_subject(a),
            second=_subject(b),
            rule="difference",
            lang=rules.lang,
        )
    for rules in RULES:
        d = rules.definition.match(text)
        if d and not (d.groupdict().get("g") and _shaped(d.group("g"))):
            # "what is an X" defines X unless X carries a data shape ("what
            # is an average order in Q3" asks for a figure).
            return Typed(
                "definition", subject=_subject(_span(d)), rule="definition", lang=rules.lang
            )
    if hit := _first("help", text):
        return Typed("capability", subject="", rule="help", lang=hit[0].lang)
    if hit := _first("locate", text):
        rules, m = hit
        span = _span(m)
        return Typed(
            "locate",
            subject=_subject(span) if span or rules.lang != "en" else _locate_subject(text),
            rule="locate",
            in_dst=bool(rules.in_dst.search(text)),
            lang=rules.lang,
        )
    if hit := _first("capability", text):
        rules, m = hit
        return Typed("capability", subject=_subject(_span(m)), rule="capability", lang=rules.lang)
    for rules in RULES:
        if rules.in_dst.search(text) and rules.where.match(text):
            subject = _locate_subject(text) if rules.lang == "en" else _in_dst_subject(rules, text)
            return Typed("locate", subject=subject, rule="in_dst", in_dst=True, lang=rules.lang)
    if hit := _first("errand", text):
        return Typed("not_a_data_question", rule="errand", lang=hit[0].lang)
    words = _content(text)
    shaped = _shaped(text)
    lang = _language(text)
    if held is not None and any(word_keys(w) & held for w in words):
        return Typed("query", rule="held", lang=lang)
    if shaped:
        return Typed("query", rule="shape", lang=lang)
    return Typed(None, rule="open", lang=lang)


def _language(text: str) -> str:
    """The language whose shape and period words the text carries most of;
    English when none does."""
    toks = _toks(text)
    best = max(RULES, key=lambda r: sum(1 for t in toks if t in r.shape or t in r.period))
    return best.lang if any(t in best.shape or t in best.period for t in toks) else "en"


def _in_dst_subject(rules: Rules, text: str) -> str:
    t = rules.in_dst.sub(" ", text)
    t = rules.where.sub("", t)
    t = re.sub(
        r"\b(?:on|ovat|löytyy|määritelty|määritellään|dokumentoitu|är|finns|ligger|definierad|"
        r"definierat|dokumenterad|dokumenterat)\b",
        " ",
        t,
    )
    return _subject(" ".join(t.split()))


def _locate_subject(text: str) -> str:
    text = re.sub(r"\b(?:in dst|in the lens|in the model|in which lens)\b", " ", text)
    text = " ".join(text.split())
    t = re.sub(
        r"^(?:please |can you |could you )*(?:where (?:can|could|do|does|would|should|might) "
        r"(?:i|we|one|you|someone) (?:find|see|get|look at|look|check|open|view|access|go to|"
        r"read|download|pull)|where (?:is|are)|(?:what|which|whats|what's|is there a|is there "
        r"any) (?:the |a |an )?(?:report|reports|dashboard|dashboards|page|view|tab|chart|"
        r"graph|sheet|workbook|looker|tableau|metabase|power bi|powerbi|bi tool|tool)s? "
        r"(?:shows?|has|have|holds?|covers?|for|with|on|about|lists?|tracks?|contains?|gives?|"
        r"do i use for|should i use for|is there for)?|(?:which|what) (?:lens|lenses|member|"
        r"members|metric|metrics|table|tables|entity|entities|cube|cubes) (?:has|have|holds|"
        r"hold|covers|cover|contains|contain|owns|own|carries|carry|shows|show|is|are))\s*",
        "",
        text,
    )
    t = re.sub(
        r"\b(?:the |a |an |our |my )?(?:report|dashboard|page|view|chart|graph|number|figure|"
        r"file|sheet)s?\b",
        " ",
        t,
    )
    t = re.sub(r"\s+(?:defined|documented|tracked|kept|stored|shown|published|live|lives)$", "", t)
    return _subject(" ".join(t.split()))


# ── the model, where no driver is present ───────────────────────────────────

_CONSULT = (
    "Classify the user's message into exactly one type. Answer with one JSON object "
    '{"type": "<type>", "subject": "<the words it is about, or empty>"} and nothing else.\n'
    "Types:\n"
    "- query: asks for a number, a list, a ranking, a comparison or a trend over data.\n"
    "- definition: asks what a term means, how a figure is calculated, or how two terms differ.\n"
    "- capability: asks whether the data holds something or whether a question can be answered.\n"
    "- sql: asks for the SQL or the query behind a figure.\n"
    "- locate: asks which report, dashboard or place shows something.\n"
    "- admin: asks about the user's own earlier requests.\n"
    "- not_a_data_question: a greeting, an errand (email, export, schedule), chit-chat, or "
    "anything not about data.\n"
    "When unsure between query and another type, answer query."
)


def consult(question: str, model: ResolvedModel) -> Typed:
    """The model's reading, for a question the rules left open: only the
    door with no driver calls this. A reply that is not one of the types is
    a query."""
    try:
        with ai_usage.purpose("question_type"):
            result = model.llm.complete(
                system=[CacheableBlock(text=_CONSULT, cache=False)],
                messages=[Message(role="user", content=question.strip()[:500])],
                model=model.name,
                temperature=0.0,
                max_tokens=80,
            )
    except Exception:  # noqa: BLE001 — a dead provider leaves the question a query
        log.exception("question-type consult failed; reading the question as a query")
        return Typed("query", rule="consult_failed", by_model=True)
    text = result.text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    kind, subject = "", ""
    if m:
        try:
            doc = json.loads(m.group(0))
            kind = str(doc.get("type") or "").strip().lower()
            subject = str(doc.get("subject") or "").strip()
        except (ValueError, AttributeError):
            kind = ""
    if not kind:
        kind = text.split()[0].strip("\"'.,:").lower() if text else ""
    if kind not in TYPES:
        kind = "query"
    return Typed(kind, subject=_subject(subject), rule="model", by_model=True)  # type: ignore[arg-type]


def decide_type(
    question: str, *, held: frozenset[str] | None, model: ResolvedModel | None
) -> Typed:
    """The rules, then the model where one is given (the no-driver door),
    else a query."""
    typed = classify(question, held=held)
    if typed.type is not None:
        return typed
    if model is not None and not ai_usage.models_off():
        return consult(question, model)
    return Typed("query", rule="open_as_query")


# ── what the lens holds, for the answers ────────────────────────────────────


@dataclass
class Member:
    lens: str
    name: str
    kind: str  # measure | dimension | segment | definition | metric
    title: str = ""
    description: str = ""
    expression: str = ""
    filters: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    not_for: list[str] = field(default_factory=list)
    readings: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        tail = self.name.split(".")[-1].replace("_", " ")
        return [tail, self.title, *self.aliases]

    def line(self) -> str:
        head = self.title or self.name.split(".")[-1].replace("_", " ")
        parts = [f"{head} ({self.lens}: {self.kind} {self.name})"]
        if self.description:
            parts[0] += f": {self.description.rstrip('.')}"
        if self.readings:
            parts.append("readings: " + "; ".join(self.readings))
        if self.expression:
            parts.append(f"computed as {self.expression}")
        if self.filters:
            parts.append("where " + " and ".join(self.filters))
        if self.aliases:
            parts.append("also called " + ", ".join(self.aliases))
        if self.not_for:
            parts.append("not for " + ", ".join(self.not_for))
        if self.values:
            shown = ", ".join(self.values[:12])
            more = f" (+{len(self.values) - 12} more)" if len(self.values) > 12 else ""
            parts.append(f"values: {shown}{more}")
        return ". ".join(parts) + "."


@dataclass
class NotComputable:
    lens: str
    measure: str
    aliases: list[str]
    reason: str
    route_to: str


@dataclass
class Catalogue:
    """What the question-type answers are read from: every member and
    definition the caller may see, per lens, and each lens's declared
    not-computable terms. Built once per request from the published bundles."""

    lenses: list[str] = field(default_factory=list)
    members: list[Member] = field(default_factory=list)
    not_computable: list[NotComputable] = field(default_factory=list)
    descriptions: dict[str, str] = field(default_factory=dict)

    @property
    def held(self) -> frozenset[str]:
        texts = [t for m in self.members for t in m.names]
        texts += [v for m in self.members for v in m.values]
        texts += [a for nc in self.not_computable for a in (nc.measure, *nc.aliases)]
        return vocabulary(texts)


def catalogue_of(
    lens: str,
    model: SemanticModel | LensModel,
    *,
    dictionaries: Mapping[str, Sequence[str]] | None = None,
    groups: Sequence[str] = (),
    description: str = "",
) -> Catalogue:
    """One lens's members as the step reads them: a Cube lens's stored
    ``LensModel`` (members hidden from this caller's groups left out), or a
    typed lens's ``SemanticModel`` (metrics, dimensions, definitions)."""
    cat = Catalogue(lenses=[lens], descriptions={lens: description} if description else {})
    dicts = dictionaries or {}
    if isinstance(model, LensModel):
        for m in model.members:
            if model.hidden(m, tuple(groups)):
                continue
            aliases: list[str] = []
            values: list[str] = []
            not_for: list[str] = []
            if m.measure is not None:
                aliases, not_for = list(m.measure.aliases), list(m.measure.not_for)
            if m.dimension is not None:
                aliases = list(m.dimension.aliases)
                values = [str(v) for v in dicts.get(m.base) or []]
                if not values and isinstance(m.dimension.dictionary, list):
                    values = [str(v) for v in m.dimension.dictionary]
            if m.segment is not None:
                aliases = [m.segment.term or "", *m.segment.aliases]
                aliases = [a for a in aliases if a]
            cat.members.append(
                Member(
                    lens=lens,
                    name=m.name,
                    kind=str(m.kind),
                    title=m.title or "",
                    description=m.description or "",
                    aliases=aliases,
                    values=values,
                    not_for=not_for,
                )
            )
        for d in [*model.definitions, *model.ambiguous]:
            cat.members.append(
                Member(
                    lens=lens,
                    name=d.metric,
                    kind="definition",
                    title=d.metric,
                    description=d.summary or d.about or "",
                    aliases=list(d.aliases),
                    readings=[f"{r.says}: {', '.join(r.members)}" for r in d.readings]
                    or list(d.possible_mappings),
                )
            )
        for nc in model.policy.not_computable:
            cat.not_computable.append(
                NotComputable(
                    lens, nc.measure, list(nc.aliases), nc.reason or "", nc.route_to or ""
                )
            )
        return cat
    for e in model.entities:
        if e.exposed and not e.metrics and not e.dimensions:
            continue
        for met in e.metrics:
            expr = met.expr or ""
            if met.type == "ratio" and met.numerator and met.denominator:
                expr = f"{met.numerator} / {met.denominator}"
            elif met.agg and not expr:
                expr = f"{met.agg}({met.name})"
            elif met.agg and expr:
                expr = f"{met.agg}({expr})"
            cat.members.append(
                Member(
                    lens=lens,
                    name=f"{e.name}.{met.name}",
                    kind="metric",
                    title=met.label or "",
                    description=met.description or "",
                    expression=expr,
                    filters=list(met.filters),
                    aliases=list(met.aliases),
                )
            )
        for dim in e.dimensions:
            cat.members.append(
                Member(
                    lens=lens,
                    name=f"{e.name}.{dim.name}",
                    kind="dimension",
                    title="",
                    description=dim.description or "",
                    expression=dim.expr or "",
                    aliases=list(dim.aliases),
                    values=[str(v) for v in dicts.get(f"{e.name}.{dim.name}") or []],
                )
            )
    for page in model.definitions:
        cat.members.append(
            Member(
                lens=lens,
                name=page.term,
                kind="definition",
                title=page.term,
                description=page.body,
                expression=page.sql_expr or "",
                aliases=list(page.aliases),
                readings=list(page.possible_mappings) if page.status == "ambiguous" else [],
            )
        )
    for entry in model.not_computable:
        cat.not_computable.append(
            NotComputable(
                lens,
                str(entry.get("measure") or ""),
                [str(a) for a in (entry.get("aliases") or [])],
                str(entry.get("reason") or ""),
                str(entry.get("route_to") or ""),
            )
        )
    return cat


def merge(cats: Sequence[Catalogue]) -> Catalogue:
    out = Catalogue()
    for c in cats:
        out.lenses += [n for n in c.lenses if n not in out.lenses]
        out.members += c.members
        out.not_computable += c.not_computable
        out.descriptions.update(c.descriptions)
    return out


# ── matching a subject to what is held ──────────────────────────────────────


def _keys(subject: str) -> set[str]:
    out: set[str] = set()
    for t in _toks(subject):
        if t in _SUBJECT_STOP or t in PERIOD or not t:
            continue
        out |= word_keys(t)
    return out


def _score(keys: set[str], m: Member) -> tuple[int, int]:
    named = len(keys & vocabulary(m.names))
    described = len(keys & vocabulary([m.description, *m.values]))
    return named, described


def find(cat: Catalogue, subject: str) -> list[Member]:
    """The members the subject names, best first: by name, title or alias
    first, then by description or a value. Empty when nothing carries any
    word of it."""
    keys = _keys(subject)
    if not keys:
        return []
    scored = [(m, *_score(keys, m)) for m in cat.members]
    best = max((n for _m, n, _d in scored), default=0)
    if best > 0:
        hits = [m for m, n, d in scored if n == best]
        hits.sort(key=lambda m: (-_score(keys, m)[1], m.kind != "definition", m.name))
        return hits
    hits = [m for m, _n, d in scored if d > 0]
    hits.sort(key=lambda m: (-_score(keys, m)[1], m.name))
    return hits


def not_computable_for(cat: Catalogue, subject: str) -> NotComputable | None:
    keys = _keys(subject)
    for nc in cat.not_computable:
        if keys and keys <= vocabulary([nc.measure, *nc.aliases]) or _phrase(subject, nc):
            return nc
    return None


def _phrase(subject: str, nc: NotComputable) -> bool:
    s = " ".join(_toks(subject))
    return any(p and p.lower() in s for p in (nc.measure, *nc.aliases))


def _nearest(cat: Catalogue, limit: int = 6) -> str:
    names = []
    for m in cat.members:
        if m.kind == "definition":
            continue
        names.append(m.title or m.name.split(".")[-1].replace("_", " "))
    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        return "nothing declared"
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _lenses(cat: Catalogue) -> str:
    return ", ".join(cat.lenses) if cat.lenses else "no lens"


# ── the answers ─────────────────────────────────────────────────────────────


@dataclass
class HandOffEntry:
    name: str
    kind: str
    url: str | None = None
    mcp: str | None = None

    def line(self) -> str:
        where = self.url or self.mcp or ""
        tag = " (MCP)" if not self.url and self.mcp else ""
        return f"{self.name} ({self.kind}) {where}{tag}".strip()


LastRequests = Callable[[int], list[dict[str, Any]]]


def _hand_off_text(hand_off: Sequence[HandOffEntry]) -> str:
    if not hand_off:
        return (
            "no hand-off is declared: name where reports and requests live in dst.yaml "
            "(hand_off: [{name, kind, url | mcp}])"
        )
    return "where to go: " + "; ".join(h.line() for h in hand_off)


def answer(
    typed: Typed,
    cat: Catalogue,
    *,
    lens: str,
    request_id: str,
    org_id: str,
    caller: str,
    question: str,
    hand_off: Sequence[HandOffEntry] = (),
    last_requests: LastRequests | None = None,
    agent: str | None = None,
) -> tuple[QueryResponse, TraceLog]:
    """The answer a non-query type gets, from the catalogue and the project
    file: nothing runs, no model is called. ``lens``: the lens the row is
    written under (the door's lens, or on the routed door the first lens
    holding the subject)."""
    started = time.monotonic()
    kind = typed.type or "query"
    mode = MODE[kind]
    status: Literal["ok", "refused"] = "ok"
    text = ""
    citations: list[Citation] = []
    row_lens = lens
    if kind == "definition":
        text, status, citations, row_lens = _definition(typed, cat, lens)
    elif kind == "capability":
        text, status, citations, row_lens = _capability(typed, cat, lens)
    elif kind == "locate":
        text, status, citations, row_lens = _locate(typed, cat, lens, hand_off)
    elif kind == "admin":
        text, status = _admin(typed, last_requests)
    elif kind == "sql":
        text, status = _sql_last(last_requests)
    elif kind == "not_a_data_question":
        status = "refused"
        text = (
            f"not a data question: dst answers questions about the data its lenses hold "
            f"({_lenses(cat)}); {_hand_off_text(hand_off)}"
        )
    resp = QueryResponse(
        lens=row_lens,
        status=status,
        answer=text,
        citations=citations,
        mode=mode,
        request_id=request_id,
    )
    trace = TraceLog(
        request_id=request_id,
        org_id=org_id,
        lens=row_lens,
        caller=caller,
        agent=agent,
        question=question,
        status=status,
        answer=text,
        citations=citations,
        mode=mode,
        serving=SERVING,
        generator_tier="question_type",
        latency_ms={"question_type": round((time.monotonic() - started) * 1000, 1)},
    )
    return resp, trace


def _cite(members: Sequence[Member]) -> list[Citation]:
    return [
        Citation(
            type="definition" if m.kind == "definition" else "context", ref=f"{m.lens}:{m.name}"
        )
        for m in members
    ]


def _definition(
    typed: Typed, cat: Catalogue, lens: str
) -> tuple[str, Literal["ok", "refused"], list[Citation], str]:
    subjects = [s for s in (typed.subject, typed.second) if s]
    if not subjects:
        return (
            "which term? name the metric, dimension or definition to define",
            "refused",
            [],
            lens,
        )
    found: list[list[Member]] = [find(cat, s)[:3] for s in subjects]
    lines: list[str] = []
    cited: list[Member] = []
    for subject, hits in zip(subjects, found, strict=True):
        nc = not_computable_for(cat, subject)
        if hits:
            lines += [m.line() for m in hits]
            cited += hits
        elif nc is not None:
            lines.append(
                f"'{subject}' is declared not computable in {nc.lens}"
                + (f": {nc.reason}" if nc.reason else "")
                + (f"; ask {nc.route_to}" if nc.route_to else "")
            )
        else:
            lines.append(
                f"nothing in {_lenses(cat)} defines '{subject}'; "
                f"it declares {_nearest(cat)}. A definition is never invented."
            )
    if len(subjects) == 2 and all(found):
        a, b = found[0][0], found[1][0]
        if a.name == b.name and a.lens == b.lens:
            lines.append(f"'{typed.subject}' and '{typed.second}' name the same member, {a.name}.")
        else:
            lines.append(f"The difference: {a.name} vs {b.name}, as described above.")
    status: Literal["ok", "refused"] = "ok" if any(found) else "refused"
    row_lens = cited[0].lens if cited else lens
    return " ".join(lines), status, _cite(cited), row_lens


def _offer(m: Member) -> str:
    head = m.title or m.name.split(".")[-1].replace("_", " ")
    if m.kind in ("dimension",):
        return f"to use it, ask a measure split by {head}, e.g. 'count by {head} last month'"
    return f"to get the figure, ask it as a question, e.g. '{head} last month'"


def _capability(
    typed: Typed, cat: Catalogue, lens: str
) -> tuple[str, Literal["ok", "refused"], list[Citation], str]:
    if not typed.subject:
        # "what can I ask": the lenses and what each holds.
        parts = []
        for name in cat.lenses:
            members = [m for m in cat.members if m.lens == name and m.kind != "definition"]
            heads = list(
                dict.fromkeys(m.title or m.name.split(".")[-1].replace("_", " ") for m in members)
            )
            desc = cat.descriptions.get(name, "")
            parts.append(
                f"{name}"
                + (f" ({desc.rstrip('.')})" if desc else "")
                + ": "
                + ", ".join(heads[:10])
                + (f" and {len(heads) - 10} more" if len(heads) > 10 else "")
            )
        text = (
            "dst answers questions about: " + "; ".join(parts)
            if parts
            else "no lens is published for you"
        )
        return text, "ok" if parts else "refused", [], lens
    nc = not_computable_for(cat, typed.subject)
    hits = find(cat, typed.subject)
    named = [m for m in hits if _score(_keys(typed.subject), m)[0] > 0]
    if nc is not None and not named:
        text = f"no: '{nc.measure}' is declared not computable in {nc.lens}"
        if nc.reason:
            text += f": {nc.reason}"
        if nc.route_to:
            text += f"; ask {nc.route_to}"
        return text, "refused", [], nc.lens
    if named:
        top = named[:3]
        text = (
            f"yes: {', '.join(f'{m.lens} holds it as {m.name}' for m in top)}. "
            + " ".join(m.line() for m in top)
            + " "
            + _offer(top[0])
        )
        return text, "ok", _cite(top), top[0].lens
    if hits:
        near = hits[:3]
        text = (
            f"not as such: nothing in {_lenses(cat)} is named '{typed.subject}'; "
            f"the closest by description: {', '.join(m.name for m in near)}. "
            + " ".join(m.line() for m in near)
        )
        return text, "ok", _cite(near), near[0].lens
    text = (
        f"no: no lens holds '{typed.subject}' ({_lenses(cat)}); what is declared: {_nearest(cat)}"
    )
    return text, "ok", [], lens


def _locate(
    typed: Typed, cat: Catalogue, lens: str, hand_off: Sequence[HandOffEntry]
) -> tuple[str, Literal["ok", "refused"], list[Citation], str]:
    if typed.in_dst:
        hits = [m for m in find(cat, typed.subject) if m.kind != "definition"][:3]
        if hits:
            text = (
                f"in dst, '{typed.subject}' is held by "
                + "; ".join(f"{m.lens} as {m.name}" for m in hits)
                + ". "
                + _offer(hits[0])
            )
            return text, "ok", _cite(hits), hits[0].lens
        text = (
            f"no lens holds '{typed.subject}' ({_lenses(cat)}); what is declared: {_nearest(cat)}"
        )
        return text, "ok", [], lens
    if not hand_off:
        return (
            "dst keeps no inventory of reports or dashboards, and no hand_off is declared in "
            "dst.yaml (hand_off: [{name, kind, url | mcp}]) to point at one",
            "refused",
            [],
            lens,
        )
    text = f"dst keeps no inventory of reports; {_hand_off_text(hand_off)}"
    hits = (
        [m for m in find(cat, typed.subject) if m.kind != "definition"][:2] if typed.subject else []
    )
    if hits:
        text += ". In dst, " + "; ".join(f"{m.lens} holds {m.name}" for m in hits)
    return text, "ok", _cite(hits), lens


def _admin(
    typed: Typed, last_requests: LastRequests | None
) -> tuple[str, Literal["ok", "refused"]]:
    if last_requests is None:
        return "the request log is not reachable from this door", "refused"
    rows = last_requests(max(1, min(typed.n, 20)))
    if not rows:
        return "no request of yours is on record", "ok"
    lines = []
    for r in rows:
        when: Any = r.get("created_at")
        when_s = (
            when.isoformat(timespec="minutes") if hasattr(when, "isoformat") else str(when or "")
        )
        line = (
            f"{when_s} {r.get('request_id')}: '{r.get('question')}' -> {r.get('lens')}, "
            f"{r.get('status')}"
        )
        if r.get("mode"):
            line += f" ({r.get('mode')})"
        lines.append(line)
    head = "your last request" if len(rows) == 1 else f"your last {len(rows)} requests"
    return f"{head}: " + "; ".join(lines), "ok"


def _sql_last(last_requests: LastRequests | None) -> tuple[str, Literal["ok", "refused"]]:
    """ "Give me the SQL" with no question behind it: the SQL of the caller's
    last answer, from the log."""
    if last_requests is None:
        return "the request log is not reachable from this door", "refused"
    rows = [r for r in last_requests(5) if r.get("sql")]
    if not rows:
        return "no earlier answer of yours is on record to take the SQL from", "refused"
    r = rows[0]
    return (
        f"the SQL of your last answer ({r.get('request_id')}: '{r.get('question')}'):\n"
        + str(r.get("sql")).strip(),
        "ok",
    )


def sql_body(sql: str | None, cube_query: Sequence[Mapping[str, object]] | None) -> str:
    """The answer body of a SQL ask: the SQL that served, and the Cube query
    it was compiled from when there was one."""
    parts = []
    if sql:
        parts.append("SQL:\n" + sql.strip())
    if cube_query:
        parts.append("Cube query:\n" + json.dumps(list(cube_query), indent=2, default=str))
    return "\n\n".join(parts) if parts else "no SQL was produced for this question"
