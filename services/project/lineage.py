"""The transformation tool's model DAG, read for `table/X+` selection.

A dbt ``target/manifest.json`` or a SQLMesh project's model files become one
graph: a node per model, seed, snapshot or source, with the relation it builds
(``schema.table`` as the warehouse holds it, lowercase, unquoted; empty for an
ephemeral model) and the nodes that read it. Selection walks it from a table
or model name down to the tables entities read. Neither tool is imported or
run: dbt's own compiled manifest is read as JSON, and SQLMesh model files are
parsed with sqlglot. A model whose inputs cannot be read (a SQLMesh Python
model, a query that does not parse) stays in the graph with ``walked`` false
and the reason, so a selection can say what it could not follow.
"""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from services.certify.bindings import source_tables
from services.dbt.artifacts import DbtArtifactError, load_manifest

_DBT_RELATIONS = ("model", "seed", "snapshot", "source")


class LineageError(ValueError):
    """The configured lineage cannot be read."""


class LineageNode(BaseModel):
    relation: str = ""
    names: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)
    walked: bool = True
    note: str = ""


class LineageGraph(BaseModel):
    tool: Literal["dbt", "sqlmesh"]
    nodes: dict[str, LineageNode] = Field(default_factory=dict)

    def matching(self, pattern: str) -> set[str]:
        """Node ids whose relation or one of whose names matches ``pattern``."""
        return {
            node_id
            for node_id, node in self.nodes.items()
            if any(name_matches(pattern, n) for n in [node.relation, *node.names] if n)
        }

    def downstream(self, starts: set[str]) -> set[str]:
        """``starts`` and every node that reads them, transitively."""
        seen: set[str] = set()
        todo = list(starts)
        while todo:
            node_id = todo.pop()
            if node_id in seen or node_id not in self.nodes:
                continue
            seen.add(node_id)
            todo.extend(self.nodes[node_id].children)
        return seen

    def unwalked(self) -> list[str]:
        return sorted(
            f"{n.relation or node_id} ({n.note})"
            for node_id, n in self.nodes.items()
            if not n.walked
        )


def parts(name: str) -> tuple[str, ...]:
    """A qualified name as lowercase parts, quotes and brackets dropped."""
    cleaned = name.strip().translate(str.maketrans("", "", '"`[]'))
    return tuple(p for p in cleaned.lower().split(".") if p)


def name_matches(pattern: str, qualified: str) -> bool:
    """``pattern`` (a name or glob, possibly qualified) matches ``qualified`` or
    its trailing parts: ``orders`` and ``marts.orders`` both match
    ``db.marts.orders``."""
    want, have = parts(pattern), parts(qualified)
    if not want or len(want) > len(have):
        return False
    return fnmatch.fnmatchcase(".".join(have[-len(want) :]), ".".join(want))


def same_table(a: str, b: str) -> bool:
    """Two qualified names name one table when the shorter is the trailing
    parts of the longer (``marts.orders`` and ``db.marts.orders``)."""
    pa, pb = parts(a), parts(b)
    n = min(len(pa), len(pb))
    return n > 0 and pa[-n:] == pb[-n:]


# ── dbt ──────────────────────────────────────────────────────────────────────


def _dbt_relation(node: dict[str, object]) -> str:
    config = node.get("config")
    if isinstance(config, dict) and config.get("materialized") == "ephemeral":
        return ""
    relation = node.get("relation_name")
    if isinstance(relation, str) and relation:
        return ".".join(parts(relation))
    pieces = [node.get("database"), node.get("schema"), node.get("alias") or node.get("identifier")]
    return ".".join(str(p).lower() for p in pieces if isinstance(p, str) and p)


def dbt_lineage(manifest: dict[str, object]) -> LineageGraph:
    """The graph of a parsed dbt manifest: ``child_map`` when present, else the
    reverse of each node's ``depends_on.nodes`` (the same edges)."""
    raw: dict[str, dict[str, object]] = {}
    for section in ("nodes", "sources"):
        entries = manifest.get(section)
        if isinstance(entries, dict):
            raw.update(
                {
                    str(k): v
                    for k, v in entries.items()
                    if isinstance(v, dict) and v.get("resource_type") in _DBT_RELATIONS
                }
            )
    nodes: dict[str, LineageNode] = {}
    for node_id, node in raw.items():
        names = {str(node.get(k)) for k in ("name", "alias", "identifier") if node.get(k)}
        if node.get("resource_type") == "source" and node.get("source_name"):
            names.add(f"{node['source_name']}.{node.get('name')}")
        nodes[node_id] = LineageNode(relation=_dbt_relation(node), names=sorted(names))
    child_map = manifest.get("child_map")
    if isinstance(child_map, dict):
        for node_id, children in child_map.items():
            if node_id in nodes and isinstance(children, list):
                nodes[node_id].children = sorted(str(c) for c in children if c in nodes)
    else:
        for node_id, node in raw.items():
            depends = node.get("depends_on")
            parents = depends.get("nodes") if isinstance(depends, dict) else None
            for parent in parents or []:
                if parent in nodes:
                    nodes[parent].children.append(node_id)
    return LineageGraph(tool="dbt", nodes=nodes)


def load_dbt(target_dir: Path) -> LineageGraph:
    try:
        return dbt_lineage(load_manifest(target_dir))
    except DbtArtifactError as exc:
        raise LineageError(f"{exc} — run `dbt parse` (or any dbt build) first") from exc


# ── SQLMesh ──────────────────────────────────────────────────────────────────


def _split_model_header(text: str) -> tuple[dict[str, str], str]:
    """(header properties, the SQL after it) for a SQLMesh model file. The
    header is ``MODEL (key value, …);``; each top-level property's first token
    is its key, the rest its value. No header returns ({}, text)."""
    from sqlglot import TokenType
    from sqlglot.tokens import Tokenizer

    tokens = Tokenizer().tokenize(text)
    if len(tokens) < 2 or tokens[0].text.upper() != "MODEL":
        return {}, text
    props: dict[str, str] = {}
    segment: list[str] = []
    depth = 0
    for token in tokens[1:]:
        kind = token.token_type
        if kind == TokenType.R_PAREN:
            depth -= 1
        if (depth == 0 and kind == TokenType.R_PAREN) or (depth == 1 and kind == TokenType.COMMA):
            if segment:
                props[segment[0].lower()] = "".join(segment[1:])
            segment = []
        elif depth >= 1:
            segment.append(token.text)
        elif kind == TokenType.SEMICOLON:
            return props, text[token.end + 1 :]
        if kind == TokenType.L_PAREN:
            depth += 1
    raise LineageError("MODEL header is not closed with `);`")


def _python_model_name(text: str) -> str | None:
    """The name a ``@model("schema.table", …)`` decorator gives a Python
    model; None for a file without one. Raises SyntaxError."""
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and getattr(deco.func, "id", getattr(deco.func, "attr", None)) == "model"
                    and deco.args
                    and isinstance(deco.args[0], ast.Constant)
                    and isinstance(deco.args[0].value, str)
                ):
                    return deco.args[0].value
    return None


def _sqlmesh_default_dialect(project: Path) -> str | None:
    import yaml

    config = project / "config.yaml"
    if not config.is_file():
        return None
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    defaults = data.get("model_defaults") if isinstance(data, dict) else None
    dialect = defaults.get("dialect") if isinstance(defaults, dict) else None
    return str(dialect) if dialect else None


def load_sqlmesh(project: Path) -> LineageGraph:
    """The graph of a SQLMesh project's ``models/``: one node per model named
    in its MODEL header, edges from every table its query reads. Tables no
    model builds (external models, raw sources) become nodes too."""
    models_dir = project / "models"
    if not models_dir.is_dir():
        raise LineageError(f"no models/ directory in SQLMesh project {project}")
    default_dialect = _sqlmesh_default_dialect(project) or ""
    nodes: dict[str, LineageNode] = {}
    reads: dict[str, set[str]] = {}
    for path in sorted(models_dir.rglob("*")):
        rel = path.relative_to(project).as_posix()
        if path.suffix == ".py":
            try:
                name = _python_model_name(path.read_text(encoding="utf-8"))
            except SyntaxError:
                nodes[rel] = LineageNode(walked=False, note=f"Python file {rel} does not parse")
                continue
            if name:
                relation = ".".join(parts(name))
                nodes[relation] = LineageNode(
                    relation=relation, walked=False, note=f"Python model {rel}, inputs not read"
                )
            continue
        if path.suffix != ".sql":
            continue
        text = path.read_text(encoding="utf-8")
        try:
            header, body = _split_model_header(text)
        except Exception as exc:  # noqa: BLE001 — a header the tokenizer rejects is reported
            raise LineageError(f"{rel}: MODEL header does not parse: {exc}") from exc
        if "name" not in header:
            continue  # not a model file (a macro or audit kept under models/)
        relation = ".".join(parts(header["name"]))
        try:
            reads[relation] = source_tables(body, header.get("dialect") or default_dialect)
            nodes[relation] = LineageNode(relation=relation)
        except Exception as exc:  # noqa: BLE001 — sqlglot raises several error types
            first = str(exc).splitlines()[0][:120]
            nodes[relation] = LineageNode(
                relation=relation, walked=False, note=f"{rel} query does not parse: {first}"
            )
    for child, tables in reads.items():
        for table in tables:
            # A short reference names every model it could be; a table no
            # model builds (an external model, a raw source) becomes a node.
            parents = [n for n, node in nodes.items() if same_table(node.relation, table)]
            if not parents:
                parents = [table]
                nodes[table] = LineageNode(relation=table)
            for parent in parents:
                nodes[parent].children = sorted({*nodes[parent].children, child})
    for node in nodes.values():
        node.names = [node.relation.rsplit(".", 1)[-1]] if node.relation else []
    return LineageGraph(tool="sqlmesh", nodes=nodes)


# ── configuration ────────────────────────────────────────────────────────────


def load_lineage(tool: str, path: Path) -> LineageGraph:
    if tool == "dbt":
        target = path if (path / "manifest.json").is_file() else path / "target"
        if not (target / "manifest.json").is_file() and not (path / "dbt_project.yml").is_file():
            target = path
        return load_dbt(target)
    if tool == "sqlmesh":
        return load_sqlmesh(path)
    raise LineageError(f"lineage tool must be dbt or sqlmesh, not '{tool}'")


def detect_lineage(path: Path) -> LineageGraph:
    """``--lineage PATH``: a dbt target dir or project (manifest.json in it or
    in its target/), else a SQLMesh project (config.yaml/config.py)."""
    if (path / "manifest.json").is_file() or (path / "dbt_project.yml").is_file():
        return load_lineage("dbt", path)
    if any((path / f).is_file() for f in ("config.yaml", "config.py")):
        return load_lineage("sqlmesh", path)
    raise LineageError(
        f"{path} is neither a dbt target/project (manifest.json, dbt_project.yml) nor a "
        "SQLMesh project (config.yaml or config.py)"
    )
