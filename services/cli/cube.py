"""`dst cube status` — is the Cube this project points at there, and what
model does it hold?

Prints the URL, whether `/readyz` answers, the version when Cube states one,
the model_version (the sha256 of the canonical meta) and the member counts,
plus how many `meta.dst` blocks the closed schema refused. Exit 0 when Cube
is reachable and its meta parsed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from services.cli import style


def register(sub: Any) -> None:
    p = sub.add_parser("cube", help="the Cube this project compiles through")
    cube_sub = p.add_subparsers(dest="cube_cmd", required=True)
    s = cube_sub.add_parser(
        "status", help="reachability, version, model_version and member counts of the Cube"
    )
    s.add_argument("--dir", default=".", help="project root (dst.yaml, .env); default: .")
    s.add_argument("--json", dest="as_json", action="store_true", help="the status as JSON")
    s.set_defaults(fn=_status)


def _status(args: argparse.Namespace) -> int:
    from services.cube.client import status

    out = status(root=Path(args.dir))
    if args.as_json:
        print(json.dumps(out if isinstance(out, dict) else {"status": out}, indent=2))
        return 0 if isinstance(out, dict) and out.get("model_version") else 1
    if not isinstance(out, dict):
        print(
            f"cube          {out} — set DST_CUBE_URL or `cube: {{url: …}}` in dst.yaml",
            file=sys.stderr,
        )
        return 1
    print(f"url           {out['url']}")
    print(f"secret        {out['secret']}")
    if not out["reachable"]:
        print(style.bad(f"readyz        FAIL — {out.get('detail', 'not reachable')}"))
        return 1
    print(f"readyz        ok ({out['version'] or 'version not stated'})")
    if "model_version" not in out:
        print(style.bad(f"meta          FAIL — {out.get('detail', '')}"))
        return 1
    print(f"model_version {out['model_version']}")
    counts = out["members"]
    print(
        f"members       {counts['views']} view(s), {counts['cubes']} cube(s), "
        f"{counts['measures']} measure(s), {counts['dimensions']} dimension(s), "
        f"{counts['segments']} segment(s)"
    )
    if out["overlay_errors"]:
        print(style.warn(f"overlay       {out['overlay_errors']} meta.dst block(s) refused"))
    return 0


def doctor_row(root: Path) -> int:
    """The `dst doctor` line: not configured is a note, unreachable is a failure
    (an install that names a Cube it cannot call cannot serve through it).
    Returns the failures to add."""
    from services.cube.client import status

    out = status(root=root)
    if not isinstance(out, dict):
        print(f"cube          {out}")
        return 0
    if out.get("model_version"):
        print(f"cube          ok ({out['url']}, model {out['model_version'][:12]})")
        return 0
    print(style.bad(f"cube          FAIL — {out['url']}: {out.get('detail', 'unreachable')}"))
    return 1
