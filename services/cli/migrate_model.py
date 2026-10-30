"""`dst migrate-model` — write the project's entity catalogue as a Cube model.

Reads semantic/ (entities, relationships, definitions, table blueprints),
lenses/ and the probe under --dir, and writes cube/model/*.yml, the views,
cube/overlay.yaml and cube/MIGRATION.md (everything the Cube model does not
carry as authored, with its failure mode). The output is generated once and
then authored: a re-run rewrites only files it generated and nobody edited
since, and refuses otherwise (exit 1, naming each file). Exit 0 when the
output is written or already current; with --check, nothing is written, the
diff is printed, and the exit is 0 when nothing would change and 2 when
something would. Both end with the steps that take the project from files to
serving through Cube, each marked done when the project already shows it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from services.cli import style


def register(sub: Any) -> None:
    p = sub.add_parser(
        "migrate-model",
        help="write the project's entities, metrics, relationships and lenses as a Cube "
        "model (cubes, views) plus the dst overlay, with a report of what Cube cannot carry",
    )
    p.add_argument("--dir", default=".", help="project root (semantic/, lenses/); default: .")
    p.add_argument("--out", default=None, help="where the model goes; default: cube/ under --dir")
    p.add_argument(
        "--check",
        "--dry-run",
        dest="check",
        action="store_true",
        help="print the diff of what would be written; write nothing. Exit 2 when "
        "something would change, 0 when the model is current",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite files edited since they were generated (their edits are lost)",
    )
    p.set_defaults(fn=_migrate)


def _migrate(args: argparse.Namespace) -> int:
    from services.project.migrate_cube import REPORT, plan_migration, write_migration

    root = Path(args.dir)
    if not (root / "semantic" / "entities").is_dir():
        print(
            f"error: no semantic/entities/ in {root.resolve()} — nothing to migrate "
            "(name the project with --dir PATH)",
            file=sys.stderr,
        )
        return 1
    out = Path(args.out) if args.out else root / "cube"
    plan = plan_migration(root, out)
    for rel, action in sorted(plan.actions.items()):
        if action == "unchanged":
            continue
        glyph = {
            "create": style.good("+"),
            "update": style.warn("~"),
            "remove": style.bad("-"),
            "kept": style.dim("="),
        }
        refused = (
            " (edited since generated)"
            if rel in plan.refused
            else " (authored, kept)"
            if action == "kept"
            else ""
        )
        print(f"  {glyph[action]} {out / rel}{refused}")
    counts = {
        a: sum(1 for x in plan.actions.values() if x == a) for a in set(plan.actions.values())
    }
    summary = ", ".join(f"{n} {a}" for a, n in sorted(counts.items()))
    carried = len(plan.notes)
    if plan.refused and not args.force:
        print(
            style.bad(
                f"error: {len(plan.refused)} file(s) under {out} were edited after "
                "`dst migrate-model` wrote them — the model is authored now; nothing was "
                "written. Re-run with --force to overwrite them (the edits are lost)."
            ),
            file=sys.stderr,
        )
        return 1
    if args.check:
        for line in _diff(plan):
            print(line)
        print(f"Migration (check): {summary}; {carried} item(s) carried outside Cube.")
        _next_steps(root, out)
        return 2 if plan.changed else 0
    write_migration(plan)
    print(
        style.bold(f"Migration: {summary}.")
        + f" {carried} item(s) carried outside the Cube model — see {out / REPORT}"
    )
    sidecar = out / "cube.js"
    if not sidecar.exists():
        from services.cli.init import _cube_js

        sidecar.write_text(_cube_js(), encoding="utf-8")
        print(f"  {style.good('+')} {sidecar} (the sidecar's config; yours from now on)")
    _next_steps(root, out)
    return 0


def _diff(plan: Any) -> list[str]:
    """A unified diff per file that would change; a new file by its size."""
    import difflib

    out: list[str] = []
    for rel, action in sorted(plan.actions.items()):
        target = plan.out / rel
        if action == "create":
            out.append(f"+++ {target} (new, {len(plan.files[rel].splitlines())} lines)")
        elif action in ("update", "remove"):
            before = target.read_text(encoding="utf-8").splitlines(keepends=True)
            after = plan.files[rel].splitlines(keepends=True) if action == "update" else []
            out += [
                line.rstrip("\n")
                for line in difflib.unified_diff(before, after, f"{target}", f"{target} (new)")
            ]
    return out


def _next_steps(root: Path, out: Path) -> None:
    """From files to serving through Cube, in order; a step the project
    already shows is marked done."""
    import yaml

    from services.project.migrate_cube import REPORT

    def has_cube_block() -> bool:
        try:
            doc = yaml.safe_load((root / "dst.yaml").read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return False
        return isinstance(doc, dict) and isinstance(doc.get("cube"), dict)

    def has_secret() -> bool:
        try:
            env = (root / ".env").read_text(encoding="utf-8")
        except OSError:
            return False
        return any(line.startswith("CUBEJS_API_SECRET=") for line in env.splitlines())

    def has_sidecar() -> bool:
        for name in ("docker-compose.yml", "compose.yml", "docker-compose.yaml", "compose.yaml"):
            try:
                doc = yaml.safe_load((root / name).read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(doc, dict) and "cube" in (doc.get("services") or {}):
                return True
        return False

    def mark(done: bool) -> str:
        return style.good("[x]") if done else "[ ]"

    steps = [
        (
            has_cube_block(),
            "dst.yaml: a `cube:` block — `cube: {url: http://localhost:4000, "
            "api_secret_env: CUBEJS_API_SECRET}`",
        ),
        (has_secret(), ".env: CUBEJS_API_SECRET=<a long random string> (dst signs with it)"),
        (
            has_sidecar(),
            "docker-compose.yml: the pinned `cube` service (the Migrate to Cube guide has it), "
            f"mounting {out / 'model'} and {out / 'cube.js'} read-only",
        ),
        (False, "start it: `docker compose --profile cube up -d`, then `dst cube status`"),
        (False, f"read {out / REPORT}: every item the Cube model does not carry"),
        (False, "`dst probe --cube`: value dictionaries and data ends through Cube"),
        (False, "`dst plan`: the overlay against Cube; each lens prints its lane (cube)"),
        (False, "`dst apply`"),
        (False, "`dst monitor`, then `dst apply`: data ends and feeds reach the answers"),
        (False, "`dst test`: the certified answers and eval cases through the Cube lane"),
    ]
    print("Next steps:")
    for i, (done, text) in enumerate(steps, 1):
        print(f"  {mark(done)} {i}. {text}")
