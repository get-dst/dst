.PHONY: install hooks up down lint fmt test dev migrate seed release-check

install:        ## install backend deps into the uv venv
	uv sync
	@$(MAKE) --no-print-directory hooks

hooks:          ## install the git hooks (credential gate on commit)
	@git config core.hooksPath .githooks
	@echo "git hooks -> .githooks (pre-commit: credential gate)"

up:             ## start local infra (Postgres + pgvector)
	docker compose up -d

down:           ## stop local infra
	docker compose down

lint:           ## ruff + format check + mypy + genuineness gate
	uv run ruff check services tests
	uv run ruff format --check services tests
	uv run mypy services
	uv run python scripts/genuine_lint.py

fmt:            ## auto-format
	uv run ruff format services tests
	uv run ruff check --fix services tests

test:           ## run backend tests
	uv run pytest

DATA ?= ./data
benchmark:      ## run the proving-ground benchmark (DATA=<generator csv root>; needs ANTHROPIC_API_KEY)
	uv run python -m services.benchmark --data $(DATA)

benchmark-selftest: ## offline harness self-test (no API key, scripted LLM)
	uv run python -m services.benchmark --data $(DATA) --offline --no-dst --out /tmp/benchmark-selftest

ci:             ## the local gate: every check, real exit codes, no pipes
	uv run ruff check services tests
	uv run ruff format --check services tests
	uv run pytest  # NOT -q: addopts already carries one, and -qq eats the pass/fail counts
	uv run mypy services  # strict: the baseline is zero and stays zero
	uv run python scripts/genuine_lint.py  # real exit code, no pipe

# Per-tree clone path, so two worktrees running this at once don't clone over
# each other and report the other run's failures.
CI_CLONE := /tmp/dst-ci-clone-$(notdir $(CURDIR))

ci-clean:       ## clean-clone gate: committed HEAD must work from scratch
	rm -rf $(CI_CLONE)
	git clone -q --depth 1 "file://$(CURDIR)" $(CI_CLONE)
	cd $(CI_CLONE) && uv sync -q && uv run pytest
	@echo "clean clone: green ($(CI_CLONE))"

release-check:  ## pre-release mechanical gates against the ASSEMBLED public cut
	./scripts/release_check.sh

dev:            ## run the backend with reload
	uv run uvicorn services.app:app --reload --port 8000

migrate:        ## apply DB migrations (+ app-role password sync from DATABASE_URL)
	uv run dst migrate

seed:           ## seed a minimal dev org + admin token
	uv run dst bootstrap

