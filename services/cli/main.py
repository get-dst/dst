"""The `dst` CLI — thin orchestration over code that already exists.

Server-side commands (migrate, bootstrap, demo, serve) run in-process against
the configured database. Project commands (export, plan, apply) are HTTP
wrappers over /mgmt/project/* — auth and RLS stay server-side.
"""

from __future__ import annotations

import argparse
import sys

from services.cli import (
    bootstrap,
    calibrate_bands,
    connection,
    cube,
    define,
    demo,
    doctor,
    drift,
    env,
    evals,
    experiment,
    init,
    introspect,
    keys,
    lens,
    migrate_model,
    monitor,
    observe,
    probe,
    project,
    prune_log,
    query,
    reindex,
    reviews,
    runs,
    semantic,
    serve,
    sql,
    style,
    test,
)
from services.cli._common import _unreachable_exit


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dst", description="dst — governed answers over your warehouse"
    )
    from services import __version__ as pkg_version
    from services.build_info import GIT_DIRTY, GIT_SHA

    # Name the build, not just the wheel: two installs of the same version can
    # differ by every unreleased commit, and only /health said which was running
    # (flywheel e2e lap 2). Wheels have no .git — version alone, as before.
    build = f" ({GIT_SHA[:12]}{' dirty' if GIT_DIRTY else ''})" if GIT_SHA else ""
    parser.add_argument("--version", action="version", version=f"dst {pkg_version}{build}")
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI color (also: NO_COLOR/DST_NO_COLOR env; color is TTY-only anyway)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve.register_migrate(sub)
    cube.register(sub)
    doctor.register(sub)
    keys.register_secret(sub)
    keys.register_rotate_key(sub)
    bootstrap.register(sub)
    env.register(sub)
    runs.register(sub)
    demo.register(sub)
    prune_log.register(sub)
    experiment.register(sub)
    reindex.register(sub)
    calibrate_bands.register(sub)
    init.register(sub)
    query.register(sub)
    define.register(sub)
    sql.register(sub)
    introspect.register(sub)
    drift.register(sub)
    migrate_model.register(sub)
    monitor.register(sub)
    probe.register(sub)
    serve.register_dev(sub)
    serve.register_serve(sub)
    serve.register_mcp(sub)
    test.register(sub)
    reviews.register_reviews(sub)
    reviews.register_rule(sub)
    reviews.register_seed_lens(sub)
    reviews.register_retire(sub)
    reviews.register_correct(sub)
    observe.register(sub)
    semantic.register(sub)
    lens.register(sub)
    connection.register(sub)
    keys.register_keys(sub)
    keys.register_revoke_key(sub)
    keys.register_revoke_token(sub)
    evals.register(sub)
    project.register(sub)

    args = parser.parse_args()
    if args.no_color:
        style.set_enabled(False)
    if getattr(args, "certify", False) and args.verdict != "approve":
        parser.error("--certify requires --verdict approve")
    try:
        return int(args.fn(args))
    except Exception as exc:  # noqa: BLE001 — narrowed in _unreachable_exit; the rest re-raises
        handled = _unreachable_exit(exc, args)
        if handled is None:
            raise
        return handled
    finally:
        # A --dir verb reconfigures this process for the project it was pointed at
        # (_adopt_project_env). One invocation, one adoption: put it back, so nothing
        # a verb resolved outlives the verb in a process that isn't one-shot.
        if restore := getattr(args, "restore_project_env", None):
            restore()


if __name__ == "__main__":
    sys.exit(main())
