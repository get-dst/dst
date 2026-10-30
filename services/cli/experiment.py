"""The `dst experiment` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from services.cli import style
from services.cli._common import (
    _resolve_url,
)


def _parse_vary(specs: list[str]) -> list[tuple[str, list[Any]]]:
    """ "model.temperature=0.0,0.7" → ("model.temperature", [0.0, 0.7]).
    Values parse as YAML scalars so numbers stay numbers in lens.yaml."""
    import yaml

    dims: list[tuple[str, list[Any]]] = []
    for spec in specs:
        key, eq, raw = spec.partition("=")
        values = [yaml.safe_load(v.strip()) for v in raw.split(",") if v.strip()]
        if not eq or not key.strip() or len(values) < 2:
            raise ValueError(
                f"--vary {spec!r}: expected <dotted.key>=<value>,<value>[,…] "
                "(at least two values — one value is not an experiment)"
            )
        dims.append((key.strip(), values))
    return dims


def _expand_variants(dims: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:
    """Cartesian product of the --vary dimensions, in declaration order."""
    import itertools

    return [
        dict(combo) for combo in itertools.product(*[[(k, v) for v in vals] for k, vals in dims])
    ]


def _patch_lens_yaml(path: Path, overrides: dict[str, Any]) -> None:
    """Set dotted keys in a lens.yaml — on the experiment's COPY, never the
    user's project."""
    import yaml

    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for dotted, value in overrides.items():
        cur = doc
        keys = dotted.split(".")
        for k in keys[:-1]:
            nxt = cur.get(k)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[k] = nxt
            cur = nxt
        cur[keys[-1]] = value
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=88), encoding="utf-8"
    )


def _dst_run(argv: list[str], cwd: str) -> tuple[int, str]:
    """One product verb as a subprocess — the same commands a user or CI runs,
    exit codes as the interface. Seam monkeypatched in tests."""
    import shutil
    import subprocess

    exe = str(Path(sys.executable).parent / "dst")
    if not Path(exe).exists():
        exe = shutil.which("dst") or exe
    proc = subprocess.run([exe, *argv], cwd=cwd, capture_output=True, text=True, timeout=3600)
    return proc.returncode, proc.stdout + proc.stderr


def _experiment(args: argparse.Namespace) -> int:
    """`dst experiment` — N lens-config variants, measured, side by side.

    An orchestrator over the product's own verbs, nothing more: mint a
    disposable env, then per variant patch a COPY of the project's lens.yaml,
    `apply --env`, `test --env`, read the recorded run — and diff every
    variant against the first. The user's project directory is never touched,
    the env is removed at the end (--keep to inspect), and each step is the
    same gated, audited command CI runs."""
    import json as _json
    import shutil
    import tempfile
    import uuid as _uuid

    try:
        dims = _parse_vary(args.vary or [])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not dims:
        print("error: at least one --vary <dotted.key>=<v1>,<v2> is required", file=sys.stderr)
        return 1
    variants = _expand_variants(dims)
    if len(variants) > 8:
        print(
            f"error: {len(variants)} variants (cartesian of --vary) — cap is 8; "
            "every variant costs one gated apply + one full test sweep",
            file=sys.stderr,
        )
        return 1

    root = str(Path(args.dir).resolve())
    lens_rel = Path("lenses") / args.lens / "lens.yaml"
    if not (Path(root) / lens_rel).exists():
        print(f"error: no {lens_rel} under {root}", file=sys.stderr)
        return 1

    # Resolve the server ONCE, at the root, and pass it explicitly to every
    # subcall: apply's --dir is the variant copy (no .env there), and letting
    # it fall back to the built-in default aimed the second live lap at
    # whatever server owned localhost:8000. A wrong server must be impossible
    # here, not merely 401-unlikely.
    resolved_url, _src = _resolve_url(args)
    url_extra = ["--url", resolved_url]
    env_name = f"exp-{_uuid.uuid4().hex[:8]}"
    code, out = _dst_run(["env", "new", env_name], cwd=root)
    if code != 0:
        print(out, file=sys.stderr)
        return 1
    # apply's --dir points at the variant COPY, and --env resolves the map
    # from the --dir project — where it deliberately doesn't exist. The
    # orchestrator owns the root's map, so it resolves the token itself and
    # hands apply the explicit credential. Found on the first live lap.
    try:
        env_token = _json.loads((Path(root) / ".dst" / "envs.json").read_text(encoding="utf-8"))[
            env_name
        ]
    except (OSError, ValueError, KeyError):
        print(
            f"error: `dst env new {env_name}` did not record a token in {root}/.dst/envs.json",
            file=sys.stderr,
        )
        return 1
    print(f"experiment env: {env_name} ({len(variants)} variant(s))")

    results: list[dict[str, Any]] = []
    hard_error = False
    try:
        for i, overrides in enumerate(variants, 1):
            label = " ".join(f"{k}={v}" for k, v in overrides.items())
            workdir = tempfile.mkdtemp(prefix=f"dst-exp-{i}-")
            shutil.copytree(
                root,
                workdir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    ".env", ".dst", ".git", "__pycache__", ".venv", "node_modules"
                ),
            )
            _patch_lens_yaml(Path(workdir) / lens_rel, overrides)
            print(f"#{i} {label}: apply …", flush=True)
            code, out = _dst_run(
                ["apply", "--dir", workdir, "--token", env_token, "--quiet", "--timeout", "2400"]
                + url_extra,
                cwd=root,
            )
            if code != 0:
                results.append({"variant": label, "status": "apply-failed", "detail": out[-400:]})
                hard_error = True
                continue
            print(f"#{i} {label}: test …", flush=True)
            code, out = _dst_run(["test", "--all", "--env", env_name, "--dir", root], cwd=root)
            # test exit 1 = failures, still a measurement; 4 = nothing verified.
            if code not in (0, 1):
                results.append({"variant": label, "status": "not-measured", "detail": out[-400:]})
                hard_error = True
                continue
            code, out = _dst_run(
                ["runs", args.lens, "--env", env_name, "--json", "--dir", root] + url_extra,
                cwd=root,
            )
            runs = _json.loads(out) if code == 0 else []
            tests = [r for r in runs if r.get("mode") == "test"]
            if not tests:
                results.append({"variant": label, "status": "no-run-recorded"})
                hard_error = True
                continue
            latest = tests[0]
            results.append(
                {
                    "variant": label,
                    "status": "ok",
                    "run_id": latest["id"],
                    "score": latest["score"],
                    "passed": latest["passed"],
                    "failed": latest["failed"],
                }
            )
        # Flips vs variant #1, via the same comparator CI uses.
        baseline = next((r for r in results if r.get("status") == "ok"), None)
        for r in results:
            if r.get("status") != "ok" or r is baseline or baseline is None:
                continue
            code, out = _dst_run(
                [
                    "runs",
                    args.lens,
                    "--diff",
                    str(baseline["run_id"]),
                    str(r["run_id"]),
                    "--env",
                    env_name,
                    "--json",
                    "--dir",
                    root,
                ]
                + url_extra,
                cwd=root,
            )
            try:
                diff = _json.loads(out)
            except ValueError:
                diff = {}
            r["newly_failing"] = diff.get("newly_failing", [])
            r["newly_passing"] = diff.get("newly_passing", [])
            r["regressed"] = bool(diff.get("regressed") or diff.get("newly_failing"))
    finally:
        if getattr(args, "keep", False):
            print(f"env kept for inspection: dst env rm {env_name} --yes when done")
        else:
            _dst_run(["env", "rm", env_name, "--yes"], cwd=root)

    if args.as_json:
        print(_json.dumps({"env": env_name, "variants": results}, indent=2, default=str))
        return 1 if hard_error else 0

    def pct(v: Any) -> str:
        return "—" if v is None else f"{float(v):.0%}"

    print()
    print(f"{'':<3}{'VARIANT':<44} {'SCORE':>6} {'PASS':>5} {'FAIL':>5}  VS #1")
    for i, r in enumerate(results, 1):
        if r.get("status") != "ok":
            print(f"#{i} {r['variant']:<44} {r['status']}")
            continue
        if r is baseline:
            note = "baseline"
        elif r.get("regressed"):
            note = style.bad(f"{len(r.get('newly_failing', []))} newly failing")
        else:
            note = style.good("no regression")
        row = f"#{i} {r['variant']:<44} {pct(r['score']):>6} {r['passed']:>5} {r['failed']:>5}"
        print(f"{row}  {note}")
        for q in r.get("newly_failing", []):
            print(style.bad(f"     newly failing: {q}"))
        for q in r.get("newly_passing", []):
            print(style.good(f"     newly passing: {q}"))
    for r in results:
        if r.get("status") not in ("ok", None) and r.get("detail"):
            print(f"\n#{results.index(r) + 1} {r['variant']} — {r['status']}:\n{r['detail']}")
    return 1 if hard_error else 0


def register(sub: Any) -> None:
    p = sub.add_parser(
        "experiment",
        help="measure N lens-config variants side by side: mints a disposable "
        "env, then per variant applies a patched COPY of the project and runs "
        "the suite — scores + per-case flips vs variant #1; the project dir "
        "is never touched and the env is removed at the end",
    )
    p.add_argument("lens", help="the lens whose lens.yaml the variants patch")
    p.add_argument(
        "--vary",
        action="append",
        metavar="KEY=V1,V2[,…]",
        help="a dimension to vary, dotted into lens.yaml (e.g. "
        "model.temperature=0.0,0.7); repeatable — variants are the cartesian "
        "product, capped at 8 (each costs one gated apply + one test sweep)",
    )
    p.add_argument("--keep", action="store_true", help="keep the env for inspection")
    p.add_argument("--dir", default=".", help="project root (default: .)")
    p.add_argument(
        "--url",
        help="server URL (default: DST_URL from env or ./.env, else http://localhost:8000)",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="parseable form")
    p.set_defaults(fn=_experiment)
