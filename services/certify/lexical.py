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
governed names plus the names its own resolution ledger declares.
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


def lexical_mismatch(
    asked: str, approved: str, names: frozenset[str], approved_names: frozenset[str] = frozenset()
) -> bool:
    """True when both questions name governed metrics/definitions and the
    names differ: an invoicing question must not take a collections answer.
    ``approved_names`` are names the approved answer carries beyond its
    wording (its resolution ledger)."""
    if not names:
        return False
    a, b = names_in(asked, names), names_in(approved, names) | approved_names
    return bool(a) and bool(b) and a != b
