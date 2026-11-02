"""The frontier lane: a model turns a question into Cube query JSON.

Used only where no driver agent is present (the HTTP door with no driver,
``serving: cube`` lenses). The model sees what the lens may call and nothing
else: the lens's cubes and views from ``/v1/meta`` with their ``meta.dst``,
the observed values of each dictionary dimension, the overlay (scope, policy,
ambiguous terms, not_computable), and certified exemplars as question → Cube
query pairs. It answers with one to three Cube queries and a word ledger, or
says it cannot, or asks one question back. It never writes SQL.

The reply is parsed and validated (``CubeQuery`` is a closed schema), so a
member the model invented or a key Cube does not read fails here, loud,
before any check or any execution.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from services.contracts.cube_meta import Dimension, Measure
from services.contracts.cube_query import Binding, BindingKind, Candidate, CubeQuery
from services.contracts.protocols import CacheableBlock, Message
from services.cube.lens_view import CubeLensView
from services.llm.registry import ResolvedModel
from services.observability import usage as ai_usage
from services.verify import periods

LANE = "frontier"
MAX_QUERIES = 3
# Observed values listed per dictionary dimension; a longer dictionary is
# shown in part and says how many it holds.
DICTIONARY_SAMPLE = 40

LEDGER_RULES = """The ledger (required with "query") accounts for every content word of the
question - every word except function words (the, of, what, how many, ...), ranking words (top,
most, worst) and comparison words (vs, compared): one entry {"word": "<the word or phrase as
asked>", "to": "member" | "filter" | "segment" | "period" | "none", "ref": "<the member, filter
value, segment or date range of YOUR query that carries it>"}. Say "none" when nothing in your
queries carries the word: the answer then asks the user instead of serving a number that ignores
part of the question. Never map a word to a member your queries do not use."""

_KINDS: dict[str, BindingKind] = {
    "member": "member",
    "measure": "member",
    "dimension": "member",
    "filter": "value",
    "value": "value",
    "segment": "segment",
    "period": "period",
}


# The modes a lane may ask back under, each with what it means: a clarify
# names one and the words of the question it is about, or it is asked again.
CLARIFY_MODES: dict[str, str] = {
    "ambiguous_metric": "a term of the question has several readings that give different "
    "numbers (an overlay ambiguous term, or two members it equally names)",
    "uncovered_word": "a word of the question that nothing in the model carries",
    "unbound_value": "a value the question names that is not among a dimension's values",
    "period_unreadable": "a period of the question that no dates can be read from",
}

REASK = (
    "Your reply asked back without naming a failure mode and the words it is about. Answer the "
    "question with a query (say any reading you chose in the note), or, only if one of these "
    "decides the number, clarify naming it as "
    '{{"answer": "clarify", "mode": "<mode>", "words": ["<the question\'s words>"], '
    '"question": "..."}}:{modes}\nReply with ONE JSON object.'
)


class LedgerEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    word: str
    to: str = "none"
    # What carries the word, as written: a model may name several members, a
    # number, or a date range as a pair or an object. Every leaf is one ref.
    ref: list[str] = Field(default_factory=list)

    @field_validator("ref", mode="before")
    @classmethod
    def _leaves(cls, v: object) -> list[str]:
        return _leaves(v)


def _leaves(v: object) -> list[str]:
    if v is None:
        return []
    if isinstance(v, dict):
        return [x for item in v.values() for x in _leaves(item)]
    if isinstance(v, (list, tuple)):
        return [x for item in v for x in _leaves(item)]
    if isinstance(v, bool):
        return ["true" if v else "false"]
    return [str(v)]


class FrontierReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answer: Literal["query", "cannot", "clarify"]
    queries: list[CubeQuery] = Field(default_factory=list)
    note: str = ""
    # Read apart from the reply (``read_reply``): a ledger that does not read
    # leaves the queries standing and the word check refusing to pass them.
    ledger: Any = None
    reason: str = ""
    question: str = ""
    mode: str = ""
    words: list[str] = Field(default_factory=list)

    @field_validator("words", mode="before")
    @classmethod
    def _words(cls, v: object) -> list[str]:
        return _leaves(v)


@dataclass
class FrontierResult:
    """What the lane decided. ``outcome`` is ``query`` (``candidates`` hold
    the queries, each carrying the whole ledger), ``cannot`` (``reason``),
    ``clarify`` (``question``, under ``mode`` about ``words``) or ``invalid``
    (the reply did not parse or validate, or asked back twice without naming
    a mode; ``reason`` says how)."""

    outcome: Literal["query", "cannot", "clarify", "invalid"]
    candidates: list[Candidate] = field(default_factory=list)
    ledger: list[Binding] | None = None
    note: str = ""
    reason: str = ""
    question: str = ""
    raw: str = ""
    spend: dict[str, float] = field(default_factory=dict)
    mode: str = ""
    words: list[str] = field(default_factory=list)
    # A clarify that named no mode (or words the question does not hold).
    unnamed: bool = False


# ── the context ─────────────────────────────────────────────────────────────


def _hidden(lens: CubeLensView, groups: list[str]) -> set[str]:
    hidden = {m for m, shown in lens.hidden_rules().items() if not set(groups) & set(shown)}
    # A view member is withheld with the cube member behind it.
    return hidden | {
        name for c in lens.doc.cubes for name in c.member_names() if lens.base(name) in hidden
    }


def _measure_entry(
    lens: CubeLensView, name: str, m: Measure, meta: dict[str, Any]
) -> dict[str, Any]:
    d: dict[str, Any] = {"name": name, "agg": m.aggType, "title": m.title}
    if m.description:
        d["description"] = m.description
    if meta:
        d["meta"] = meta
    return d


def _dimension_entry(
    lens: CubeLensView, name: str, x: Dimension, meta: dict[str, Any]
) -> dict[str, Any]:
    d: dict[str, Any] = {"name": name, "type": x.type}
    if x.primaryKey:
        d["pk"] = True
    if x.description:
        d["description"] = x.description
    if meta:
        d["meta"] = meta
    values = lens.dictionary(name)
    if values:
        d["values"] = values[:DICTIONARY_SAMPLE]
        if len(values) > DICTIONARY_SAMPLE:
            d["values_total"] = len(values)
    return d


_REFS = ("clock", "as_of", "label_of")


def _view_refs(lens: CubeLensView, meta: dict[str, Any], cube: str, view: str) -> dict[str, Any]:
    """A cube member's ``meta`` as a view member reads it: ``dst.clock``,
    ``dst.as_of`` and ``dst.label_of`` name cube dimensions, and the query
    must name the view's member for each."""
    dst = meta.get("dst")
    if not isinstance(dst, dict) or lens.model is None:
        return meta
    out = {**meta, "dst": dict(dst)}
    for key in _REFS:
        ref = dst.get(key)
        if not isinstance(ref, str) or not ref:
            continue
        base = ref if "." in ref else f"{cube}.{ref}"
        named = lens.model.view_member_for(base, view)
        if named:
            out["dst"][key] = named
    return out


def prefixed_members(lens: CubeLensView) -> bool:
    """Whether some view member of the lens carries its cube's name as a
    prefix (``view.stores_region`` for ``stores.region``): the way
    ``dst migrate-model`` names a joined cube's members."""
    if lens.model is None:
        return False
    return any(
        m.name.split(".", 1)[1] == f"{m.cube}_{m.member}" and m.member != m.name.split(".", 1)[1]
        for m in lens.model.members
    )


def _view_meta(lens: CubeLensView, hidden: set[str]) -> list[dict[str, Any]]:
    """A lens over views: each view with every member it serves named
    exactly as the query must name it, the cubes behind them as context
    (grain, population) with no members of their own."""
    assert lens.model is not None
    out: list[dict[str, Any]] = []
    for v in lens.model.views:
        measures, dims, segments = [], [], []
        for lm in (m for m in lens.model.members if m.view == v.name):
            if lm.name in hidden or lm.base in hidden:
                continue
            base = lens.member(lm.name)
            meta = _view_refs(lens, base.meta if base is not None else {}, lm.cube, v.name)
            if isinstance(base, Measure):
                measures.append(_measure_entry(lens, lm.name, base, meta))
            elif isinstance(base, Dimension):
                dims.append(_dimension_entry(lens, lm.name, base, meta))
            else:
                segments.append({"name": lm.name, **({"meta": meta} if meta else {})})
        out.append(
            {
                "view": v.name,
                "description": v.description,
                "meta": v.dst.model_dump(exclude_defaults=True) if v.dst else None,
                "measures": measures,
                "dimensions": dims,
                "segments": segments,
            }
        )
    for c in lens.model.cubes:
        out.append(
            {
                "cube": c.name,
                "description": c.description,
                "meta": {"dst": c.dst.model_dump(exclude_defaults=True)} if c.dst else None,
            }
        )
    return out


def compact_meta(lens: CubeLensView, groups: list[str]) -> list[dict[str, Any]]:
    """One object per cube or view the lens may call: members with their
    aggregation, title, description, ``meta`` and observed values; members
    hidden from this caller left out. A lens over views lists each view's
    members by their exact names (``_view_meta``); a lens with no views
    lists every cube Cube serves."""
    hidden = _hidden(lens, groups)
    if lens.model is not None and lens.model.views:
        return _view_meta(lens, hidden)
    out: list[dict[str, Any]] = []
    for c in lens.doc.cubes:
        if c.is_view:
            out.append(
                {
                    "view": c.name,
                    "description": c.description,
                    "meta": c.meta or None,
                    "members": len(c.measures) + len(c.dimensions),
                }
            )
            continue
        measures = [
            _measure_entry(lens, m.name, m, m.meta) for m in c.measures if m.name not in hidden
        ]
        dims = [
            _dimension_entry(lens, x.name, x, x.meta) for x in c.dimensions if x.name not in hidden
        ]
        out.append(
            {
                "cube": c.name,
                "description": c.description,
                "meta": c.meta or None,
                "joins": [j.name for j in c.joins],
                "measures": measures,
                "dimensions": dims,
                "segments": [
                    {"name": s.name, **({"meta": s.meta} if s.meta else {})} for s in c.segments
                ],
            }
        )
    return out


CUBE_NAMING = """## The Cube model (one JSON object per cube or view; members are `cube.member`)
Cubes joined to each other can be combined in one query (Cube joins them); cubes with no join
path cannot."""

VIEW_NAMING = """## The Cube model (one JSON object per view, then its cubes; members are
`view.member`)
Name every member EXACTLY as listed under its view: the listed name is the only one Cube
knows. The members of one view can be combined in one query (Cube joins the cubes behind
them); members of different views cannot. The cubes are listed for what they describe (grain,
population); a query never names a cube member directly."""

PREFIX_NOTE = """
A joined cube's members carry the cube name as a prefix (`view.stores_region` is the region of
the stores cube): write that prefixed name, never `view.region`."""


def _overlay_for(lens: CubeLensView, groups: list[str]) -> dict[str, Any]:
    """The overlay as this caller may read it: the hide entries left out, and
    so is every definition about a member hidden from them (its prose names
    what the member holds); a caller who may see the member reads it."""
    hidden = {m for m, shown in lens.hidden_rules().items() if not set(groups) & set(shown)}
    doc = lens.overlay.model_dump(exclude={"hide"}, exclude_defaults=True)
    for key in ("definitions", "ambiguous"):
        if key in doc:
            doc[key] = [d for d in doc[key] if d.get("about") not in hidden]
    return doc


def context(
    lens: CubeLensView,
    *,
    today: date,
    timezone: str = "",
    groups: list[str] | None = None,
    exemplars: list[tuple[str, CubeQuery]] | None = None,
) -> str:
    """The system prompt: the task, the clock, the model, the overlay and
    the exemplars. Fixed per lens and caller groups, so it caches."""
    meta = "\n".join(
        json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=str)
        for x in compact_meta(lens, groups or [])
    )
    overlay = yaml.safe_dump(_overlay_for(lens, groups or []), sort_keys=False, allow_unicode=True)
    ex = "\n".join(
        json.dumps({"question": q, "cube_query": cq.wire()}, ensure_ascii=False)
        for q, cq in exemplars or []
    )
    loaded = lens.probe.clock_end
    zone = f" ({timezone})" if timezone else ""
    scoped = lens.model is not None and bool(lens.model.views)
    naming = (
        (VIEW_NAMING + (PREFIX_NOTE if prefixed_members(lens) else "")) if scoped else CUBE_NAMING
    )
    data_line = f"; the warehouse holds data up to {loaded}" if loaded else ""
    return f"""You turn a business question into Cube REST API query JSON for a semantic layer.
You never write SQL.

TODAY is {today.strftime("%A")} {today.isoformat()}{zone}: this is the asked-on date; ignore any
other current date you may have been told{data_line}. Resolve every relative period ("this
quarter", "last 90 days", "year to date", "this week") to absolute ISO dates in
timeDimensions.dateRange (inclusive start and end days). Weeks start on Monday.

{naming} meta.dst is the semantic layer's own metadata: aliases (words people use), values
(the observed values of a dimension, use these exact strings in filters), value_aliases,
additivity: level (a stock: the latest snapshot in the window is kept automatically when you send
a dateRange without granularity), clock (the time dimension this measure must be filtered on),
currency, better, grain, population, key, system.
{meta}

## The overlay (scope and policy; not_computable = refuse; not_for = another lens's; ambiguous
terms = ask)
{overlay}
## Certified exemplars (question -> Cube query)
{ex or "(none yet)"}

## Your task
Reply with ONE JSON object and nothing else (no prose, no code fence):
  {{"answer": "query", "queries": [<Cube query>, ...], "note": "<one line: what the numbers
  are>", "ledger": [<entry>, ...]}}   (1-{MAX_QUERIES} queries)
  {{"answer": "cannot", "reason": "<why: not in the model / not computable / data gap>"}}
  {{"answer": "clarify", "mode": "<one mode below>", "words": ["<the question's words it is
  about>"], "question": "<the one question you need answered>"}}
A Cube query uses only: measures, dimensions, segments, filters [{{member, operator, values}}],
timeDimensions [{{dimension, dateRange, granularity?}} or {{dimension, compareDateRange:
[[start,end],[start,end]]}}], order, limit. Use only members listed above. Answer "cannot" when
the question needs something the model does not hold. A share, a ratio or a per-day / per-week
average: write the numerator query first and the denominator second (for a per-day average, the
one total over the window); dst divides the parts itself and leads the answer with the figure.
"clarify" only when one of these decides
the number, naming it and the words of the question it is about:{_modes()}
Any other doubt: answer with the reading you chose, said in the note.
{LEDGER_RULES}
"""


# ── the reply ───────────────────────────────────────────────────────────────


def _modes() -> str:
    return "\n" + "\n".join(f"- {m}: {what}" for m, what in CLARIFY_MODES.items())


def parse(raw: str) -> dict[str, Any] | None:
    """The first JSON object in the reply (a code fence tolerated)."""
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    i = s.find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[i:])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def ledger_bindings(entries: list[LedgerEntry]) -> list[Binding]:
    """The reply's ledger as bindings. A word the model mapped to nothing is
    left out: the word check reads an absent word and an unmapped one alike."""
    out = []
    for e in entries:
        kind = _KINDS.get(e.to.strip().lower())
        if kind is None or not e.word.strip():
            continue
        out += [Binding(word=e.word, kind=kind, target=r, source=LANE) for r in e.ref or [""]]
    return out


_LEDGER = TypeAdapter(list[LedgerEntry])


def read_ledger(raw: object) -> list[Binding] | None:
    """The reply's ledger as bindings, or None when there is none or it does
    not read as one (the word check then clarifies, ``uncovered_word``)."""
    if raw is None:
        return None
    try:
        return ledger_bindings(_LEDGER.validate_python(raw))
    except ValidationError:
        return None


def _names(reply: FrontierReply, question: str) -> bool:
    """A clarify names a mode of the vocabulary and words the question says."""
    asked = question.lower()
    return (
        reply.mode in CLARIFY_MODES
        and bool(reply.words)
        and all(w.strip() and w.strip().lower() in asked for w in reply.words)
    )


def read_reply(raw: str, question: str | None = None) -> FrontierResult:
    obj = parse(raw)
    if obj is None:
        return FrontierResult("invalid", reason="the reply held no JSON object", raw=raw)
    if isinstance(obj.get("queries"), list):
        obj["queries"] = obj["queries"][:MAX_QUERIES]
    try:
        reply = FrontierReply.model_validate(obj)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        return FrontierResult(
            "invalid", reason=f"the reply did not validate: {where}: {first['msg']}", raw=raw
        )
    if reply.answer == "cannot":
        return FrontierResult("cannot", reason=reply.reason, raw=raw)
    if reply.answer == "clarify":
        return FrontierResult(
            "clarify",
            question=reply.question,
            raw=raw,
            mode=reply.mode,
            words=reply.words,
            unnamed=question is not None and not _names(reply, question),
        )
    if not reply.queries:
        return FrontierResult("invalid", reason="a query answer with no query", raw=raw)
    ledger = read_ledger(reply.ledger)
    return FrontierResult(
        "query",
        candidates=[Candidate(query=q, ledger=ledger or [], lane=LANE) for q in reply.queries],
        ledger=ledger,
        note=reply.note,
        raw=raw,
    )


def resolve(
    question: str,
    lens: CubeLensView,
    model: ResolvedModel,
    *,
    today: date,
    timezone: str = "",
    groups: list[str] | None = None,
    exemplars: list[tuple[str, CubeQuery]] | None = None,
    max_tokens: int = 4000,
) -> FrontierResult:
    """One question through the model: the context as a cached system block,
    the question as the user turn, the reply read and validated."""
    system = context(lens, today=today, timezone=timezone, groups=groups, exemplars=exemplars)
    # The question's relative periods, resolved here on the lens clock: a
    # fact in the user turn (the system block stays the same, so it caches).
    stated = periods.facts(periods.resolve(question, today), today, lens.probe.clock_end)
    turn = f"QUESTION: {question}" + (f"\n\n{stated}" if stated else "")
    messages = [Message(role="user", content=turn)]
    spend = {"input_tokens": 0.0, "output_tokens": 0.0, "cache_read_tokens": 0.0}
    out = FrontierResult("invalid")
    for attempt in range(2):
        with ai_usage.purpose("generate"):
            result = model.llm.complete(
                system=[CacheableBlock(text=system, ttl="1h")],
                messages=messages,
                model=model.name,
                temperature=0.0,
                max_tokens=max_tokens,
            )
        spend["input_tokens"] += result.input_tokens
        spend["output_tokens"] += result.output_tokens
        spend["cache_read_tokens"] += result.cache_read_tokens
        out = read_reply(result.text, question)
        if not out.unnamed:
            break
        if attempt == 1:
            out = FrontierResult(
                "invalid",
                reason="the lane asked back twice without naming a failure mode: "
                + (out.question or "(no question)"),
                raw=result.text,
            )
            break
        # Asked back once more: answer, or name the mode and the words.
        messages = [
            *messages,
            Message(role="assistant", content=result.text),
            Message(role="user", content=REASK.format(modes=_modes())),
        ]
    out.spend = spend
    for c in out.candidates:
        c.spend = dict(out.spend)
    return out
