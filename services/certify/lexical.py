"""The lexical veto on a certified match.

Cosine similarity cannot tell two governed names apart when the sentences
around them agree: "invoicing revenue last quarter" and "collections revenue
last quarter" sit well inside the exact band, and the approved SQL of one
served the other as ``certified``. The veto is deterministic and narrow: when
the asked question and the approved one EACH name a governed metric or
definition (whole words, case-insensitive, underscores read as spaces) and the
names they carry differ, the pair is not a match whatever the score says. A
question that names nothing governed is left to the score.

A name is read through its regular English inflections: "invoice", "invoiced"
and "invoicing" are one word of the governed term ``invoiced_revenue``, and a
question about invoicing names it. Only the regular suffixes (s, es, ed, ing,
a trailing e) fold, on stems of four letters or more; no stemming library,
nothing irregular. The set a certified answer is read against is the lens's
governed names plus the names its own resolution ledger declares and the
terms attribution stored for it at apply.

A candidate that carries no governed name at all (raw SQL over bare columns,
a question that names none) is read token by token instead: the asked
question's governed words must appear, by inflection, in the candidate's
question or in the columns its SQL reads, or the hit is not a match. A
question about invoicing never takes a raw collections template.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import sqlglot
from sqlglot import exp

from services.contracts.semantic_model import SemanticModel


def governed_names(model: SemanticModel) -> frozenset[str]:
    """Every metric name on a selected entity, every definition term, and the
    entity names themselves (the nouns a question is about)."""
    names = {d.term for d in model.definitions}
    for e in model.entities:
        names.add(e.name)
        names.update(m.name for m in e.metrics)
    return frozenset(n.strip().lower() for n in names if n and n.strip())


def name_aliases(model: SemanticModel) -> dict[str, str]:
    """What people CALL a governed name -> the name: a metric's ``label`` and
    ``aliases``, a definition's ``aliases``. The typed rails read these word
    for word; the veto must too, or a question that names ``overdue_ar_eur``
    by its label "overdue AR in EUR" names nothing and takes the sibling's
    template. First declaration wins a contested phrasing."""
    out: dict[str, str] = {}
    for e in model.entities:
        for m in e.metrics:
            for phrase in (m.label, *m.aliases):
                key = (phrase or "").strip().lower()
                if key and key != m.name.lower():
                    out.setdefault(key, m.name.lower())
    for d in model.definitions:
        for phrase in d.aliases:
            key = (phrase or "").strip().lower()
            if key and key != d.term.lower():
                out.setdefault(key, d.term.lower())
    return out


def declared_units(model: SemanticModel) -> dict[str, str]:
    """metric name -> the unit its author declared: an ISO currency code
    (lowercased) or ``percent``. The only unit facts the lens states outright;
    everything else the unit veto reads off column names."""
    out: dict[str, str] = {}
    for e in model.entities:
        for m in e.metrics:
            if m.currency:
                out[m.name.lower()] = m.currency.strip().lower()
            elif m.format == "percent":
                out[m.name.lower()] = "percent"
    return out


def measure_names(model: SemanticModel) -> frozenset[str]:
    """The metric names and definition terms alone — the names that make two
    questions the same question when both carry them."""
    names = {d.term for d in model.definitions}
    for e in model.entities:
        names.update(m.name for m in e.metrics)
    return frozenset(n.strip().lower() for n in names if n and n.strip())


def answer_names(resolution: Mapping[str, object] | None) -> frozenset[str]:
    """The metric and definition names a certified answer's own resolution
    ledger declares — the names its SQL is known to carry, whatever the
    question's wording."""
    slots = resolution.get("slots") if isinstance(resolution, Mapping) else None
    out: set[str] = set()
    for slot in slots if isinstance(slots, list) else []:
        if not isinstance(slot, Mapping):
            continue
        name = str(slot.get("name") or "").strip().lower()
        if (
            name
            and slot.get("kind") in ("metric", "definition")
            and slot.get("source")
            in (
                "declared",
                "certified",
            )
        ):
            out.add(name)
    return frozenset(out)


_SUFFIXES = ("ing", "ed", "es", "s")
_MIN_STEM = 4


def _stem(word: str) -> str:
    """The word without one regular English suffix (invoiced → invoic,
    customers → customer, revenue → revenu). Short words keep their spelling:
    a three-letter stem would read too much as the name."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            return word[: -len(suffix)]
    if word.endswith("e") and len(word) > _MIN_STEM:
        return word[:-1]
    return word


def _word_pattern(word: str) -> str:
    stem = _stem(word)
    if len(stem) < _MIN_STEM:
        return re.escape(word)
    return re.escape(stem) + r"(?:e|es|ed|ing|s)?"


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[_\s-]+", name.lower()) if w]


def _compile(words: list[str]) -> re.Pattern[str]:
    body = r"[_\s-]+".join(_word_pattern(w) for w in words)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])", re.IGNORECASE)


def _pattern(name: str) -> re.Pattern[str]:
    return _compile(_words(name))


def _distinctive(name: str, names: frozenset[str]) -> list[str]:
    """The words of a multi-word name no other governed name shares (by stem):
    "invoice" alone names ``invoiced_revenue`` when nothing else governed is
    about invoicing, while "revenue" names nothing on its own."""
    words = _words(name)
    if len(words) < 2:
        return []
    others = {_stem(w) for n in names if n != name for w in _words(n)}
    return [w for w in words if len(_stem(w)) >= _MIN_STEM and _stem(w) not in others]


def names_in(
    text: str, names: frozenset[str], aliases: Mapping[str, str] | None = None
) -> frozenset[str]:
    """The governed names the text carries: the whole name as whole words
    (inflections folded), or one of its distinctive words, or one of the
    phrasings ``aliases`` maps to it (a label, an alias — whole words, no
    distinctive-word reading: a phrasing is the author's exact wording)."""
    found = {n for n in names if _pattern(n).search(text)}
    for n in names - found:
        if any(_compile([w]).search(text) for w in _distinctive(n, names)):
            found.add(n)
    for phrase, name in (aliases or {}).items():
        if name in names and name not in found and _pattern(phrase).search(text):
            found.add(name)
    return frozenset(found)


# The words a question spends on asking rather than on what it asks: the
# interrogative frame, articles, auxiliaries, politeness. Everything else —
# a grain (month, quarter), a shape (growth, trend, top, by, per, share, which),
# a dimension, a value, a number — is content that tells two questions apart.
_STOPWORDS = frozenset(
    "a an the of in on at to for from with and or is are was were be been being "
    "do does did we our us you your i me my it its this that these those what how "
    "much many show give tell please number figure value total overall all any "
    "some there here s whats get got have has had can could would should will "
    "as about".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")


def content_tokens(text: str, names: frozenset[str] = frozenset()) -> frozenset[str]:
    """The stems of the words that carry content: stopwords out, and the words
    of every name in ``names`` out (a shared governed name is what two
    questions have in common, not what tells them apart)."""
    shared = {_stem(w) for n in names for w in _words(n)}
    return frozenset(
        _stem(w)
        for w in _TOKEN.findall(text.lower())
        if w not in _STOPWORDS and _stem(w) not in shared
    )


def distinguishing_tokens(
    one: str, other: str, shared_names: frozenset[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """What each question says that the other does not, once stopwords and
    the governed names both carry are removed: ``(only in one, only in other)``.
    Both empty means nothing downstream could tell the questions apart."""
    a, b = content_tokens(one, shared_names), content_tokens(other, shared_names)
    return a - b, b - a


def spoken_words(text: str, names: frozenset[str]) -> frozenset[str]:
    """The stems of the governed words the text actually says: for every name
    it carries, the words of that name present in the text by inflection
    ("invoice" says the ``invoiced`` term; "revenue" alone says nothing of
    ``invoiced_revenue`` when it is not distinctive)."""
    out: set[str] = set()
    for name in names_in(text, names):
        words = _words(name)
        candidates = words if len(words) == 1 else _distinctive(name, names) or words
        out.update(_stem(w) for w in candidates if _compile([w]).search(text))
    return frozenset(out)


def measures_said(
    text: str, measures: frozenset[str], aliases: Mapping[str, str] | None = None
) -> frozenset[str]:
    """The metrics and definitions a question could mean by the words it says:
    every measure one of whose words (four letters or more, by inflection) the
    text carries, and every measure one of whose labels or aliases the text
    says whole. "did we invoice" says ``invoiced_eur`` and ``invoice_count``;
    it says nothing of ``collected_eur``. Looser than ``names_in`` on purpose:
    this is the asked side's reach, compared against what a candidate is
    attributed to carry."""
    said = {
        m
        for m in measures
        if any(len(_stem(w)) >= _MIN_STEM and _compile([w]).search(text) for w in _words(m))
    }
    for phrase, name in (aliases or {}).items():
        if name in measures and _pattern(phrase).search(text):
            said.add(name)
    return frozenset(said)


def lexical_mismatch(
    asked: str,
    approved: str,
    names: frozenset[str],
    approved_names: frozenset[str] = frozenset(),
    approved_terms: frozenset[str] = frozenset(),
    measures: frozenset[str] = frozenset(),
    aliases: Mapping[str, str] | None = None,
) -> bool:
    """True when the asked question names a governed metric/definition the
    approved answer is not about: an invoicing question must not take a
    collections answer. A governed name the asked question carries that the
    approved answer does not is a mismatch; a name the approved answer carries
    beyond the asked one is not (its attribution names the metric its SQL
    computes, which the question rarely spells). ``approved_names`` are names
    the approved answer carries beyond its wording (its resolution ledger);
    ``approved_terms`` is what attribution stored for its SQL at apply —
    governed names and the columns it aggregates. An approved answer that
    carries no governed name at all is read word by word: every governed word
    the asked question says must appear, by inflection, in the approved
    question or in its terms.

    ``measures`` (the metric and definition names alone) adds the measure
    veto: when the asked question's words reach some measures and the
    candidate is attributed to others, with none in common, the candidate
    computes a figure the question did not ask for — "how much did we
    invoice" against a template attributed to ``collected_eur``."""
    if not names:
        return False
    a = names_in(asked, names, aliases)
    if a:
        b = names_in(approved, names, aliases) | approved_names | (approved_terms & names)
        if b:
            if a - b:
                return True
        else:
            carried = {_stem(w) for w in _TOKEN.findall(approved.lower())}
            carried |= {_stem(w) for t in approved_terms for w in _words(t)}
            if any(stem not in carried for stem in spoken_words(asked, a)):
                return True
    if not measures:
        return False
    said = measures_said(asked, measures, aliases)
    carried_measures = (
        names_in(approved, measures, aliases) | approved_names | approved_terms
    ) & measures
    return bool(said and carried_measures and not (said & carried_measures))


# ── the unit veto ────────────────────────────────────────────────────────────
#
# A question that says "in EUR" against a template that reports every balance
# in its own currency sits inside the exact band (the sentences agree) and the
# names agree (both say overdue AR); only the unit differs, and the served
# number is SEK under a question that said EUR. Units are read as declared
# facts: the words the question says, the words the approved question and its
# SQL's identifiers say, the currency or percent format the lens declares on
# the metrics the answer is attributed to. Each group folds its synonyms to
# one spelling; a group the candidate says nothing about is left to the score
# (false positives are forbidden), and a candidate that reports BY currency
# is read as saying "local currency". Deterministic, no model.

_UNIT_GROUPS: dict[str, dict[str, str]] = {
    "currency": {
        **{
            c: c
            for c in (
                "eur usd gbp sek nok dkk chf jpy pln czk huf cad aud nzd cny inr brl zar "
                "isk ron bgn try mxn sgd hkd krw"
            ).split()
        },
        "euro": "eur",
        "euros": "eur",
        "€": "eur",
        "dollar": "usd",
        "dollars": "usd",
        "$": "usd",
        "pound": "gbp",
        "pounds": "gbp",
        "£": "gbp",
        "krona": "sek",
        "kronor": "sek",
        "krone": "nok",
        "kroner": "nok",
        "local": "local",
    },
    "duration": {
        "second": "seconds",
        "seconds": "seconds",
        "minute": "minutes",
        "minutes": "minutes",
        "min": "minutes",
        "mins": "minutes",
        "hour": "hours",
        "hours": "hours",
        "hr": "hours",
        "hrs": "hours",
        # days/weeks/months are left out on purpose: "this month" against a
        # template whose SQL filters a `day` column is a period, not a unit.
    },
    "volume": {
        "litre": "litres",
        "litres": "litres",
        "liter": "litres",
        "liters": "litres",
        "ltr": "litres",
        "ml": "millilitres",
        "millilitre": "millilitres",
        "millilitres": "millilitres",
        "milliliter": "millilitres",
        "milliliters": "millilitres",
        "gallon": "gallons",
        "gallons": "gallons",
    },
    "share": {
        "%": "percent",
        "percent": "percent",
        "percentage": "percent",
        "pct": "percent",
        "perc": "percent",
    },
}
# What the candidate side says that reads as a unit without being one of the
# words above: a result reported BY currency is in local currency.
_UNIT_HINTS: dict[str, tuple[str, str]] = {
    "currency": ("currency", "local"),
    "currencies": ("currency", "local"),
}
_UNIT_TOKEN = re.compile(r"[a-z]+|[€$£%]")


def _unit_words(text: str) -> dict[str, set[str]]:
    """group -> the folded unit words *text* says: "in EUR" says currency eur,
    "local currency" says currency local, "hours" says duration hours."""
    out: dict[str, set[str]] = {}
    lowered = text.lower()
    tokens = _UNIT_TOKEN.findall(lowered)
    for group, words in _UNIT_GROUPS.items():
        said = {words[t] for t in tokens if t in words}
        if group == "currency" and "local" in said and "local currency" not in lowered:
            said.discard("local")  # "local hub" is not a currency
        if said:
            out[group] = said
    return out


def _unit_hints(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for t in _UNIT_TOKEN.findall(text.lower()):
        if t in _UNIT_HINTS:
            group, word = _UNIT_HINTS[t]
            out.setdefault(group, set()).add(word)
    return out


def _identifier_words(sql: str) -> str:
    """The SQL's identifiers as words: ``amount_local`` says "amount local",
    ``fuel_litres`` says litres — a column name is the author's declaration of
    what it holds. String literals are left out (a `WHERE currency = 'EUR'`
    filter is read on its own below)."""
    without_literals = re.sub(r"'[^']*'", " ", sql)
    return re.sub(r"[_.]", " ", " ".join(re.findall(r"[A-Za-z_][\w.]*", without_literals)))


def unit_mismatch(
    asked: str,
    approved: str,
    approved_sql: str,
    approved_terms: frozenset[str] = frozenset(),
    units: Mapping[str, str] | None = None,
) -> str | None:
    """Why the candidate does not produce the unit the question asked for, or
    None. For every unit group the question names (currency, duration,
    volume, share) the candidate's own words — its approved question, the
    identifiers and literals of its SQL, the units the lens declares on the
    measures it is attributed to, "by currency" read as local — are
    collected; a group the candidate says nothing about decides nothing, and
    a group where the candidate says a different unit and never the asked
    one is the mismatch, named."""
    asked_units = _unit_words(asked)
    if not asked_units:
        return None
    literals = " ".join(re.findall(r"'([^']*)'", approved_sql))
    candidate = _unit_words(f"{approved} {_identifier_words(approved_sql)} {literals}")
    for group, words in _unit_hints(approved).items():
        candidate.setdefault(group, set()).update(words)
    for term in approved_terms:
        declared = (units or {}).get(term.lower())
        if declared is None:
            continue
        group = "share" if declared == "percent" else "currency"
        candidate.setdefault(group, set()).add(declared)
    for group, wanted in asked_units.items():
        have = candidate.get(group)
        if not have or wanted & have:
            continue
        return (
            f"the question asks in {'/'.join(sorted(wanted))} and the certified answer "
            f"reports {'/'.join(sorted(have))}"
        )
    return None


# "per driver", "by store tier", "for each account", "for every rep": the
# question asks one figure per member of something. A bare "every" is "all"
# ("revenue from every large order") and asks for one total.
_BREAKDOWN = re.compile(
    r"\b(?:per|by|each|for\s+each|for\s+every)\s+(?:the\s+|a\s+|an\s+)?"
    r"([a-z][a-z0-9_-]*)(?:\s+([a-z][a-z0-9_-]*))?",
    re.I,
)
_NOT_A_BREAKDOWN = frozenset(
    """
    the a an end now then far default cent percent one other way
    in on at of for to from and or with during within over since until than
    """.split()
)
# Words that ask for a grouped answer without naming the unit: a ranking, a
# listing, a series.
_GROUPED_CUE = re.compile(
    r"\b(?:per|by|each|every|which|who|top|rank|ranked|ranking|most|least|highest|lowest|"
    r"hourly|daily|weekly|monthly|quarterly|yearly|annually|over\s+time|trend|breakdown|split)\b",
    re.I,
)


def breakdown_words(question: str) -> list[set[str]]:
    """One word set per breakdown the question asks ("per SDR" -> {sdr})."""
    out: list[set[str]] = []
    for m in _BREAKDOWN.finditer(question):
        words = [w.lower() for w in m.groups() if w and w.lower() not in _NOT_A_BREAKDOWN]
        if m.group(1) and m.group(1).lower() not in _NOT_A_BREAKDOWN and words:
            out.append(set(words))
    return out


def _outer_group(sql: str) -> list[str] | None:
    """The outermost SELECT's GROUP BY expressions as lowercased SQL text — the
    grain the answer comes back at (a GROUP BY inside a subquery dedups rows
    and still returns one total) — or None when the text does not parse."""
    try:
        tree = sqlglot.parse_one(sql)
    except sqlglot.errors.ParseError:
        return None
    while isinstance(tree, exp.Subquery | exp.Paren):
        tree = tree.this
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None:
        return None
    group = select.args.get("group")
    if group is None:
        return []
    out: list[str] = []
    for g in group.expressions:
        # GROUP BY 1 names the first projection
        if isinstance(g, exp.Literal) and g.is_int and 0 < int(g.this) <= len(select.selects):
            g = select.selects[int(g.this) - 1]
        out.append(g.sql().lower())
    return out


def breakdown_mismatch(asked: str, approved: str, approved_sql: str) -> str | None:
    """Why a certified answer does not come back at the grain the question
    asks, or None. "Delivery target and parcels delivered per driver" is not
    "Delivery target and parcels delivered": cosine sees one word apart, the
    certified SQL returns one total. A question asking a breakdown ("per X",
    "by X", "each X", "for every X") that the certified SQL's outer GROUP BY
    does not name, or a certified SQL grouped for a breakdown its question
    asks and the asked question does not, is the mismatch. Deterministic: the
    question's words and the SQL's parse."""
    groups = _outer_group(approved_sql)
    if groups is None:
        return None
    grouped_words = {w for g in groups for w in re.findall(r"[a-z0-9]+", g)}
    # A breakdown the approved question says itself is the approved grain,
    # whatever the SQL groups by ("top 5 stores by sales" orders by it).
    approved_words = {w for ws in breakdown_words(approved) for w in ws}
    for words in breakdown_words(asked):
        forms = {f for w in words for f in (w, w + "s", w[:-1] if w.endswith("s") else w)}
        if not forms & (grouped_words | approved_words):
            return (
                f"the question asks a breakdown by {' '.join(sorted(words))} and the certified "
                "SQL does not group by it"
            )
    if groups and _GROUPED_CUE.search(approved) and not _GROUPED_CUE.search(asked):
        return "the certified SQL is grouped and the question asks no breakdown"
    return None
