#!/usr/bin/env bash
# Pre-release mechanical gates, run against the ASSEMBLED PUBLIC CUT — not the
# dev tree, so the gates point at the artifact people actually download.
#
#   scripts/release_check.sh            full run (assembles the cut, then gates it)
#   scripts/release_check.sh --no-tests skip the cut-tree pytest (the slow gate)
#
# Every gate prints PASS / FAIL / SKIP. A SKIP is loud and never counts as
# green — it means a tool is absent here and CI must run it. The script exits
# non-zero if any hard gate FAILED; SKIPs alone exit 0 but are summarised so
# nothing hides.
#
# CI-only gates and manual sign-offs are not mechanised here; run them before
# tagging.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_TESTS=1
[ "${1:-}" = "--no-tests" ] && RUN_TESTS=0

CUT="$(mktemp -d)/dst-public"
trap 'rm -rf "$(dirname "$CUT")"' EXIT

FAILED=()
SKIPPED=()
PASSED=()

pass() { PASSED+=("$1"); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { FAILED+=("$1"); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
skip() { SKIPPED+=("$1"); printf '  \033[33mSKIP\033[0m  %s — %s\n' "$1" "$2"; }

_summarise() {
  echo
  echo "================ release-check summary ================"
  printf 'passed:  %d\n' "${#PASSED[@]}"
  printf 'skipped: %d%s\n' "${#SKIPPED[@]}" "$([ ${#SKIPPED[@]} -gt 0 ] && echo '  (LOUD — CI/tooling must cover these)')"
  for s in "${SKIPPED[@]}"; do echo "   - $s"; done
  printf 'failed:  %d\n' "${#FAILED[@]}"
  for f in "${FAILED[@]}"; do echo "   - $f"; done
  echo "======================================================"
}

echo "==> assembling the public cut (scripts/extract_public.sh + private-string gate)"
# extract_public.sh IS the denylist + private-identifier gate. If it exits
# non-zero the cut leaks something private or fails to lift — a hard stop; no
# later gate is meaningful against a tree that should not exist.
if ! "$ROOT/scripts/extract_public.sh" "$CUT" >/tmp/release_extract.log 2>&1; then
  fail "cut assembles + private-string gate (see /tmp/release_extract.log)"
  tail -5 /tmp/release_extract.log
  echo; echo "cut did not assemble — nothing downstream can be gated."
  exit 1
fi
pass "cut assembles + private-string gate"

echo "==> gating the assembled cut at $CUT"
cd "$CUT" || { fail "cd into cut"; exit 1; }

echo "==> uv sync (deps resolve in the cut tree alone)"
if uv sync -q >/tmp/release_sync.log 2>&1; then
  pass "uv sync"
else
  fail "uv sync (see /tmp/release_sync.log)"
  echo; echo "deps do not resolve in the cut — later Python gates are meaningless."
  _summarise; exit 1
fi

# --- G: the published tree imports (B1 in the audit — the one that shipped) ---
if uv run python -c "import services.app" 2>/tmp/release_import.log; then
  pass "import services.app"
else
  fail "import services.app (see /tmp/release_import.log)"
fi

# --- G: strict types, zero baseline ---
if uv run mypy services >/tmp/release_mypy.log 2>&1; then
  pass "mypy services (strict, zero)"
else
  fail "mypy services (see /tmp/release_mypy.log)"
fi

# --- G: lint + format over EVERY shipped dir, not just services/tests ---
# The audit: migrations/ scripts/ sit outside the dev gate; 0001_initial.py, the
# DDL that runs on customer Postgres, was unformatted. The cut must be clean
# over everything it ships that ruff understands.
LINT_DIRS=()
for d in services tests migrations scripts; do [ -d "$d" ] && LINT_DIRS+=("$d"); done
if uv run ruff check "${LINT_DIRS[@]}" >/tmp/release_ruff.log 2>&1 \
   && uv run ruff format --check "${LINT_DIRS[@]}" >>/tmp/release_ruff.log 2>&1; then
  pass "ruff check + format (${LINT_DIRS[*]})"
else
  fail "ruff check/format over shipped dirs (see /tmp/release_ruff.log)"
fi

# --- G: the wheel builds ---
if uv build >/tmp/release_build.log 2>&1; then
  pass "uv build (wheel + sdist)"
else
  fail "uv build (see /tmp/release_build.log)"
fi

# --- G: docs site builds strict (B6 — dead links / escaping paths) ---
if uv run --with mkdocs --with mkdocs-material python -m mkdocs build --strict \
     -f mkdocs.yml -d /tmp/release_site >/tmp/release_docs.log 2>&1; then
  pass "mkdocs build --strict"
elif grep -q "No module named" /tmp/release_docs.log 2>/dev/null; then
  skip "mkdocs build --strict" "mkdocs/material not resolvable — CI must run this"
else
  fail "mkdocs build --strict (see /tmp/release_docs.log)"
fi

# --- G: container does not run as root (B9) ---
if [ -f Dockerfile ]; then
  # The last USER wins; pass only if it names a non-root user or uid.
  LAST_USER="$(grep -iE '^\s*USER\s+' Dockerfile | tail -1 | awk '{print $2}')"
  if [ -n "$LAST_USER" ] && [ "$LAST_USER" != "root" ] && [ "$LAST_USER" != "0" ]; then
    pass "Dockerfile drops root (USER $LAST_USER)"
  else
    fail "Dockerfile runs as root (no non-root USER) — B9"
  fi
else
  skip "Dockerfile non-root" "no Dockerfile in cut"
fi

# --- G: no shipped default DB password (B5) ---
# A login-capable role password living in a public tree, or a compose soft
# default (:-) that boots on it, is an automatic procurement finding.
# .env.example is deliberately out of scope: a documented LOCAL-dev DSN default
# is standard practice (postgres:postgres class); what must never carry the
# string is the DDL that runs on production clusters and the deploy configs.
PWHITS="$(grep -rn "dst_app_dev" migrations deploy 2>/dev/null || true)"
if [ -z "$PWHITS" ]; then
  pass "no shipped default DB password"
else
  fail "shipped default DB password present — B5"
  echo "$PWHITS" | sed 's/^/        /'
fi

# --- G: full suite in the cut tree (needs Postgres; the slow gate) ---
if [ "$RUN_TESTS" = 1 ]; then
  echo "==> pytest in the cut tree (needs a reachable Postgres)"
  if uv run pytest -q >/tmp/release_pytest.log 2>&1; then
    pass "pytest (cut tree)"
  else
    # Distinguish 'no DB here' from real failures — the audit's point that
    # ci-clean skipping DB tests and exiting 0 is itself a vacuous gate.
    if grep -qiE "could not connect|connection refused|Postgres not reachable" /tmp/release_pytest.log; then
      skip "pytest (cut tree)" "Postgres unreachable here — CI service container must run it"
    else
      fail "pytest in the cut tree (see /tmp/release_pytest.log)"
      tail -15 /tmp/release_pytest.log | sed 's/^/        /'
    fi
  fi
else
  skip "pytest (cut tree)" "--no-tests"
fi

_summarise

if [ ${#FAILED[@]} -gt 0 ]; then
  echo "release-check: NOT GREEN — ${#FAILED[@]} hard gate(s) failed."
  exit 1
fi
echo "release-check: mechanical gates green. Accuracy probes and manual"
echo "sign-offs are NOT covered here — run them before tagging."
exit 0
