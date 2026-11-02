"""`dst migrate-model`: a dst entity catalogue → a Cube model plus the dst overlay.

Reads the project's shared layer (semantic/entities, relationships,
definitions, tables), its lenses and its probe, and writes under ``cube/``:

- ``model/<entity>.yml``: one cube per entity (and a companion cube per
  entity whose ``sum_distinct_on`` metrics sum at another grain);
- ``model/views/<lens>.yml``: per lens a scope view, plus one fact view per
  cube with in-lens joins, holding every dimension cube its many-to-one joins
  reach (a Cube view resolves one join tree);
- ``overlay.yaml``: everything dst reads that is not a member's ``meta.dst``;
- ``MIGRATION.md``: the census and every item the Cube model could not carry
  as authored, each with the failure mode a question would meet there.

The mapping, onto the closed ``meta.dst`` schema (any other key fails plan):

- entity → cube: ``sql_table``, or ``sql`` that bakes the population filter
  and a blueprint's ``versions`` (the latest sync per key) when the entity's
  key is the blueprint key;
- field → dimension; a text ``YYYY-MM`` month → a ``time`` dimension cast to
  the month's first day; primary keys stay callable;
- ``internal`` / ``hide`` → ``public: false`` with ``meta.dst.internal`` /
  ``meta.dst.hide`` (and the overlay's hide list for the exception groups);
  views list their members, so neither reaches the API through a view, and a
  hidden member with exception groups is listed under the view's
  ``access_policy`` (``group:`` read from ``security_context.dst.groups``);
- metric → measure (sum, count, count_distinct, avg, min, max with filters;
  ratio and derived → ``number``; ``sum_distinct_on`` → the companion cube,
  joined as its parent is wherever the join's columns are the distinct key);
- relationship → a join on its left cube; a definition with ``sql`` → a
  segment on the cube it is about; lens → views.

The output is generated once and then authored: a re-run writes only what it
generated before and nobody changed since (a manifest of sha256 per file,
``.dst-migrate.json``), refuses a file edited by hand, and leaves files it
did not write alone. A file edited by hand that dst would write unchanged is
kept as the author's, and the views follow the cubes as authored: a member
added to a cube is listed by name in the views that carry it, one removed is
dropped, and a cube written by hand joins the lens that selects a cube its
``meta.dst.sources`` names. The same input writes the same bytes.
"""

from __future__ import annotations

import collections
import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from services.contracts.cube_probe import date_meaning
from services.contracts.failure_modes import FailureMode
from services.project.loader import asset_dir
from services.semantic.files import DEFINITION_DIR, ENTITY_DIR, RELATIONSHIP_DIR, TABLE_DIR

MANIFEST = ".dst-migrate.json"
REPORT = "MIGRATION.md"
OVERLAY = "overlay.yaml"

TYPE_MAP = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "date": "time",
    "timestamp": "time",
    "boolean": "boolean",
}
AGG_MAP = {
    "sum": "sum",
    "count": "count",
    "count_distinct": "count_distinct",
    "avg": "avg",
    "min": "min",
    "max": "max",
}
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
# A dictionary this small, complete, on a key column: the table is split by
# its values (one book, one feed per value), a candidate feed axis.
FEED_AXIS_MAX = 12
# The legal forms a company name ends with: a key column whose values mostly
# carry one names the companies (the overlay's scope.names).
LEGAL_FORMS = frozenset(
    "oy oyj ab as asa aps gmbh ag ltd limited llc inc corp plc sa sas bv nv srl spa kk".split()
)

Doc = dict[str, Any]


class _Dumper(yaml.SafeDumper):  # type: ignore[misc]  # yaml ships untyped
    def ignore_aliases(self, data: object) -> bool:
        return True


def _str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_presenter)


def _escape(obj: Any, key: str | None = None) -> Any:
    """Cube reads every YAML string as a template, so braces outside ``sql``
    fields are doubled."""
    if isinstance(obj, dict):
        return {k: _escape(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_escape(v, key) for v in obj]
    if isinstance(obj, str) and key not in ("sql", "sql_table") and ("{" in obj or "}" in obj):
        return obj.replace("{", "{{").replace("}", "}}")
    return obj


def dump(obj: Any, cube_file: bool = True) -> str:
    return str(
        yaml.dump(
            _escape(obj) if cube_file else obj,
            Dumper=_Dumper,
            sort_keys=False,
            allow_unicode=True,
            width=110,
        )
    )


def _load(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _frontmatter(path: Path, root: Path) -> Doc:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return {}
    d: Doc = yaml.safe_load(m.group(1)) or {}
    d["_body"] = m.group(2).strip()
    d["_file"] = path.relative_to(root).as_posix()
    return d


def cube_sql(expr: str, entity: str, alias_map: dict[str, str]) -> str:
    """``entity.col`` → ``{CUBE}.col``; another entity's prefix → ``{other}.col``."""

    def sub(m: re.Match[str]) -> str:
        ent, col = m.group(1), m.group(2)
        if ent == entity:
            return "{CUBE}." + col
        if ent in alias_map:
            return "{" + alias_map[ent] + "}." + col
        return m.group(0)

    return re.sub(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", sub, expr)


@dataclass
class Note:
    """One item the Cube model does not carry as authored."""

    where: str
    item: str
    how: str
    mode: FailureMode


# What the Cube model carries outside its own vocabulary, by design: each is
# meta.dst or the overlay, and each names the mode a question meets when the
# carrier is missing.
BY_DESIGN: tuple[tuple[str, FailureMode], ...] = (
    (
        "`additivity: level` + `snapshot_grain`: Cube sums a level across months; dst must "
        "pin the latest snapshot per bucket (meta.dst).",
        FailureMode.wrong_grain,
    ),
    (
        "`agg_time_field` (a measure's own clock): Cube applies whichever time dimension the "
        "query names; `meta.dst.clock` tells dst which one to name.",
        FailureMode.window_misbound,
    ),
    (
        "`default_time_field`: Cube has no default time dimension; "
        "`overlay.cubes.<cube>.default_time_dimension`.",
        FailureMode.window_misbound,
    ),
    ("`pinned_dimensions`: no Cube equivalent (overlay).", FailureMode.wrong_grain),
    (
        "`use_cases` / `common_questions` / blueprint `caveats`: routing text, overlay per cube.",
        FailureMode.unroutable,
    ),
    (
        "`internal` / `hide`: `public: false` on the cube, and no view lists the member; a "
        "hide with `except_groups` is listed under the view's `access_policy`, granted to "
        "those groups only (Cube's `contextToGroups` reads `security_context.dst.groups`), "
        "and overlay.hide.",
        FailureMode.hidden_column,
    ),
    (
        "Lens scope/policy (`not_for`, `not_computable`, `sources`, `access`, `expose`, "
        "`timezone`, `stale_after_days`): overlay.lenses.",
        FailureMode.scope_violation,
    ),
    (
        "Definitions without `sql`, ambiguous terms with readings/audiences: "
        "overlay.definitions / overlay.ambiguous.",
        FailureMode.ambiguous_metric,
    ),
    (
        "Certified answers stay SQL pairs in `lenses/<lens>/certified_answers.yaml`.",
        FailureMode.not_expressible,
    ),
    (
        "dst `fields` vs `dimensions`: both are Cube dimensions; the group-by hint is only the "
        "presence of `meta.dst.aliases`.",
        FailureMode.wrong_grain,
    ),
    (
        "Composite primary keys: every key column carries `primary_key: true`.",
        FailureMode.wrong_grain,
    ),
    (
        "View members are prefixed with the cube name (`prefix: true`, `<view>.<cube>_<member>`) "
        "only in a view where a member name occurs in two of its cubes (`month`, `entity`, the "
        "join key); a view whose cubes share no member name lists them bare "
        "(`<view>.<member>`).",
        FailureMode.not_declared,
    ),
    (
        "A Cube view resolves one join tree: a lens becomes `<lens>_lens` (scope, direct "
        "includes) plus one `<lens>_<fact>` view per cube with in-lens joins (the fact + the "
        "dimensions of every cube its many_to_one / one_to_one joins reach, by the first path "
        "found).",
        FailureMode.not_expressible,
    ),
    (
        "Primary keys are public (Cube hides them by default); an `internal` or hidden key "
        "stays `public: false`.",
        FailureMode.hidden_column,
    ),
)


class Converter:
    """The catalogue → Cube mapping, in memory. ``files()`` is the output."""

    def __init__(self, project: Path, authored: dict[str, Doc] | None = None) -> None:
        self.project = project
        # Cubes authored under the output (edited since generated, or written
        # by hand): their members are what the views list, and a hand-written
        # cube whose meta.dst.sources names a lens's cube joins that lens.
        self.authored: dict[str, Doc] = dict(authored or {})
        paths = sorted(p for p in project.rglob("*") if p.is_file())
        by_dir: dict[str, list[Path]] = collections.defaultdict(list)
        for p in paths:
            rel = p.relative_to(project).as_posix()
            if (kind := asset_dir(rel)) is not None:
                by_dir[kind].append(p)
        self.entities: dict[str, Doc] = {
            e["name"]: e for e in (_load(p) for p in by_dir[ENTITY_DIR]) if isinstance(e, dict)
        }
        self.relationships: list[Doc] = [
            r for r in (_load(p) for p in by_dir[RELATIONSHIP_DIR]) if isinstance(r, dict)
        ]
        self.blueprints: dict[str, Doc] = {
            b["table"]: b
            for b in (_load(p) for p in by_dir[TABLE_DIR])
            if isinstance(b, dict) and b.get("table")
        }
        self.definitions: list[Doc] = [
            d for d in (_frontmatter(p, project) for p in by_dir[DEFINITION_DIR]) if d
        ]
        lenses_dir = project / "lenses"
        self.lenses: dict[str, Doc] = (
            {
                d.name: _load(d / "lens.yaml")
                for d in sorted(lenses_dir.iterdir())
                if (d / "lens.yaml").exists()
            }
            if lenses_dir.is_dir()
            else {}
        )
        self.dialect = _warehouse_dialect(project)
        self.probe: dict[str, Doc] = {}
        for p in sorted((project / "profiles").glob("*.probe.json")):
            if p.name.startswith("cube."):
                continue  # dst's own Cube probe, not a warehouse profile
            for t in json.loads(p.read_text(encoding="utf-8")).get("tables", []):
                self.probe.setdefault(t["table"], t)
        self.notes: list[Note] = []
        self.census: collections.Counter[str] = collections.Counter()
        self.shared_axes = self.key_axes()
        self.cubes: dict[str, Doc] = {}
        self.extra_cubes: dict[str, Doc] = {}
        # One cube per role relationship, `extends:` its target: a booking's
        # payer and passenger are both persons, and Cube joins a cube once, so
        # each role is its own cube (<target>_<role>) joined by its own
        # relationship and listed in the views under its own prefix.
        self.role_cubes: dict[str, Doc] = {}
        self.overlay: Doc = {
            "version": 1,
            "cubes": {},
            "lenses": {},
            "definitions": [],
            "ambiguous": [],
            "hide": [],
        }

    # ── helpers ─────────────────────────────────────────────────────────

    def note(self, where: str, item: str, how: str, mode: FailureMode) -> None:
        self.notes.append(Note(where, item, how, mode))

    def text_month_columns(self, table: str, fields: list[Doc]) -> set[str]:
        cols = {c["name"]: c for c in self.probe.get(table, {}).get("columns", [])}
        out = set()
        for f in fields:
            if f.get("type") != "string":
                continue
            c = cols.get(f["name"])
            vals = (c or {}).get("top_values") or []
            if vals and all(isinstance(v, str) and MONTH_RE.match(v) for v in vals):
                out.add(f["name"])
            elif f["name"].endswith("month") and not vals:
                out.add(f["name"])
        return out

    @staticmethod
    def hidden_as(
        hidden: dict[str, list[str] | None], name: str, table: str, col: str
    ) -> str | None:
        """The `hide` entry that withholds ``col`` — fnmatch on the bare name,
        `entity.column` or `table.column`, as the compile matches it — or
        None. A column the entity's population filter reads is withheld all
        the same: the filter is baked into the cube's sql, the column never
        arrives as a public dimension."""
        forms = (col, f"{name}.{col}", f"{table}.{col}", f"{table.rsplit('.', 1)[-1]}.{col}")
        for pattern in hidden:
            if any(fnmatchcase(f.lower(), pattern.lower()) for f in forms):
                return pattern
        return None

    @staticmethod
    def hide_entries(e: Doc) -> dict[str, list[str] | None]:
        out: dict[str, list[str] | None] = {}
        for h in e.get("hide") or []:
            if isinstance(h, str):
                out[h] = None
            else:
                out[h["column"]] = list(h.get("except_groups") or [])
        return out

    def key_dictionary(self, e: Doc, col: str) -> list[str]:
        """The probe's complete value list of a string key column of the
        entity's blueprint (the time field and a sync column left out), when
        it is small enough to name feeds; else nothing."""
        bp = self.blueprints.get(e["source"]["table"], {})
        sync = (bp.get("versions") or {}).get("sync_column")
        fields = {f["name"]: f for f in e["fields"]}
        if col not in (bp.get("key") or []) or col in (sync, e.get("default_time_field")):
            return []
        if (fields.get(col) or {}).get("type") != "string":
            return []
        cols = {c["name"]: c for c in self.probe.get(e["source"]["table"], {}).get("columns", [])}
        pc = cols.get(col) or {}
        values = [str(v) for v in pc.get("top_values") or []]
        if pc.get("values_complete") and 2 <= len(values) <= FEED_AXIS_MAX:
            return values
        return []

    def key_axes(self) -> dict[str, list[str]]:
        """Key columns that split several tables by the same values (an
        entity, a revenue stream): name → the values, first seen first. One
        table split by a column is a breakdown; two by the same values is how
        the warehouse keeps its books."""
        seen: dict[str, list[tuple[str, list[str]]]] = collections.defaultdict(list)
        for name, e in self.entities.items():
            bp = self.blueprints.get(e["source"]["table"], {})
            for col in bp.get("key") or []:
                if values := self.key_dictionary(e, col):
                    seen[col].append((name, values))
        out: dict[str, list[str]] = {}
        for col, hits in seen.items():
            if len(hits) >= 2 and set(hits[0][1]) & set().union(*(set(v) for _n, v in hits[1:])):
                out[col] = list(dict.fromkeys(v for _n, vs in hits for v in vs))
        return out

    def feed_axes(self, e: Doc) -> list[str]:
        """The dimensions that say which book or system a row came from, on a
        cube with a clock: a key axis several tables share (``key_axes``), and
        a key column whose every value the blueprint's ``system`` names
        ("Google Ads / Meta Ads exports" → platform). The monitor reads each
        combination's data end as its own feed."""
        if not e.get("default_time_field"):
            return []
        bp = self.blueprints.get(e["source"]["table"], {})
        system = (bp.get("system") or "").lower()
        out = []
        for f in e["fields"]:
            col = f["name"]
            if col == e.get("default_time_field") or f.get("type") != "string":
                continue
            if col in self.shared_axes:
                out.append(col)
                continue
            values = self.key_dictionary(e, col)
            named = [v.lower().replace("_", " ") for v in values]
            if values and all(re.search(rf"\b{re.escape(v)}\b", system) for v in named):
                out.append(col)
        return out

    def clock_kinds(self, e: Doc) -> dict[str, str]:
        """The date fields the probe saw rows for after the day it ran, each
        an event or a schedule on the evidence the monitor reads: what the
        field's name, description or blueprint clock ``meaning`` says
        (``date_meaning``: decided, paid = event; due, planned = schedule).
        Said nothing, such a field is a schedule (a due or end date, never
        clipped), and a cube's or a measure's clock is left to the monitor."""
        table = e["source"]["table"]
        pr = self.probe.get(table, {})
        probed = str(pr.get("profiled_at") or "")[:10]
        if not probed:
            return {}
        clocks = {e.get("default_time_field")} | {m.get("agg_time_field") for m in e["metrics"]}
        cols = {c["name"]: c for c in pr.get("columns", [])}
        meanings = (self.blueprints.get(table) or {}).get("clocks") or {}
        out: dict[str, str] = {}
        for f in e["fields"]:
            name = f["name"]
            if f.get("type") not in ("date", "timestamp"):
                continue
            if not str((cols.get(name) or {}).get("max") or "")[:10] > probed:
                continue
            said = meanings.get(name)
            kind = date_meaning(
                name,
                f.get("description"),
                said.get("meaning") if isinstance(said, dict) else None,
            )
            if kind is not None:
                out[name] = kind
            elif name not in clocks:
                out[name] = "schedule"
        return out

    def scope_names(self) -> Doc:
        """``scope.names``: each company a shared key axis names (most of its
        values end in a legal form) → the words that name it, its name and
        its name without the legal form. The words a question may also use
        (a country, a system, a region list) are the author's to add."""
        names: Doc = {}
        for values in self.shared_axes.values():
            legal = [v for v in values if v.split()[-1].lower().strip(".") in LEGAL_FORMS]
            if 2 * len(legal) < len(values):
                continue
            for v in values:
                words = v.split()
                short = words[:-1] if v in legal and len(words) > 1 else words
                names[v] = list(dict.fromkeys([v.lower(), " ".join(short).lower()]))
        return names

    # ── entity → cube ───────────────────────────────────────────────────

    def convert_entity(self, e: Doc) -> Doc:
        name, table = e["name"], e["source"]["table"]
        bp = self.blueprints.get(table, {})
        pk = list(e.get("primary_key") or [])
        pop = e.get("population_filter")
        versions: Doc = bp.get("versions") or {}
        key = [
            k for k in list(bp.get("key") or []) if versions and k != versions.get("sync_column")
        ]
        dedup = bool(
            versions
            and versions.get("sync_column")
            and key
            and set(key) <= {f["name"] for f in e["fields"]}
        )
        if dedup and pk != key:  # the latest version per blueprint key: the key is the grain
            self.note(
                name,
                f"primary_key {pk}",
                f"-> {key} (versions dedup keeps the latest sync per key)",
                FailureMode.wrong_grain,
            )
            pk = key
        cube: Doc = {
            "name": name,
            "title": name.replace("_", " "),
            "description": e.get("description", "").strip(),
        }
        if dedup or pop:
            inner = f"SELECT * FROM {table}"
            if dedup:
                inner += (
                    f" QUALIFY ROW_NUMBER() OVER (PARTITION BY {', '.join(key)} "
                    f"ORDER BY {versions['sync_column']} DESC) = 1"
                )
            sql = inner
            if pop:
                where = cube_sql(pop, name, {}).replace("{CUBE}.", "")
                sql = (
                    f"SELECT * FROM ({inner}) AS {name}\nWHERE {where}"
                    if dedup
                    else f"{inner}\nWHERE {where}"
                )
            cube["sql"] = sql
            self.census["cubes with sql"] += 1
            if dedup:
                self.census["cubes with versions dedup"] += 1
            if pop:
                self.census["cubes with population baked"] += 1
        else:
            cube["sql_table"] = table
        meta: Doc = {"grain": e.get("grain"), "key": pk}
        if e.get("population"):
            meta["population"] = e["population"]
        if bp.get("system"):
            meta["system"] = bp["system"]
        cube["meta"] = {"dst": meta}

        joins = []
        for r in self.relationships:
            if r["left"] == name and r["right"] in self.entities:
                right = r["right"]
                if role := r.get("role"):
                    right = self.role_cube(r["right"], role, name)
                joins.append(
                    {
                        "name": right,
                        "relationship": r.get("relationship", "many_to_one"),
                        "sql": cube_sql(r["on"], name, {r["right"]: right}),
                    }
                )
                self.census["joins"] += 1
        if joins:
            cube["joins"] = joins

        dim_meta = {d["name"] if isinstance(d, dict) else d: d for d in e.get("dimensions") or []}
        internal = set(e.get("internal") or [])
        hidden = self.hide_entries(e)
        text_months = self.text_month_columns(table, e["fields"])
        probe_cols = {c["name"]: c for c in self.probe.get(table, {}).get("columns", [])}
        clocks = bp.get("clocks") or {}
        value_aliases: dict[str, Any] = {}
        for d in self.definitions:
            about = d.get("about") or ""
            if d.get("value_aliases") and about.startswith(name + "."):
                value_aliases[about.split(".", 1)[1]] = d["value_aliases"]
        metric_names = {m["name"] for m in e["metrics"]}
        dims = []
        for f in e["fields"]:
            col = f["name"]
            dim: Doc = {
                "name": col,
                "sql": "{CUBE}." + col,
                "type": TYPE_MAP.get(f.get("type") or "", "string"),
            }
            if col in metric_names:  # one namespace per cube for every member kind
                dim["name"] = col + "_value"
                dim["title"] = col + " (the column)"
                self.note(
                    name,
                    f"field {col} shares its name with a metric",
                    f"dimension renamed {col}_value (one namespace per cube)",
                    FailureMode.not_declared,
                )
                self.census["dimensions renamed (name shared with a measure)"] += 1
            if col in text_months:
                dim["type"] = "time"
                dim["sql"] = "CAST({CUBE}." + col + " || '-01' AS DATE)"
                dim["title"] = col.replace("_", " ") + " (text YYYY-MM cast to a date)"
                self.census["text-month time dimensions"] += 1
            if (
                dim["type"] == "time"
                and self.dialect == "bigquery"
                and (
                    col in text_months
                    or str(probe_cols.get(col, {}).get("type")).upper() != "TIMESTAMP"
                )
            ):
                # Cube's BigQuery dialect compares a time dimension with
                # TIMESTAMP(?) bounds, and BigQuery has no comparison of a
                # DATE or a DATETIME with a TIMESTAMP: the window fails on
                # the warehouse. TIMESTAMP() takes both; a column the probe
                # types TIMESTAMP stays bare, so a filter on it still prunes
                # the table's partitions.
                dim["sql"] = "TIMESTAMP(" + dim["sql"] + ")"
                self.census["time dimensions cast to TIMESTAMP (BigQuery)"] += 1
            if f.get("description"):
                dim["description"] = f["description"]
            if col in pk:
                dim["primary_key"] = True
                dim["public"] = True  # Cube hides primary keys by default
            dmeta: Doc = {}
            declared = dim_meta.get(col)
            if isinstance(declared, dict) and declared.get("aliases"):
                dmeta["aliases"] = list(declared["aliases"])
            if col in internal:
                dim["public"] = False
                dmeta["internal"] = True
                self.census["internal (public false)"] += 1
            if (entry := self.hidden_as(hidden, name, table, col)) is not None:
                dim["public"] = False
                dmeta["hide"] = {"except_groups": hidden[entry] or []}
                self.overlay["hide"].append(
                    {"cube": name, "dimension": col, "except_groups": hidden[entry] or []}
                )
                self.census["hide (public false)"] += 1
            if col in clocks:
                dmeta["clock"] = {
                    k: v for k, v in clocks[col].items() if k in ("meaning", "timezone")
                }
            pc = probe_cols.get(col) or {}
            if (
                f.get("type") == "string"
                and col not in text_months
                and pc.get("is_low_cardinality")
                and pc.get("values_complete")
                and pc.get("top_values")
                and len(pc["top_values"]) <= 40
                and self.hidden_as(hidden, name, table, col) is None
            ):
                dmeta["dictionary"] = list(pc["top_values"])  # the probe's complete value list
                self.census["dimensions with a dictionary"] += 1
            if col in value_aliases:
                dmeta["value_aliases"] = [
                    {"alias": k, "value": v} for k, v in value_aliases[col].items()
                ]
            if dmeta:
                dim["meta"] = {"dst": dmeta}
            dims.append(dim)
            self.census["dimensions"] += 1
            if dim["type"] == "time":
                self.census["time dimensions"] += 1
        cube["dimensions"] = dims
        cubes_overlay = self.overlay["cubes"]
        if e.get("default_time_field"):
            cubes_overlay.setdefault(name, {})["default_time_dimension"] = e["default_time_field"]
        for k in ("use_cases", "common_questions"):
            if e.get(k):
                cubes_overlay.setdefault(name, {})[k] = list(e[k])
        if bp.get("caveats"):
            cubes_overlay.setdefault(name, {})["caveats"] = list(bp["caveats"])
        if e.get("pinned_dimensions"):
            cubes_overlay.setdefault(name, {})["pinned_dimensions"] = list(e["pinned_dimensions"])
            self.note(
                name,
                f"pinned_dimensions {e['pinned_dimensions']}",
                "overlay.cubes.<name>.pinned_dimensions (Cube has no 'always group by this')",
                FailureMode.wrong_grain,
            )
        if kinds := self.clock_kinds(e):
            cubes_overlay.setdefault(name, {})["clock_kind"] = kinds
            self.note(
                name,
                f"clock_kind {kinds}",
                "rows dated after the probe ran: an event date (its name or meaning says "
                "so) is clipped to the lens clock, a schedule (due, end) is read as asked",
                FailureMode.window_misbound,
            )
        if axes := self.feed_axes(e):
            cubes_overlay.setdefault(name, {})["feed_axes"] = axes
            self.census["cubes with feed axes"] += 1
        time_field = e.get("default_time_field")
        snapshot_grain = "month" if time_field in text_months else None
        creation_dated = self.creation_dated(e)
        by_metric = {m["name"]: m for m in e["metrics"]}

        measures: list[Doc] = []
        distinct_measures: list[tuple[Doc, list[str]]] = []
        for m in e["metrics"]:
            mm: Doc = {"name": m["name"], "title": m.get("label", m["name"])}
            if m.get("description"):
                mm["description"] = m["description"]
            agg, mtype = m.get("agg"), m.get("type")
            if mtype == "ratio":
                mm["type"] = "number"
                mm["sql"] = "{" + m["numerator"] + "} / NULLIF({" + m["denominator"] + "}, 0)"
                self.census["measures ratio (number)"] += 1
            elif mtype == "derived":
                mm["type"] = "number"
                mm["sql"] = m["expr"]
                self.census["measures derived (number)"] += 1
            elif agg == "sum_distinct_on":
                keys = [k.split(".", 1)[1] for k in m["distinct_on"]]
                if dedup and set(keys) == set(key):
                    mm["type"] = "sum"
                    mm["sql"] = cube_sql(m["expr"], name, {})
                    self.census["measures sum_distinct_on -> sum (dedup cube)"] += 1
                else:
                    distinct_measures.append((m, keys))
                    continue
            elif agg in AGG_MAP:
                mm["type"] = AGG_MAP[agg]
                if m.get("expr"):
                    mm["sql"] = cube_sql(str(m["expr"]), name, {})
                elif agg != "count":
                    mm["sql"] = "{CUBE}." + pk[0]
                self.census[f"measures {agg}"] += 1
            else:
                self.note(
                    name,
                    f"metric {m['name']} agg={agg} type={mtype}",
                    "DROPPED: unknown aggregation",
                    FailureMode.not_expressible,
                )
                continue
            if m.get("filters"):
                mm["filters"] = [{"sql": cube_sql(f, name, {})} for f in m["filters"]]
                self.census["measure filters"] += len(m["filters"])
            if m.get("format") in ("currency", "percent"):
                mm["format"] = m["format"]
            mmeta: Doc = {}
            for k in ("additivity", "window_reading", "better", "currency"):
                if m.get(k):
                    mmeta[k] = m[k]
            if mtype in ("ratio", "derived") and m.get("additivity") == "level":
                # A ratio of flows reads as its operands do: the lane must
                # never pin it to a snapshot the operands do not have.
                operands = (
                    [m["numerator"], m["denominator"]]
                    if mtype == "ratio"
                    else re.findall(r"\{(\w+)\}", str(m.get("expr") or ""))
                )
                if not any(by_metric.get(o, {}).get("additivity") == "level" for o in operands):
                    del mmeta["additivity"]
                    self.note(
                        name,
                        f"metric {m['name']} additivity=level on a ratio of flows",
                        "DROPPED: each operand windows on its own clock; fix the entity file",
                        FailureMode.wrong_grain,
                    )
            if m.get("aliases"):
                mmeta["aliases"] = list(m["aliases"])
            if mmeta.get("additivity") == "level":
                mmeta["snapshot_grain"] = snapshot_grain or time_field or "row"
                if creation_dated:
                    mmeta["creation_dated"] = True
            if m.get("agg_time_field"):
                mmeta["clock"] = m["agg_time_field"]
                self.census["measures with their own clock"] += 1
            if mmeta:
                mm["meta"] = {"dst": mmeta}
            measures.append(mm)
            self.census["measures"] += 1
        cube["measures"] = measures

        segs = []
        for d in self.definitions:
            about = d.get("about") or ""
            if d.get("sql") and (about == name or about.startswith(name + ".")):
                segs.append(
                    {
                        "name": d["metric"],
                        "sql": cube_sql(d["sql"], name, {}),
                        "meta": {
                            "dst": {
                                "meaning": d.get("summary"),
                                "aliases": list(d.get("aliases") or []),
                                "term": d["metric"],
                            }
                        },
                    }
                )
                self.census["segments"] += 1
        if segs:
            cube["segments"] = segs

        if distinct_measures:
            self.make_distinct_cube(e, cube, distinct_measures)
        self.census["cubes"] += 1
        return cube

    def creation_dated(self, e: Doc) -> bool:
        """Does the probe show the entity's table holds one row per record
        (a key column unique on its own), so a level on it has no snapshot
        to pin when no period is asked? ``value_guard.is_creation_dated``."""
        from services.contracts.profile import TableProfile
        from services.runtime.value_guard import is_creation_dated

        pr = self.probe.get(e["source"]["table"])
        if not pr:
            return False
        try:
            profile = TableProfile.model_validate({"connection": "", **pr})
        except ValueError:
            return False  # a hand-written profile with no counts says nothing
        return is_creation_dated(list(e.get("primary_key") or []), profile)

    def make_distinct_cube(
        self, e: Doc, cube: Doc, distinct_measures: list[tuple[Doc, list[str]]]
    ) -> None:
        """``sum_distinct_on`` on a key other than the row grain: a second cube
        at that grain, one row per key."""
        name = e["name"]
        keys = distinct_measures[0][1]
        dname = f"{name}_by_{'_'.join(keys)}"
        table = e["source"]["table"]
        pop = e.get("population_filter")
        where = (" WHERE " + cube_sql(pop, name, {}).replace("{CUBE}.", "")) if pop else ""
        other = [f["name"] for f in e["fields"] if f["name"] not in keys]
        selects = list(keys) + [f"ANY_VALUE({c}) AS {c}" for c in other]
        for m, _ in distinct_measures:
            expr = cube_sql(m["expr"], name, {}).replace("{CUBE}.", "")
            cond = " AND ".join(
                cube_sql(f, name, {}).replace("{CUBE}.", "") for f in m.get("filters") or []
            )
            selects.append(
                (f"MAX(CASE WHEN {cond} THEN {expr} END)" if cond else f"MAX({expr})")
                + f" AS _{m['name']}"
            )
        sql = f"SELECT {', '.join(selects)}\nFROM {table}{where}\nGROUP BY {', '.join(keys)}"
        dims = [copy.deepcopy(d) for d in cube["dimensions"]]
        for d in dims:
            d["primary_key"] = d["name"] in keys
            if d["primary_key"]:
                d["public"] = True
            else:
                d.pop("primary_key")
        measures = []
        for m, _ in distinct_measures:
            mm: Doc = {
                "name": m["name"],
                "title": m.get("label", m["name"]),
                "type": "sum",
                "sql": "{CUBE}._" + m["name"],
            }
            if m.get("description"):
                mm["description"] = m["description"]
            if m.get("format") in ("currency", "percent"):
                mm["format"] = m["format"]
            meta = {k: m[k] for k in ("better", "currency") if m.get(k)}
            if m.get("aliases"):
                meta["aliases"] = list(m["aliases"])
            if m.get("filters"):
                meta["rejected_reading"] = (
                    "the filter is applied per distinct key inside the cube sql (one value per "
                    + ", ".join(keys)
                    + ")"
                )
            if meta:
                mm["meta"] = {"dst": meta}
            measures.append(mm)
            self.census["measures sum_distinct_on -> second cube"] += 1
        companion: Doc = {
            "name": dname,
            "title": dname.replace("_", " "),
            "description": f"{name} collapsed to one row per {', '.join(keys)} so that "
            "sum_distinct_on measures sum one value per key.",
            "sql": sql,
            "meta": {
                "dst": {
                    "grain": f"one row per {', '.join(keys)}",
                    "key": keys,
                    "population": e.get("population"),
                    "sources": [name],
                }
            },
        }
        # The parent's joins whose own columns are the distinct key hold at that
        # grain; a join on any other column would read one arbitrary row's value.
        joins = []
        for j in cube.get("joins") or []:
            if set(re.findall(r"\{CUBE\}\.([a-z_][a-z0-9_]*)", j["sql"])) <= set(keys):
                joins.append(copy.deepcopy(j))
                self.census["joins (distinct-grain companions)"] += 1
            else:
                self.note(
                    dname,
                    f"join {j['name']}",
                    f"not joined: its columns are not the distinct key {keys}",
                    FailureMode.not_expressible,
                )
        if joins:
            companion["joins"] = joins
        companion["dimensions"] = dims
        companion["measures"] = measures
        self.extra_cubes[dname] = companion
        self.census["cubes (distinct-grain companions)"] += 1
        self.note(
            name,
            f"sum_distinct_on {[m['name'] for m, _ in distinct_measures]}",
            f"second cube {dname} at the distinct grain",
            FailureMode.wrong_grain,
        )

    def role_cube(self, target: str, role: str, left: str) -> str:
        """The cube a role relationship joins: ``<target>_<role>``, extending
        the target cube so it holds every member, under its own name."""
        alias = f"{target}_{role}"
        if alias not in self.role_cubes:
            self.role_cubes[alias] = {
                "name": alias,
                "title": alias.replace("_", " "),
                "description": f"{target} as the {role} of {left}.",
                "extends": target,
                "meta": {"dst": {"sources": [target], "role": role}},
            }
            self.census["cubes (role copies)"] += 1
        return alias

    def cube_for(self, name: str) -> Doc:
        """The cube whose members ``name`` lists: a role copy's are its target's."""
        if name in self.role_cubes:
            return self.cubes[self.role_cubes[name]["extends"]]
        return self.cubes.get(name) or self.extra_cubes[name]

    # ── lens → views ────────────────────────────────────────────────────

    def listed(
        self,
        cube: Doc,
        path: str,
        policy: dict[str, list[str]],
        *,
        measures: list[str] | None = None,
        dims: bool = False,
    ) -> Doc:
        """``view_cube`` over the cube as authored, when it was: the members
        the generated cube lists that it still holds, in their order, then
        its new members by name."""
        made = self.view_cube(cube, path, policy, measures=measures, dims=dims)
        authored = self.authored.get(cube["name"])
        if authored is None or authored is cube:
            return made
        now = self.view_cube(authored, path, policy, measures=measures, dims=dims)
        kept = [m for m in made["includes"] if m in now["includes"]]
        added = [m for m in now["includes"] if m not in made["includes"]]
        if added:
            self.census["view members added from authored cubes"] += len(added)
        return {**made, "includes": kept + added}

    @staticmethod
    def view_cube(
        cube: Doc,
        path: str,
        policy: dict[str, list[str]],
        *,
        measures: list[str] | None = None,
        dims: bool = False,
    ) -> Doc:
        """One ``cubes:`` entry of a view, its members listed: a view's
        ``includes: "*"`` would serve a ``public: false`` member through the
        view. Internal and hidden dimensions are left out; a hidden one with
        exception groups goes in, its prefixed name into ``policy`` with the
        groups that may read it. ``dims`` lists dimensions only (a joined
        cube's measures would fan out); ``measures`` is a lens's subset."""
        prefix = path.rsplit(".", 1)[-1]
        includes = []
        for d in cube["dimensions"]:
            dst = (d.get("meta") or {}).get("dst") or {}
            groups = list((dst.get("hide") or {}).get("except_groups") or [])
            if dst.get("internal") or (dst.get("hide") is not None and not groups):
                continue
            if d.get("public", True) is False and not groups:
                continue
            includes.append(d["name"])
            if groups:
                policy[f"{prefix}_{d['name']}"] = groups
        if not dims:
            held = [m["name"] for m in cube["measures"]]
            includes += held if measures is None else [m for m in measures if m in held]
            includes += [seg["name"] for seg in cube.get("segments") or []]
        return {"join_path": path, "includes": includes, "prefix": True}

    @staticmethod
    def prefixed(
        cubes: list[Doc], policy: dict[str, list[str]]
    ) -> tuple[list[Doc], dict[str, list[str]]]:
        """A view's cube entries keep ``prefix: true`` only where a member
        name occurs in two of them (``month``, ``entity``, the join key):
        then every member is ``<cube>_<member>``, and so is every policy
        key. A view whose cubes share no member name lists them bare, so a
        query names ``<view>.<member>`` and the policy names the member."""
        seen = collections.Counter(m for c in cubes for m in c["includes"])
        if any(n > 1 for n in seen.values()):
            return cubes, policy
        bare = [{k: v for k, v in c.items() if k != "prefix"} for c in cubes]
        unprefixed: dict[str, list[str]] = {}
        for c in cubes:
            prefix = c["join_path"].rsplit(".", 1)[-1] + "_"
            for key, groups in policy.items():
                if key.startswith(prefix) and key[len(prefix) :] in c["includes"]:
                    unprefixed[key[len(prefix) :]] = groups
        return bare, unprefixed

    @staticmethod
    def access_policy(policy: dict[str, list[str]]) -> list[Doc]:
        """Every caller reads the view but its excepted hidden members; each
        exception group reads those it is excepted for too."""
        hidden = sorted(policy)
        out: list[Doc] = [{"group": "*", "member_level": {"excludes": hidden}}]
        for group in sorted({g for gs in policy.values() for g in gs}):
            level: Doc = {"includes": "*"}
            if barred := [m for m in hidden if group not in policy[m]]:
                level["excludes"] = barred
            out.append({"group": group, "member_level": level})
        return out

    def reachable(self, lname: str, root: str, members: set[str]) -> list[str]:
        """Join paths from ``root`` to every in-lens cube its joins reach,
        breadth first, through many_to_one / one_to_one joins only (a
        one_to_many join fans out the root's measures). A cube two paths
        reach is joined by the first."""
        every = {**self.cubes, **self.extra_cubes, **{a: self.cube_for(a) for a in self.role_cubes}}
        paths: list[str] = []
        seen = {root}
        queue = [(root, root)]
        while queue:
            name, path = queue.pop(0)
            for j in every[name].get("joins") or []:
                right = j["name"]
                if right not in members:
                    continue
                if j.get("relationship") not in ("many_to_one", "one_to_one"):
                    self.note(
                        f"lens {lname}",
                        f"join {name} -> {right} ({j.get('relationship')})",
                        f"not in the {root} view: it fans out the measures of {root}",
                        FailureMode.wrong_grain,
                    )
                    continue
                if right in seen:
                    if right != root and f"{path}.{right}" not in paths:
                        self.note(
                            f"lens {lname}",
                            f"{root} reaches {right} again via {path}",
                            "the view joins it by the first path found",
                            FailureMode.not_expressible,
                        )
                    continue
                seen.add(right)
                paths.append(f"{path}.{right}")
                queue.append((right, f"{path}.{right}"))
        return paths

    def convert_lens(self, lname: str, lens: Doc) -> list[Doc]:
        """One scope view per lens (direct includes) plus one fact view per
        in-lens relationship: a Cube view resolves one join tree, so a
        dimension cube reachable by two paths cannot sit in one view."""
        picks = lens.get("select", {}).get("entities") or []
        sel = [s["name"] if isinstance(s, dict) else s for s in picks]
        subsets = {
            s["name"]: list(s["metrics"])
            for s in picks
            if isinstance(s, dict) and s.get("metrics") is not None
        }
        cubes: list[Doc] = []
        policy: dict[str, list[str]] = {}
        for ent in sel:
            if ent not in self.entities:
                self.note(
                    f"lens {lname}",
                    f"select {ent}",
                    "DROPPED: unknown entity",
                    FailureMode.not_declared,
                )
                continue
            cubes.append(self.listed(self.cubes[ent], ent, policy, measures=subsets.get(ent)))
            for extra, c in self.extra_cubes.items():
                if c["meta"]["dst"]["sources"] == [ent]:
                    cubes.append(self.listed(c, extra, policy))
            for alias, c in self.role_cubes.items():
                if c["extends"] == ent:
                    cubes.append(self.listed(self.cubes[ent], alias, policy, dims=True))
        # A cube written by hand joins the lens that selects a cube it is
        # built from (its meta.dst.sources): the lens's views cover it.
        for name, c in sorted(self.authored.items()):
            if name in self.cubes or name in self.extra_cubes:
                continue
            built_from = ((c.get("meta") or {}).get("dst") or {}).get("sources") or []
            if set(built_from) & set(sel):
                cubes.append(self.view_cube(c, name, policy))
                self.census["authored cubes in lens views"] += 1
        sources = list(lens.get("sources") or [])
        display = lens.get("display_name", lname)
        cubes, policy = self.prefixed(cubes, policy)
        scope: Doc = {
            "name": f"{lname}_lens",
            "title": display,
            "description": lens.get("description", ""),
            "meta": {"dst": {"sources": sources}},
            "cubes": cubes,
        }
        if policy:
            scope["access_policy"] = self.access_policy(policy)
        views: list[Doc] = [scope]
        members = set(sel) & set(self.entities)
        members |= {a for a, c in self.role_cubes.items() if c["extends"] in members}
        roots = [r["left"] for r in self.relationships if r["left"] in members]
        roots += [
            x for x, c in self.extra_cubes.items() if c["meta"]["dst"]["sources"][0] in members
        ]
        for left in dict.fromkeys(roots):
            paths = self.reachable(lname, left, members)
            if not paths:
                continue
            policy = {}
            left_cube = self.cubes.get(left) or self.extra_cubes[left]
            vc = [self.listed(left_cube, left, policy)]
            rights = [path.rsplit(".", 1)[-1] for path in paths]
            for path, right in zip(paths, rights, strict=True):
                vc.append(self.listed(self.cube_for(right), path, policy, dims=True))
            vc, policy = self.prefixed(vc, policy)
            view: Doc = {
                "name": f"{lname}_{left}",
                "title": f"{display}: {left} with its dimensions",
                "description": f"{left} with the dimensions of {', '.join(rights)} "
                f"joined (the {lname} lens).",
                "meta": {"dst": {"sources": sources}},
                "cubes": vc,
            }
            if policy:
                view["access_policy"] = self.access_policy(policy)
            views.append(view)
            self.census["fact views (joined dimensions)"] += 1
            self.census["fact view joins past one hop"] += sum(p.count(".") > 1 for p in paths)
        ov = {
            k: lens[k]
            for k in (
                "owner",
                "timezone",
                "stale_after_days",
                "scope",
                "sources",
                "not_for",
                "not_computable",
                "access",
                "expose",
                "eval_gate",
                "auto_review",
            )
            if k in lens
        }
        ov["definitions"] = list(lens.get("select", {}).get("definitions") or [])
        ov["views"] = [v["name"] for v in views]
        for k in ("untyped_fallback", "connections"):
            if k in lens:
                self.note(
                    f"lens {lname}",
                    k,
                    "DROPPED: a serving-config key the Cube lens has no use for",
                    FailureMode.not_expressible,
                )
        self.overlay["lenses"][lname] = ov
        self.census["views"] += 1
        return views

    # ── output ──────────────────────────────────────────────────────────

    def files(self) -> dict[str, str]:
        """Every output file, path (relative to ``cube/``) → content."""
        out: dict[str, str] = {}
        for name, e in self.entities.items():
            self.cubes[name] = self.convert_entity(e)
        for name, cube in {**self.cubes, **self.extra_cubes, **self.role_cubes}.items():
            out[f"model/{name}.yml"] = dump({"cubes": [cube]})
        for lname, lens in self.lenses.items():
            out[f"model/views/{lname}.yml"] = dump({"views": self.convert_lens(lname, lens)})
        for d in self.definitions:
            rec = {
                k: d[k]
                for k in (
                    "metric",
                    "about",
                    "summary",
                    "aliases",
                    "status",
                    "possible_mappings",
                    "audiences",
                )
                if d.get(k)
            }
            rec["file"] = d["_file"]
            if d.get("status") == "ambiguous":
                self.overlay["ambiguous"].append(rec)
                self.census["ambiguous terms"] += 1
            elif d.get("sql"):
                self.census["definitions as segments"] += 1
            else:
                self.overlay["definitions"].append(rec)
                self.census["definitions in overlay (prose)"] += 1
            if d.get("sql") and not d.get("about"):
                self.note(
                    "definitions",
                    d["metric"],
                    "DROPPED: sql without an about entity",
                    FailureMode.not_declared,
                )
        if names := self.scope_names():
            self.overlay["scope"] = {"names": names}
            self.note(
                "overlay",
                f"scope.names {sorted(names)}",
                "each company by its name; add the words a question may also use for one "
                "(a country, a system, `dictionary:<cube.dimension>` for its regions)",
                FailureMode.scope_violation,
            )
        out[OVERLAY] = (
            "# dst overlay over the Cube model: everything dst-only that is not a member "
            "meta key.\n"
            "# Generated by `dst migrate-model`; lenses = scope + policy over views.\n"
            + dump(self.overlay, cube_file=False)
        )
        out[REPORT] = self.report()
        return out

    def report(self) -> str:
        lines = [
            "# Migration report: dst catalogue -> Cube model",
            "",
            "Written by `dst migrate-model`. The model is generated once and then authored: "
            "a re-run refuses to overwrite a file edited since.",
            "",
            "## Census",
            "",
            "| item | n |",
            "|---|---:|",
        ]
        src = {
            "entities": len(self.entities),
            "relationships": len(self.relationships),
            "metrics": sum(len(e["metrics"]) for e in self.entities.values()),
            "fields": sum(len(e["fields"]) for e in self.entities.values()),
            "definitions": len(self.definitions),
            "lenses": len(self.lenses),
            "blueprints": len(self.blueprints),
        }
        for k, v in src.items():
            lines.append(f"| dst {k} | {v} |")
        for k, n in sorted(self.census.items()):
            lines.append(f"| cube {k} | {n} |")
        lines += [
            "",
            "## Carried outside the Cube model",
            "",
            "| where | item | how it is carried | failure mode |",
            "|---|---|---|---|",
        ]
        for note in self.notes:
            lines.append(f"| {note.where} | {note.item} | {note.how} | {note.mode} |")
        lines += [
            "",
            "## Not native to Cube, carried as meta.dst or overlay (by design)",
            "",
            "| how it is carried | failure mode without it |",
            "|---|---|",
        ]
        for text, mode in BY_DESIGN:
            lines.append(f"| {text} | {mode} |")
        lines.append("")
        return "\n".join(lines)


# ── writing, idempotently ─────────────────────────────────────────────────


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class MigrationPlan:
    """What a run would do under ``out``: per path its action."""

    out: Path
    files: dict[str, str]
    actions: dict[str, str] = field(default_factory=dict)  # create|update|unchanged|kept|remove
    refused: list[str] = field(default_factory=list)  # edited by hand since generated
    notes: list[Note] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(a in ("create", "update", "remove") for a in self.actions.values())


def authored_cubes(out: Path, manifest: dict[str, str]) -> dict[str, Doc]:
    """The cubes under ``out/model`` someone authored: a file dst never wrote,
    or one edited since it did. Cube name → the cube as written."""
    found: dict[str, Doc] = {}
    model = out / "model"
    if not model.is_dir():
        return found
    for path in sorted(model.glob("*.yml")):
        rel = path.relative_to(out).as_posix()
        text = path.read_text(encoding="utf-8")
        if manifest.get(rel) == _sha(text):
            continue
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        for cube in doc.get("cubes") or [] if isinstance(doc, dict) else []:
            if isinstance(cube, dict) and cube.get("name"):
                found[str(cube["name"])] = cube
    return found


def _warehouse_dialect(project: Path) -> str | None:
    """The one warehouse type the project's connections name, if one: a Cube
    deployment compiles for one database, and the model's SQL is written in it."""
    from services.project.schema import parse_project_yaml

    path = project / "dst.yaml"
    if not path.is_file():
        return None
    try:
        types = {
            c.type
            for c in parse_project_yaml(path.read_text(encoding="utf-8")).connections.values()
        }
    except Exception:  # noqa: BLE001 — a dst.yaml plan reports on; the model stays generic
        return None
    return types.pop() if len(types) == 1 else None


def plan_migration(project: Path, out: Path) -> MigrationPlan:
    manifest_path = out / MANIFEST
    manifest: dict[str, str] = {}
    if manifest_path.exists():
        manifest = dict(json.loads(manifest_path.read_text(encoding="utf-8")).get("files") or {})
    converter = Converter(project, authored_cubes(out, manifest))
    files = converter.files()
    plan = MigrationPlan(out=out, files=files, notes=converter.notes)
    for rel, text in sorted(files.items()):
        target = out / rel
        if not target.exists():
            plan.actions[rel] = "create"
            continue
        current = target.read_text(encoding="utf-8")
        if current == text:
            plan.actions[rel] = "unchanged"
            continue
        if manifest.get(rel) != _sha(current):
            if manifest.get(rel) == _sha(text):
                # Edited by hand, and dst would write what it wrote before:
                # the file is the author's now, kept as it is.
                plan.actions[rel] = "kept"
                continue
            plan.refused.append(rel)  # not what dst wrote: someone authored it
        plan.actions[rel] = "update"
    for rel, sha in sorted(manifest.items()):
        target = out / rel
        if rel in files or not target.exists():
            continue
        if _sha(target.read_text(encoding="utf-8")) != sha:
            plan.refused.append(rel)
        plan.actions[rel] = "remove"
    return plan


def write_migration(plan: MigrationPlan) -> None:
    """Apply ``plan`` (the caller checked ``refused``)."""
    for rel, action in plan.actions.items():
        target = plan.out / rel
        if action == "remove":
            target.unlink()
        elif action in ("create", "update"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(plan.files[rel], encoding="utf-8")
    manifest = {
        "generated_by": "dst migrate-model",
        "files": {rel: _sha(text) for rel, text in sorted(plan.files.items())},
    }
    (plan.out / MANIFEST).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
