"""MEMO — the lens's conventions as a 4 KB CONVENTIONS.md.

MEMO is not a strawman control, it is *the* control: a published evaluation
found a 4 KB markdown conventions file buying +17–23pp across three frontier
models (arXiv 2604.25149), so an arm reported only against an uninformed
baseline is uninterpretable — it is measuring the presence of a file, and one
markdown file refutes it.

**The honesty rule: this file is RENDERED FROM THE LENS ITSELF.** Its inputs are
the lens's own description (entities, fields, definitions, standing orders), the
same prose context the lens retrieves, and the same certified library the lens
serves — same author, conventions verbatim. So the arms differ in *form*, not in
*information*. A hand-written file would be a strawman if written thin and a
contamination if written against known misses; deriving it removes both.

Three deliberate choices, all favouring the memo, because a tie found here is
worth more than a win a customer refutes:

1. The SQL arms get the certified SQL *bodies*; the lens arm's ``describe``
   gets certified *questions* (it cannot run SQL, so bodies would burn tokens).
2. Sections are packed most-decisive-first, so the 4 KB cap spends its bytes on
   standing orders and definitions and drops trivia — never the reverse.
3. The cap is filled, not merely respected: the last section that does not fit
   whole is truncated at a line boundary rather than dropped.
"""

from __future__ import annotations

from services.lenses.describe import LensDescription

from .lanes import DstLane

# The size used by the published evaluation (arXiv 2604.25149). Build MEMO at
# this size: bigger is a different experiment, smaller is a strawman.
MEMO_BYTES = 4096

_CERT_SQL_CHARS = 600


def _sections(
    description: LensDescription,
    *,
    tables: dict[str, str] | None,
    context: list[str] | None,
    certified: list[tuple[str, str]] | None,
    certified_sql: bool,
) -> list[list[str]]:
    """Most decisive first. The packer spends the byte budget in this order."""
    out: list[list[str]] = []
    if description.instructions:
        out.append(
            ["## Standing orders (how an answer must be shaped)", "", description.instructions]
        )

    ambiguous = [d for d in description.definitions if d.status == "ambiguous"]
    if ambiguous:
        block = ["## Ambiguous terms — DO NOT GUESS", ""]
        for d in ambiguous:
            block += [f"### {d.term}", d.body]
            if d.possible_mappings:
                block += ["Possible meanings:"] + [f"- {m}" for m in d.possible_mappings]
            block += [
                "Asking this without picking a meaning produces a plausible "
                "wrong number. Say which meaning you used.",
                "",
            ]
        out.append(block)

    active = [d for d in description.definitions if d.status != "ambiguous"]
    if active:
        block = ["## Definitions", ""]
        for d in active:
            block += [f"### {d.term}", d.body, ""]
        out.append(block)

    # The prose conventions come BEFORE the approved SQL: a real data team's
    # CONVENTIONS.md is rules, not ten pasted dashboard queries, and at 4 KB the
    # certified block alone would crowd every rule out of the file.
    for i, chunk in enumerate(context or [], 1):
        out.append([f"## Reference notes ({i})", "", chunk])

    if certified:
        if certified_sql:
            block = [
                "## Approved answers",
                "",
                "The data team has approved this SQL for these questions. It is "
                "correct by review, not by inference — prefer it over anything "
                "you would write yourself for the same question.",
            ]
            for q, sql in certified:
                block += ["", f"### {q}", "```sql", sql.strip()[:_CERT_SQL_CHARS], "```"]
        else:
            block = [
                "## Approved answers",
                "",
                "These questions have human-approved SQL behind them and are "
                "served deterministically, with no generation. Phrasing your ask "
                "close to one of these gets you the approved answer:",
            ] + [f"- {q}" for q, _ in certified]
        out.append(block)

    described = [
        e for e in description.entities if e.description or any(f.description for f in e.fields)
    ]
    if described:
        block = ["## Tables and columns (only where the name is not the whole story)", ""]
        for e in described:
            physical = (tables or {}).get(e.name, e.name)
            block.append(f"### {physical}" + (f" — {e.description}" if e.description else ""))
            block += [
                f"- `{f.name}` ({f.type}) — {f.description}" for f in e.fields if f.description
            ]
            block.append("")
        out.append(block)

    if description.sample_questions:
        out.append(
            ["## Questions this warehouse is known to answer", ""]
            + [f"- {q}" for q in description.sample_questions]
        )
    return out


def render_conventions(
    description: LensDescription,
    *,
    tables: dict[str, str] | None = None,
    context: list[str] | None = None,
    certified: list[tuple[str, str]] | None = None,
    certified_sql: bool = True,
    cap: int = MEMO_BYTES,
) -> str:
    """The lens as one CONVENTIONS.md, packed to ``cap`` bytes.

    ``tables`` maps entity name → physical ``schema.table`` so an arm that
    writes raw SQL can address what the definitions talk about.
    """
    header = "\n".join(
        [
            f"# {description.display_name} — CONVENTIONS.md",
            "",
            description.description,
            f"Dialect: {description.dialect}. Qualify every table as `schema.table`.",
            "",
            "These are the company's meanings, not general knowledge. Where a "
            "convention below contradicts what the column names suggest, the "
            "convention wins — that is the whole point of writing it down.",
        ]
    )
    out = header
    for block in _sections(
        description,
        tables=tables,
        context=context,
        certified=certified,
        certified_sql=certified_sql,
    ):
        text = "\n\n" + "\n".join(block).strip()
        room = cap - len(out)
        if room <= 0:
            break
        if len(text) <= room:
            out += text
            continue
        # Fill the budget rather than waste it: cut at the last line boundary.
        clipped = text[:room].rsplit("\n", 1)[0].rstrip()
        if len(clipped) > len(text) // 8:  # a stub of a section teaches nothing
            out += clipped
        break
    return out.strip() + "\n"


def memo_file(lane: DstLane, *, certified_sql: bool = True, cap: int = MEMO_BYTES) -> str:
    """Render MEMO straight off a ``DstLane``.

    Both the memo arms and the lens arm's ``describe`` tool are built from this
    one call, so information parity between them is structural rather than a
    promise someone has to keep updated.
    """
    index = lane.certified_index
    return render_conventions(
        lane.describe(),
        tables={e.name: e.source.table for e in lane.semantic_model.entities},
        context=[c.text for c in lane.prose_context],
        certified=index.pairs() if index else None,
        certified_sql=certified_sql,
        cap=cap,
    )
