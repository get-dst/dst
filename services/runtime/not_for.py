"""The asks a lens declares it does not answer, refused before typing or generation.

A lens over tickets sold can produce a number for "door-scan attendance";
a seat-level table can sum something for "booking-level refunds".
Both figures are well-formed and wrong, and nothing in the SQL says so. The
author knows which asks those are: ``not_for: [{ask, sibling}]`` in lens.yaml,
and ``Avoid for:`` lines in an entity's ``use_cases``.

The match is literal and deterministic: the ask's content words must appear
in the question as a phrase, in order and next to each other (function words
between them ignored, hyphens and underscores read as spaces, case ignored).
A question sharing one word with an ask, or scattering its words, is not that
ask, so it is never refused for it. When several asks match, the longest
names the refusal. A one-word ask whose word sits inside one of the lens's
own multi-word metrics ("rate" on a lens measuring "retention rate") would
mask that metric, so it is never an ask.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from services.contracts.semantic_model import SemanticModel

# Function words carry no part of what an ask is about; everything else does.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "it",
        "its",
        "me",
        "of",
        "on",
        "or",
        "per",
        "show",
        "the",
        "their",
        "to",
        "what",
        "when",
        "where",
        "which",
        "with",
    }
)
_AVOID = re.compile(r"^\s*avoid\s+for\s*:\s*(.+)$", re.IGNORECASE)
_PAREN = re.compile(r"\([^)]*\)")


@dataclass(frozen=True)
class NotForAsk:
    ask: str
    sibling: str = ""


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", re.sub(r"[_\-]+", " ", text.lower()))


def content_words(ask: str) -> list[str]:
    """The words of *ask* a question must all name for it to be that ask."""
    return [w for w in _words(ask) if w not in _STOPWORDS]


def masked(ask: str, metric_phrases: Iterable[str]) -> bool:
    """Is *ask* one content word that sits inside a multi-word metric phrase
    of the lens's own? Such an ask would refuse the lens's own metric."""
    words = content_words(ask)
    if len(words) != 1:
        return False
    for phrase in metric_phrases:
        own = content_words(phrase)
        if len(own) > 1 and words[0] in own:
            return True
    return False


def usable_asks(asks: Iterable[NotForAsk], metric_phrases: Iterable[str]) -> list[NotForAsk]:
    """*asks* without those that would mask one of the lens's own metrics,
    the longest first (the one a refusal names when several match)."""
    phrases = list(metric_phrases)
    kept = [a for a in asks if a.ask.strip() and not masked(a.ask, phrases)]
    return sorted(kept, key=lambda a: -len(content_words(a.ask)))


def metric_phrases(model: SemanticModel) -> list[str]:
    """What the lens's metrics are called: names, labels and aliases."""
    out: list[str] = []
    for e in model.entities:
        for m in e.metrics:
            out += [m.name, m.label or "", *m.aliases]
    return [p for p in out if p.strip()]


def declared_asks(model: SemanticModel) -> list[NotForAsk]:
    """Every ask the lens declares it does not answer: the lens's ``not_for``
    entries, then each ``Avoid for:`` use case of its entity (no sibling;
    a parenthetical is commentary, and ``;`` or ``,`` separates asks). Entity
    lines count only when the lens has one governed entity: on a lens with
    several, an ask one entity avoids may be exactly what another answers.
    An ask that would mask one of the lens's own metrics is left out; the
    longest come first."""
    asks = [
        NotForAsk(ask=str(e.get("ask") or ""), sibling=str(e.get("sibling") or ""))
        for e in model.not_for
        if str(e.get("ask") or "").strip()
    ]
    governed = [e for e in model.entities if not e.exposed]
    if len(governed) == 1:
        for line in governed[0].use_cases:
            m = _AVOID.match(line)
            if m is None:
                continue
            for part in re.split(r"[;,]", _PAREN.sub(" ", m.group(1))):
                part = part.strip().rstrip(".").strip()
                if part:
                    asks.append(NotForAsk(ask=part))
    return usable_asks(asks, metric_phrases(model))


def is_avoid_line(line: str) -> bool:
    """Is this ``use_cases`` line an ``Avoid for:`` line (what the entity is not for)?"""
    return _AVOID.match(line) is not None


def names_ask(question: str, ask: str) -> bool:
    """Does *question* say *ask* as a phrase: its content words in order,
    next to each other once the question's function words are dropped? The
    one matcher: the lens's own door refuses on it and the router drops a
    candidate on it."""
    words = content_words(ask)
    said = content_words(question)
    if not words or len(words) > len(said):
        return False
    return any(said[i : i + len(words)] == words for i in range(len(said) - len(words) + 1))


def not_for_hit(question: str, model: SemanticModel) -> NotForAsk | None:
    """The longest declared ask the question says as a phrase, or None."""
    return next((a for a in declared_asks(model) if names_ask(question, a.ask)), None)


def not_for_refusal(question: str, model: SemanticModel) -> str | None:
    """The refusal for a question that says a declared ask as a phrase, or
    None. The longest matching ask wins."""
    item = not_for_hit(question, model)
    if item is None:
        return None
    reason = f"this lens does not answer {item.ask.strip()}"
    return f"{reason}; ask {item.sibling.strip()}" if item.sibling.strip() else reason
