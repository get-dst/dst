"""Certified-definitions-bound lens — the certified-definitions directory IS the lens.

A lens is defined by its certified-definition pages. Binding gives both accuracy levers
at once, correct-by-construction:

  - SCOPE  = the union of the pages' ``sources`` (+ declared join tables). The lens
             sees only the tables its certified needs — fewer wrong-table options, a
             smaller/cheaper prompt, and the governance allow-list, from one field.
  - CONTEXT = the pages' rules and SQL exemplars, retrieved per question.

The two levers trade off: scoping alone buys the cheapest prompt,
certified-context over the full schema the most accurate but the biggest, and
the BOUND lane (scoped + certified) sits on the accuracy-per-token frontier
rather than winning outright — over-scoping can exclude a table some question
needs. The decisive point is scale: at production schema sizes "full schema +
certified" does not FIT a context window, so certified-derived scoping is not an
optimization — it is the only path that scales.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from services.certdefs import CertifiedDefinition, ground_truth, load_certified_defs, select_context


@dataclass
class CertifiedDefLens:
    name: str
    metrics: list[CertifiedDefinition]
    extra_tables: tuple[str, ...] = ()  # join tables (dims) no single metric owns
    _scope: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        scope = {src.lower() for m in self.metrics for src in m.sources}
        scope.update(t.lower() for t in self.extra_tables)
        self._scope = frozenset(scope)

    @classmethod
    def from_dir(
        cls, name: str, directory: Path, *, extra_tables: tuple[str, ...] = ()
    ) -> CertifiedDefLens:
        return cls(name=name, metrics=load_certified_defs(directory), extra_tables=extra_tables)

    @property
    def scope(self) -> frozenset[str]:
        """The lens's table allow-list — accuracy boundary and governance boundary, one set."""
        return self._scope

    def in_scope(self, table: str) -> bool:
        return table.lower() in self._scope

    def scoped_schema(self, full_schema: str) -> str:
        """Filter a ``table(cols)``-per-line schema down to the lens's scope."""
        keep = []
        for line in full_schema.splitlines():
            name = line.split("(", 1)[0].strip().lower()
            if name in self._scope:
                keep.append(line)
        return "\n".join(keep)

    def context(self, question: str, *, k: int = 6, include_sql: bool = True) -> str:
        """The certified rules + SQL exemplars relevant to one question."""
        return select_context(self.metrics, question, k=k, include_sql=include_sql)

    @property
    def ground_truth(self) -> dict[str, float | str]:
        return ground_truth(self.metrics)

    @property
    def certified(self) -> dict[str, str]:
        """question → approved SQL, for deterministic serving on exact matches."""
        return {m.question: m.sql for m in self.metrics if m.question and m.sql}
