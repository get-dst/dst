# syntax=docker/dockerfile:1

# ---- Stage 1: build the React SPA ----
FROM node:22-slim AS web
WORKDIR /web
RUN npm install -g pnpm@11.0.9
COPY apps/web/package.json apps/web/pnpm-lock.yaml apps/web/.npmrc apps/web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/web/ ./
# Vite inlines these at build time. Empty VITE_API_URL => same-origin API calls.
ARG VITE_CLERK_PUBLISHABLE_KEY=""
ARG VITE_API_URL=""
ENV VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY \
    VITE_API_URL=$VITE_API_URL
RUN pnpm build

# ---- Stage 2: Python runtime (API + serves the SPA) ----
FROM python:3.12-slim AS app
# UV_COMPILE_BYTECODE: precompile .pyc at build time — the venv is root-owned
# and the runtime user can't write into it, so imports would otherwise skip
# bytecode caching on every start.
ENV PYTHONUNBUFFERED=1 \
    UV_FROZEN=1 \
    UV_NO_DEV=1 \
    UV_COMPILE_BYTECODE=1 \
    DST_WEB_DIST=/app/web \
    PORT=8000
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.9.0 /uv /usr/local/bin/uv

# Install dependencies first for layer caching. UV_EXTRA opts one pyproject
# extra into the image (e.g. --build-arg UV_EXTRA=local-embed for in-process
# embeddings — the stock image is weight-free and skips the fastembed SDK, so
# a {"type":"local"} embed provider resolves only when built with this).
ARG UV_EXTRA=""
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project ${UV_EXTRA:+--extra $UV_EXTRA}

# App code + assets the runtime needs. README/LICENSE/NOTICE/
# THIRD-PARTY-NOTICES.md/hatch_build.py are package build inputs — the
# project install below fails without them.
COPY README.md LICENSE NOTICE THIRD-PARTY-NOTICES.md hatch_build.py ./
COPY services/ ./services/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY fixtures/ ./fixtures/
# Install the project itself: puts `dst` on PATH (the compose header and
# docs say `docker compose exec app dst …`) and registers the package
# metadata `services.__version__` reads — without this the image reports "dev".
RUN uv sync --frozen --no-dev ${UV_EXTRA:+--extra $UV_EXTRA}
ENV PATH="/app/.venv/bin:$PATH"
COPY --from=web /web/dist ./web
COPY docker/entrypoint.sh /entrypoint.sh

# Run as a non-root user. Serving writes only to Postgres, so /app stays
# root-owned and read-only; the home directory exists for the optional
# local-embed build, whose model cache lands under ~/.cache.
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/dst \
    --shell /usr/sbin/nologin dst
ENV HOME=/home/dst
USER 10001

EXPOSE 8000
CMD ["/entrypoint.sh"]
