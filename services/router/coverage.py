"""Whole-question coverage for routing: does a lens hold every word the question asks with?

A lens is a candidate for a question only when everything the question asks
for has somewhere to land in it: a metric (name, label, aliases, description),
a field or dimension (and a dimension's aliases), a stored value, a
definition, the entity's row noun or declared scope, or the period. One word
the lens has nothing for (a filter it lacks, a value only a sibling stores)
means it would answer a different question, so the router passes it over and
tries the next candidate.

The check reads the lens's declared vocabulary (``CoverageProfile.vocabulary``,
built from the published files and the probe's stored dictionaries) with the
typing-time word rules (``typed_resolver``: the same grammar words, period
phrases and stems the qualifier check reads). It is the router's predicate,
made before any typing. A word counts against a lens only when some lens the
caller may use declares it as a name (a metric, label, alias, field,
dimension, stored value, definition or source system): a word no lens
declares (a verb, a synonym the metric vote reads, a word met only in some
description) separates no lens from another, so it does not pass one over
here; the routed lens's own typing settles it.

``covers`` is the narrow seam: a lens, a question, the words the lenses
declare -> ``(covers, unused words)``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from services.router import CoverageProfile
from services.runtime import timewindow
from services.runtime.typed_resolver import (
    _WORD,
    _coverage_exempt,
    _forms,
    _sentence_initial,
    _word_stem,
)


def word_keys(word: str) -> set[str]:
    """The spellings a word matches a lens's words by: its stem and its
    singular/plural forms, stemmed."""
    return {_word_stem(word), *(_word_stem(f) for f in _forms(word))}


def vocabulary(texts: Iterable[str]) -> frozenset[str]:
    """The stems of every word of ``texts``: what a lens holds, as the
    coverage check matches it."""
    out: set[str] = set()
    for t in texts:
        for w in _WORD.findall(str(t).lower()):
            out |= word_keys(w)
    return frozenset(out)


def _asked_words(question: str) -> list[tuple[str, bool]]:
    """``(word, written as a name)`` for each content word the question asks
    with, period phrases and possessives left out, in order, once each. A word
    is written as a name when it is capitalised away from a sentence start or
    quoted: it may be a value of a column whose dictionary is not complete."""
    text = re.sub(r"[\'’]s\b", "  ", timewindow.beside_windows(question))
    seen: set[str] = set()
    out: list[tuple[str, bool]] = []
    for m in re.finditer(r"[A-Za-z0-9]+", text):
        raw = m.group(0)
        w = raw.lower()
        if w in seen or len(w) < 3 or _coverage_exempt(w):
            continue
        seen.add(w)
        quoted = m.start() > 0 and text[m.start() - 1] in "'\"`‘“"
        capital = raw[0].isupper() and not _sentence_initial(text, m.start())
        out.append((w, quoted or capital))
    return out


def asked_words(question: str) -> list[str]:
    """The content words the question asks with, period phrases left out."""
    return [w for w, _as_name in _asked_words(question)]


def unused_words(
    question: str, profile: CoverageProfile, held: frozenset[str] | None = None
) -> list[str]:
    """The question's content words this lens has nothing for. ``held``: the
    words the lenses declare as names (``held_by``); a word outside it is left to the routed lens's
    own typing (see the module docstring). A word written as a name counts as
    held by a lens with a text column whose stored values are incomplete."""
    vocab = profile.vocabulary
    if vocab is None:
        return []
    out = []
    for word, as_name in _asked_words(question):
        keys = word_keys(word)
        if keys & vocab:
            continue
        if held is not None and not keys & held:
            continue
        if as_name and profile.open_values:
            continue
        out.append(word)
    return out


def covers(
    profile: CoverageProfile, question: str, held: frozenset[str] | None = None
) -> tuple[bool, list[str]]:
    """``(True, [])`` when the lens holds every word of the question that some
    lens declares; else ``(False, the words it lacks)``. A lens with no
    vocabulary (a profile not built from published files) is not checked."""
    unused = unused_words(question, profile, held)
    return not unused, unused


def held_by(profiles: Iterable[CoverageProfile]) -> frozenset[str] | None:
    """Every word any of these lenses declares as a name (``declared``: not
    prose); None when none is checkable."""
    vocabs = [p.declared for p in profiles if p.declared is not None]
    return frozenset().union(*vocabs) if vocabs else None
