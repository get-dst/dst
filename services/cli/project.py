"""The `dst export`, `dst plan`, `dst apply` commands: the file-first deployment door."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.cli import style
from services.cli._common import (
    _client,
    _declared_connection,
    _declared_warehouses,
    _drift_tolerance,
    _read_project,
)

if TYPE_CHECKING:  # httpx is imported per-verb (CLI startup cost) — types only here
    from collections.abc import Callable

    import httpx

    from services.project.lineage import LineageGraph

# How long `plan` will wait on the warehouse before it drops the staleness check
# and says nothing. plan is the verb an engineer runs every day and the check is a
# courtesy, so a warehouse that accepts the connection and then never answers must
# cost this much and not one second more.
PLAN_WAREHOUSE_TIMEOUT = 5.0


def _connections_snippet(connections: list[dict[str, object]]) -> str:
    """DB connections as a dst.yaml SNIPPET — printed, never written: the
    user's dst.yaml is authored (comments, env refs) and stays theirs.
    Secrets never leave the server; a stored one renders as the secret_env
    line to fill (the init convention: DST_API_KEY_<NAME>)."""
    import yaml

    lines = ["connections:"]
    for c in connections:
        lines.append(f"  {c['name']}:")
        lines.append(f"    type: {c['type']}")
        if c.get("config"):
            flow = yaml.safe_dump(c["config"], default_flow_style=True).strip()
            lines.append(f"    config: {flow}")
        if c.get("has_secret"):
            env = "DST_API_KEY_" + str(c["name"]).upper().replace("-", "_")
            lines.append(f"    secret_env: {env}  # secret stays server-side; set this in .env")
    return "\n".join(lines)


def _comment_lines(text: str) -> list[str]:
    """The whole-line YAML comments in a file. Trailing comments ride on a line
    that also carries data, so they are counted with it rather than alone; a
    definition page's `#` is a markdown heading, never a comment, which is why
    only YAML is asked."""
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("#")]


def _refuses_comment_loss(
    args: argparse.Namespace,
    project: dict[str, str],
    files: dict[str, str],
    moved: dict[str, str],
) -> bool:
    """Name the comments this export is about to drop, BEFORE it drops them —
    and take y/N on it (`--yes` headless), the `dst lens rm` idiom.

    Export rewrites files from server state, and the server stores no comments:
    the provenance header of a certified_answers.yaml and the inline `# 200
    truncated a 2000-row answer` note in a lens.yaml both vanished, silently, on
    a command whose job is to be a safe round-trip. The loss is unavoidable —
    those bytes were never sent — so consent is the honest half. Nothing to lose
    (a fresh checkout, an uncommented tree) still asks nothing."""
    losing = {}
    for path, content in files.items():
        dest = moved.get(path, path)
        if not dest.endswith((".yaml", ".yml")):
            continue
        old = project.get(dest)
        if old is None:
            continue
        keeping = set(_comment_lines(content))
        lost = [c for c in _comment_lines(old) if c not in keeping]
        if lost:
            losing[dest] = lost
    if not losing:
        return False
    print(
        "these files are rewritten from server state, and the server stores no "
        "comments — export will drop:"
    )
    for dest, lost in sorted(losing.items()):
        print(f"  {dest}: {len(lost)} comment line(s), e.g. {lost[0][:70]}")
    if args.yes:
        return False
    if not sys.stdin.isatty():
        print("error: refusing without confirmation — pass --yes", file=sys.stderr)
        return True
    if input("overwrite? y/N: ").strip().lower() != "y":
        print("aborted — nothing written")
        return True
    return False


def _export(args: argparse.Namespace) -> int:
    import httpx

    url, headers = _client(args)
    params = [("lens", name) for name in (args.lens or [])]
    r = httpx.get(f"{url}/mgmt/project/export", headers=headers, params=params, timeout=60)
    if r.status_code == 404:
        print(f"error: {r.json()['detail']}", file=sys.stderr)
        return 1
    from services.project.loader import existing_asset_paths

    out = r.raise_for_status().json()
    files = out["files"]
    # --out redirects the whole tree — the safe path (export elsewhere, diff,
    # then adopt). It used to be accepted and silently ignored here, so the
    # flag that meant "write output here" overwrote the project instead.
    # All guards below run against the destination.
    root = Path(args.out) if args.out else Path(args.dir)
    project = _read_project(root)
    # The server renders every asset to its canonical path; this project may
    # already author it somewhere else (a folder, or the term's own spelling —
    # `customer_nodes.yaml`, not the slug's `customer-nodes.yaml`). Writing the
    # canonical path anyway left a SECOND page per asset and the next `dst
    # plan` exited 1 on a tree export itself had just written.
    moved = existing_asset_paths(project, files)
    if _refuses_comment_loss(args, project, files, moved):
        return 1
    import yaml

    for path, content in files.items():
        dest = root / moved.get(path, path)
        if (
            path.endswith(("certified_answers.yaml", "cases.yaml"))
            and not dest.exists()
            and not yaml.safe_load(content)
        ):
            # Never stub an empty list file into being: files win on apply, so a
            # `[]` this export creates would delete server-side answers on a
            # later, unrelated apply.
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    for path, here in sorted(moved.items()):
        print(f"{path} -> {here} (this project already authors that asset there)")
    print(f"exported {len(files)} files to {root.resolve()}/")
    if out.get("connections"):
        print("\n# server connections — merge into dst.yaml yourself (never auto-written):")
        print(_connections_snippet(out["connections"]))
    # Adoption summary: from here on the files govern these lenses. Absolute
    # paths — a relative `wrote lenses/...` in scrollback reads as "it went
    # where you asked" even when it didn't.
    for name in sorted({p.split("/")[1] for p in files if p.startswith("lenses/")}):
        print(f"wrote {root.resolve()}/lenses/{name}/ — commit it; future applies now govern it")
    return 0


def _project_files(args: argparse.Namespace, verb: str) -> dict[str, str]:
    """The project under --dir — or a named refusal, before any request.

    A directory with no project used to be indistinguishable from a project
    with no changes: `_read_project` returned {}, the server answered `[]`, and
    plan and apply both exited 0 with nothing to show. So a mistyped --dir, or a
    shell one level out of the project, reads as "it worked": an agent can take
    `exit 0` + `[]` as proof its correction landed. No dst.yaml and
    no lenses/ or semantic/ files is not an empty project, it is no project."""
    root = Path(getattr(args, "dir", ".") or ".")
    files = _read_project(root)
    if not files:
        print(
            f"error: no dst project in {root.resolve()} — no dst.yaml, and no "
            f"lenses/ or semantic/ files, so there is nothing to {verb}. Run `dst init` "
            "to create one here, or name the project with --dir PATH.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if "dst.yaml" not in files:
        # A real tree can lack one — `dst export` writes assets and never
        # authors dst.yaml — but then connections and providers are not in
        # this push, and a silent omission reads as a push that covered them.
        print(
            f"warning: no dst.yaml in {root.resolve()} — {len(files)} asset file(s) "
            f"in this {verb}; connections and providers are NOT part of it",
            file=sys.stderr,
        )
    for note in _probe_age_notes(files):
        print(f"warning: {note}", file=sys.stderr)
    return files


def _probe_age_notes(files: dict[str, str]) -> list[str]:
    """The push's probe artifacts past the project's ``probe.max_age`` — said on
    plan and apply both, since serving will say it on every answer. A dst.yaml
    that does not parse is the server's error to report, not this note's."""
    from services.project.probe import probe_age_warnings
    from services.project.schema import parse_project_yaml

    if "dst.yaml" not in files:
        return []
    try:
        project = parse_project_yaml(files["dst.yaml"])
    except ValueError:
        return []
    return probe_age_warnings(files, project.probe.max_age_delta)


def _warehouse_probe(args: argparse.Namespace, connection: str) -> dict[str, object]:
    """One `scope: warehouse` plan entry: has the warehouse moved since profiling?

    Deliberately the cheap half of `dst drift` — a catalog pass and a count.
    It does not load `semantic/`, does not cross-reference and does not format, so
    what plan pays is one metadata round-trip regardless of project size."""
    from services.lenses.connections import build_connector
    from services.lenses.profiler_catalog import catalog_profiles
    from services.project import warehouse_drift as wd

    root = Path(getattr(args, "dir", ".") or ".")
    entry: dict[str, object] = {"scope": "warehouse", "connection": connection, "drift": "armed"}
    baseline = wd.read_baseline(root, connection)
    if baseline is None:  # slugged to this file under another name
        return {
            **entry,
            "drift": "unarmed",
            "status": "unavailable",
            "note": "no baseline recorded",
        }
    entry["baseline_recorded_at"] = baseline.recorded_at.isoformat()
    type_, config, secret = _declared_connection(args, connection)
    if not type_:
        return {
            **entry,
            "status": "unavailable",
            "note": f"'{connection}' has a recorded baseline but is not declared in dst.yaml",
        }
    try:
        current = catalog_profiles(build_connector(type_, config, secret), connection)
    except Exception as exc:  # noqa: BLE001 — a courtesy check never fails a plan
        first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        return {**entry, "status": "unavailable", "note": f"did not profile from here ({first})"}
    deltas = wd.schema_delta_count(baseline.tables, current)
    return {
        **entry,
        "status": "changed" if deltas else "unchanged",
        "deltas": deltas,
        "note": (
            f"the warehouse has changed since profiling ({deltas} schema delta(s)) — "
            "run `dst drift`"
        ),
    }


def _warehouse_entries(args: argparse.Namespace) -> list[dict[str, object]]:
    """The staleness check, bounded twice over — plus the standing armed line.

    Every warehouse connection dst.yaml declares gets a drift entry: armed ones
    (a recorded baseline) pay one metadata round-trip for the staleness check;
    UNARMED ones cost nothing and say so — the silent unarmed state is the
    failure `dst drift`'s exit 4 exists for, and plan is where it stops being
    silent. Second bound: the whole probe runs on a daemon thread with
    one deadline, so a warehouse that hangs costs `PLAN_WAREHOUSE_TIMEOUT` and
    then plan carries on. Degradation is silence on the human path and a
    `status: unavailable` entry under --json — never an error, because the
    warehouse being unreachable from this laptop says nothing about whether the
    project is valid, which is what plan answers."""
    import threading

    from services.project import warehouse_drift as wd

    root = Path(getattr(args, "dir", ".") or ".")
    armed = wd.baseline_connections(root)
    declared = _declared_warehouses(root)
    # No dst.yaml (an export-only tree): the recorded baselines are all we know.
    unarmed = [n for n in declared or [] if n not in armed]
    connections = armed
    out: list[dict[str, object]] = [
        {
            "scope": "warehouse",
            "connection": name,
            "drift": "unarmed",
            "status": "unarmed",
            "note": "drift UNARMED — run `dst probe` to record the baseline",
        }
        for name in unarmed
    ]
    if not connections:
        return out

    def probe() -> None:
        for name in connections:
            out.append(_warehouse_probe(args, name))

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(PLAN_WAREHOUSE_TIMEOUT)
    if worker.is_alive():
        # One snapshot, because the thread we gave up on is still appending to it.
        out = list(out)
        done = {str(e.get("connection")) for e in out}
        return out + [
            {
                "scope": "warehouse",
                "connection": name,
                "status": "unavailable",
                "note": f"no answer within {PLAN_WAREHOUSE_TIMEOUT:g}s — skipped",
            }
            for name in connections
            if name not in done
        ]
    return out


def _selection_line(entry: dict[str, Any]) -> str:
    """The resolved --select/--scope, printed before anything it selected."""
    selectors = " ".join(entry.get("selectors") or []) or "(everything)"
    lenses = ", ".join(entry.get("lenses") or []) or "none"
    assets = ", ".join(entry.get("assets") or []) or "none"
    line = (
        f"selection: {selectors} (scope: {entry.get('kinds')}) → lenses: {lenses}; "
        f"assets: {assets}; {entry.get('excluded', 0)} file(s) left out"
    )
    paths = dict.fromkeys(
        "  "
        + " → ".join(
            str(p[k]) for k in ("table", "entity", "definition", "lens") if p.get(k) is not None
        )
        for p in entry.get("paths") or []
    )
    return "\n".join([line, *paths, *(f"  ! {n}" for n in entry.get("notes") or [])])


def _render_plan(plan: list[dict[str, Any]], write: Callable[[str], None]) -> int:
    """Print the server's plan; return how many files apply would reject.

    Split out from `_plan` so `--json` can reuse the counting without the prose —
    two implementations of "would apply reject this?" is one too many."""
    invalid = 0
    for entry in plan:
        if entry.get("scope") == "selection":
            write(_selection_line(entry))
            continue
        if entry.get("scope") == "server_only":
            write(f"server-only: {entry['note']}")
            continue
        if entry.get("scope") == "unchecked":
            # A clean plan is not a clean bill of health — say what it could not
            # check, or a green exit 0 reads as one (regression sweep, finding 2).
            write("not checked by plan — apply still runs these, and they need a live warehouse")
            for check in entry["checks"]:
                write(f"  - {check}")
            continue
        if entry.get("scope") == "project":
            invalid += 1
            write(f"dst.yaml: invalid — {entry['error']}")
            continue
        if entry.get("scope") == "cube":
            invalid += _render_cube_row(entry, write)
            continue
        if entry.get("scope") == "serving":
            write(_serving_line(entry))
            continue
        if entry.get("scope") == "semantic":
            if entry.get("status") == "hint":
                write(f"hint: {entry['hint']}")
            elif entry.get("status") == "warning":
                write(f"warning: {entry['warning']}")
            elif entry.get("status") == "coverage":
                write(str(entry["note"]))
            elif entry.get("status") == "orphan":
                write(f"orphan: {entry['note']}")
            elif entry.get("error"):
                invalid += 1
                where = f"{entry['path']}: " if entry.get("path") else "semantic: "
                write(f"{where}invalid — {entry['error']}")
            else:
                write(f"{entry['path']}: {entry['status']}")
                if entry.get("diff"):
                    write(entry["diff"])
            continue
        errors = entry.get("errors") or ([entry["error"]] if entry.get("error") else [])
        if entry.get("status") == "invalid" or errors:
            invalid += 1
        # One error still reads on the status line; a lens rejected on several
        # gates at once gets one per line — apply prints them that way, and a
        # semicolon-joined wall of five is what people stop reading.
        note = f" — {errors[0]}" if len(errors) == 1 else ""
        if entry.get("stale"):
            note += (
                f" — STALE compile (shared changed: {', '.join(entry['stale'])})"
                " → will recompile on apply"
            )
        write(f"{entry['lens']}: {entry['status']}{note}")
        if len(errors) > 1:
            for message in errors:
                write(f"  - {message}")
        # A behaviour change with no file behind it: `!` marks the one line the
        # rest of a plan cannot express — nothing here is a diff to apply.
        for line in entry.get("semantics", []):
            write(f"  ! {line}")
        # The grant resolved at apply: how many tables the lens reads without
        # definitions, and the one warning that makes the grant unreachable.
        if entry.get("exposes"):
            write(f"  {entry['exposes']}")
        for line in entry.get("warnings", []):
            write(f"  ! {line}")
        if entry.get("certified"):  # re-verify is a human act — the plan only names them
            write(f"  {entry['certified']['note']}")
            for q in entry["certified"]["questions"]:
                write(f"    re-verify: {q}")
        for d in entry.get("diffs", []):
            write(d["diff"] or f"  + {d['path']}")
    return invalid


def _serving_line(entry: dict[str, Any]) -> str:
    """The lane each lens serves on: ``serving: a → cube (Cube-backed), …``."""
    lanes = entry.get("lanes") or {}
    return "serving: " + ", ".join(
        f"{name} → {row['lane']} ({row['why']})" for name, row in lanes.items()
    )


def _render_cube_row(entry: dict[str, Any], write: Callable[[str], None]) -> int:
    """One Cube-side plan row; returns 1 for an error apply would refuse on."""
    where = f"cube ({entry['lens']})" if entry.get("lens") else "cube"
    status = entry.get("status")
    if status == "keys":
        write(
            f"{where}: model {str(entry['model_version'])[:12]}, overlay "
            f"{str(entry['overlay_version'])[:12]}, views {', '.join(entry.get('views') or [])}"
        )
        write("  meta.dst keys read:")
        for row in entry.get("keys") or []:
            write(f"    {row['on']:<9} {row['key']:<16} {row['members']:>4}  {row['reader']}")
        return 0
    mode = f" [{entry['mode']}]" if entry.get("mode") else ""
    if status == "invalid":
        write(f"{where}: invalid — {entry['error']}{mode}")
        return 1
    if status == "warning":
        write(f"warning: {where}: {entry['warning']}{mode}")
    elif status == "hint":
        write(f"hint: {where}: {entry['hint']}{mode}")
    return 0


_PLAN_GLYPHS = {
    "create": ("+", style.good),
    "update": ("~", style.warn),
    "invalid": ("✗", style.bad),
    "stale": ("!", style.warn),
    "certified-stale": ("!", style.warn),
    "orphan": ("-", style.bad),
}


def _summarize_plan(plan: list[dict[str, Any]]) -> None:
    """The terraform-shaped default: one row per asset that needs attention,
    then the counts line — diffs ride behind --full. A ~2k-line full plan was
    the daily read; the summary is the read, --full the review."""
    counts: dict[str, int] = {}
    rows: list[str] = []
    hints = 0
    coverage: str | None = None

    def row(status: str, name: str, note: str = "") -> None:
        glyph, paint = _PLAN_GLYPHS.get(status, ("~", style.warn))
        head = paint(glyph) + f" {name}"
        rows.append(f"  {head}" + (f" — {note}" if note else ""))

    for entry in plan:
        if entry.get("scope") == "selection":
            rows.append(_selection_line(entry))
            continue
        if entry.get("scope") == "server_only":
            # Verbatim, not a glyph row: the note IS the adoption pointer.
            rows.append(f"server-only: {entry.get('note')}")
            continue
        if entry.get("scope") == "unchecked":
            continue  # printed verbatim below the rows — it is a guarantee statement
        if entry.get("scope") == "project":
            counts["invalid"] = counts.get("invalid", 0) + 1
            row("invalid", "dst.yaml", str(entry.get("error") or ""))
            continue
        if entry.get("scope") == "serving":
            rows.append(_serving_line(entry))
            continue
        if entry.get("scope") == "cube":
            where = f"cube ({entry['lens']})" if entry.get("lens") else "cube"
            status = str(entry.get("status") or "")
            if status == "invalid":
                counts["invalid"] = counts.get("invalid", 0) + 1
                row("invalid", where, f"{entry.get('error')} [{entry.get('mode')}]")
            elif status == "warning":
                rows.append("  " + style.warn("!") + f" {where}: {entry.get('warning')}")
            elif status == "hint":
                hints += 1
            elif status == "keys":
                unread = [k["key"] for k in entry.get("keys") or [] if "not read" in k["reader"]]
                note = f"; carried but not read yet: {', '.join(unread)}" if unread else ""
                rows.append(
                    f"    {where}: model {str(entry.get('model_version'))[:12]}, "
                    f"{len(entry.get('keys') or [])} meta.dst key(s) read{note} (--full lists them)"
                )
            continue
        if entry.get("scope") == "semantic":
            status = str(entry.get("status") or "")
            if status == "hint":
                hints += 1
                continue
            if status == "warning":
                rows.append("  " + style.warn("!") + f" {entry.get('warning')}")
                continue
            if status == "coverage":
                coverage = str(entry.get("note") or "")
                continue
            if status == "orphan":
                row("orphan", str(entry.get("note") or ""))
                continue
            if entry.get("error"):
                counts["invalid"] = counts.get("invalid", 0) + 1
                path = str(entry.get("path") or "semantic")
                note = str(entry["error"]).removeprefix(f"{path}: ")
                row("invalid", path, note)
                continue
            counts[status] = counts.get(status, 0) + 1
            if status != "unchanged":
                row(status, str(entry.get("path") or ""))
            continue
        # lens rows
        status = str(entry.get("status") or "")
        errors = entry.get("errors") or ([entry["error"]] if entry.get("error") else [])
        stale_note = (
            f"STALE compile (shared changed: {', '.join(entry['stale'])})"
            if entry.get("stale")
            else ""
        )
        if status == "invalid" or errors:
            # The state alone ("invalid", "STALE compile") names no cause: every
            # error apply would reject on prints here, one per line.
            counts["invalid"] = counts.get("invalid", 0) + 1
            errors = errors or ["no reason on the row — --json shows it whole"]
            head = " — ".join(n for n in (str(errors[0]), stale_note) if n)
            row("invalid", str(entry.get("lens") or ""), head)
            for message in errors[1:]:
                rows.append(f"      {message}")
            continue
        counts[status] = counts.get(status, 0) + 1
        note = stale_note
        if status != "unchanged" or note:
            row(entry.get("status") or "update", str(entry.get("lens") or ""), note or status)
        for line in entry.get("semantics", []):
            rows.append("  " + style.warn("!") + f" {line}")
        if entry.get("exposes"):
            rows.append(f"    {entry.get('lens')}: {entry['exposes']}")
        for line in entry.get("warnings", []):
            rows.append("  " + style.warn("!") + f" {entry.get('lens')}: {line}")
        if entry.get("certified"):
            n = len(entry["certified"].get("questions") or [])
            rows.append(
                "  " + style.warn("!") + f" {entry.get('lens')}: {n} certified answer(s) "
                "need re-verify (--full lists them)"
            )

    for line in rows:
        print(line)
    if rows:
        print()
    for entry in plan:  # the unchecked block survives summarization verbatim
        if entry.get("scope") == "unchecked":
            print("not checked by plan — apply still runs these, and they need a live warehouse")
            for check in entry["checks"]:
                print(f"  - {check}")
    parts = []
    for key, label in (
        ("create", "to add"),
        ("update", "to change"),
        ("invalid", "invalid"),
        ("stale", "stale"),
        ("unchanged", "unchanged"),
    ):
        if counts.get(key):
            parts.append(f"{counts[key]} {label}")
    if hints:
        parts.append(f"{hints} hint(s)")
    summary = "Plan: " + (", ".join(parts) if parts else "nothing to do") + "."
    print(style.bold(summary), style.dim("(--full shows diffs and hints)"))
    if coverage:
        # The share of the tables in scope whose grain and key are declared:
        # the facts the grain check on an exposed answer reads.
        print(coverage[0].upper() + coverage[1:] + ".")


def _plan(args: argparse.Namespace) -> int:
    """Diff the project against the server — and say when the WAREHOUSE moved too.

    plan is the verb an engineer runs every day, which is the whole reason the
    staleness line lives here: on the morning the warehouse gains the column that
    invalidates a definition, `introspect`, `plan` and `test` are all structurally
    incapable of mentioning it. The line is a pointer, not the analysis —
    `dst drift` does the analysis."""
    import httpx

    if _nothing_selected(args, "plan"):
        return 0
    body: dict[str, Any] = {"files": _project_files(args, "plan")}
    url, headers = _client(args)
    params: dict[str, str | list[str]] = {}
    if selectors := _selectors(args):
        params["select"] = selectors
        if (lineage := _lineage(args, body["files"], selectors)) is not None:
            body["lineage"] = lineage
    if (scope := getattr(args, "scope", None) or "all") != "all":
        params["scope"] = scope
    try:
        plan_url = f"{url}/mgmt/project/plan"
        if params:
            r = httpx.post(
                plan_url, headers=headers, params=params, json=body, timeout=args.timeout
            )
        else:  # an unselected plan sends no query string at all
            r = httpx.post(plan_url, headers=headers, json=body, timeout=args.timeout)
    except httpx.TimeoutException:
        # A raw ReadTimeout traceback tells the operator nothing.
        # plan takes no lock and writes nothing — say so, and name the knob.
        print(
            f"error: no response from {url} within {args.timeout}s — plan is read-only "
            "and changed nothing. A large project or a cold server needs longer: "
            "retry with --timeout SECONDS.",
            file=sys.stderr,
        )
        return 1
    if r.status_code == 422:
        print(f"error: {_error_detail(r) or r.text[:300]}", file=sys.stderr)
        return 1
    plan = r.raise_for_status().json()
    warehouse = _warehouse_entries(args)
    invalid = _render_plan(plan, lambda _line: None)  # the one count, however rendered
    if args.as_json:
        print(json.dumps(plan + warehouse, indent=2, ensure_ascii=False))
    elif getattr(args, "full", False):
        _render_plan(plan, _print_plan_line)
    else:
        _summarize_plan(plan)
    if not args.as_json:
        # The standing drift line, one per declared warehouse connection: armed
        # with its baseline date, or UNARMED loudly — the silent unarmed state
        # is exactly what this line retires. Staleness stays quieter:
        # only `changed` earns its extra line; `unchanged` is the daily case and
        # `unavailable` is a degradation of a courtesy — readable under --json,
        # which is where a caller that needs to know goes looking.
        for entry in warehouse:
            name = entry.get("connection")
            if entry.get("drift") == "unarmed":
                print(style.warn(f"drift: '{name}' UNARMED — run `dst probe`"))
            else:
                date = str(entry.get("baseline_recorded_at") or "")[:10]
                print(style.dim(f"drift: '{name}' armed (baseline {date})"))
            if entry.get("status") == "changed":
                print(style.accent("warehouse:") + f" '{entry['connection']}' — {entry['note']}")
        # The scaffolded skills are the author's files, so plan has no server
        # view of them — but plan is where an author is, and an init-time
        # snapshot that silently outlives its release is the defect (#51).
        # Dim, one line, and only when something actually differs.
        from services.cli.init import stale_agent_note

        if note := stale_agent_note(Path(getattr(args, "dir", ".") or ".")):
            print(style.dim(note))
    if invalid:
        # A plan that predicts a rejected apply must not exit 0 — the one line
        # saying so scrolled past under a screenful of clean create-diffs, and
        # the agent reading the exit code went straight to apply (blocker 5).
        print(
            style.bad(
                f"error: {invalid} file(s) would be REJECTED by apply — fix them above; "
                "nothing was changed",
                sys.stderr,
            ),
            file=sys.stderr,
        )
        return 1
    return 0


def _print_plan_line(line: str) -> None:
    """--full's writer: today's byte layout, with diff/verdict lines painted."""
    if "\n" in line:
        print("\n".join(style.diff_line(part) for part in line.splitlines()))
    elif ": invalid — " in line or line.startswith("dst.yaml: invalid"):
        print(style.bad(line))
    elif "STALE compile" in line:
        print(style.warn(line))
    else:
        print(line)


_APPLY_IN_FLIGHT = (
    "the server may still be applying — it holds the org apply lock and will commit "
    "when done. Do not re-apply until it does (a second apply gets 409 while the "
    "lock is held). Check liveness with `curl <server>/ready` FIRST: if /ready "
    "does not answer within seconds, the server is wedged, not working — restart "
    "it (a dead warehouse socket can hang the whole process; "
    "`dst plan` polls the SAME server, so it cannot distinguish the two states)."
)


def _error_detail(r: httpx.Response) -> str | None:
    """The server's own `detail` on an error response, or None for a body that
    is not dst's (a proxy's HTML or text page)."""
    try:
        detail = r.json().get("detail")
    except Exception:  # noqa: BLE001 — non-JSON error body
        return None
    return detail if isinstance(detail, str) else None


def _summarize_apply(out: list[dict[str, Any]], quiet: bool = False) -> None:
    """The terraform-shaped apply report: grouped sections, warnings and errors
    painted, one counts line — the raw row array (agents' shape) is --json.
    Rendering is defensive on purpose: server rows are an open dict contract,
    and an unknown row must degrade to its scope name, never crash the CLI.

    ``quiet`` keeps only what needs a human: rejected lenses,
    errors, deletions, each DISTINCT warning once with who it fired on, the
    counts line and the gate footer — a routine apply over a few dozen lenses
    prints hundreds of warning lines, and the noise floor is where a genuinely
    new one hides."""
    published = rejected = warnings_n = errors_n = 0
    # The same sentence repeated once per lens buries the two lines that matter:
    # most of a big apply's warnings are one warning fired many times, and the
    # single connection-probe diagnostic scrolls off the top. A warning identical
    # across 3+ rows prints once, counted; --json keeps every row verbatim.
    warn_counts: dict[str, int] = {}
    warn_where: dict[str, list[str]] = {}
    for entry in out:
        row_name = str(entry.get("lens") or entry.get("scope") or "")
        for note in entry.get("warnings") or []:
            warn_counts[str(note)] = warn_counts.get(str(note), 0) + 1
            warn_where.setdefault(str(note), []).append(row_name)
    deduped: set[str] = set()
    refusals: list[dict[str, Any]] = []
    for entry in out:
        if entry.get("scope") == "apply":
            continue  # the abort row becomes the banner below
        if entry.get("scope") == "selection":
            print(_selection_line(entry))
            continue
        head = str(entry.get("lens") or entry.get("scope") or "")
        action = str(entry.get("action") or "")
        version = entry.get("version")
        label = style.accent(f"lens {head}" if entry.get("lens") else head)
        loud = bool(entry.get("errors")) or action.startswith("rejected")
        if action.startswith("rejected"):
            refusals.append(entry)
        if entry.get("lens"):
            verdict = action or "unchanged"
            if action == "published":
                published += 1
                verdict = style.good(f"published v{version}" if version is not None else action)
            elif action.startswith("rejected"):
                rejected += 1
                verdict = style.bad(action)
            if not quiet or loud:
                print(f"{label}: {verdict}")
            # A deletion is the one staged count that must not be summarized
            # away: files-win removal of a certified answer printed only
            # "updated", and the operator's sole evidence lived in --json.
            # The server emits "deleted N" only when N > 0, so this is quiet
            # on every non-deleting apply — and it survives --quiet. The
            # --probe-certified count ("probed N of N") is the other staged
            # line a human must see on the lens row, not only in --json.
            for a in entry.get("applied") or []:
                staged = str(a)
                if "deleted" in staged or staged.startswith(
                    ("--probe-certified", "--refresh-samples")
                ):
                    if quiet and not loud:
                        print(f"{label}: {a}")
                    else:
                        print(f"  {a}")
        elif not quiet or loud:
            detail = action or ", ".join(str(a) for a in entry.get("applied") or []) or "ok"
            print(f"{label}: {detail}")
        if not quiet:
            for cap in entry.get("capabilities") or []:
                print(f"  {cap}")
        for note in entry.get("warnings") or []:
            warnings_n += 1
            if quiet:
                # Every distinct warning exactly once, naming who it fired on —
                # counted here, printed after the loop so order is stable.
                continue
            if warn_counts.get(str(note), 0) >= 3:
                if str(note) in deduped:
                    continue
                deduped.add(str(note))
                times = warn_counts[str(note)]
                print(
                    f"  {style.warn('warning:')} {note} "
                    + style.dim(f"(identical on {times} rows — --json for each)")
                )
                continue
            print(f"  {style.warn('warning:')} {note}")
        for err in entry.get("errors") or []:
            errors_n += 1
            print(f"  {style.bad('error:')} {err}")
    if quiet:
        for note, rows in warn_where.items():
            where = ", ".join(rows[:3]) + (f", +{len(rows) - 3} more" if len(rows) > 3 else "")
            print(f"{style.warn('warning:')} {note} {style.dim(f'({where})')}")
    if refusals and not quiet:
        # A big apply's report runs to hundreds of lines and a rejection's reason
        # scrolls past mid-stream, leaving only "1 rejected" on the counts line.
        # Repeat each refusal with its reasons (the row's own `errors`, as --json
        # carries them) right above that line, where it is read.
        print(style.bad("rejected:"))
        for entry in refusals:
            name = entry.get("lens") or entry.get("scope") or ""
            print(f"  {'lens ' if entry.get('lens') else ''}{name}: {entry.get('action')}")
            for err in entry.get("errors") or ["no reason on the row — --json shows it whole"]:
                print(f"    - {err}")
    bits = []
    if published:
        bits.append(f"{published} lens(es) published")
    if rejected:
        bits.append(f"{rejected} rejected")
    if warnings_n:
        bits.append(f"{warnings_n} warning(s)")
    bits.append(f"{errors_n} error(s)")
    line = "Apply complete. " if not (errors_n or rejected) else "Apply finished. "
    paint = style.good if not (errors_n or rejected) else style.bad
    print(paint(line.strip()), ", ".join(bits) + ".", style.dim("(--json for the row array)"))
    # One line for the safety net: 40 identical skip
    # warnings above are unreadable; whether the gates actually ran must be
    # legible without grepping. Skips break out by reason — "empty suite" is
    # benign, "provider error" means the scorer broke.
    gate_counts: dict[str, int] = {}
    for entry in out:
        g = entry.get("gate")
        if entry.get("lens") and g:
            gate_counts[str(g)] = gate_counts.get(str(g), 0) + 1
    if gate_counts:
        parts = [
            f"{n} {outcome}"
            for outcome, n in sorted(gate_counts.items())
            if not outcome.startswith("skipped")
        ]
        skipped = {k: n for k, n in gate_counts.items() if k.startswith("skipped")}
        if skipped:
            reasons = ", ".join(
                f"{n} {k[len('skipped (') : -1]}" for k, n in sorted(skipped.items())
            )
            parts.append(f"{sum(skipped.values())} skipped ({reasons})")
        gate_line = f"eval gates: {', '.join(parts)}"
        print(style.warn(gate_line) if skipped else gate_line)


def _apply(args: argparse.Namespace) -> int:
    import httpx

    if getattr(args, "action", None) == "status":
        return _apply_status(args)
    if _nothing_selected(args, "apply"):
        return 0
    body: dict[str, Any] = {"files": _project_files(args, "apply")}
    url, headers = _client(args)
    params: dict[str, str | list[str]] = {}
    if getattr(args, "probe_certified", False):
        params["probe_certified"] = "true"
    if getattr(args, "refresh_samples", False):
        params["refresh_samples"] = "true"
    if getattr(args, "require_gates", False):
        params["require_gates"] = "true"
    if selectors := _selectors(args):
        params["select"] = selectors
        if (lineage := _lineage(args, body["files"], selectors)) is not None:
            body["lineage"] = lineage
    if (scope := getattr(args, "scope", None) or "all") != "all":
        params["scope"] = scope
    # The server runs the apply as a job and answers with its id at once; this
    # client polls it, so a client timeout or a proxy cut never loses the
    # result. A server that predates jobs ignores the flag and answers rows.
    params["background"] = "true"
    # --allow-failing-cases is None when absent, [] when bare (blanket), and
    # a list of ids when it names the cases it covers.
    named = getattr(args, "allow_failing_cases", None)
    allow_cases = [
        c for c in [*(getattr(args, "allow_case", None) or []), *(named or [])] if c.strip()
    ]
    if named is not None or allow_cases:
        reason = (getattr(args, "reason", None) or "").strip()
        if not reason:
            print(
                "error: --allow-case / --allow-failing-cases needs --reason '…' — the "
                "audited one-off must say why (e.g. 'intended behaviour change: term "
                "now ambiguous')",
                file=sys.stderr,
            )
            return 2
        params["override_reason"] = reason
        if allow_cases:
            params["allow_case"] = allow_cases
        else:
            # The blanket form: still accepted, reported as blanket. Name the
            # cases instead — an override that names yesterday's red case cannot
            # silently cover today's new one.
            print(
                "note: --allow-failing-cases is blanket; prefer --allow-case <id> "
                "(repeatable) so the override names exactly the cases it covers",
                file=sys.stderr,
            )
            params["allow_failing_cases"] = "true"
    try:
        r = httpx.post(
            f"{url}/mgmt/project/apply",
            headers=headers,
            params=params or None,
            json=body,
            timeout=args.timeout,
        )
    except httpx.TimeoutException:
        print(
            f"error: no response from {url} within {args.timeout}s — {_APPLY_IN_FLIGHT} "
            "Raise --timeout SECONDS to wait longer.",
            file=sys.stderr,
        )
        return 1
    if r.status_code == 202:
        job_id = str(r.json()["job_id"])
        if getattr(args, "no_wait", False):
            print(f"apply job {job_id} started — `dst apply status {job_id}` shows its progress")
            return 0
        job = _wait_apply_job(args, url, headers, job_id)
        if job is None:
            return 1
        if job.get("state") == "failed":
            return _apply_failure(
                url, int(str(job.get("status_code") or 500)), str(job.get("detail") or ""), ""
            )
        return _apply_report(args, list(job.get("result") or []))
    if r.status_code >= 400:
        return _apply_failure(url, r.status_code, _error_detail(r), r.text)
    return _apply_report(args, r.json())


def _selectors(args: argparse.Namespace) -> list[str]:
    """--select selectors, with --lens NAME as the alias for lens/NAME and each
    name --tables-from reads as table/NAME+."""
    lenses = [f"lens/{n.strip()}" for n in (getattr(args, "lens", None) or []) if n.strip()]
    tables = [f"table/{n}+" for n in _tables_from(args)]
    if _select_all(args):  # `*` is the whole project: send no selector at all
        return []
    return [
        *(s.strip() for s in getattr(args, "select", None) or [] if s.strip()),
        *lenses,
        *tables,
    ]


def _select_all(args: argparse.Namespace) -> bool:
    """`--select '*'`: everything, the same as leaving --select out."""
    return "*" in (s.strip() for s in getattr(args, "select", None) or [])


def _tables_from(args: argparse.Namespace) -> list[str]:
    """The table or model names --tables-from FILE|- reads, one per line (the
    output of `dbt -q ls --output name`, a SQLMesh plan's changed models).
    Blank and # lines are skipped; a line that is not one name (a log line) is
    skipped and counted. Read once: stdin cannot be read twice."""
    import re

    source = getattr(args, "tables_from", None)
    if source is None:
        return []
    if (cached := getattr(args, "_table_names", None)) is not None:
        return list(cached)
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: --tables-from {source}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    names: list[str] = []
    skipped: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            (names if re.fullmatch(r"[^\s+]+", line) else skipped).append(line)
    if skipped:
        print(
            f"warning: --tables-from {source}: skipped {len(skipped)} line(s) that are not "
            f"one table or model name (first: {skipped[0][:60]!r}); `dbt -q ls` prints "
            "names only",
            file=sys.stderr,
        )
    args._table_names = names
    return names


def _nothing_selected(args: argparse.Namespace, verb: str) -> bool:
    """--tables-from read no name and nothing else was selected: say so, and
    the caller exits 0 without a request (an empty `dbt ls` is a normal CI
    state, and no selector at all would mean the whole project)."""
    if getattr(args, "tables_from", None) is None or _selectors(args) or _select_all(args):
        return False
    print(f"--tables-from {args.tables_from} names no table or model — nothing to {verb}")
    return True


def _lineage(args: argparse.Namespace, files: dict[str, str], selectors: list[str]) -> Any:
    """`_lineage_graph` as the JSON the project endpoints take."""
    graph = _lineage_graph(args, files, selectors)
    return None if graph is None else graph.model_dump(mode="json")


def _lineage_graph(
    args: argparse.Namespace, files: dict[str, str], selectors: list[str]
) -> LineageGraph | None:
    """The model DAG a `table/` selector walks: --lineage PATH (a dbt target
    or project, or a SQLMesh project) or dst.yaml's `lineage:` (relative to
    the project root). None without a table selector or a configured
    lineage; a lineage that is configured but unreadable is an error, never a
    silent walk over nothing."""
    from services.project.lineage import LineageError, detect_lineage, load_lineage
    from services.project.schema import parse_project_yaml

    if not any("table/" in s for s in selectors):
        return None
    try:
        if override := getattr(args, "lineage", None):
            graph = detect_lineage(Path(override))
        else:
            try:
                decl = parse_project_yaml(files.get("dst.yaml", "")).lineage
            except ValueError:
                return None  # the server names what is wrong with dst.yaml
            if decl is None:
                return None
            tool, where = decl.tool
            graph = load_lineage(tool, Path(getattr(args, "dir", ".") or ".") / where)
    except LineageError as exc:
        print(f"error: lineage: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    return graph


def _add_table_selection_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--tables-from",
        dest="tables_from",
        metavar="FILE",
        help="select table/NAME+ for every table or model name in FILE, one per line "
        "(- reads stdin): pipe `dbt -q ls -s state:modified+ --resource-type model "
        "--output name` or a SQLMesh plan's changed models",
    )
    p.add_argument(
        "--lineage",
        metavar="PATH",
        help="the model DAG table/ selectors walk: a dbt target directory (or project) "
        "or a SQLMesh project; overrides dst.yaml's `lineage:`",
    )


def _job_line(job: dict[str, Any]) -> str:
    done = job.get("progress") or []
    lenses = ", ".join(f"{p.get('lens')} {p.get('action')}" for p in done) or "no lens yet"
    return f"{job.get('state')} — {len(done)} lens(es): {lenses}"


def _wait_apply_job(
    args: argparse.Namespace, url: str, headers: dict[str, str], job_id: str
) -> dict[str, Any] | None:
    """Poll one apply job until it ends; print each lens as it lands. A poll
    that fails is retried — the job runs on the server either way — and when
    the client gives up it says how to re-attach, never that nothing landed."""
    import time

    import httpx

    seen = 0
    failures = 0
    quiet = bool(getattr(args, "quiet", False) or getattr(args, "as_json", False))
    while True:
        try:
            r = httpx.get(
                f"{url}/mgmt/project/apply/jobs/{job_id}", headers=headers, timeout=args.timeout
            )
            r.raise_for_status()
            job: dict[str, Any] = r.json()
            failures = 0
        except (httpx.HTTPError, ValueError) as exc:
            failures += 1
            if failures >= 10:
                print(
                    f"error: lost contact with {url} while apply job {job_id} ran ({exc}) — "
                    f"the apply continues on the server; `dst apply status {job_id}` "
                    "re-attaches",
                    file=sys.stderr,
                )
                return None
            time.sleep(2)
            continue
        progress = job.get("progress") or []
        if not quiet:
            for row in progress[seen:]:
                print(style.dim(f"  … {row.get('lens')}: {row.get('action')}"), file=sys.stderr)
        seen = len(progress)
        if job.get("state") in ("done", "failed"):
            return job
        time.sleep(1)


def _apply_status(args: argparse.Namespace) -> int:
    """`dst apply status <job id>`: the job's state and per-lens progress, and
    once it is done, the same report `dst apply` prints."""
    import httpx

    job_id = getattr(args, "job_id", None)
    if not job_id:
        print(
            "error: `dst apply status` needs the job id `dst apply --no-wait` printed",
            file=sys.stderr,
        )
        return 2
    url, headers = _client(args)
    r = httpx.get(f"{url}/mgmt/project/apply/jobs/{job_id}", headers=headers, timeout=args.timeout)
    if r.status_code == 404:
        print(f"error: no apply job '{job_id}' on {url}", file=sys.stderr)
        return 1
    job = r.raise_for_status().json()
    if getattr(args, "as_json", False) and job.get("state") != "done":
        print(json.dumps(job, indent=2, ensure_ascii=False))
        return 0
    print(f"apply job {job_id}: {_job_line(job)}")
    if job.get("state") == "failed":
        return _apply_failure(
            url, int(str(job.get("status_code") or 500)), str(job.get("detail") or ""), ""
        )
    if job.get("state") == "done":
        return _apply_report(args, list(job.get("result") or []))
    return 0


def _apply_failure(url: str, status_code: int, detail: str | None, body_text: str) -> int:
    """Name an apply's HTTP failure (from the request, or from a failed job)."""
    from services.runtime.bounded import APPLY_ABORTED_PREFIX

    if status_code in (502, 503, 504) and not (detail or "").startswith(APPLY_ABORTED_PREFIX):
        # A proxy's verdict, not the server's: upstream never answered, so the
        # apply may be running still. The one 5xx family that must NOT claim
        # "nothing was deployed" (the incident that taught this arrived as a 502).
        # dst's own 504 — a warehouse step past its deadline, rolled back — says
        # so in its detail and is reported below with what it says.
        print(
            f"error: apply got HTTP {status_code} from a gateway, not from dst — "
            f"{_APPLY_IN_FLIGHT}",
            file=sys.stderr,
        )
        return 1
    if status_code == 409:  # another apply holds the per-org lock
        # The holder is a live apply, not a stuck one — it will
        # commit. "Retry shortly" alone let the operator read the lock as
        # wreckage from the run they thought had failed.
        print(
            f"error: {detail} — that apply is still running and will "
            "commit when it finishes; `dst plan` shows committed state.",
            file=sys.stderr,
        )
        return 1
    # A server error must name itself, not become a raw traceback: a 500's
    # actual cause is only in the server's log, so say where to look. It
    # said "check serve.log", which nothing ever writes: the
    # server logs to the TERMINAL running it, so name that instead.
    where = "the server's log (locally: the terminal running `dst dev`/`serve`)"
    # Apply has been read as non-atomic ("half of it is live"). Measured: it is
    # not — semantic assets, lens publishes, eval cases and certified
    # upserts share ONE request transaction, and an unhandled error rolls it
    # all back (tests/test_certified_gates.py). Say so: the operator was
    # left guessing which stages survived, and guessed wrong.
    print(
        f"error: apply failed (HTTP {status_code}): "
        f"{detail or body_text[:300] or 'no detail'} — nothing was deployed "
        "(one transaction: certified answers, eval cases and the lens publish "
        f"rolled back together) — full traceback in {where}",
        file=sys.stderr,
    )
    return 1


def _apply_report(args: argparse.Namespace, out: list[dict[str, Any]]) -> int:
    """Render an apply's rows and turn them into the exit code."""
    if getattr(args, "as_json", False):
        # ensure_ascii=False: server rows carry prose warnings (em dashes, quotes);
        # \uXXXX escapes would make the one surface a CLI user reads unreadable.
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        _summarize_apply(out, quiet=bool(getattr(args, "quiet", False)))
    abort = next(
        (e for e in out if e.get("scope") == "apply" and e.get("action") == "aborted"), None
    )
    if abort is not None:
        # Blue/green: the server rolled the whole apply back — say it loudly,
        # with every error, after the report.
        print(style.bad(f"\nAPPLY ABORTED — {abort['detail']}", sys.stderr), file=sys.stderr)
        for entry in out:
            for err in entry.get("errors") or []:
                name = entry.get("lens") or entry.get("scope")
                print("  " + style.bad(f"{name}: {err}", sys.stderr), file=sys.stderr)
        return 1
    # Belt for older servers: non-zero on ANY error, not just rejected lenses —
    # a bulk certified import where every entry hit a gate must not look green
    # to CI/agents.
    ok = all(r.get("action") != "rejected" and not r.get("errors") for r in out)
    if ok:
        _advance_baselines(
            Path(getattr(args, "dir", ".") or "."),
            accept_drift=bool(getattr(args, "accept_drift", False)),
        )
    return 0 if ok else 1


def _advance_baselines(root: Path, *, accept_drift: bool = False) -> None:
    """A deploy is the moment the layer was reviewed against the warehouse as
    the probe artifact records it, so the drift baseline advances to that
    artifact here — the one other moment besides `dst drift --accept`. Only
    when the recorded facts actually differ: a deploy that moved nothing
    leaves no timestamp-only commit. And only when the artifact carries no
    BREAKING finding against the baseline, or `--accept-drift` was passed:
    a CI job that runs drift and then apply must not have apply bury the
    breakage drift just reported. The layer still lands either way; what is
    left alone is the baseline, and the report says so and why — on stdout,
    after the apply report, closing with one line that names what was left
    and the flag that accepts it. (On stderr it ran ahead of the piped
    report, and a log read from its tail showed a clean apply.)"""
    from services.project import probe as probe_artifact
    from services.project import warehouse_drift as wd

    sys.stdout.flush()
    project_files: dict[str, str] | None = None
    left: list[str] = []
    for path in sorted((root / wd.BASELINE_DIR).glob(f"*{probe_artifact.PROBE_SUFFIX}")):
        try:
            artifact = probe_artifact.parse_probe(path.read_text(encoding="utf-8"), str(path))
        except (ValueError, OSError):
            continue
        if project_files is None:
            project_files = _read_project(root)
        findings = wd.pending_findings(
            root, artifact.connection, artifact.tables, project_files, _drift_tolerance(root)
        )
        breaking = [f for f in findings if wd.is_breaking(f)]
        if breaking and not accept_drift:
            print(
                style.warn(
                    f"drift baseline for '{artifact.connection}' left alone: {len(breaking)} "
                    "BREAKING finding(s) against the pushed artifact — review with `dst drift`, "
                    "then `dst drift --accept` or `dst apply --accept-drift` once reviewed:"
                )
            )
            for finding in breaking:
                for line in wd.render(finding):
                    print("  " + line)
            left.append(f"{len(breaking)} BREAKING finding(s) on '{artifact.connection}'")
            continue
        # The same merge `dst drift --accept` performs: the artifact's catalog
        # with its measured facts, and the prior baseline's facts for columns
        # this artifact did not measure — never a null written over a number.
        written = wd.advance_baseline(root, artifact.connection, artifact.tables, artifact.tables)
        if written is not None:
            rel = written.relative_to(root) if written.is_relative_to(root) else written
            over = f" over {len(findings)} finding(s)" if findings else ""
            print(
                f"drift baseline advanced for '{artifact.connection}'{over} -> {rel}"
                + (":" if findings else "")
            )
            for line in wd.accepted_lines(findings):
                print(line)
    if left:
        print(
            style.warn(
                f"drift: {', '.join(left)} left in the baseline — the layer landed; "
                "`dst apply --accept-drift` accepts them once reviewed"
            )
        )


def register(sub: Any) -> None:
    for name, fn, help_ in [
        ("export", _export, "write every lens's file tree into a project dir"),
        ("plan", _plan, "diff a project dir against the server (dry run)"),
        ("apply", _apply, "apply a project dir to the server (files win)"),
    ]:
        p = sub.add_parser(name, help=help_)
        p.add_argument(
            "--env",
            help="target a named environment minted by `dst env new` — resolves "
            "its recorded admin token from .dst/envs.json; no secret changes "
            "hands (mutually exclusive with --token)",
        )
        if name == "export":
            p.add_argument(
                "--lens",
                action="append",
                help="export just this lens's tree (repeatable) — the adoption "
                "path for server-only lenses; default: the full project",
            )
            p.add_argument(
                "--yes",
                action="store_true",
                help="skip the confirmation when export would drop comments the "
                "server does not store (headless)",
            )
            p.add_argument(
                "--out",
                help="directory to write the lens tree into (default: the project dir)",
            )
        if name in ("plan", "apply"):
            p.add_argument(
                "--select",
                nargs="+",
                action="extend",
                metavar="SELECTOR",
                help="push only these nodes of the project graph: definition/NAME, "
                "entity/NAME, lens/NAME, table/NAME (the entities reading that table), a "
                "glob on the name (lens/finance_*), +X (X and what it reads), X+ (X and "
                "the lenses that read it; table/X+ walks the dbt or SQLMesh lineage down "
                "first), @changed (what differs from the server), * (everything). "
                "Unselected lenses are "
                "not validated or "
                "touched; a selection that changes an asset an unselected published "
                "lens reads is refused, naming it",
            )
            p.add_argument(
                "--lens",
                action="append",
                metavar="NAME",
                help="alias for --select lens/NAME (repeatable)",
            )
            _add_table_selection_args(p)
            p.add_argument(
                "--scope",
                choices=["all", "semantic", "certified", "cases", "config"],
                default="all",
                help="which kinds of files to push: semantic/**, certified_answers.yaml, "
                "evals/cases.yaml, or dst.yaml and other project files (default: all); "
                "--scope cases lands eval cases only and runs no gate",
            )
        if name == "apply":
            p.add_argument(
                "action",
                nargs="?",
                choices=["status"],
                help="`dst apply status JOB_ID`: a running or finished apply's state, "
                "per-lens progress and, when done, its report",
            )
            p.add_argument("job_id", nargs="?", help="the job id `dst apply --no-wait` printed")
            p.add_argument(
                "--no-wait",
                action="store_true",
                help="start the apply on the server and print its job id at once; "
                "`dst apply status JOB_ID` attaches",
            )
            p.add_argument(
                "--probe-certified",
                action="store_true",
                help="execute each NEW certified answer once (read-only, row-capped) "
                "to record its verified value — new entries only: answers already "
                "stored are not re-probed (re-author sql, or run `dst test` for a "
                "sweep); opt-in: costs one warehouse query per answer; a probe failure "
                "warns and stores the answer anyway",
            )
            p.add_argument(
                "--refresh-samples",
                action="store_true",
                help="re-record the sample result of every stored template: executes the "
                "sample-bound SQL once more and re-stamps verified value, bindings and "
                "terms; prints old -> new per template. The acceptance route for a "
                "template the eval gate reports as restated",
            )
            p.add_argument(
                "--accept-drift",
                action="store_true",
                help="advance the drift baseline to the pushed probe artifact even when "
                "it carries BREAKING findings against the recorded baseline; without "
                "it a clean apply still lands the layer but leaves the baseline alone "
                "and names the findings (same act as `dst drift --accept`)",
            )
            p.add_argument(
                "--json",
                dest="as_json",
                action="store_true",
                help="print the server's raw row array (the pre-2026-08 default) "
                "instead of the summarized report",
            )
            p.add_argument(
                "--require-gates",
                action="store_true",
                help="fail closed: abort the apply (non-zero exit) if any lens "
                "configured for an eval gate had it SKIPPED — empty suite, "
                "provider error, or unreachable warehouse alike; by default a "
                "skipped gate publishes with a warning (CI wants this flag)",
            )
            p.add_argument(
                "--quiet",
                action="store_true",
                help="print only what needs a human: rejected lenses, errors, each "
                "distinct warning once with a count, and the gate footer — a "
                "routine 42-lens apply is a handful of lines instead of hundreds",
            )
            p.add_argument(
                "--allow-failing-cases",
                nargs="*",
                action="extend",
                metavar="CASE_ID",
                help="publish once past a gate block WITHOUT editing eval_gate in the "
                "file — audited: requires --reason, which lands in the version history "
                "and the apply row. With case ids it covers exactly those cases "
                "(a certified divergence is `certified:<answer id>`) and applies only "
                "when every failing case is named; bare, it is blanket",
            )
            p.add_argument(
                "--allow-case",
                action="append",
                metavar="CASE_ID",
                help="publish once past a gate block for THIS failing case only "
                "(repeatable; the override applies only when every failing case is "
                "named) — audited: requires --reason",
            )
            p.add_argument(
                "--reason",
                help="why this red publish is intended (required with --allow-case / "
                "--allow-failing-cases), e.g. 'intended behaviour change: term now "
                "ambiguous — cases reconciled next push'",
            )
        if name in ("plan", "apply"):
            # Both were hard-coded. An apply that outruns the client is a
            # zombie — the operator needs the dial, not a traceback.
            p.add_argument(
                "--timeout",
                type=int,
                default=120 if name == "plan" else 300,
                help=f"seconds to wait for the server (default: {120 if name == 'plan' else 300})",
            )
        if name == "plan":
            p.add_argument(
                "--full",
                action="store_true",
                help="full per-file output with diffs (the pre-2026-08 default); "
                "without it plan prints the summarized rows + counts line",
            )
            p.add_argument(
                "--json",
                dest="as_json",
                action="store_true",
                help="print the plan as a parseable list instead of prose, including "
                "the `scope: warehouse` entry — that is where a warehouse the check "
                "could not reach is readable, since the human path stays silent on it",
            )
        p.add_argument("--dir", default=".")
        p.add_argument(
            "--url",
            help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
        )
        p.add_argument(
            "--token", help="a dstadm_ admin token (default: DST_ADMIN_TOKEN from env or ./.env)"
        )
        p.set_defaults(fn=fn)
