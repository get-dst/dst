# Security Policy

dst stores warehouse and context-source credentials (encrypted at rest with
`DST_SECRET_KEY`) and executes model-generated SQL against your warehouse
behind a guard layer. Bugs in credential handling, the SQL guard, tenant
isolation (Postgres RLS), or caller-key auth are high severity — please report
them privately.

## Reporting a vulnerability

- **Preferred:** GitHub → Security → "Report a vulnerability" (private advisory).
- **Email:** security@dataservetool.com — include steps to reproduce and the
  version (`dst --version`, or `version` from `/health`).

You'll get an acknowledgement within 72 hours. Please don't open public issues
for suspected vulnerabilities, and allow a fix to ship before disclosure.

## Supported versions

Pre-1.0: the latest release only. Pin image tags in production and follow the
[upgrade guide](docs/upgrading.md) — migrations are versioned and
advisory-locked.
