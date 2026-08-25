"""Plugin seam.

A package extends the core by declaring an entry point in the ``dst.plugins``
group that resolves to a ``register(app: FastAPI)`` callable — this is how a
package such as ``dst-cloud`` mounts onto a stock core.
Plugins load after every core router, so they can never shadow a core route; a
plugin that raises on load or register is logged and skipped — core always boots.

Mounting is never SILENT. `pip install some-dst-plugin` changes an install's
route table, and until an operator can see that from the outside it is a change
nobody signed off on: every mount logs its name, distribution, version and how many
routes it added, and `/ready` reports what is running.

Open by default, lockable. `DST_PLUGINS` unset means every installed plugin
mounts — a closed default would make every deployment that depends on a plugin
also depend on remembering one env var, where forgetting it means half the
product silently missing. That is the same
disease (silence) one layer up. And an allowlist is not a security boundary here:
entry points only resolve for packages already installed in this environment, which
already run their own code at import. What it is good for is intent — so setting
`DST_PLUGINS` pins the route table to exactly those names, and anything else
installed is refused out loud.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.metadata import entry_points

from fastapi import FastAPI

from services.config import settings

log = logging.getLogger("dst")


@dataclass(frozen=True)
class LoadedPlugin:
    """One mounted plugin, as an operator needs to see it: which entry point, which
    distribution shipped it, and how much of the route table is now its."""

    name: str
    dist: str
    version: str
    routes: int

    def __str__(self) -> str:
        return f"{self.name} ({self.dist} {self.version}, +{self.routes} routes)"


loaded: list[LoadedPlugin] = []


def _allowlist() -> frozenset[str] | None:
    """The names `DST_PLUGINS` pins, or None when it is unset (load everything).
    An empty value is a real answer — "no plugins" — not an accident to ignore."""
    if settings.plugins is None:
        return None
    return frozenset(n.strip() for n in settings.plugins.split(",") if n.strip())


def load_plugins(app: FastAPI) -> list[LoadedPlugin]:
    """Resolve and invoke every ``dst.plugins`` entry point against ``app``."""
    loaded.clear()
    allowed = _allowlist()
    for ep in entry_points(group="dst.plugins"):
        dist = getattr(ep, "dist", None)
        if allowed is not None and ep.name not in allowed:
            log.warning(
                "plugin %r (%s) is installed but not in DST_PLUGINS — not mounted",
                ep.name,
                getattr(dist, "name", "unknown distribution"),
            )
            continue
        before = len(app.routes)
        try:
            ep.load()(app)
        except Exception:
            log.exception("plugin %r failed to register; skipping", ep.name)
        else:
            loaded.append(
                LoadedPlugin(
                    ep.name,
                    getattr(dist, "name", "unknown"),
                    getattr(dist, "version", "unknown"),
                    len(app.routes) - before,
                )
            )
            log.info("plugin registered: %s", loaded[-1])
    if allowed is not None and not loaded:
        log.warning("DST_PLUGINS=%r mounted nothing", settings.plugins)
    return list(loaded)


def status() -> str:
    """What `/ready` reports: the mounted plugins, or why there are none."""
    if loaded:
        return ", ".join(f"{p.name}@{p.version}" for p in loaded)
    return "none" if _allowlist() is None else "none (DST_PLUGINS)"
