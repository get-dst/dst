"""Where the database sits relative to THIS build's migrations.

Nothing in ``services/`` used to read ``alembic_version``. ``dst serve`` started
against any schema at all, and ``/ready``'s database check was ``SELECT 1`` — so a
schema two revisions behind answered ``{"status":"ready","db":"ok"}`` while every
``request_log`` INSERT failed inside a FastAPI background task. Answers kept being
served correctly, with a ``request_id``, at exit 0; only the receipt was gone, and the
traceback reached the server's stderr and nobody else. For a governance product that
is the worst thing to lose silently: the review queue, the drift audit, ``dst
test`` and the correction loop are all views over ``request_log``.

One module asks the question, so the CLI preflight, readiness, and the messages that
would otherwise misdiagnose it (``dst correct``) all answer it the same way.

Read through ``database_admin_url``: ``dst_app`` has no grant on
``alembic_version`` (permission denied), and every deployment that can migrate — the
compose file, the helm hook, a laptop ``pip install`` — has the admin URL. Where it is
absent or unreachable the state is ``unknown``, never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from alembic.config import Config

# What to do about it — one sentence, so every surface that reports the problem
# also reports the same fix.
REMEDY = "run `dst migrate` (or `dst dev`, which migrates and then serves)"


def _clip(exc: Exception) -> str:
    """A driver error's first line, capped — `detail` is a one-line health field,
    and psycopg's connection failures run to a dozen lines of per-host retries."""
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return first if len(first) <= 160 else first[:157] + "..."


def alembic_config() -> Config:
    """The repo's alembic.ini with an absolute script_location.

    Both are force-included in the wheel (pyproject [tool.hatch.build.targets.wheel.
    force-include]), so this resolves in a `pip install dst-core` too.
    """
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    return cfg


@dataclass(frozen=True)
class SchemaState:
    """``status`` is the only thing callers should branch on.

    ``behind`` — the database has not caught up with this code; writes against the new
    columns fail. ``ahead`` — the database carries a revision this build has never
    heard of, i.e. older code on a newer schema, which is the SAFE deployment order
    (expand, then roll the code) and must never be refused. ``unknown`` — the question
    could not be asked; also never a refusal, because a database that is merely slow to
    come up is not a broken one.
    """

    status: Literal["ok", "behind", "ahead", "unknown"]
    current: str | None = None
    head: str | None = None
    pending: tuple[str, ...] = ()
    detail: str = ""

    def summary(self) -> str:
        """One line, for ``/ready`` and for any message that has to name this."""
        if self.status == "ok":
            return f"ok ({self.current})"
        if self.status == "ahead":
            return (
                f"ahead — the database is at {self.current}, newer than this build's "
                f"head {self.head} (older code on a newer schema: the safe order)"
            )
        if self.status == "unknown":
            return f"unknown — {self.detail}"
        if self.current is None:
            return f"BEHIND — no dst schema in this database; this build needs {self.head}"
        return (
            f"BEHIND — the database is at {self.current}, this build needs {self.head} "
            f"({len(self.pending)} unapplied)"
        )


def schema_state() -> SchemaState:
    """Compare ``alembic_version`` against this build's migration head."""
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    from services.config import settings

    try:
        head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    except Exception as exc:  # a build with no migrations shipped — say so, don't guess
        return SchemaState("unknown", detail=f"cannot read this build's migrations ({_clip(exc)})")

    engine = create_engine(settings.database_admin_url, connect_args={"connect_timeout": 2})
    try:
        with engine.connect() as conn:
            # to_regclass distinguishes "never migrated" from "database unreachable";
            # selecting the table blind would report both as the same failure.
            if conn.execute(text("SELECT to_regclass('alembic_version')")).scalar() is None:
                return SchemaState("behind", None, head, detail="no alembic_version table")
            current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception as exc:
        return SchemaState(
            "unknown", head=head, detail=f"cannot read alembic_version ({_clip(exc)})"
        )
    finally:
        engine.dispose()

    if current is None:
        return SchemaState("behind", None, head, detail="alembic_version is empty")
    if current == head:
        return SchemaState("ok", current, head)
    try:
        script = ScriptDirectory.from_config(alembic_config())
        pending = tuple(s.revision for s in script.iterate_revisions(head, current))
    except Exception:
        # This build's script directory has never heard of `current` — the database is
        # on a revision from a NEWER build. Safe, and not ours to refuse.
        return SchemaState("ahead", current, head)
    return SchemaState("behind", current, head, pending)


def serve_refusal(state: SchemaState) -> str:
    """Why ``dst serve`` will not start, what it would have cost, and the fix.

    A hard refusal, not a warning: the loss it prevents is unrecoverable (the traces
    were never written) and invisible to the caller, while the refusal costs one
    command and is impossible to miss. `dst dev` and the container entrypoint
    already migrate, so this fires only on the path that had no guard at all —
    `pip install -U dst-core` && `dst serve`.
    """
    where = (
        f"this database has no dst schema yet; this build needs {state.head}"
        if state.current is None
        else (
            f"this database is at {state.current}, this build needs {state.head} "
            f"({len(state.pending)} unapplied migration"
            f"{'s' if len(state.pending) != 1 else ''}: {', '.join(reversed(state.pending))})"
        )
    )
    return (
        f"error: the schema is behind this build — {where}.\n"
        "Serving anyway loses the audit trail in silence: answers are served correctly "
        "and get a request_id, but every request_log write fails in a background task "
        "where no caller can see it — and the review queue, drift audit, `dst test` "
        "and `dst correct` are all views over request_log.\n"
        f"{REMEDY[0].upper()}{REMEDY[1:]}, then start the server."
    )
