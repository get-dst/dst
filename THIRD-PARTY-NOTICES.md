# Third-party notices

This file lists third-party material redistributed with dst. The dst source
code itself is licensed under Apache-2.0 (see [LICENSE](LICENSE) and
[NOTICE](NOTICE)).

## Bundled dashboard (built artifact)

The released wheel and container image include the compiled dashboard
(`services/web_dist/`), which bundles its npm dependencies — notably the
**Inter** typeface (`@fontsource/inter`), copyright The Inter Project Authors,
licensed under the SIL Open Font License 1.1
(<https://openfontlicense.org>), and the runtime portions of the packages
listed in `apps/web/package.json`, each under its own license (React and the
rest are MIT unless noted in their packages). The font files are vendored into
the bundle precisely so that the dashboard loads nothing from third-party
CDNs at runtime.

## jaffle_shop (dbt Labs)

`fixtures/jaffle/` is a derivative of
[dbt-labs/jaffle_shop](https://github.com/dbt-labs/jaffle_shop), the demo dbt
project published by dbt Labs, Inc. under the Apache License 2.0. It has been
modified for dst: the schema is extended and the seed data regenerated. The
prebuilt demo database `fixtures/jaffle_shop.duckdb` is built from those seeds
and models and is a derivative of the same work.

Copyright dbt Labs, Inc. Licensed under the Apache License 2.0; the upstream
license text is carried verbatim at
[fixtures/jaffle/LICENSE](fixtures/jaffle/LICENSE).

## Vendor logos

`apps/web/src/logos/` contains logo marks of third-party products (Snowflake,
dbt, Google BigQuery, DuckDB, MySQL, PostgreSQL, GitHub) that dst integrates
with. They are used solely to identify those integrations in the dashboard. All
trademarks, service marks and logos are the property of their respective
owners; their use here does not imply any affiliation with, or endorsement by,
those owners. A mark is removed on request from its owner — open an issue or
write to security@dataservetool.com.
