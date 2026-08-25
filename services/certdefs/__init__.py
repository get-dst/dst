"""Certified definitions — one governed-metric page per file.

The encoding unit, taking the best of ktx's per-metric wiki pages and adding the
thing ktx structurally can't: a *verified value*. Each page is markdown with YAML
frontmatter and a prose body, and it does five jobs at once:

  - context for generation (summary + prose + the canonical SQL as an exemplar)
  - ground truth for grading (``verified_value`` — the "known assertion")
  - a certified answer (``sql``)
  - governance metadata (``owner``, ``source_of_truth``)
  - a correctness lever (``grain``, ``sources``)

``usage_mode`` is the context-selection knob the "too much context hurts" finding
demands: ``auto`` pages are always injected, ``search`` pages only on retrieval.
A lens's certified definitions are a directory of these pages — git-diffable,
PR-reviewable, CI-checkable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AliasChoices, ConfigDict, Field

from services.contracts.authoring import Authored, parse_authored
from services.contracts.semantic_model import StrList

UsageMode = Literal["auto", "search"]

# The same page format is filed in three places, and only ONE of them runs the
# per-question selector these keys steer: a directory named by
# ``model.certified_dir``. A page under ``semantic/definitions/`` or a lens's
# ``definitions/`` is compiled into the lens's Definition list and injected
# whenever the lens serves — there is no retrieval step to tune and no grading
# run to anchor — so these three parse and then do nothing there. Named, so the
# definition-page seam can say which directory would make them live.
CERTIFIED_DIR_ONLY = ("question", "usage_mode", "verified_value")

# Documented, never read — in any directory. Kept because a page is also a
# governance document people read, but a reader of this file should not have to
# guess which half the product acts on.
METADATA_ONLY = ("owner", "source_of_truth")


class VerifiedValue(Authored):
    """A documented known-good answer — the bridge from encoding to measurement."""

    model_config = ConfigDict(extra="ignore")
    value: float | str
    as_of: str | None = None


class CertifiedDefinition(Authored):
    # populate_by_name: the field NAMES stay valid kwargs for in-code
    # construction while the aliases below accept what authors actually type.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    metric: str = Field(
        # `term` is what the rest of the product calls this (Definition.term,
        # lens.yaml's definitions: list, every error message) — authors and
        # their agents write `term:` and got "field required: metric" for it,
        # a repeat finding across benchmark runs. Accept both; render stays
        # `metric:` so the plan's parse-then-re-render canonicalization keeps
        # aliased pages diff-free.
        validation_alias=AliasChoices("metric", "term"),
        description="the governed metric/term key, e.g. net_invoiced_revenue",
    )
    summary: str = Field(default="", description="one-line meaning, shown in listings")
    question: str | None = Field(
        default=None, description="the canonical question this metric answers"
    )
    owner: str = "lens-owner"
    grain: str | None = Field(default=None, description="e.g. one row per invoice month")
    sources: StrList = Field(default_factory=list, description="underlying tables (sl_refs)")
    source_of_truth: str | None = Field(
        default=None, description="where the number is officially reported"
    )
    usage_mode: UsageMode = Field(
        default="auto",
        description="'auto' pages are always injected as context; 'search' only on match",
    )
    verified_value: VerifiedValue | None = Field(
        default=None, description="documented known-good answer: {value, as_of}"
    )
    sql: str | None = Field(
        default=None,
        # Same class: the contract field is Definition.sql_expr, so that is what
        # gets typed into pages; silently ignoring it dropped the enforceable
        # expression and the term went unenforced with no error anywhere.
        validation_alias=AliasChoices("sql", "sql_expr"),
        description="enforceable SQL expression for this term (optional)",
    )
    about: str | None = Field(
        default=None,
        description="optional binding to the semantic object this term explains: "
        "'entity' or 'entity.member'",
    )
    status: Literal["active", "ambiguous"] = Field(
        default="active",
        description="'ambiguous' makes the system ask which meaning is intended "
        "instead of guessing; the prose below carries the curator's note",
    )
    possible_mappings: StrList = Field(
        default_factory=list,
        description="for ambiguous terms: each entry 'meaning - where it lives'",
    )
    aliases: StrList = Field(
        default_factory=list,
        description="business-English phrasings that trigger this term's rail — for "
        "ambiguous terms these are what make the clarification reachable for the "
        "questions users actually type",
    )
    body: str = Field(
        default="",
        description="not a frontmatter key — the markdown prose below it: the governed "
        "meaning, edge cases, and rejected readings; answers cite this",
    )


_PAGE_SHAPE = (
    "a definition page is YAML frontmatter + markdown prose:\n"
    "---\n"
    "term: repeat_customer            # the governed term (alias: metric)\n"
    "sql_expr: customers.number_of_orders > 1   # optional, enforceable (alias: sql)\n"
    "---\n\n"
    "A repeat customer has more than one order.   <- the prose IS the meaning"
)


def parse_definition_page(
    text: str, *, path: str | None = None, notes: list[str] | None = None
) -> CertifiedDefinition:
    """Parse one markdown-with-frontmatter page into a CertifiedDefinition.

    A page with no frontmatter (prose only) is the classic first attempt — it
    used to fail with a bare pydantic "field required: metric", which names the
    internal field but not the file shape. Show the shape instead.

    ``path`` switches on authoring strictness: an unknown frontmatter key is an
    error naming the file and the nearest valid key. Pass it from every seam
    that reads a file a human wrote; leave it off for internal round-trips.
    """
    # Every raise below carries the file when we know it: the callers now let a
    # ValueError through untouched (parse_authored already names the path), so a
    # message that forgot to say WHICH page would be exactly the unhelpful error
    # this seam exists to kill.
    where = f"{path}: " if path else ""
    if text.lstrip().startswith("---"):
        _, frontmatter, body = text.split("---", 2)
        meta = yaml.safe_load(frontmatter) or {}
    else:
        meta, body = {}, text
    if not isinstance(meta, dict):
        raise ValueError(f"{where}definition frontmatter must be a YAML mapping — {_PAGE_SHAPE}")
    if not (meta.get("metric") or meta.get("term")):
        raise ValueError(f"{where}definition page has no `term:` — {_PAGE_SHAPE}")
    if path is None:
        return CertifiedDefinition(**meta, body=body.strip())
    if "body" in meta:
        # The prose below the frontmatter IS the body; a `body:` key up here is
        # overwritten by it, which is the silent-drop this seam exists to end.
        raise ValueError(
            f"{where}`body` is not a frontmatter key — the markdown prose BELOW the "
            f"closing --- is the body. {_PAGE_SHAPE}"
        )
    return parse_authored(CertifiedDefinition, {**meta, "body": body.strip()}, path, notes=notes)


def render_definition_page(metric: CertifiedDefinition, *, minimal: bool = False) -> str:
    """CertifiedDefinition → the markdown-with-frontmatter on-disk form (round-trips).

    ``minimal`` drops every field left at its default, so a page that only carries
    a term (+ optional SQL) renders clean frontmatter instead of certified boilerplate
    (``owner``/``usage_mode``/empty ``summary``). Still certified-parseable — used for
    definition pages, which are minimal certified-definition pages that can later graduate into one.
    """
    meta = metric.model_dump(
        exclude={"body"}, exclude_none=True, exclude_defaults=minimal, mode="json"
    )
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{metric.body}\n"


def load_certified_defs(directory: Path) -> list[CertifiedDefinition]:
    return [
        parse_definition_page(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.md"))
    ]


def render_context(
    metrics: list[CertifiedDefinition],
    *,
    modes: tuple[UsageMode, ...] = ("auto",),
    include_sql: bool = True,
) -> str:
    """Render the selected certified definitions as generation context.

    ``modes`` is the selection knob — default injects only ``auto`` pages, so the
    prompt carries the org's standing judgment without the whole corpus."""
    blocks = []
    for m in metrics:
        if m.usage_mode not in modes:
            continue
        lines = [f"### {m.metric} — {m.summary}".rstrip(" —")]
        if m.grain:
            lines.append(f"grain: {m.grain} (aggregate at this grain; dedupe before summing)")
        if m.sources:
            lines.append(f"sources: {', '.join(m.sources)}")
        if m.body:
            lines.append(m.body)
        if include_sql and m.sql:
            lines.append(f"canonical SQL:\n{m.sql.strip()}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def ground_truth(metrics: list[CertifiedDefinition]) -> dict[str, float | str]:
    """The documented known-good values, keyed by metric — mode-2 grading anchors."""
    return {m.metric: m.verified_value.value for m in metrics if m.verified_value is not None}


_TOKEN = re.compile(r"[a-z]+")


def _relevance(question: str, metric: CertifiedDefinition) -> int:
    q = set(_TOKEN.findall(question.lower()))
    hay = " ".join([metric.metric, metric.summary, metric.question or "", " ".join(metric.sources)])
    return len(q & set(_TOKEN.findall(hay.lower())))


def select_context(
    metrics: list[CertifiedDefinition],
    question: str,
    *,
    k: int = 6,
    include_sql: bool = True,
) -> str:
    """Context for ONE question: every ``auto`` page plus the top-k ``search`` pages
    by relevance. This is what lets the SQL-exemplar format scale past a handful of
    metrics — you retrieve the relevant certified definitions instead of dumping the whole corpus
    (the "too much context hurts" finding, honoured)."""
    auto = [m for m in metrics if m.usage_mode == "auto"]
    search = sorted(
        (m for m in metrics if m.usage_mode == "search"),
        key=lambda m: _relevance(question, m),
        reverse=True,
    )
    chosen = auto + [m for m in search[:k] if _relevance(question, m) > 0]
    return render_context(chosen, modes=("auto", "search"), include_sql=include_sql)
