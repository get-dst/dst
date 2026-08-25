"""The prompt an author can actually look at — assembled, never generated.

`generator.serialize_model` renders what the model sees, and its only production
caller is inside the generation call itself: no route, no verb, no way to look.
The cost is real — an author can write `dimensions:` blocks that parse, validate,
apply and land in compiled.yaml while never reaching the prompt, with a full
benchmark lap as the only feedback channel.

`preview()` runs the SAME assembly seam production serves (`runtime.assembly`),
renders the prompt off it, and then checks every authored asset against the
rendered text. Zero LLM calls and zero warehouse calls: assembly already built
all of it. The question it answers is not "dump the prompt" but "is my
definition reaching the model, and if not, why not" — so an absence is a row,
with the reason on it.

The certified serve bypass is OFF here (`serve_certified=False`): the preview is
about the GENERATION path. Whether a question serves from the certified library
instead is a different question, and `/v1/lenses/{name}/certified?q=` answers it.

A served request makes TWO model calls, and both are rendered here. Rendering
only the generation call is how a surface built to show "what the model sees"
misses the composer — the call that writes the English a person reads — and a
composer with no semantic model writes *"no bond is recorded … a null or
placeholder value"* about a `'-'` the lens's own definition page calls the
single bond, with every badge on the response saying verified. The composer
half renders off `answer.compose_prompt` itself, so neither can drift from
what is actually sent.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, Field

from services.contracts.protocols import GeneratedQuery
from services.contracts.semantic_model import SemanticModel
from services.contracts.warehouse import QueryResult
from services.lenses.store import LensBundle
from services.runtime import assembly
from services.runtime.answer import bound_column, compose_prompt
from services.runtime.generator import serialize_model
from services.runtime.intent_generator import serialize_layer
from services.runtime.prompt_version import PROMPT_HASH

# The intent tier's first pass renders a leaner prompt (names only, no join
# graph, no sql_expr, no sample queries) — an asset the raw-SQL prompt carries
# but that one drops reaches the model ONLY if the first pass fails and
# generation escalates.
_ESCALATION_ONLY = (
    "the metric-layer first pass does not render it — it reaches the model only "
    "if generation escalates to the raw-SQL tier"
)
_NOT_RENDERED = "authored, but the generation prompt does not render it"
_NOT_SELECTED = "in the shared semantic layer, but this lens's select block does not pick it"
_ROUTER_ONLY = "router anchors only — use_when never rides a generation prompt"
_TRIMMED_QUESTIONS = "trimmed: serialize_model renders only the first 5 common_questions per entity"
_COMPOSE_UNBOUND = (
    "no `about: entity.column` — reaches the composer only if a result column is "
    "literally named after the term"
)

# PromptAsset.kind -> the key an author actually types, for the apply-time
# warning. Only kinds whose spelling differs need an entry.
_AUTHORED_AS = {
    "join": "joins",
    "sample_query": "sample_queries",
    "common_question": "common_questions",
    "definition sql": "definition sql_expr",
}


class PromptAsset(BaseModel):
    """One authored thing, and whether it reached the model.

    ``note`` carries the WHY of an absence (not selected / trimmed / not
    rendered) or a qualifier on a presence (escalation-only)."""

    kind: str
    name: str
    in_prompt: bool
    note: str | None = None


class PromptChunk(BaseModel):
    source: str
    score: float = 0.0
    text: str


class PromptPreview(BaseModel):
    lens: str
    question: str
    generator_tier: str
    prompt_hash: str
    system: str  # generator.serialize_model — the raw-SQL prompt
    # The metric-layer first-pass prompt, present only when that tier serves
    # first; the model sees THIS one before it ever sees `system`.
    intent_system: str | None = None
    prose: list[PromptChunk]
    counts: dict[str, int]
    assets: list[PromptAsset]
    # THE SECOND MODEL CALL. Everything above decides the SQL; these decide the
    # English a person reads. Rendered off `answer.compose_prompt` with the SQL,
    # rows and row count slotted (the preview runs no generator and no warehouse),
    # and with every column-bound definition shown — a real serve carries only the
    # ones bound to a column its own answer projects.
    compose_system: str = ""
    compose_user: str = ""
    compose_assets: list[PromptAsset] = Field(default_factory=list)


def _present(text: str, token: str) -> bool:
    """Is ``token`` rendered in ``text``, on its own (word-boundary) terms?

    Boundaries only where the token has a word edge, so probes like ``- churn``
    and ``Entity 'orders':`` match as written. The check reads the RENDERED
    prompt rather than re-implementing the serializer, so a field the serializer
    silently stops rendering shows up here the same day it stops."""
    left = r"\b" if token[:1].isalnum() or token[:1] == "_" else ""
    right = r"\b" if token[-1:].isalnum() or token[-1:] == "_" else ""
    return re.search(left + re.escape(token) + right, text) is not None


def prompt_assets(
    model: SemanticModel,
    system: str,
    intent_system: str | None = None,
    *,
    unselected: Sequence[tuple[str, str]] = (),
) -> list[PromptAsset]:
    """Every authored asset of ``model``, checked against the rendered prompt(s).

    ``unselected`` are shared-layer assets this lens does NOT select — the most
    common reason a definition an author swears they wrote is nowhere in the
    prompt (the caller supplies them; this module stays free of the DB)."""
    out: list[PromptAsset] = []

    def add(
        kind: str, name: str, probe: str, *, absent: str = _NOT_RENDERED, lean: str = ""
    ) -> None:
        if not _present(system, probe):
            out.append(PromptAsset(kind=kind, name=name, in_prompt=False, note=absent))
            return
        note = None
        if intent_system is not None and not _present(intent_system, lean or probe):
            note = _ESCALATION_ONLY
        out.append(PromptAsset(kind=kind, name=name, in_prompt=True, note=note))

    for e in model.entities:
        add("entity", e.name, e.source.table, lean=e.name)
        for f in e.fields:
            add("field", f"{e.name}.{f.name}", f"{e.name}.{f.name}", lean=f.name)
        for dim in e.dimensions:
            add(
                "dimension",
                f"{e.name}.{dim.name}",
                f"dimension {e.name}.{dim.name}",
                lean=dim.name,
            )
        for m in e.metrics:
            add("metric", m.name, f"metric {m.name}", lean=m.name)
        for q in e.common_questions:
            add("common_question", f"{e.name}: {q}", f"answers: {q}", absent=_TRIMMED_QUESTIONS)
    for j in model.joins:
        add("join", f"{j.left} -> {j.right}", j.on)
    for d in model.definitions:
        add("definition", d.term, f"- {d.term}", lean=f"- {d.term}:")
        if d.sql_expr:
            # The enforceable half of a definition, tracked separately: a term can
            # be rendered while the `[sql: <expr>]` the generator is told to embed
            # VERBATIM is not (the metric-layer prompt renders bodies only).
            add("definition sql", f"{d.term} [sql]", d.sql_expr)
    for s in model.sample_queries:
        add("sample_query", s.question, f"Q: {s.question}")
    if model.ai_instructions:
        add("instructions", "ai_instructions", model.ai_instructions)
    if model.use_when:
        # Real and load-bearing — for the ROUTER, which is not the model that
        # writes the SQL. An author who put steering here is steering nothing.
        out.append(
            PromptAsset(
                kind="use_when",
                name=f"{len(model.use_when)} routing example(s)",
                in_prompt=False,
                note=_ROUTER_ONLY,
            )
        )
    out += [
        PromptAsset(kind=kind, name=name, in_prompt=False, note=_NOT_SELECTED)
        for kind, name in unselected
    ]
    return out


def escalation_only(model: SemanticModel) -> list[str]:
    """The authored things this lens loses on the FIRST generation pass, named as
    an author typed them (``joins``, ``sample_queries``, …), most-authored first.

    A lens with metrics generates on the intent tier, whose leaner serialization
    drops assets the raw-SQL prompt carries — so they reach the model only when
    that pass fails and the pipeline escalates. That condition is invisible from
    lens.yaml, which is the same silence `dimensions:` and `use_when` cost four
    agents apiece. Derived from the RENDERED prompts, never a hand-kept list: a
    field that stops (or starts) surviving the lean pass changes this the same
    day. Empty for a lens without metrics — there is no first pass to lose."""
    if not any(e.metrics for e in model.entities):
        return []
    assets = prompt_assets(model, serialize_model(model), serialize_layer(model))
    counts = Counter(a.kind for a in assets if a.note == _ESCALATION_ONLY)
    return [f"{n} {_AUTHORED_AS.get(kind, kind)}" for kind, n in counts.most_common()]


def compose_assets(model: SemanticModel) -> list[PromptAsset]:
    """Which definition pages can reach the COMPOSER, and on what condition.

    A page bound to a column (`about: entity.column`) is sent whenever the answer
    projects that column; a page without one is sent only on the coincidence that
    a result column is named exactly like the term. That is the whole rule, and
    an author who wants a page to govern the English needs to see it — this is
    the surface where "my definition is in the prompt" stopped being one fact."""
    out: list[PromptAsset] = []
    for d in model.definitions:
        col = bound_column(d)
        out.append(
            PromptAsset(
                kind="definition",
                name=d.term,
                in_prompt=bool(col),
                note=f"sent when the answer projects `{col}`" if col else _COMPOSE_UNBOUND,
            )
        )
    return out


def _compose_preview(model: SemanticModel, question: str) -> tuple[str, str]:
    """The composer's two blocks, rendered by the composer's own builder.

    The preview runs no generator and no warehouse, so the SQL, the rows and the
    row count are named placeholders — the SHAPE is exact because it is the same
    f-string production sends. Columns are every column a definition binds to, so
    the governed-definition block renders in full; a real serve carries the subset
    its own answer projects."""
    cols = sorted({c for c in (bound_column(d) for d in model.definitions) if c})
    return compose_prompt(
        question=question,
        generated=GeneratedQuery(
            sql="<the SQL generation returns>", definition_used="<the definition it reports>"
        ),
        result=QueryResult(
            columns=cols or ["<the answer's columns>"],
            rows=[["<value>"] * (len(cols) or 1)],
        ),
        semantic_model=model,
    )


def preview(
    bundle: LensBundle,
    question: str,
    org_id: uuid.UUID | str,
    *,
    unselected: Sequence[tuple[str, str]] = (),
) -> PromptPreview:
    """Assemble this question exactly as serving would and render what the model
    would be sent — no generation, no warehouse, no LLM."""
    assembled = assembly.assemble(bundle, question, org_id, serve_certified=False)
    tier = assembly.generator_tier(assembled)
    system = serialize_model(assembled.model)
    intent_system = serialize_layer(assembled.model) if tier == "intent" else None
    compose_system, compose_user = _compose_preview(assembled.model, question)
    return PromptPreview(
        lens=bundle.config.name,
        question=question,
        generator_tier=tier,
        prompt_hash=PROMPT_HASH,
        system=system,
        intent_system=intent_system,
        prose=[
            PromptChunk(source=c.source, score=round(c.score, 4), text=c.text)
            for c in assembled.prose
        ],
        counts=assembled.counts,
        assets=prompt_assets(assembled.model, system, intent_system, unselected=unselected),
        compose_system=compose_system,
        compose_user=compose_user,
        compose_assets=compose_assets(assembled.model),
    )
