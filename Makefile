.PHONY: install hooks up down lint fmt test dev migrate seed ci ci-clean ci-newest benchmark benchmark-selftest

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

# Every line of Python that ships. `migrations/` was outside the old scope for its
# whole life, which is the wrong one to leave unchecked: it is the code that runs
# DDL against a customer's production database. Stated once so lint, fmt and ci
# cannot drift apart, which is how the gap opened in the first place.
LINT_SCOPE := services tests migrations scripts hatch_build.py

lint:           ## ruff + format check + mypy + genuineness gate
	uv run ruff check $(LINT_SCOPE)
	uv run ruff format --check $(LINT_SCOPE)
	uv run mypy services
	uv run python scripts/genuine_lint.py

fmt:            ## auto-format
	uv run ruff format $(LINT_SCOPE)
	uv run ruff check --fix $(LINT_SCOPE)

test:           ## run backend tests
	uv run pytest

DATA ?= ./data
benchmark:      ## run the proving-ground benchmark (DATA=<generator csv root>; needs ANTHROPIC_API_KEY)
	uv run python -m services.benchmark --data $(DATA)

benchmark-selftest: ## offline harness self-test (no API key, scripted LLM)
	uv run python -m services.benchmark --data $(DATA) --offline --no-dst --out /tmp/benchmark-selftest

# The gate's own honesty clause. Without a database the suite degrades to
# "skip what needs one and report the rest" (tests/conftest.py) — right for a
# person reading the repo, fatal for a gate, which would then pass while proving
# nothing. DST_TEST_REQUIRE_DB=1 turns that degradation into an immediate refusal
# naming `make up`. It applies to every ci target, with no exemption for a change
# that "only touches docs": the gate that decides what a green run means cannot
# also be the thing deciding which runs need to be green.
REQUIRE_DB := DST_TEST_REQUIRE_DB=1

ci:             ## the local gate: every check, real exit codes, no pipes
	uv run ruff check $(LINT_SCOPE)
	uv run ruff format --check $(LINT_SCOPE)
	$(REQUIRE_DB) uv run pytest  # NOT -q: addopts already carries one, and -qq eats the pass/fail counts
	uv run mypy services  # strict: the baseline is zero and stays zero
	uv run python scripts/genuine_lint.py  # real exit code, no pipe
	uv run python -m scripts.gen_env_example --check  # .env.example must match Settings

# Per-tree clone path, so two worktrees running this at once don't clone over
# each other and report the other run's failures.
CI_CLONE := /tmp/dst-ci-clone-$(notdir $(CURDIR))

ci-clean:       ## clean-clone gate: committed HEAD must work from scratch
	rm -rf $(CI_CLONE)
	git clone -q --depth 1 "file://$(CURDIR)" $(CI_CLONE)
	cd $(CI_CLONE) && uv sync -q && $(REQUIRE_DB) uv run pytest
	@echo "clean clone: green ($(CI_CLONE))"

# The lockfile pins sqlglot and duckdb at their floors, so `ci` proves the floor.
# A fresh `pip install dst-core` resolves the newest versions pyproject allows,
# and sqlglot's optimizer has changed behaviour between minor releases (the SQL
# equivalence proof, gold attribution and the guards all read through it). This
# runs the suite with every other package at its locked version and those two
# lifted to the newest the declared range allows.
NEWEST_VENV := /tmp/dst-newest-$(notdir $(CURDIR))

ci-newest:      ## the suite on the newest sqlglot + duckdb pyproject allows
	rm -rf $(NEWEST_VENV)
	uv venv -q $(NEWEST_VENV)
	uv export -q --frozen --no-hashes --no-emit-project -o $(NEWEST_VENV)/locked.txt
	uv pip install -q --python $(NEWEST_VENV)/bin/python -r $(NEWEST_VENV)/locked.txt
	uv pip install -q --python $(NEWEST_VENV)/bin/python --upgrade-package sqlglot --upgrade-package duckdb -e .
	$(NEWEST_VENV)/bin/python -c "import sqlglot, duckdb; print('sqlglot', sqlglot.__version__, '/ duckdb', duckdb.__version__)"
	DST_TEST_DB=dst_test_newest_$(subst -,_,$(notdir $(CURDIR))) $(REQUIRE_DB) $(NEWEST_VENV)/bin/python -m pytest


dev:            ## run the backend with reload
	uv run uvicorn services.app:app --reload --port 8000

migrate:        ## apply DB migrations (+ app-role password sync from DATABASE_URL)
	uv run dst migrate

seed:           ## seed a minimal dev org + admin token
	uv run dst bootstrap

