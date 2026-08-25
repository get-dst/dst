"""The Definition Drift Report as a designed artifact.

Renders ``DriftFinding``s three ways, all pure (no network, no DB):

- ``render_html`` — ONE self-contained HTML file: inline CSS, system font
  stacks, zero external requests, no JavaScript, no telemetry. The document a
  data lead forwards: header block, a one-paragraph verdict, findings as
  numbered exhibits with the diverging numbers large and the SQL small, and —
  the point of the artifact — each conflict closing with a pre-drafted lens
  definition (the bridge from "this is wrong" to "adopt the canon").
- ``render_terminal`` — the same report compact enough for a terminal.
- ``render_defs_yaml`` — the bridge as an import file: one
  ``{term, body, source: "dst-audit"}`` entry per conflict finding, the
  proposed canon naming the chosen variant and the competing variants kept as
  rejected readings.

Statements come straight from the customer's query history and are untrusted:
every interpolated string is HTML-escaped.

Canon choice is deliberate and deterministic: among variants whose observed
result is a single comparable number, the value most variants agree on wins
(ties: more total runs, then variant order); a finding with no comparable
numbers falls back to its most-run variant. The draft is a starting point for
the lens wizard, not a verdict — the body says so.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol, TypedDict

import yaml

from services.probe.drift import CanonMatch, DriftFinding, DriftVariant, _tokens


class DraftDefinition(TypedDict):
    """One bridge entry — the shape the lens wizard's import accepts."""

    term: str
    body: str
    source: str


_SOURCE = "dst-audit"
_SCOPE_LINE = "Read-only; statements and metadata only"
_TERM_WIDTH = 100


# ── observed-value helpers ────────────────────────────────────────────────────


def _fmt_number(value: object) -> str | None:
    """Format a numeric cell (40,818,677.59 / 771); None for non-numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return None


def _fmt_cell(value: object) -> str:
    if value is None:
        return "NULL"
    return _fmt_number(value) or str(value)


def _scalar(variant: DriftVariant) -> int | float | None:
    """The variant's single comparable number, when its result is exactly that."""
    rows = variant.observed_rows
    if rows and len(rows) == 1 and len(rows[0]) == 1:
        value = rows[0][0]
        if isinstance(value, int | float) and not isinstance(value, bool):
            return value
    return None


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def _observed_summary(variant: DriftVariant) -> str:
    """One-line account of what the variant produced (terminal + defs body)."""
    value = _scalar(variant)
    if value is not None:
        return _fmt_number(value) or str(value)
    if variant.observed_error:
        return f"failed: {variant.observed_error}"
    rows = variant.observed_rows
    if rows:
        first = " · ".join(_fmt_cell(cell) for cell in rows[0])
        return first if len(rows) == 1 else f"{len(rows)} rows, first: {first}"
    return "not executed"


def _spread(finding: DriftFinding) -> tuple[str, str | None] | None:
    """(absolute spread, pct of the lowest reading) across comparable numbers."""
    values = [float(s) for v in finding.variants if (s := _scalar(v)) is not None]
    if len(set(values)) < 2:
        return None
    low, high = min(values), max(values)
    delta = high - low
    absolute = f"{int(delta):,}" if delta.is_integer() else f"{delta:,.2f}"
    pct = f"{delta / abs(low) * 100:.1f}%" if low != 0 else None
    return absolute, pct


def _distinct_answers(finding: DriftFinding) -> list[str]:
    """The distinct comparable numbers, formatted, largest first."""
    seen: dict[str, float] = {}
    for variant in finding.variants:
        value = _scalar(variant)
        if value is not None:
            seen.setdefault(_fmt_number(value) or str(value), float(value))
    return sorted(seen, key=lambda k: -seen[k])


def _canon_index(finding: DriftFinding) -> int:
    """The variant the audit proposes as canon.

    Prefers the skill-aware choice stamped by ``services.probe.correctness`` (medallion
    tier, not a vote). Falls back to the deterministic agreement rule (the
    value most variants agree on, ties to most-run) for findings mined before correctness
    annotation existed, so the renderer never regresses on an un-annotated run.
    """
    if finding.canon_index is not None and 0 <= finding.canon_index < len(finding.variants):
        return finding.canon_index
    groups: dict[str, list[int]] = {}
    for i, variant in enumerate(finding.variants):
        value = _scalar(variant)
        if value is not None:
            groups.setdefault(_fmt_number(value) or str(value), []).append(i)
    if not groups:
        return 0  # no comparable numbers — most-run variant (they arrive sorted)

    def agreement(indices: list[int]) -> tuple[int, int, int]:
        runs = sum(finding.variants[i].run_count for i in indices)
        return (len(indices), runs, -min(indices))

    best = max(groups.values(), key=agreement)
    return max(best, key=lambda i: (finding.variants[i].run_count, -i))


def _variant_label(index: int) -> str:
    return chr(ord("a") + index) if index < 26 else str(index + 1)


def _n(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or singular + "s")
    return f"{count} {word}"


# ── the verdict (shared by HTML and terminal) ─────────────────────────────────


def _verdict_text(findings: list[DriftFinding], org_name: str) -> str:
    if not findings:
        return (
            f"No definition drift was found in {org_name}'s audited query history window. "
            "Either the definitions agree — or the window was too quiet to tell."
        )
    conflicts = sum(1 for f in findings if f.severity == "conflict")
    duplications = len(findings) - conflicts
    total_runs = sum(f.blast_radius for f in findings)
    top = max(findings, key=lambda f: f.blast_radius)

    sentences = [
        f"In the audited history window, {org_name}'s warehouse computed "
        f"“{top.metric_intent}” {_n(len(top.variants), 'different way')}."
    ]
    answers = _distinct_answers(top)
    if len(answers) >= 2:
        listed = ", ".join(answers[:-1]) + " and " + answers[-1]
        spread = _spread(top)
        apart = f" — {spread[1]} apart" if spread and spread[1] else ""
        sentences.append(
            f"{len(answers)} of the readings are directly comparable, and they "
            f"disagree: {listed}{apart}."
        )
    dup_part = f" and {_n(duplications, 'duplicated one')}" if duplications else ""
    sentences.append(
        f"Across the audit: {_n(conflicts, 'conflicting definition')}{dup_part}, "
        f"behind {total_runs} recorded runs."
    )
    sentences.append(
        "Until one reading is made canon, two reports can cite the same metric "
        "from the same warehouse and both be different."
    )
    return " ".join(sentences)


# ── the bridge: pre-drafted lens definitions ──────────────────────────────────


def draft_definition(finding: DriftFinding) -> DraftDefinition:
    """Pre-draft the lens definition that resolves one conflict finding."""
    canon_index = _canon_index(finding)
    canon = finding.variants[canon_index]
    canon_value = _scalar(canon)
    observed = f" — observed {_fmt_number(canon_value)}" if canon_value is not None else ""
    rejected = [
        f"({_variant_label(i)}) {variant.distinguishing} "
        f"(observed {_observed_summary(variant)}; {_n(variant.run_count, 'run')})"
        for i, variant in enumerate(finding.variants)
        if i != canon_index
    ]
    body = (
        f"Canonical reading ({_n(canon.run_count, 'run')} of {finding.blast_radius} "
        f"observed): {canon.distinguishing}{observed}. "
        f"Rejected readings: {'; '.join(rejected)}. "
        "Drafted by the dst audit from warehouse query history; review before certifying."
    )
    return DraftDefinition(term=finding.metric_intent, body=body, source=_SOURCE)


# ── declared-canon match ──────────────────────────────────────────────────────


class _DeclaredDef(Protocol):
    """A declared canonical definition — duck-typed over OrgStandard / Definition."""

    term: str
    sql_expr: str | None


def _alignment(
    finding: DriftFinding, declared: _DeclaredDef
) -> Literal["agrees", "contradicts", "undetermined"]:
    """Which observed reading the declared definition points at, vs the proposed canon.

    Deterministic, no LLM: rank the variants by how well their measure/source tokens
    overlap the declared SQL expression's tokens (the same tokenizer the miner clusters
    with, so structural vocabulary is stripped and only the business identity counts).
    The best-aligned variant is the one the org *declared*; if it is also the audit's
    proposed canon they agree, else they contradict. Undetermined when the standard has
    no SQL to align (body-only) or no variant overlaps it.
    """
    if not declared.sql_expr:
        return "undetermined"
    decl_tokens = _tokens(declared.sql_expr)
    if not decl_tokens:
        return "undetermined"
    scored = [
        (i, len(_tokens(v.distinguishing, *v.source_tables) & decl_tokens))
        for i, v in enumerate(finding.variants)
    ]
    best_i, best_score = max(scored, key=lambda t: (t[1], -t[0]))
    if best_score == 0:
        return "undetermined"
    return "agrees" if best_i == _canon_index(finding) else "contradicts"


def _match_declared(finding: DriftFinding, declared: Sequence[_DeclaredDef]) -> CanonMatch | None:
    """The declared standard whose term best matches this metric, with how the audit's
    canon aligns to it — None when nothing is declared for this metric intent."""
    metric_tokens = _tokens(finding.metric_intent)
    best: _DeclaredDef | None = None
    best_overlap = 0
    for d in declared:
        overlap = len(_tokens(d.term) & metric_tokens)
        if overlap > best_overlap:
            best, best_overlap = d, overlap
    if best is None:
        return None
    return CanonMatch(term=best.term, state=_alignment(finding, best))


def annotate_against_canon(
    findings: list[DriftFinding], declared: Sequence[_DeclaredDef]
) -> list[DriftFinding]:
    """Tag each finding with how the audit's canon relates to the org's *declared*
    definitions — turning "computed 4 ways" into "computed 4 ways, and the
    popular one isn't the one you declared". Pure: the caller supplies the declared
    standards (e.g. ``services.definitions.standards.list_standards``, compiled from dbt
    or authored). Findings with no declared canon are returned unchanged.
    """
    out: list[DriftFinding] = []
    for finding in findings:
        match = _match_declared(finding, declared)
        out.append(finding.model_copy(update={"canon_match": match}) if match else finding)
    return out


def render_defs_yaml(findings: list[DriftFinding]) -> str:
    """The import file: one drafted definition per **conflict** finding."""
    defs = [draft_definition(f) for f in findings if f.severity == "conflict"]
    text: str = yaml.safe_dump(
        [dict(d) for d in defs], sort_keys=False, allow_unicode=True, width=88
    )
    return text


# ── terminal rendering ────────────────────────────────────────────────────────


def render_terminal(findings: list[DriftFinding]) -> str:
    """The report compacted for a terminal — plain text, ≤100 columns."""
    lines = ["DST · DEFINITION DRIFT AUDIT"]
    if not findings:
        lines += ["", "No definition drift found in the audited window."]
    else:
        conflicts = sum(1 for f in findings if f.severity == "conflict")
        duplications = len(findings) - conflicts
        total_runs = sum(f.blast_radius for f in findings)
        lines.append(
            f"{_n(len(findings), 'finding')}: {_n(conflicts, 'conflict')}, "
            f"{_n(duplications, 'duplication')} · blast radius {total_runs} runs"
        )
    for number, finding in enumerate(findings, 1):
        lines.append("")
        head = f"[{number}] {finding.severity.upper():<12}{finding.metric_intent}"
        tail = f"{_n(len(finding.variants), 'definition')} · {finding.blast_radius} runs"
        pad = max(_TERM_WIDTH - len(head) - len(tail), 2)
        lines.append(_clip(head + " " * pad + tail, _TERM_WIDTH))
        for variant in finding.variants:
            value = _observed_summary(variant)
            value_col = f"{value:>18}" if _scalar(variant) is not None else value
            tier = f" [{variant.tier}]" if variant.tier and variant.tier != "unknown" else ""
            lines.append(
                _clip(
                    f"    {value_col}  · {_n(variant.run_count, 'run'):<7}· "
                    f"{variant.distinguishing}{tier}",
                    _TERM_WIDTH,
                )
            )
        spread = _spread(finding)
        if spread is not None:
            absolute, pct = spread
            of_low = f" ({pct} of the lowest reading)" if pct else ""
            lines.append(f"    spread across comparable answers: {absolute}{of_low}")
        if finding.severity == "conflict":
            canon_index = _canon_index(finding)
            canon = finding.variants[canon_index]
            lines.append(
                _clip(
                    f"    draft definition → canon ({_variant_label(canon_index)}): "
                    f"{canon.distinguishing}",
                    _TERM_WIDTH,
                )
            )
            if finding.canon_rationale:
                lines.append(_clip(f"    why: {finding.canon_rationale}", _TERM_WIDTH))
            if finding.canon_match is not None:
                cm = finding.canon_match
                verb = {"agrees": "matches", "contradicts": "contradicts", "undetermined": "vs"}[
                    cm.state
                ]
                lines.append(
                    _clip(f'    declared standard "{cm.term}": canon {verb} it', _TERM_WIDTH)
                )
    lines += ["", "read-only · statements and metadata only · generated by the dst audit"]
    return "\n".join(lines)


# ── HTML rendering ────────────────────────────────────────────────────────────

# Amber + paper + mono — the house identity in standalone form (cf. the deck and
# landing pages). A document, not an app: ruled masthead, numbered exhibits,
# sharp edges, system font stacks only. Print lands on A4.
_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #FAF9F6; --surface: #FFFFFF; --surface-2: #F5F4F0;
  --border: #E5E2DB; --border-strong: #D6D2C8;
  --ink: #1A1917; --muted: #6B6964; --faint: #9A968E;
  --accent: #C2410C; --amber: #D97706; --amber-deep: #92400E;
  --amber-bg: #FEF9EE; --amber-light: #FEF3C7; --amber-line: #FCD34D;
  --red: #B91C1C; --red-bg: #FEF2F2; --red-line: #FCA5A5;
  --sans: ui-sans-serif, system-ui, -apple-system, 'Segoe UI', 'Helvetica Neue', sans-serif;
  --mono: 'SF Mono', ui-monospace, 'Cascadia Code', 'JetBrains Mono', 'Courier New', monospace;
}
html { background: var(--bg); }
body {
  font-family: var(--sans); color: var(--ink); background: var(--bg);
  font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased;
  max-width: 820px; margin: 0 auto; padding: 56px 48px 72px;
}
.doc-rule { border-top: 3px solid var(--ink); border-bottom: 1px solid var(--ink);
  height: 7px; margin-bottom: 30px; }
.kicker { display: inline-flex; align-items: center; gap: 9px; font-family: var(--mono);
  font-size: 11px; font-weight: 600; letter-spacing: 1.6px; text-transform: uppercase;
  color: var(--accent); }
.kicker::before { content: ''; width: 22px; height: 1px; background: var(--accent); }
h1.org { font-size: 38px; font-weight: 750; letter-spacing: -1.6px; line-height: 1.05;
  margin: 10px 0 28px; }
dl.meta { display: grid; grid-template-columns: repeat(4, auto); gap: 8px 40px;
  justify-content: start; border-top: 1px solid var(--border-strong);
  border-bottom: 1px solid var(--border-strong); padding: 13px 0; }
dl.meta dt { font-family: var(--mono); font-size: 9.5px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 1.3px; color: var(--faint); margin-bottom: 3px; }
dl.meta dd { font-size: 13px; }
.verdict { margin: 36px 0 8px; border-left: 3px solid var(--amber); padding: 4px 0 4px 20px; }
.verdict p { font-size: 16.5px; line-height: 1.65; max-width: 62ch; }
h3.sec { font-family: var(--mono); font-size: 10.5px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 1.6px; color: var(--faint);
  border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-top: 40px; }
table.index { width: 100%; border-collapse: collapse; font-size: 12.5px; }
table.index th { font-family: var(--mono); font-size: 9.5px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 1px; color: var(--faint); text-align: left;
  padding: 9px 14px 7px 0; border-bottom: 1px solid var(--border); }
table.index td { padding: 8px 14px 8px 0; border-bottom: 1px solid var(--border);
  vertical-align: baseline; }
table.index td.no, table.index td.num { font-family: var(--mono);
  font-variant-numeric: tabular-nums; }
table.index td.num { color: var(--muted); }
.tag { display: inline-block; font-family: var(--mono); font-size: 9.5px; font-weight: 700;
  letter-spacing: 1.2px; text-transform: uppercase; padding: 3px 9px; border: 1px solid; }
.tag.conflict { color: var(--red); border-color: var(--red-line); background: var(--red-bg); }
.tag.duplication { color: var(--muted); border-color: var(--border-strong);
  background: var(--surface-2); }
article.exhibit { margin-top: 48px; border-top: 2px solid var(--ink); padding-top: 16px; }
.ex-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.ex-no { font-family: var(--mono); font-size: 10.5px; font-weight: 700;
  letter-spacing: 1.6px; text-transform: uppercase; color: var(--accent); }
h2.ex-title { font-size: 23px; font-weight: 730; letter-spacing: -0.6px; margin-top: 3px; }
.ex-meta { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-top: 7px; }
.spread { margin-top: 14px; background: var(--amber-light); border: 1px solid var(--amber-line);
  padding: 10px 14px; font-size: 13px; color: var(--amber-deep); }
.spread b { font-family: var(--mono); font-variant-numeric: tabular-nums; font-weight: 600; }
ol.variants { list-style: none; margin-top: 14px; }
li.variant { display: grid; grid-template-columns: 215px 1fr; gap: 2px 24px;
  padding: 15px 0 14px; border-top: 1px solid var(--border); }
.v-label { grid-column: 1 / -1; font-family: var(--mono); font-size: 10px;
  color: var(--faint); margin-bottom: 4px; }
.v-num { font-family: var(--mono); font-size: 24px; font-weight: 600;
  letter-spacing: -0.5px; line-height: 1.2; font-variant-numeric: tabular-nums;
  word-break: break-word; }
.v-none { font-family: var(--mono); font-size: 11.5px; color: var(--faint); padding-top: 6px; }
.v-err { font-family: var(--mono); font-size: 11px; color: var(--red);
  line-height: 1.5; word-break: break-word; padding-top: 4px; }
table.obs { border-collapse: collapse; font-family: var(--mono); font-size: 11px; }
table.obs th { text-align: left; font-size: 8.5px; letter-spacing: 0.8px;
  text-transform: uppercase; color: var(--faint); padding: 1px 10px 2px 0; }
table.obs td { padding: 1px 10px 1px 0; font-variant-numeric: tabular-nums; }
.canon-mark { display: inline-block; margin-top: 7px; font-family: var(--mono);
  font-size: 9px; font-weight: 700; letter-spacing: 1.1px; text-transform: uppercase;
  color: var(--accent); border: 1px solid var(--amber-line); background: var(--amber-bg);
  padding: 2px 7px; }
.tier { display: inline-block; margin-top: 6px; margin-left: 6px; font-family: var(--mono);
  font-size: 9px; font-weight: 600; letter-spacing: 0.6px; text-transform: uppercase;
  padding: 2px 7px; border: 1px solid; }
.tier-gold { color: var(--amber-deep); border-color: var(--amber-line);
  background: var(--amber-bg); }
.tier-silver { color: var(--muted); border-color: var(--border-strong);
  background: var(--surface-2); }
.tier-bronze { color: var(--red); border-color: var(--red-line); background: var(--red-bg); }
.v-dist { font-family: var(--mono); font-size: 12px; line-height: 1.55; word-break: break-word; }
.v-where { font-family: var(--mono); font-size: 10.5px; color: var(--faint); margin-top: 5px; }
pre.sql { margin-top: 9px; font-family: var(--mono); font-size: 10.5px; line-height: 1.6;
  color: var(--muted); background: var(--surface-2); border-left: 2px solid var(--border-strong);
  padding: 10px 13px; white-space: pre-wrap; word-break: break-word; }
aside.bridge { margin-top: 6px; border: 1px solid var(--amber-line);
  background: var(--amber-bg); padding: 16px 18px; }
.bridge-k { font-family: var(--mono); font-size: 9.5px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 1.5px; color: var(--accent); }
.bridge-term { font-family: var(--mono); font-size: 14px; font-weight: 700; margin-top: 9px; }
.bridge-why { font-size: 12.5px; color: var(--ink); line-height: 1.6; margin-top: 7px;
  max-width: 78ch; font-weight: 500; }
.bridge-body { font-size: 12.5px; color: var(--muted); line-height: 1.6; margin-top: 6px;
  max-width: 78ch; }
.bridge-note { font-family: var(--mono); font-size: 10px; color: var(--faint); margin-top: 11px; }
aside.consolidate { margin-top: 6px; border: 1px solid var(--border);
  background: var(--surface-2); padding: 12px 16px; font-size: 12.5px; color: var(--muted); }
footer { margin-top: 60px; border-top: 1px solid var(--border-strong); padding-top: 13px;
  display: flex; justify-content: space-between; gap: 24px; font-family: var(--mono);
  font-size: 10.5px; color: var(--faint); }
@page { size: A4; margin: 15mm; }
@media print {
  html, body { background: #fff; }
  body { max-width: none; padding: 0; }
  li.variant, aside.bridge, aside.consolidate, .spread, .verdict, dl.meta { break-inside: avoid; }
  .ex-head, h3.sec { break-after: avoid; }
  article.exhibit { margin-top: 36px; }
}
"""


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _html_observed(variant: DriftVariant, *, is_canon: bool) -> str:
    """The left column of a variant row: the number, large — or what stands in."""
    parts: list[str] = []
    value = _scalar(variant)
    if value is not None:
        parts.append(f'<div class="v-num">{_esc(_fmt_number(value) or str(value))}</div>')
    elif variant.observed_error:
        parts.append(f'<div class="v-err">failed: {_esc(variant.observed_error)}</div>')
    elif variant.observed_rows:
        head = ""
        if variant.observed_columns:
            cells = "".join(f"<th>{_esc(c)}</th>" for c in variant.observed_columns)
            head = f"<tr>{cells}</tr>"
        body = "".join(
            "<tr>" + "".join(f"<td>{_esc(_fmt_cell(cell))}</td>" for cell in row) + "</tr>"
            for row in variant.observed_rows[:5]
        )
        more = (
            f'<div class="v-none">+{len(variant.observed_rows) - 5} more rows</div>'
            if len(variant.observed_rows) > 5
            else ""
        )
        parts.append(f'<table class="obs">{head}{body}</table>{more}')
    else:
        parts.append('<div class="v-none">not executed</div>')
    if is_canon:
        parts.append('<span class="canon-mark">Proposed canon</span>')
    if variant.tier and variant.tier != "unknown":
        parts.append(
            f'<span class="tier tier-{_esc(variant.tier)}">{_esc(_TIER_LABEL[variant.tier])}</span>'
        )
    return "".join(parts)


# Human label per medallion tier, for the source chip under each reading.
_TIER_LABEL = {
    "gold": "gold · business-ready",
    "silver": "silver · cleaned",
    "bronze": "bronze · raw",
    "unknown": "",
}


def _html_variant(variant: DriftVariant, label: str, *, is_canon: bool) -> str:
    where_bits = [_n(variant.run_count, "run")]
    if variant.principals:
        where_bits.append("by " + ", ".join(variant.principals))
    if variant.source_tools:
        where_bits.append("via " + ", ".join(variant.source_tools))
    return (
        '<li class="variant">'
        f'<div class="v-label">({_esc(label)})</div>'
        f"<div>{_html_observed(variant, is_canon=is_canon)}</div>"
        "<div>"
        f'<div class="v-dist">{_esc(variant.distinguishing)}</div>'
        f'<div class="v-where">{_esc(" · ".join(where_bits))}</div>'
        f'<pre class="sql">{_esc(variant.statement)}</pre>'
        "</div></li>"
    )


def _html_exhibit(finding: DriftFinding, number: int) -> str:
    principals = sorted({p for v in finding.variants for p in v.principals})
    tools = sorted({t for v in finding.variants for t in v.source_tools})
    meta_bits = [
        _n(len(finding.variants), "definition"),
        f"blast radius {finding.blast_radius} runs",
    ]
    if principals:
        meta_bits.append("run by " + ", ".join(principals[:4]))
    if tools:
        meta_bits.append("via " + ", ".join(tools[:3]))

    spread_html = ""
    spread = _spread(finding)
    if spread is not None:
        absolute, pct = spread
        of_low = f" — <b>{_esc(pct)}</b> of the lowest reading" if pct else ""
        spread_html = (
            f'<div class="spread">Comparable answers are <b>{_esc(absolute)}</b>'
            f" apart{of_low}.</div>"
        )

    is_conflict = finding.severity == "conflict"
    canon_index = _canon_index(finding) if is_conflict else -1
    variants = "".join(
        _html_variant(v, _variant_label(i), is_canon=(i == canon_index))
        for i, v in enumerate(finding.variants)
    )

    if is_conflict:
        draft = draft_definition(finding)
        why = (
            f'<p class="bridge-why">{_esc(finding.canon_rationale)}</p>'
            if finding.canon_rationale
            else ""
        )
        canon_match_html = ""
        if finding.canon_match is not None:
            cm = finding.canon_match
            msg = {
                "agrees": f"Matches your declared standard “{cm.term}”.",
                "contradicts": (
                    f"Contradicts your declared standard “{cm.term}” — the popular reading "
                    "isn't the one you declared."
                ),
                "undetermined": f"A declared standard “{cm.term}” exists; align it by hand.",
            }[cm.state]
            canon_match_html = f'<div class="bridge-note">Declared canon · {_esc(msg)}</div>'
        closing = (
            '<aside class="bridge">'
            '<div class="bridge-k">Drafted lens definition</div>'
            f'<div class="bridge-term">{_esc(draft["term"])}</div>'
            f"{why}"
            f'<p class="bridge-body">{_esc(draft["body"])}</p>'
            f"{canon_match_html}"
            '<div class="bridge-note">included in the audit-defs import file · '
            "source: dst-audit</div>"
            "</aside>"
        )
    else:
        closing = (
            '<aside class="consolidate">Equivalent definitions in different SQL — '
            "consolidate into one certified statement so the duplication stops "
            "accruing cost and risk.</aside>"
        )

    return (
        '<article class="exhibit">'
        '<header class="ex-head"><div>'
        f'<div class="ex-no">Exhibit {number:02d}</div>'
        f'<h2 class="ex-title">{_esc(finding.metric_intent)}</h2>'
        f'<div class="ex-meta">{_esc(" · ".join(meta_bits))}</div>'
        f'</div><span class="tag {finding.severity}">{_esc(finding.severity)}</span></header>'
        f"{spread_html}"
        f'<ol class="variants">{variants}</ol>'
        f"{closing}"
        "</article>"
    )


def render_html(
    findings: list[DriftFinding],
    *,
    org_name: str,
    warehouse_label: str,
    generated_at: datetime,
) -> str:
    """The Definition Drift Report as one self-contained HTML document."""
    conflicts = sum(1 for f in findings if f.severity == "conflict")
    duplications = len(findings) - conflicts
    stamp = generated_at.strftime("%d %B %Y, %H:%M").lstrip("0")
    if generated_at.tzinfo is not None:
        stamp += f" {generated_at.tzname()}"

    index_rows = []
    for number, finding in enumerate(findings, 1):
        spread = _spread(finding)
        divergence = f"{spread[0]}{f' ({spread[1]})' if spread[1] else ''}" if spread else "—"
        index_rows.append(
            "<tr>"
            f'<td class="no">{number:02d}</td>'
            f"<td>{_esc(finding.metric_intent)}</td>"
            f'<td><span class="tag {finding.severity}">{_esc(finding.severity)}</span></td>'
            f'<td class="num">{len(finding.variants)}</td>'
            f'<td class="num">{finding.blast_radius}</td>'
            f'<td class="num">{_esc(divergence)}</td>'
            "</tr>"
        )
    index_html = (
        '<h3 class="sec">Exhibits</h3><table class="index">'
        "<thead><tr><th>No.</th><th>Metric intent</th><th>Severity</th>"
        "<th>Definitions</th><th>Runs</th><th>Divergence</th></tr></thead>"
        f"<tbody>{''.join(index_rows)}</tbody></table>"
        if findings
        else ""
    )

    exhibits = "".join(_html_exhibit(f, n) for n, f in enumerate(findings, 1))

    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>Definition Drift Report — {_esc(org_name)}</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n"
        '<header><div class="doc-rule"></div>'
        '<span class="kicker">dst · Definition Drift Audit</span>'
        f'<h1 class="org">{_esc(org_name)}</h1>'
        '<dl class="meta">'
        f"<div><dt>Warehouse</dt><dd>{_esc(warehouse_label)}</dd></div>"
        f"<div><dt>Generated</dt><dd>{_esc(stamp)}</dd></div>"
        f"<div><dt>Scope</dt><dd>{_esc(_SCOPE_LINE)}</dd></div>"
        f"<div><dt>Findings</dt><dd>{_n(conflicts, 'conflict')} · "
        f"{_n(duplications, 'duplication')}</dd></div>"
        "</dl></header>\n"
        f'<section class="verdict"><p>{_esc(_verdict_text(findings, org_name))}</p></section>\n'
        f"{index_html}\n<main>{exhibits}</main>\n"
        "<footer><span>Generated by the dst audit</span>"
        f"<span>{_esc(_SCOPE_LINE.lower())}</span>"
        "<span>self-contained · no scripts · no network requests · no telemetry</span>"
        "</footer>\n</body>\n</html>\n"
    )
