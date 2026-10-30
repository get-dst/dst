"""The `dst drift` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from services.cli import style
from services.cli._common import (
    _declared_connection,
    _declared_warehouses,
    _drift_tolerance,
    _read_project,
    _undeclared_connection,
)


def _drift(args: argparse.Namespace) -> int:
    """What changed in the warehouse since this project recorded it — crossed with
    the semantic assets that read the tables that changed.

    The verb exists because every other change-detection surface was off the daily
    path: `introspect` prints a snapshot and never a diff, `dst test` compares
    certified SQL against generation with both sides reading TODAY's warehouse (so
    a consistently wrong definition passes 1/1), and profile drift was REST-only.
    So a layer keeps serving its day-1 derivation of "discount" long after the
    warehouse publishes the company's own `discount_amount` column — confidently
    off by whatever the derivation missed, on every answer, until somebody notices.

    The cross-reference is the feature. A bare column list is what `introspect`
    already printed and nobody read; "`ops.orders` gained `discount_amount`, and
    definition `discount` derives that quantity from `list_price - unit_price`" is
    a line somebody acts on.

    File-first: the baseline is `profiles/<connection>.json` in the project, so
    this runs before any apply, without a server, and lands in git next to the
    layer it describes. `dst probe` arms it (one artifact family); `--accept` is
    the ONLY writer here — a bare run never records, because self-baselining an
    already-mutated warehouse destroys the evidence it exists to keep (a
    self-baselining first run silently accepts the very drift it was asked about).

    Exit codes (a gate reads them): 0 clean · 2 changes, none breaking ·
    1 changes breaking declared references (entities/definitions/certified) —
    operational errors also exit 1, the act-now side · 4 not armed.

    Without --connection, every warehouse connection dst.yaml declares runs in
    turn, one section per connection, and the exit code is the worst of them
    (`--json` then prints one list, an object per connection).
    """
    root = Path(args.dir or ".")
    if args.connection:
        code, payload = _drift_one(args, root)
        if payload is not None:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        return code
    names = _declared_warehouses(root)
    if names is None:
        print(
            f"error: no dst.yaml in {root} (or it does not parse) — drift reads "
            "connections from it; name one with --connection or declare it there",
            file=sys.stderr,
        )
        return 1
    if not names:
        print("error: dst.yaml declares no warehouse connection to diff", file=sys.stderr)
        return 1
    worst, payloads = 0, []
    for name in names:
        args.connection = name
        code, payload = _drift_one(args, root, section=len(names) > 1)
        if payload is not None:
            payloads.append(payload)
        worst = max(worst, code, key=lambda c: _DRIFT_SEVERITY.get(c, 3))
    if args.as_json:
        print(json.dumps(payloads, indent=2, ensure_ascii=False))
    return worst


# Worst-first order of `dst drift` exit codes: clean, changes, not armed, then
# breaking (operational errors share 1, the act-now side).
_DRIFT_SEVERITY = {0: 0, 2: 1, 4: 2, 1: 3}


def _drift_one(
    args: argparse.Namespace, root: Path, *, section: bool = False
) -> tuple[int, dict[str, object] | None]:
    """One connection of `dst drift` (`args.connection`): prints the prose,
    returns the exit code and — under `--json` — the object to print, so a
    run over several connections can emit one list. *section* heads the
    findings with the connection's name."""
    from services.lenses.connections import build_connector
    from services.lenses.profiler_catalog import catalog_profiles
    from services.project import probe as probe_artifact
    from services.project import warehouse_drift as wd
    from services.project.loader import split_semantic

    type_, config, secret = _declared_connection(args)
    if not type_:
        # No server fallback: the stored profile the server would diff against is
        # one generation deep and rewritten by any pass that upserts, so it cannot
        # answer "since I authored this". The declaration is what drift needs.
        print(f"error: {_undeclared_connection(args)}", file=sys.stderr)
        return 1, None
    try:
        connector = build_connector(type_, config, secret)
        current = catalog_profiles(connector, args.connection)
    except Exception as exc:  # noqa: BLE001 — a warehouse dead end must name itself
        first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        print(
            f"error: connection '{args.connection}' did not profile from here ({first}) — "
            "drift compares the live catalog against the recorded one and cannot "
            "report on a warehouse it could not read",
            file=sys.stderr,
        )
        return 1, None

    project_files = _read_project(root)
    entities, definitions, relationships = wd.parse_layer(split_semantic(project_files))
    # The newest probe artifact carries what the catalog cannot: the measured
    # distributions and dictionaries. Drift diffs the baseline against BOTH —
    # the live catalog for schema and comments, the artifact for the rest.
    artifact = probe_artifact.read_probe(root, args.connection)
    tolerance = _drift_tolerance(root)
    baseline = wd.read_baseline(root, args.connection)
    if baseline is None:
        if not args.accept:
            # Unarmed is its own exit code, and a bare run NEVER writes: the
            # first run against an already-mutated warehouse would record the
            # mutation as normal and report clean forever after.
            print(
                f"error: drift is not armed for '{args.connection}' — no baseline recorded. "
                "Run `dst probe` (or `dst drift --accept`) to record the warehouse as it "
                "stands now; the next run reports what changed against it.",
                file=sys.stderr,
            )
            return 4, None
        recorded = wd.accepted_tables(current, artifact.tables if artifact else None)
        path = wd.write_baseline(root, args.connection, recorded)
        if args.as_json:
            return 0, {"connection": args.connection, "recorded": len(current)}
        print(
            f"note: recorded {len(current)} table(s) for '{args.connection}' in {path}. "
            "Commit it: the next run reports what changed against it, and "
            "`dst plan` says so on its own.",
            file=sys.stderr,
        )
        return 0, None

    deltas = wd.baseline_drift(baseline.tables, current)
    if artifact is not None:
        deltas += wd.profile_drift(baseline.tables, artifact.tables, tolerance)
    findings = wd.cross_reference(
        deltas,
        entities,
        definitions,
        wd.parse_certified(project_files),
        relationships,
        wd.parse_exposes(project_files),
    )
    payload: dict[str, object] | None = (
        {
            "connection": args.connection,
            "baseline_recorded_at": baseline.recorded_at.isoformat(),
            "probed_at": artifact.probed_at.isoformat() if artifact else None,
            "tables": len(current),
            "findings": [
                {**f.model_dump(), "breaking": wd.is_breaking(f), "quiet": wd.is_quiet(f)}
                for f in findings
            ],
        }
        if args.as_json
        else None
    )
    if artifact is None and not args.as_json:
        # Said once, on stderr: without an artifact the measured half of the
        # diff is not running, and a clean line must not read as "nothing moved".
        print(
            f"note: no probe artifact for '{args.connection}' — schema and comments "
            "compared, measured distributions and dictionaries not; `dst probe` records them",
            file=sys.stderr,
        )
    if payload is not None:
        pass
    elif not findings:
        # Silence on stdout is the point: this runs in a loop, and a verb that
        # prints a wall on a quiet day is one people stop reading on a loud one.
        print(
            f"note: no change in '{args.connection}' since "
            f"{baseline.recorded_at.date().isoformat()} ({len(current)} tables)",
            file=sys.stderr,
        )
    else:
        if section:
            print(style.accent(f"connection {args.connection}:"))
        for finding in findings:
            for line in wd.render(finding):
                print(line)
        loud = [f for f in findings if not wd.is_quiet(f)]
        read = sum(1 for f in loud if f.referenced)
        broken = sum(1 for f in loud if wd.is_breaking(f))
        verdict = f", {broken} BREAKING declared references" if broken else ""
        quiet = len(findings) - len(loud)
        comments = f"; {quiet} comment-only change(s)" if quiet else ""
        print(
            f"\n{len(loud)} change(s) since "
            f"{baseline.recorded_at.date().isoformat()}, {read} on tables the semantic "
            f"layer reads{verdict}{comments}. Re-record with `dst drift --accept` once reviewed."
        )
    if args.accept:
        # Re-record from the live catalog and the newest artifact whenever the
        # recorded facts differ — a finding is one reason, a probe that measured
        # what the baseline holds as null is another ("no change" used to skip
        # both, and a baseline never gained its means and offsets). An
        # identical baseline is left alone: no timestamp-only commit.
        written = wd.advance_baseline(
            root, args.connection, current, artifact.tables if artifact else None
        )
        if written is not None and not args.as_json:
            rel = written.relative_to(root) if written.is_relative_to(root) else written
            if findings:
                # What was signed off, table by table, old -> new: an accept
                # that closes findings in silence is the evidence gone.
                print(
                    f"note: baseline for '{args.connection}' advanced over "
                    f"{len(findings)} finding(s) -> {rel}:",
                    file=sys.stderr,
                )
                for line in wd.accepted_lines(findings):
                    print(line, file=sys.stderr)
            else:
                print(
                    f"note: baseline for '{args.connection}' re-recorded with the newest "
                    f"probe's measured facts -> {rel}",
                    file=sys.stderr,
                )
        return 0, payload
    return wd.exit_code(findings), payload


def register(sub: Any) -> None:
    p = sub.add_parser(
        "drift",
        help="what changed in the warehouse since this project recorded it, crossed "
        "with the definitions, entities and certified answers that read the tables "
        "that changed — the diff `introspect` does not print (it prints a snapshot) "
        "and the move `dst test` cannot see (both its sides read today's warehouse). "
        "Exit codes gate a nightly run: 0 clean, 2 changes (none breaking), 1 changes "
        "breaking declared references, 4 not armed (run `dst probe`)",
    )
    p.add_argument(
        "--connection",
        help="one declared connection; omit to run every warehouse connection dst.yaml "
        "declares (exit code: the worst of them)",
    )
    p.add_argument(
        "--accept",
        action="store_true",
        help="re-record the baseline as the warehouse now stands (schema and comments "
        "from the catalog, measured facts from the newest probe artifact), after "
        "reviewing the findings — the reviewed-it act, so the next run reports what "
        "changed since THIS review and not since the first one; `dst apply` does the "
        "same at deploy",
    )
    p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print the findings as a parseable object, each with the semantic "
        "assets that read the changed table",
    )
    p.add_argument(
        "--dir",
        default=".",
        help="project root: dst.yaml declares the connection, semantic/ is what "
        "the findings are crossed against, profiles/ holds the baseline",
    )
    p.set_defaults(fn=_drift)
