"""What changed in the warehouse under an authored semantic layer — file-first.

The layer's worst failure mode is not a wrong answer, it is a RIGHT answer that
stopped being right. `discount` gets authored as ``list_price - unit_price``
because no discount column exists; later the warehouse gains
``ops.orders.discount_amount``, the company's own authoritative figure; and the
layer goes on serving the day-1 derivation, confidently, on every answer, until
somebody notices by hand. Every surface that could catch that is off the users'
path: `introspect` prints a snapshot and never a diff, `dst test` compares
certified SQL against generation with BOTH reading today's warehouse (so a
consistent wrongness passes 1/1), and profile drift was reachable only over REST
from a dashboard nobody opened.

So the join this module computes is the product, not the column list: a schema
delta CROSSED WITH the semantic assets that read that table. "`ops.orders` gained
`discount_amount`; definition `discount` derives that quantity from
`list_price - unit_price`" is a sentence somebody acts on. "ops.orders gained a
column" is one nobody reads twice.

FILE-FIRST. The baseline is a JSON file in the project (`profiles/<conn>.json`),
not a row in the control plane's `table_profile`. Three reasons: it works before
the first apply and without a server, exactly like `introspect --profile`; it is
committed next to the layer it describes, so a teammate's `plan` warns too and
`git log` says when the warehouse moved under the layer; and the stored baseline
is one generation deep and destroyed by any pass that upserts, which is fine for
a dashboard poll and useless for "since I authored this".
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from services.contracts.profile import TableProfile
from services.contracts.semantic_model import Definition
from services.contracts.shared_semantic import SharedEntity
from services.lenses.profile_drift import DriftKind, ProfileDrift, diff_profiles

# Committed, not hidden: the point is that a teammate who pulls the project gets
# the warning too. `init` writes `.env` into .gitignore and nothing else, so this
# lands in version control by default, which is the intent.
BASELINE_DIR = "profiles"

# Schema deltas only, and the exclusions are deliberate. `enum_value_added` is
# the one literal-bearing kind (a catalog pass collects no literals at all);
# `stale_table` and `partition_changed` are real signals
# but a different finding — a table that stopped updating, or got cheaper to
# scan, is not a layer that stopped matching its warehouse. They stay on the
# REST surface, which reports every kind.
SCHEMA_KINDS: frozenset[str] = frozenset(
    {"column_added", "column_dropped", "column_retyped", "table_added", "table_dropped"}
)


class LayerRef(BaseModel):
    """One semantic asset that reads the table a delta landed on."""

    kind: Literal["entity", "definition", "certified"]
    name: str  # entity name / definition term / certified question
    path: str  # semantic/definitions/discounts.md
    why: str  # the sentence fragment that says HOW it reads it
    sql_expr: str | None = None  # the derivation a new column may supersede


class Finding(BaseModel):
    table: str
    kind: DriftKind
    detail: str
    refs: list[LayerRef] = Field(default_factory=list)

    @property
    def referenced(self) -> bool:
        """Whether the semantic layer reads this table — the signal/noise split."""
        return bool(self.refs)


class Baseline(BaseModel):
    connection: str
    recorded_at: datetime
    tables: list[TableProfile] = Field(default_factory=list)


# What a baseline SERIALIZES — a whitelist, and both halves of that word matter.
#
# Stable: a profile carries `profiled_at`, `row_count` and freshness timestamps
# that move every single pass. Writing them into a committed file would put a
# hundred-line diff under every `--accept` and bury the two lines that are the
# schema actually changing, which is the entire reason the baseline lives in git.
#
# A whitelist, not a blacklist, because this file is committed: a field added to
# `ColumnProfile` later is excluded until somebody decides otherwise. The value
# fields (`top_values`, `min`, `max`) are the ones that decision is about, and
# leaving them out here means no sampled literal reaches the repository through
# the baseline at all — the probe artifact is the file that carries those, on
# purpose and by itself.
_BASELINE_INCLUDE: Any = {  # pydantic's IncEx, which is not exported for annotating
    "connection": True,
    "recorded_at": True,
    "tables": {
        "__all__": {
            "connection": True,
            "table": True,
            "columns": {"__all__": {"name": True, "type": True, "nullable": True}},
        }
    },
}


# ── the baseline file ────────────────────────────────────────────────────────


def connection_slug(connection: str) -> str:
    """A connection name as a filename stem — shared with the probe artifact."""
    return re.sub(r"[^a-z0-9]+", "-", connection.lower()).strip("-") or "connection"


def baseline_path(root: Path, connection: str) -> Path:
    return root / BASELINE_DIR / f"{connection_slug(connection)}.json"


def read_baseline(root: Path, connection: str) -> Baseline | None:
    """The recorded warehouse for *connection*, or None when there is none yet.

    A file that does not parse reads as no baseline: drift's job is to report a
    change, and a corrupt baseline must cost a re-record, never the command."""
    path = baseline_path(root, connection)
    if not path.exists():
        return None
    try:
        baseline = Baseline.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    # Two connection names can slug to one filename; the name inside decides.
    return baseline if baseline.connection == connection else None


def baseline_connections(root: Path) -> list[str]:
    """Connections this project has recorded a warehouse for.

    `plan`'s gate: no baseline means no check, no warehouse round-trip and no
    line — a project that never ran `dst drift` pays nothing for this."""
    out: list[str] = []
    for path in sorted((root / BASELINE_DIR).glob("*.json")):
        try:
            out.append(Baseline.model_validate_json(path.read_text(encoding="utf-8")).connection)
        except (ValueError, OSError):
            continue
    return out


def write_baseline(root: Path, connection: str, profiles: list[TableProfile]) -> Path:
    path = baseline_path(root, connection)
    path.parent.mkdir(parents=True, exist_ok=True)
    baseline = Baseline(
        connection=connection,
        recorded_at=datetime.now(UTC),
        # Sorted so re-recording an unchanged warehouse is a one-line git diff.
        tables=sorted(profiles, key=lambda p: p.table),
    )
    path.write_text(
        baseline.model_dump_json(indent=2, include=_BASELINE_INCLUDE) + "\n", encoding="utf-8"
    )
    return path


# ── the diff ─────────────────────────────────────────────────────────────────


def baseline_drift(previous: list[TableProfile], current: list[TableProfile]) -> list[ProfileDrift]:
    """Named schema differences between a recorded warehouse and the live one.

    `diff_profiles` handles a table present on both sides; whole tables appearing
    and disappearing are this function's own two kinds, because a table an entity
    reads going missing is the same class of event as a column going missing."""
    prev = {p.table: p for p in previous}
    cur = {p.table: p for p in current}
    out: list[ProfileDrift] = []
    for name in sorted(cur.keys() - prev.keys()):
        out.append(ProfileDrift(table=name, kind="table_added", detail=name))
    for name in sorted(prev.keys() - cur.keys()):
        out.append(ProfileDrift(table=name, kind="table_dropped", detail=name))
    for name in sorted(prev.keys() & cur.keys()):
        out.extend(diff_profiles(prev[name], cur[name]))
    return [d for d in out if d.kind in SCHEMA_KINDS]


def schema_delta_count(previous: list[TableProfile], current: list[TableProfile]) -> int:
    """How many schema deltas separate the two — `plan`'s whole question.

    The cheap half of this module: a count, no semantic layer loaded, no
    formatting, nothing that grows with the size of the project."""
    return len(baseline_drift(previous, current))


# ── the cross-reference: the actual feature ──────────────────────────────────


def _bare(table: str) -> str:
    return table.rsplit(".", 1)[-1].lower()


def same_table(candidate: str, table: str) -> bool:
    """Whether an authored table reference names *table*.

    Matches unqualified too (`orders` ~ `ops.orders`): authors qualify
    inconsistently, and over-matching costs one line in a warning while
    under-matching costs the entire finding. Shared with the probe artifact's
    entity mapping."""
    return candidate.lower() == table.lower() or _bare(candidate) == _bare(table)


def _mentions(expr: str, token: str) -> bool:
    """Whether *expr* names *token* as a word — `total` must not match `subtotal`."""
    return re.search(rf"(?<![\w.]){re.escape(token)}(?![\w])", expr, re.IGNORECASE) is not None


def _mentions_qualified(sql: str, token: str) -> bool:
    """`_mentions` for real SQL, where names arrive dot-qualified: `ops.orders`
    and `o.unit_price` must count as mentions of `orders` / `unit_price` —
    the authored-expression matcher above deliberately rejects a leading dot."""
    return re.search(rf"(?<!\w){re.escape(token)}(?!\w)", sql, re.IGNORECASE) is not None


class CertifiedRef(BaseModel):
    """One certified answer's declared reference surface: its approved SQL.

    The SQL is the binding — a human approved that exact text against the
    warehouse as it stood, so a column it names going away breaks the approval
    itself, not just a generated answer that could regenerate around it."""

    question: str
    sql: str
    path: str  # lenses/<lens>/certified_answers.yaml


def parse_certified(files: dict[str, str]) -> list[CertifiedRef]:
    """`lenses/*/certified_answers.yaml` → the certified reference surface.

    Same degradation rule as `parse_layer`: a file that does not parse is
    skipped, not raised — apply already rejects malformed certified files, and
    drift refusing to run because of one is drift not running."""
    import yaml

    out: list[CertifiedRef] = []
    for path, content in sorted(files.items()):
        if not (path.startswith("lenses/") and path.endswith("/certified_answers.yaml")):
            continue
        try:
            entries = yaml.safe_load(content)
        except yaml.YAMLError:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            question, sql = entry.get("question"), entry.get("sql")
            if isinstance(question, str) and isinstance(sql, str) and question and sql:
                out.append(CertifiedRef(question=question, sql=sql, path=path))
    return out


def parse_layer(files: dict[str, str]) -> tuple[dict[str, SharedEntity], dict[str, Definition]]:
    """`semantic/**` → (entities by path, definitions by path).

    Keyed by PATH, unlike `semantic.files.parse_semantic_files` (by name), because
    a finding has to name the file you open to fix it — and the term does not give
    it: the `discount` of the example above lives in `discounts.md`. A file that does not
    parse is skipped, not raised: `plan` already reports malformed assets, and
    drift refusing to run because of one is drift not running."""
    from services.semantic.files import parse_semantic_file

    entities: dict[str, SharedEntity] = {}
    definitions: dict[str, Definition] = {}
    for path, content in sorted(files.items()):
        try:
            asset = parse_semantic_file(path, content)
        except ValueError:
            continue
        if isinstance(asset, SharedEntity):
            entities[path] = asset
        elif isinstance(asset, Definition):
            definitions[path] = asset
    return entities, definitions


def _entity_refs(table: str, entities: dict[str, SharedEntity]) -> list[LayerRef]:
    return [
        LayerRef(kind="entity", name=e.name, path=path, why="reads this table")
        for path, e in sorted(entities.items())
        if same_table(e.source.table, table)
    ]


def _definition_refs(
    table: str, column: str | None, entities: dict[str, SharedEntity], defs: dict[str, Definition]
) -> list[LayerRef]:
    """Definitions bound to *table*, by any of the four bindings an author can write."""
    on_table = {e.name.lower() for e in entities.values() if same_table(e.source.table, table)}
    out: list[LayerRef] = []
    for path, d in sorted(defs.items()):
        why: str | None = None
        expr = d.sql_expr or ""
        # Strongest first: the definition's own SQL names the column that moved.
        if column and expr and _mentions(expr, column):
            why = f"names `{column}` in its sql_expr"
        elif any(same_table(s, table) for s in d.sources):
            why = "lists this table in `sources`"
        elif d.about and d.about.split(".")[0].lower() in on_table:
            why = f"is about `{d.about}`, which reads this table"
        elif expr and _mentions(expr, _bare(table)):
            why = "names this table in its sql_expr"
        if why is not None:
            out.append(
                LayerRef(kind="definition", name=d.term, path=path, why=why, sql_expr=d.sql_expr)
            )
    return out


def _fold_name(name: str) -> str:
    return re.sub(r"[\s_-]+", "_", name.strip().lower())


def _supersession_refs(
    table: str, column: str | None, defs: dict[str, Definition]
) -> list[LayerRef]:
    """Definitions a NEW column may supersede — the *replaces-this* binding.

    Every other rule here is a *reads-this* relationship, and that family has a
    structural blind spot: **a derivation exists precisely because the real column
    did not**, so the asset a new column supersedes systematically does not read
    the table the column landed on. Concretely: `ops.orders` gains
    `discount_amount`, while the `discount_amount` definition reads order_items,
    products, payments and refunds — every reads-this rule correctly finds
    nothing, drift correctly reports "nothing in semantic/ reads this table",
    and the metric silently goes on serving a derivation the warehouse has just
    made obsolete. Someone can run drift, see the change, and never connect it
    to the definition it supersedes.

    The binding that survives the blind spot is the NAME: the author called the
    definition `discount_amount` because that is the quantity it stands in for,
    and the warehouse called the column `discount_amount` for the same reason.
    An exact fold-match between a definition's term and a new column's name is a
    supersession candidate, whatever tables it reads."""
    if not column:
        return []
    col = _fold_name(column)
    out: list[LayerRef] = []
    for path, d in sorted(defs.items()):
        if _fold_name(d.term) != col:
            continue
        reads = ", ".join(d.sources) if d.sources else "other tables"
        out.append(
            LayerRef(
                kind="definition",
                name=d.term,
                path=path,
                why=(
                    f"is NAMED for this new column but reads {reads} — the warehouse "
                    f"now publishes what this definition derives; review whether it "
                    f"is superseded"
                ),
                sql_expr=d.sql_expr,
            )
        )
    return out


def _certified_refs(
    table: str, column: str | None, kind: DriftKind, certified: list[CertifiedRef]
) -> list[LayerRef]:
    """Certified answers whose approved SQL reads what a DESTRUCTIVE delta hit.

    Only the destructive kinds: a gained column cannot invalidate an approval.
    The blast-radius rule requires the SQL to name BOTH the bare table and (for
    column deltas) the column as words — `amount` alone matches half a corpus,
    and a false 'breaking' here turns exit 1 into noise a gate stops trusting."""
    if kind not in ("column_dropped", "column_retyped", "table_dropped"):
        return []
    out: list[LayerRef] = []
    for ref in certified:
        if not _mentions_qualified(ref.sql, _bare(table)):
            continue
        if column is not None and not _mentions_qualified(ref.sql, column):
            continue
        what = f"`{column}`" if column else f"table `{table}`"
        out.append(
            LayerRef(
                kind="certified",
                name=ref.question,
                path=ref.path,
                why=f"its approved SQL names {what}",
            )
        )
    return out


def cross_reference(
    drift: list[ProfileDrift],
    entities: dict[str, SharedEntity],
    defs: dict[str, Definition],
    certified: list[CertifiedRef] | None = None,
) -> list[Finding]:
    """Each delta with the semantic assets that read the table it landed on.

    Findings the layer actually reads sort first — an unreferenced table's new
    column is true and uninteresting, and burying the one line that matters under
    forty of those is how the day-5 column got missed in `introspect` output."""

    def _refs(d: ProfileDrift) -> list[LayerRef]:
        refs = _entity_refs(d.table, entities) + _definition_refs(
            d.table, _column_of(d), entities, defs
        )
        refs += _certified_refs(d.table, _column_of(d), d.kind, certified or [])
        if d.kind == "column_added":
            named = {(r.kind, r.name) for r in refs}
            refs += [
                r
                for r in _supersession_refs(d.table, _column_of(d), defs)
                if (r.kind, r.name) not in named  # already bound by a reads-this rule
            ]
        return refs

    findings = [Finding(table=d.table, kind=d.kind, detail=d.detail, refs=_refs(d)) for d in drift]
    return sorted(
        findings,
        # A definition is a stated meaning, an entity is a table it reads — so a
        # delta under a definition outranks one that only lands on a mapped table.
        key=lambda f: (not f.referenced, not _has_definition(f), f.table, f.kind, f.detail),
    )


def _has_definition(finding: Finding) -> bool:
    return any(r.kind == "definition" for r in finding.refs)


def _column_of(drift: ProfileDrift) -> str | None:
    """The column name a delta is about — `detail` is `name` or `name: a -> b`."""
    if drift.kind in ("table_added", "table_dropped"):
        return None
    return drift.detail.split(":", 1)[0].strip() or None


# The kinds that can BREAK a declared reference: something the layer reads went
# away or changed type under it. A gained column/table is real signal (the
# supersession finding) but nothing that referenced it can be broken by it.
_BREAKING_KINDS: frozenset[str] = frozenset({"column_dropped", "column_retyped", "table_dropped"})


def is_breaking(finding: Finding) -> bool:
    """Whether this delta breaks a declared reference — the exit-1 condition.

    Both halves required: a destructive kind AND an asset that reads it. A
    dropped column nothing declares is drift worth a look (exit 2), not a
    broken layer (exit 1)."""
    return finding.kind in _BREAKING_KINDS and finding.referenced


def exit_code(findings: list[Finding]) -> int:
    """`dst drift`'s verdict as a gate can read it: 0 clean · 2 changes, none
    breaking · 1 changes that break declared references. (4 — not armed — is
    decided before a diff exists; operational errors stay 1, the act-now side.)
    """
    if not findings:
        return 0
    return 1 if any(is_breaking(f) for f in findings) else 2


# ── rendering ────────────────────────────────────────────────────────────────

_VERB = {
    "column_added": "gained column",
    "column_dropped": "DROPPED column",
    "column_retyped": "retyped",
    "table_added": "new table",
    "table_dropped": "DROPPED table",
}


def _consequence(finding: Finding, ref: LayerRef) -> str:
    """Why this pairing is worth a human's attention, in one clause.

    The `column_added` + `sql_expr` case is the module docstring's: a definition
    that DERIVES a quantity, and a warehouse that now publishes it."""
    if finding.kind == "column_added" and ref.sql_expr:
        return (
            f"derives from this table: `{ref.sql_expr}` — review whether "
            f"`{finding.detail}` supersedes the derivation"
        )
    if finding.kind in ("column_dropped", "table_dropped"):
        return f"{ref.why} — that reference is now broken"
    if finding.kind == "column_retyped":
        return f"{ref.why} — review casts, rounding and comparisons"
    return ref.why


def _headline(finding: Finding) -> str:
    """`ops.payments: retyped `amount` DOUBLE -> DECIMAL(12,2)` — the name in
    backticks, the change outside them, because `amount: DOUBLE -> DECIMAL` all
    inside one pair of ticks reads as a column called that."""
    verb = _VERB.get(finding.kind, finding.kind)
    if finding.kind == "column_retyped" and ": " in finding.detail:
        column, change = finding.detail.split(": ", 1)
        return f"{finding.table}: {verb} `{column}` {change}"
    return f"{finding.table}: {verb} `{finding.detail}`"


def render(finding: Finding) -> list[str]:
    """One line per finding, plus one per semantic asset it implicates."""
    head = _headline(finding)
    if not finding.refs:
        return [f"{head} — nothing in semantic/ reads this table"]
    return [head] + [
        f"  ! {r.kind} `{r.name}` ({r.path}) {_consequence(finding, r)}" for r in finding.refs
    ]
