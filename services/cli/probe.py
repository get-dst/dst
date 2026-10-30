"""The `dst probe` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from services.cli._common import (
    _bytes,
    _declared_connection,
    _read_project,
    _undeclared_connection,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _sample_scope(
    args: argparse.Namespace, profiles: list[Any], entities: list[Any], connection: str
) -> set[str]:
    """Which tables the sampling pass reads: an explicit --tables wins, then the
    tables the semantic layer maps on this connection, then — no layer to scope
    by — everything (the bootstrap probe that precedes authoring)."""
    from services.project.warehouse_drift import same_table
    from services.runtime.sql_guard import _names_table

    if getattr(args, "tables", None):
        # Named as an entity names it: `raw_shopify.orders` means the catalog's
        # `acme-prod.raw_shopify.orders`, and a name that leaves out a leading
        # qualifier still binds — the rule the guard reads table references by.
        # Exact equality sampled nothing on a warehouse that lists tables fully
        # qualified, and said only "sampled 0".
        wanted = [t.strip() for t in args.tables.split(",") if t.strip()]
        scope = {p.table for p in profiles if any(_names_table(w, p.table) for w in wanted)}
        for w in wanted:
            if not any(_names_table(w, p.table) for p in profiles):
                print(
                    f"warning: --tables names '{w}', which matches no table '{connection}' "
                    "lists — nothing sampled for it",
                    file=sys.stderr,
                )
        return scope
    if getattr(args, "sample_all", False):
        return {p.table for p in profiles}
    mapped = {
        p.table
        for p in profiles
        if any(
            e.source.connection == connection and same_table(e.source.table, p.table)
            for e in entities
        )
    }
    return mapped or {p.table for p in profiles}


def _probe(args: argparse.Namespace) -> int:
    """Materialize the warehouse's full profile into `profiles/<conn>.probe.json`.

    `introspect --profile` runs the same passes and PRINTS them — gone when the
    terminal scrolls, so every scaffolded project served with zero value
    dictionaries. This writes the artifact the project keeps: partitions, row
    counts, freshness, value dictionaries, crossed with the entities that read
    each table. Committed, it rides `dst apply` into the store the serving
    prompt reads, so generation filters on literals the warehouse actually holds
    ('FI') instead of guessing formats ('Finland'). Re-run freely — nightly cron
    is the intended cadence — and no server is needed.

    Without --connection, every warehouse connection dst.yaml declares is
    probed; one failing loudly does not stop the rest (exit 1 if any failed).

    SCALE. The catalog pass is metadata-only and always covers every table; the
    SAMPLING pass (one capped read per table) covers the tables the semantic
    layer reads — on a 5000-table warehouse the nightly cron pays for the
    governed scope, not the catalog. No layer yet means no scope, so everything
    samples (bootstrap mode, per-table caps still apply). `--sample-all` widens
    to every table; `--tables a,b` narrows the sampling to exactly those. What
    was NOT sampled is said out loud — a silent cap reads as coverage.

    With --cube the probe reads through Cube instead (`services.cli.monitor`)."""
    if getattr(args, "cube", False):
        from services.cli.monitor import probe_cube

        return probe_cube(args)
    from services.project.compile import CompileError, dialect_for
    from services.project.loader import split_semantic
    from services.project.schema import parse_project_yaml
    from services.project.warehouse_drift import parse_layer

    root = Path(args.dir or ".")
    path = root / "dst.yaml"
    if not path.exists():
        print(f"error: no dst.yaml in {root} — probe reads connections from it", file=sys.stderr)
        return 1
    try:
        project = parse_project_yaml(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"error: {path}: {exc}", file=sys.stderr)
        return 1

    def _is_warehouse(conn_type: str) -> bool:
        try:
            dialect_for(conn_type)
        except CompileError:
            return False  # a context source — nothing to sample
        return True

    if args.connection:
        if args.connection not in project.connections:
            print(f"error: {_undeclared_connection(args)}", file=sys.stderr)
            return 1
        wanted = [args.connection]
    else:
        wanted = [n for n, d in project.connections.items() if _is_warehouse(d.type)]
        if not wanted:
            print("error: dst.yaml declares no warehouse connection to probe", file=sys.stderr)
            return 1
    entities_by_path, _defs, _rels = parse_layer(split_semantic(_read_project(root)))
    entities = list(entities_by_path.values())
    # A table skipped by the sampling pass warns through its module logger
    # — mirror that to stderr so the operator sees it
    # here, not only in a server log (finding 4d's lesson).
    import logging as _logging

    from services.connectors import sampling as _sampling

    class _MirrorFormat(_logging.Formatter):
        # Progress lines (INFO) ride unprefixed so a long probe is visibly
        # alive; skips keep their warning: prefix.
        def format(self, record: _logging.LogRecord) -> str:
            prefix = "warning: " if record.levelno >= _logging.WARNING else ""
            return prefix + record.getMessage()

    mirror = _logging.StreamHandler(sys.stderr)
    mirror.setFormatter(_MirrorFormat())
    mirror.setLevel(_logging.INFO)
    prior_level = _sampling.logger.level
    _sampling.logger.setLevel(_logging.INFO)
    _sampling.logger.addHandler(mirror)
    try:
        return _probe_connections(
            args, root, wanted, entities, recent_partitions=project.probe.recent_partitions
        )
    finally:
        _sampling.logger.removeHandler(mirror)
        _sampling.logger.setLevel(prior_level)


def _probe_connections(
    args: argparse.Namespace,
    root: Path,
    wanted: list[str],
    entities: list[Any],
    *,
    recent_partitions: int = 0,
) -> int:
    from services.connectors import sampling
    from services.lenses.connections import build_connector
    from services.lenses.profiler import sample_profiles
    from services.lenses.profiler_catalog import catalog_profiles
    from services.observability import cost
    from services.project import probe as probe_artifact
    from services.project import warehouse_drift as wd

    worst = 0
    for name in wanted:
        type_, config, secret = _declared_connection(args, name)
        # The artifact this run refreshes, read BEFORE the checkpoint below
        # overwrites it: a windowed read keeps the dictionaries it proved
        # complete, and the very first probe (no artifact yet) reads whole
        # tables so there is something complete to keep.
        prior = probe_artifact.read_probe(root, name)
        try:
            connector = build_connector(type_, config, secret)
            # The semantic layer's tables are guaranteed into the catalog pass
            # A wide warehouse's listing cap must never starve
            # the tables every lens actually reads.
            needed = {
                e.source.table for e in entities if e.source.connection == name and e.source.table
            }
            # One bill for the connection: the catalog pass's metadata reads
            # (a billed job each on BigQuery) and the sampling statements.
            meter = sampling.SampleMeter()
            with sampling.metered(meter):
                profiles = catalog_profiles(connector, name, priority_tables=needed)
            if not profiles:
                # Empty output is never success (blocker 1): say so, exit non-zero.
                print(f"error: connection '{name}' listed no tables", file=sys.stderr)
                worst = 1
                continue
            scope = _sample_scope(args, profiles, entities, name)
            # Checkpoint BEFORE the slow pass: without it a wedged run discards
            # everything, leaving profiles/ empty and a nightly that produces
            # nothing indefinitely. The catalog artifact lands
            # first; sampling enriches it in place afterwards.
            probe_artifact.write_probe(root, name, profiles, entities)
            print(
                f"'{name}': catalog checkpoint written ({len(profiles)} tables) — "
                f"sampling {len(scope)} table(s) now",
                file=sys.stderr,
            )
            # Whole dictionaries (for matching names the question states) only for
            # the dimensions the semantic layer declares on this connection.
            matchable = frozenset(
                f"{e.source.table}.{d.name}"
                for e in entities
                if e.source.connection == name and e.source.table
                for d in e.dimensions
            )
            windows = (
                probe_artifact.sample_windows(profiles, entities, name, recent_partitions, prior)
                if prior is not None
                else {}
            )
            # Every other time column's offset is measured from the entity's
            # declared clock — the number drift compares a semantic change by.
            time_fields = {
                p.table: e.default_time_field
                for p in profiles
                for e in entities
                if e.source.connection == name
                and e.default_time_field
                and wd.same_table(e.source.table, p.table)
            }
            with sampling.metered(meter):
                merged = sample_profiles(
                    connector,
                    [p for p in profiles if p.table in scope],
                    matchable=matchable,
                    windows=windows,
                    time_fields=time_fields,
                )
            profiles = probe_artifact.carry_complete_dictionaries(
                probe_artifact.keep_unsampled([merged.get(p.table, p) for p in profiles], prior),
                prior,
            )
        except Exception as exc:  # noqa: BLE001 — one dead warehouse must not stop the rest
            first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
            print(f"error: connection '{name}' did not probe ({first})", file=sys.stderr)
            worst = 1
            continue
        # Report what was SAMPLED, not what was scheduled: a pass that sampled
        # nothing must not read as a pass that sampled everything.
        sampled = sum(1 for p in merged.values() if p.source != "catalog")
        kept = sum(1 for p in profiles if p.source != "catalog") - sampled
        if kept > 0:
            print(
                f"'{name}': {kept} table(s) not sampled this run keep the sampled facts "
                "of the previous probe",
                file=sys.stderr,
            )
        if lost := probe_artifact.lost_dictionaries(profiles, prior):
            many = len(lost) != 1
            print(
                f"warning: {len(lost)} value {'dictionaries' if many else 'dictionary'} complete "
                f"in the previous probe {'are' if many else 'is'} not complete now "
                f"({', '.join(lost[:10])}{', …' if len(lost) > 10 else ''}): this read did not "
                "prove them complete (more values than a dictionary keeps, or a sampled read), "
                "so a value a question names is no longer checked against them",
                file=sys.stderr,
            )
        if sampled < len(scope):
            print(
                f"warning: sampled {sampled} of the {len(scope)} table(s) in scope — "
                f"the rest carry catalog metadata only",
                file=sys.stderr,
            )
        if len(scope) < len(profiles):
            chose = (
                "--tables names" if getattr(args, "tables", None) else "the semantic layer reads"
            )
            print(
                f"sampled {sampled} of {len(profiles)} table(s) — {chose} "
                f"{len(scope)}; catalog metadata covers the rest (--sample-all "
                "samples every table, --tables a,b exactly those)",
                file=sys.stderr,
            )
        out, artifact = probe_artifact.write_probe(root, name, profiles, entities)
        # One artifact family: the FIRST probe arms drift too, so a project that
        # ran `dst probe` — the documented first step — never sits in the silent
        # unarmed state `dst drift` refuses (exit 4). Never again after that: the
        # baseline is the evidence drift diffs the artifact against, and a
        # nightly probe that rewrote it erased every change before anyone read
        # it. It advances by `dst drift --accept` or `dst apply` only.
        if wd.read_baseline(root, name) is None:
            wd.write_baseline(root, name, artifact.tables)
            print(f"'{name}': drift baseline recorded (first probe)", file=sys.stderr)
        _print_probe_bill(name, type_, meter, cost.warehouse_cost_usd)
        # Counted off the artifact, so the summary describes the file on disk.
        complete = sum(
            1 for p in artifact.tables for c in p.columns if c.top_values and c.values_complete
        )
        partial = sum(
            1 for p in artifact.tables for c in p.columns if c.top_values and not c.values_complete
        )
        partitioned = sum(1 for p in artifact.tables if p.partitioning is not None)
        windowed = sum(
            1 for p in artifact.tables if p.sample_window and p.sample_window.kind != "none"
        )
        bits = [f"{complete} complete value dictionaries"]
        if partial:
            bits.append(f"{partial} partial")
        if partitioned:
            bits.append(f"{partitioned} partitioned table(s)")
        if windowed:
            bits.append(f"{windowed} sampled over the last {recent_partitions} partitions/days")
        rel = out.relative_to(root) if out.is_relative_to(root) else out
        print(f"probed '{name}': {len(artifact.tables)} table(s) -> {rel} ({', '.join(bits)})")
    if worst == 0:
        print(
            "commit profiles/ — the next `dst apply` lands these facts in the serving prompt",
            file=sys.stderr,
        )
    return worst


def _print_probe_bill(
    name: str, type_: str, meter: Any, price: Callable[[int | None, str], float | None]
) -> None:
    """What the probe cost, on stderr: statements (the catalog pass's billed
    metadata reads and the sampling statements), the bytes the engine billed
    for them, and the estimate where the engine prices bytes. An engine that
    reports no bytes gets the statement count and says the bytes are
    unreported, never $0. BigQuery's per-job minimum is said, since a probe of
    small tables bills it on every statement."""
    if not meter.priced:
        print(
            f"'{name}': {meter.statements} probe statement(s); bytes not reported by {type_}",
            file=sys.stderr,
        )
        return
    usd = price(meter.bytes_scanned, type_)
    line = f"'{name}': {meter.statements} probe statement(s), {_bytes(meter.bytes_scanned)} billed"
    if usd is not None:
        line += f", about ${usd:.4f}"
    if type_ == "bigquery" and meter.statements:
        # BigQuery bills every query job it runs at least 10 MB, a metadata
        # read included; a result served from its cache bills nothing.
        line += " (BigQuery bills at least 10 MB per statement it runs; a cached one bills 0)"
    print(line, file=sys.stderr)


def register(sub: Any) -> None:
    p = sub.add_parser(
        "probe",
        help="record the warehouse's full profile — partitions, freshness, value "
        "dictionaries, crossed with the entities that read each table — into "
        "profiles/<conn>.probe.json; committed, it rides `dst apply` into every "
        "served prompt. Re-run freely (nightly cron is the cadence); `drift` diffs "
        "the schema, this records the truth",
    )
    p.add_argument(
        "--connection",
        help="probe just this declared connection (default: every warehouse "
        "connection dst.yaml declares)",
    )
    p.add_argument(
        "--sample-all",
        action="store_true",
        help="sample every table, not just the ones the semantic layer reads — "
        "one capped read per table, so on a wide warehouse this is the expensive "
        "form (the catalog pass always covers everything either way)",
    )
    p.add_argument(
        "--tables",
        help="comma-separated tables to sample (exact names as introspect prints "
        "them); the catalog pass still records every table",
    )
    p.add_argument(
        "--dir",
        default=".",
        help="project root: dst.yaml declares the connections, semantic/ is what "
        "the artifact is crossed against, profiles/ receives it",
    )
    p.add_argument(
        "--cube",
        action="store_true",
        help="read through the project's Cube instead: the value dictionary of every "
        "dimension declaring meta.dst.dictionary and the data end of every time dimension "
        "of the lens views, into profiles/cube.probe.json",
    )
    p.add_argument(
        "--as-of",
        dest="as_of",
        help="with --cube: the clock data ends are read against (default: yesterday, UTC)",
    )
    p.set_defaults(fn=_probe)
