"""Render the Definition Drift Report.

    # from a findings dump (the miner's DriftFinding JSON):
    uv run python -m services.probe --findings findings.json \\
        --org "Acme Analytics Oy" --out report.html --defs audit-defs.yaml

    # live against Snowflake (env: SNOWFLAKE_ACCOUNT, SNOWFLAKE_USERNAME or
    # SNOWFLAKE_USER, SNOWFLAKE_PASSWORD, SNOWFLAKE_WAREHOUSE, SNOWFLAKE_DATABASE;
    # optional SNOWFLAKE_SCHEMA / SNOWFLAKE_ROLE):
    uv run python -m services.probe --snowflake --org "Acme Analytics Oy" --out report.html

Always prints the terminal summary; ``--out`` gets the self-contained HTML
artifact and ``--defs`` the lens-definition import file (the bridge). The live
path is read-only end to end: history + metadata via ACCOUNT_USAGE, variant
execution behind the miner's SELECT-only guard (``--no-execute`` skips even that).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from services.probe.drift import DriftFinding, execute_variants, mine_drift
from services.probe.report import render_defs_yaml, render_html, render_terminal


def _load_findings(path: Path) -> list[DriftFinding]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"--findings {path}: expected a JSON list of DriftFinding dumps")
    return [DriftFinding.model_validate(item) for item in raw]


def _snowflake_findings(args: argparse.Namespace) -> tuple[list[DriftFinding], str]:
    """Mine + (optionally) execute against live Snowflake. Reads SNOWFLAKE_* env."""
    env = os.environ
    user = env.get("SNOWFLAKE_USERNAME") or env.get("SNOWFLAKE_USER")
    required = {
        "SNOWFLAKE_ACCOUNT": env.get("SNOWFLAKE_ACCOUNT"),
        "SNOWFLAKE_USERNAME (or SNOWFLAKE_USER)": user,
        "SNOWFLAKE_PASSWORD": env.get("SNOWFLAKE_PASSWORD"),
        "SNOWFLAKE_WAREHOUSE": env.get("SNOWFLAKE_WAREHOUSE"),
        "SNOWFLAKE_DATABASE": env.get("SNOWFLAKE_DATABASE"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit(f"--snowflake needs env vars: {', '.join(missing)}")

    from services.connectors.snowflake import SnowflakeConnector  # heavy import, live path only

    account = env["SNOWFLAKE_ACCOUNT"]
    database = env["SNOWFLAKE_DATABASE"]
    connector = SnowflakeConnector(
        account=account,
        user=str(user),
        password=env["SNOWFLAKE_PASSWORD"],
        warehouse=env["SNOWFLAKE_WAREHOUSE"],
        database=database,
        schema=env.get("SNOWFLAKE_SCHEMA") or "PUBLIC",
        role=env.get("SNOWFLAKE_ROLE"),
    )
    records = connector.query_history(days=args.days, limit=args.limit)
    findings = mine_drift(records, dialect="snowflake")
    if not args.no_execute:
        findings = [execute_variants(connector, f) for f in findings]
    return findings, f"Snowflake · {account} / {database}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="services.probe", description="Render the Definition Drift Report."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--findings", type=Path, help="JSON dump of DriftFinding objects")
    source.add_argument(
        "--snowflake", action="store_true", help="mine live Snowflake (SNOWFLAKE_* env vars)"
    )
    parser.add_argument("--org", required=True, help="organization name on the report")
    parser.add_argument("--out", type=Path, default=Path("report.html"), help="HTML output path")
    parser.add_argument("--defs", type=Path, help="also write the lens-definition import YAML")
    parser.add_argument("--warehouse-label", help="warehouse line in the report header")
    parser.add_argument("--days", type=int, default=30, help="history window (live mode)")
    parser.add_argument("--limit", type=int, default=1000, help="history record cap (live mode)")
    parser.add_argument(
        "--no-execute",
        action="store_true",
        help="live mode: skip running variants (metadata-only report, no numbers)",
    )
    args = parser.parse_args(argv)

    if args.snowflake:
        findings, default_label = _snowflake_findings(args)
    else:
        findings = _load_findings(args.findings)
        default_label = "(warehouse not specified)"

    html = render_html(
        findings,
        org_name=args.org,
        warehouse_label=args.warehouse_label or default_label,
        generated_at=datetime.now(UTC),
    )
    args.out.write_text(html, encoding="utf-8")

    print(render_terminal(findings))
    print(f"\nreport: {args.out}")
    if args.defs is not None:
        args.defs.write_text(render_defs_yaml(findings), encoding="utf-8")
        print(f"lens definitions: {args.defs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
