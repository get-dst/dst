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

## [0.1.0] — unreleased

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
