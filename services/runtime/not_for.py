"""The asks a lens declares it does not answer, refused before typing or generation.

A lens over tickets sold can produce a number for "door-scan attendance";
a seat-level table can sum something for "booking-level refunds".
Both figures are well-formed and wrong, and nothing in the SQL says so. The
author knows which asks those are: ``not_for: [{ask, sibling}]`` in lens.yaml,
and ``Avoid for:`` lines in an entity's ``use_cases``.

The match is literal and deterministic: every content word of the ask must
appear in the question as a whole word (hyphens and underscores read as
spaces, case ignored). A question sharing one word with an ask is not that
ask, so it is never refused for it.
"""

from __future__ import annotations

import re
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


def declared_asks(model: SemanticModel) -> list[NotForAsk]:
    """Every ask the lens declares it does not answer: the lens's ``not_for``
    entries, then each ``Avoid for:`` use case of its entity (no sibling;
    a parenthetical is commentary, and ``;`` or ``,`` separates asks). Entity
    lines count only when the lens has one governed entity: on a lens with
    several, an ask one entity avoids may be exactly what another answers."""
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
    return asks


def is_avoid_line(line: str) -> bool:
    """Is this ``use_cases`` line an ``Avoid for:`` line (what the entity is not for)?"""
    return _AVOID.match(line) is not None


def names_ask(question: str, ask: str) -> bool:
    """Does *question* name every content word of *ask*? The one matcher: the
    lens's own door refuses on it and the router drops a candidate on it."""
    words = content_words(ask)
    said = set(_words(question))
    return bool(words) and all(w in said for w in words)


def not_for_refusal(question: str, model: SemanticModel) -> str | None:
    """The refusal for a question that names every content word of a declared
    ask, or None. First declared ask wins."""
    for item in declared_asks(model):
        if names_ask(question, item.ask):
            reason = f"this lens does not answer {item.ask.strip()}"
            return f"{reason}; ask {item.sibling.strip()}" if item.sibling.strip() else reason
    return None
