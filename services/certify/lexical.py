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

from services.contracts.semantic_model import SemanticModel


def governed_names(model: SemanticModel) -> frozenset[str]:
    """Every metric name on a selected entity, every definition term, and the
    entity names themselves (the nouns a question is about)."""
    names = {d.term for d in model.definitions}
    for e in model.entities:
        names.add(e.name)
        names.update(m.name for m in e.metrics)
    return frozenset(n.strip().lower() for n in names if n and n.strip())


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


def names_in(text: str, names: frozenset[str]) -> frozenset[str]:
    """The governed names the text carries: the whole name as whole words
    (inflections folded), or one of its distinctive words."""
    found = {n for n in names if _pattern(n).search(text)}
    for n in names - found:
        if any(_compile([w]).search(text) for w in _distinctive(n, names)):
            found.add(n)
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


def measures_said(text: str, measures: frozenset[str]) -> frozenset[str]:
    """The metrics and definitions a question could mean by the words it says:
    every measure one of whose words (four letters or more, by inflection) the
    text carries. "did we invoice" says ``invoiced_eur`` and ``invoice_count``;
    it says nothing of ``collected_eur``. Looser than ``names_in`` on purpose:
    this is the asked side's reach, compared against what a candidate is
    attributed to carry."""
    return frozenset(
        m
        for m in measures
        if any(len(_stem(w)) >= _MIN_STEM and _compile([w]).search(text) for w in _words(m))
    )


def lexical_mismatch(
    asked: str,
    approved: str,
    names: frozenset[str],
    approved_names: frozenset[str] = frozenset(),
    approved_terms: frozenset[str] = frozenset(),
    measures: frozenset[str] = frozenset(),
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
    a = names_in(asked, names)
    if a:
        b = names_in(approved, names) | approved_names | (approved_terms & names)
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
    said = measures_said(asked, measures)
    carried_measures = (names_in(approved, measures) | approved_names | approved_terms) & measures
    return bool(said and carried_measures and not (said & carried_measures))
