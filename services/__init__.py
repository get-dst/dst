"""dst backend (FastAPI modular monolith)."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The DIST is dst-core; `dst` is only the console script (and a squatted
    # PyPI name — asking for it would report a stranger's version).
    __version__ = version("dst-core")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "dev"
