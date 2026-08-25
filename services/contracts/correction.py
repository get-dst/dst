"""Correction delta — the wrong-vs-right structure on a review ticket.

A ticket that carries one of these is a *correction*: not just "this answer was wrong"
but which kind of wrong and, when the caller/owner knows it, the corrected answer/SQL.
The patch drafter (`services/reviews/patch.py`) routes on ``target`` first and
``kind`` second; ``corrected_sql`` is the strongest signal — it can become a
certified candidate. ``target`` pins WHERE a definition patch lands (P19a): the
note's prose steered placement by vocabulary similarity alone, which mistargeted
cross-cutting notes onto unrelated shared definitions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

CorrectionKind = Literal["definition", "scope", "number", "freshness", "other"]


class CorrectionDelta(BaseModel):
    kind: CorrectionKind
    note: str  # what the caller/owner says is wrong
    corrected_answer: str | None = None
    corrected_sql: str | None = None
    # P19a: the definition term this correction is about — used VERBATIM by the
    # drafter (an unknown term drafts a NEW definition), and AUTHORITATIVE over
    # `kind`: naming a term is the routing decision, whatever kind of wrong it was
    # filed as. Absent → note-based routing.
    target: str | None = None
