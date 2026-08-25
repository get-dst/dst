"""The certified rung: question → approved SQL, skipping generation entirely.

Mirrors the product's certified-answer flow (services/certify/store.py +
FixedSQLGenerator + certification="certified" through the real pipeline) with
the pgvector store swapped for an in-memory cosine index over the same
``Embedder`` seam — the matching math and the ≥0.95 run-as-is threshold are
the product's (see search_certified's contract in services/mcp/server.py).

Knowing what's right is the definitions file; *doing* it reliably is this.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from services.contracts.protocols import Embedder

RUN_AS_IS_THRESHOLD = 0.95  # the product's bar for serving certified SQL directly


@dataclass(frozen=True)
class CertifiedMatch:
    question: str
    sql: str
    score: float


class CertifiedIndex:
    def __init__(
        self,
        entries: list[tuple[str, str]],
        embedder: Embedder,
        templates: list[tuple[str, str]] | None = None,
    ) -> None:
        self._entries = entries
        self._embedder = embedder
        self._vectors = self._embed_batch([q for q, _ in entries]) if entries else []
        self._cache: dict[str, list[float]] = {}
        # Parameterized templates: (regex with a (?P<name>…) group, SQL with
        # {name}) — one certified pattern covers a whole question family.
        self._templates = [(re.compile(p, re.IGNORECASE), sql) for p, sql in (templates or [])]

    @classmethod
    def from_yaml(cls, path: Path, embedder: Embedder) -> CertifiedIndex:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(
            [(e["question"], e["sql"]) for e in raw["certified"]],
            embedder,
            templates=[(t["pattern"], t["sql"]) for t in raw.get("templates", [])],
        )

    def __len__(self) -> int:
        return len(self._entries) + len(self._templates)

    def pairs(self) -> list[tuple[str, str]]:
        """The library as (question, approved SQL) — what a caller sees through
        ``GET /v1/lenses/{name}/certified``. Templates render as their pattern."""
        return list(self._entries) + [(p.pattern, sql) for p, sql in self._templates]

    def warm(self, questions: list[str]) -> None:
        """Embed the whole question set in ONE batched call — per-question
        embedding dies on throttled provider tiers."""
        fresh = [q for q in dict.fromkeys(questions) if q not in self._cache]
        if fresh:
            for q, v in zip(fresh, self._embed_batch(fresh), strict=True):
                self._cache[q] = v

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        delay = 21.0  # one tick over a 3-RPM window
        for attempt in range(4):
            try:
                return self._embedder.embed(texts)
            except Exception:  # noqa: BLE001 — throttled tiers; retry then surface
                if attempt == 3:
                    raise
                time.sleep(delay * (attempt + 1))
        raise RuntimeError("unreachable")

    def match(self, question: str) -> CertifiedMatch | None:
        # Templates first: a pattern hit is exact by construction.
        for pattern, sql in self._templates:
            m = pattern.search(question)
            if m:
                name = m.group("name").replace("'", "''")  # SQL-literal escape
                return CertifiedMatch(
                    question=pattern.pattern, sql=sql.replace("{name}", name), score=1.0
                )
        if not self._entries:
            return None
        qv = self._cache.get(question)
        if qv is None:
            (qv,) = self._embed_batch([question])
            self._cache[question] = qv
        best_i, best_score = -1, -1.0
        for i, v in enumerate(self._vectors):
            score = _cosine(qv, v)
            if score > best_score:
                best_i, best_score = i, score
        if best_score < RUN_AS_IS_THRESHOLD:
            return None
        q, sql = self._entries[best_i]
        return CertifiedMatch(question=q, sql=sql, score=round(best_score, 4))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0
