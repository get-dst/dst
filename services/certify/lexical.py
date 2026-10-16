"""The lexical veto on a certified match.

Cosine similarity cannot tell two governed names apart when the sentences
around them agree: "invoicing revenue last quarter" and "collections revenue
last quarter" sit well inside the exact band, and the approved SQL of one
served the other as ``certified``. The veto is deterministic and narrow: when
the asked question and the approved one EACH name a governed metric or
definition (whole words, case-insensitive, underscores read as spaces) and the
names they carry differ, the pair is not a match whatever the score says. A
question that names nothing governed is left to the score.
"""

from __future__ import annotations

import re

from services.contracts.semantic_model import SemanticModel


def governed_names(model: SemanticModel) -> frozenset[str]:
    """Every metric name on a selected entity plus every definition term."""
    names = {d.term for d in model.definitions}
    for e in model.entities:
        names.update(m.name for m in e.metrics)
    return frozenset(n.strip().lower() for n in names if n and n.strip())


def _pattern(name: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in re.split(r"[_\s-]+", name) if w]
    return re.compile(r"(?<![a-z0-9])" + r"[_\s-]+".join(words) + r"(?![a-z0-9])", re.IGNORECASE)


def names_in(text: str, names: frozenset[str]) -> frozenset[str]:
    """The governed names the text carries as whole words."""
    return frozenset(n for n in names if _pattern(n).search(text))


def lexical_mismatch(asked: str, approved: str, names: frozenset[str]) -> bool:
    """True when both questions name governed metrics/definitions and the
    names differ: an invoicing question must not take a collections answer."""
    if not names:
        return False
    a, b = names_in(asked, names), names_in(approved, names)
    return bool(a) and bool(b) and a != b
