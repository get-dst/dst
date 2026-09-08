# Changelog

All notable changes to dst are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file begins at the open-source launch. dst was built privately before that, and
none of the earlier history is reconstructed here.

## The upgrade contract

**Migrate before you serve.** The database schema and the code ship together:

```bash
pip install -U dst-core
dst migrate
# then restart the server
```

`dst dev` migrates automatically, and in containers the entrypoint does it for you
(`DST_MIGRATE_ON_START`, default `true`); orchestrated deploys set that to `false`
and run `dst migrate` once per release.

`dst serve` **refuses to start against a schema behind its build**, by design. A
server on an out-of-date schema answers questions correctly and loses every trace in
silence — the audit trail, the review queue, the drift audit and `dst test` are all
views over the request log. The refusal names the current revision, the one the build
needs, and the unapplied list, then tells you to run `dst migrate`.

Full upgrade, rollback and restore paths: **[docs/upgrading.md](docs/upgrading.md)**.

## [0.2.0] — 2026-09-07

Schema changes ship with this release: run `dst migrate` before serving (the
upgrade contract above).

### Changed — relationships are their own files

A join is a fact about a *pair* of entities, so it no longer lives inside one
of them. Each pair gets one file:

```yaml
# semantic/relationships/orders__customers.yaml
left: orders          # the FK side (the many side of many_to_one)
right: customers
"on": orders.customer_id = customers.customer_id
type: left
relationship: many_to_one
```

**To migrate an existing project:** move each entity's `joins:` list into
per-pair files — `left:` is the entity that carried the block, the other
fields are unchanged. An entity file still carrying `joins:` fails at plan
and apply with a pointer to the new home; nothing is silently ignored.

What one home per pair buys:

- Declaring the same two entities twice — reversed direction included — is
  refused at plan and apply as a competing claim, instead of both declarations
  silently compiling.
- A lens gets the join exactly when it selects both endpoints; lens files are
  unchanged.
- Staleness and re-verification get sharper: adding, editing or deleting a
  relationship recompiles exactly the published lenses that select both
  endpoints, and flags for re-verification exactly the certified answers
  whose SQL joins over it.
- `dst drift` names the relationships a schema change breaks (a dropped
  column referenced in an ON clause, a dropped table a pair joins through).
- `dst import dbt` and OSI import/export read and write the new shape;
  `dst introspect --check-joins` measures the declared cardinalities as
  before.

### Added

- **Ambiguity that resolves itself.** An ambiguous definition can declare
  `audiences:` — which meaning each audience of a lens gets — and a question
  that already settles the ambiguity is answered deterministically instead of
  triggering a clarify round. Genuinely open ambiguity still asks.
- **Freshness measures coverage, not just recency.** Profiling records the
  date coverage of a table, and a period the warehouse never loaded is
  disclosed in the answer rather than served as a silent zero.
- **Certified corrections converge.** A reviewed correction wins over the
  answer it corrects, each question holds one certified pair, and
  un-certifying is a first-class path back out.
- A composed answer whose prose was cut off by the token cap now says so
  instead of ending mid-sentence as if complete.

### Fixed

- Numbers formatted in locales that use space grouping and decimal commas
  (e.g. `1 234,56`) ground correctly during answer verification.
- A filter the lens demands but the question never bound is waived with a
  disclosure instead of dead-ending the request.
- A cost total that could not be fully counted is labeled as such instead of
  reading as a counted total.
- A lens with an empty access allow-list reports that it refuses every
  caller, instead of implying admin-only access.
- Columns the model references in expressions count as modelled for the
  read allow-list.
- Authoring errors got sharper: a required key names what it is for, and the
  YAML flow-map comma trap (`{a: one, two}` inventing a phantom key) is
  named as such with the fix.
- The eval gate runs cases at concurrency 16 by default (was 4), so a
  full-corpus gate finishes in minutes.

## [0.1.1] — 2026-08-29

### Security

Dependency refresh clearing every open Dependabot advisory against the locked
development environment. User installs resolve dependencies freshly and were
already receiving the patched versions; this pins the repository's own
toolchain and CI to the same ground.

- `cryptography` 48.0.0 → 50.0.1 (PKCS#7 Bleichenbacher oracle, certificate
  path-building exhaustion, name-constraint wildcard escape)
- `mcp` 1.27.2 → 1.29.1 (WebSocket transport Host/Origin validation — dst
  serves MCP over streamable HTTP, so the vulnerable transport was never
  exposed, but the SDK moves past it regardless)
- `pyasn1` 0.6.3 → 0.6.4 (three decoder denial-of-service advisories)
- `pillow` 12.2.0 → 12.3.0 (development lockfile only — Pillow is not a
  dst-core dependency and never reaches an installed copy)

No schema changes: upgrading from 0.1.0 is `pip install --upgrade dst-core`
with nothing to migrate.

## [0.1.0] — 2026-08-25

The first public release. Nothing public preceded it, so this entry describes
what dst is, not what changed.

dst is the governed layer an AI calls instead of opening a raw connection to
the warehouse. A caller — the AI your team uses over MCP, or the agent inside
your product over MCP or REST — asks a natural-language question; dst grounds
it in your semantic files, generates SQL inside a lens's allow-list, executes
it read-only, and returns a cited answer with the SQL, a verification grade,
and the cost attached. What the lens cannot answer is declined, never guessed.

- **Lenses and semantic files are the truth.** Definitions, entities and
  access live in versioned files; `dst plan` shows what a change does and
  `dst apply` publishes it. Static inconsistency dies at apply, not at 2am.
- **Certified answers are regression tests.** A reviewed correction is served
  verbatim from then on and pinned by `dst test`, so a mistake fixed once
  stays fixed.
- **Every answer carries its receipt.** Provenance for the caller; a native
  audit ledger, review queue and drift audit for the platform engineer — all
  views over the same request log.
- **One pipeline for every caller.** MCP and REST enter the same answer path,
  so who asks changes access, never the answer.

Install `dst-core` from PyPI, or run the container image. `dst init` scaffolds
a project with a demo warehouse; the [quickstart](docs/quickstart.md) goes
from empty directory to a governed answer.
