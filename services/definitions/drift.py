"""Definition drift — flag when a lens's definition conflicts with an org standard
or with the same term in another lens.

Compares authored definitions against (a) org standards and (b) other lenses'
definitions; it does not mine candidates from raw sources. A conflict is a
same-term, different-meaning pair, surfaced for merge/resolve.
"""

from __future__ import annotations

from pydantic import BaseModel

from services.contracts.semantic_model import Definition
from services.definitions.standards import OrgStandard


class DriftConflict(BaseModel):
    term: str
    kind: str  # "org_standard" | "other_lens" | "dbt"
    source: str  # "org standard", the other lens name, or "dbt"
    lens_body: str
    other_body: str


def _differs(a: Definition, body: str, sql_expr: str | None) -> bool:
    return a.body.strip() != body.strip() or (a.sql_expr or "").strip() != (sql_expr or "").strip()


def compare(
    definitions: list[Definition],
    standards: list[OrgStandard],
    other_lenses: list[tuple[str, list[Definition]]],
) -> list[DriftConflict]:
    """Return conflicts between `definitions` and the org standards / other lenses."""
    by_standard = {s.term: s for s in standards}
    conflicts: list[DriftConflict] = []

    for d in definitions:
        std = by_standard.get(d.term)
        if std is not None and _differs(d, std.body, std.sql_expr):
            conflicts.append(
                DriftConflict(
                    term=d.term,
                    kind="org_standard",
                    source="org standard",
                    lens_body=d.body,
                    other_body=std.body,
                )
            )
        for lens_name, other_defs in other_lenses:
            for od in other_defs:
                if od.term == d.term and _differs(d, od.body, od.sql_expr):
                    conflicts.append(
                        DriftConflict(
                            term=d.term,
                            kind="other_lens",
                            source=lens_name,
                            lens_body=d.body,
                            other_body=od.body,
                        )
                    )
    return conflicts
