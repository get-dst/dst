// dst's Cube Core config, mounted at /cube/conf/cube.js.
//
// Groups: dst mints each request's token with `{dst: {groups: [...]}}`, and a
// view's `access_policy` grants by `group:`. contextToGroups is the bridge, so
// a hidden member with exception groups reaches those groups and no one else.
// A Cube without it matches no named group: the exception fails closed.
//
// A DuckDB warehouse is opened READ-ONLY. Cube's DuckDB driver opens its
// databasePath read-write (no access mode option), which takes DuckDB's
// exclusive lock - so dst's own connector could not open the file beside it -
// and lets Cube's bundled DuckDB checkpoint the file. Instead the driver opens
// an in-memory database and ATTACHes the file READ_ONLY in its initSql.
// Set DST_DUCKDB_PATH to the file as the container sees it; leave it unset for
// any other warehouse and Cube's own CUBEJS_DB_* settings apply unchanged.
const path = process.env.DST_DUCKDB_PATH;

const contextToGroups = ({ securityContext }) => {
  const groups = securityContext && securityContext.dst && securityContext.dst.groups;
  return Array.isArray(groups) ? groups.map(String) : [];
};

module.exports = path
  ? {
      contextToGroups,
      driverFactory: () => {
        const { DuckDBDriver } = require('@cubejs-backend/duckdb-driver');
        return new DuckDBDriver({
          databasePath: ':memory:',
          initSql: `ATTACH '${path.replace(/'/g, "''")}' AS wh (READ_ONLY); USE wh;`,
        });
      },
    }
  : { contextToGroups };
