import type { WarehouseSpec } from './types'

const trimmed = (v?: string) => (v ?? '').trim()

const bigquery: WarehouseSpec = {
  type: 'bigquery',
  label: 'BigQuery',
  blurb: 'Google Cloud data warehouse. Connect with a service-account key.',
  fields: [
    {
      key: 'project',
      label: 'GCP project',
      placeholder: 'my-project-id',
      required: true,
      mono: true,
    },
  ],
  secret: {
    label: 'Service-account JSON',
    placeholder: '{\n  "type": "service_account",\n  "project_id": "...",\n  ...\n}',
    required: true,
    multiline: true,
  },
  permissions: {
    read: [
      'BigQuery Data Viewer — read tables & schema',
      'BigQuery Job User — run queries',
      'BigQuery Resource Viewer — optional, enables query-history drift audits',
    ],
    write: ['BigQuery Data Editor — create & write tables'],
  },
  guide: [
    {
      title: 'Create a service account',
      detail:
        'Google Cloud Console → IAM & Admin → Service Accounts → Create service account. Name it e.g. dst-reader.',
    },
    {
      title: 'Grant the roles',
      detail:
        'On the project (or just the dataset), grant BigQuery Data Viewer and BigQuery Job User. Add BigQuery Data Editor only if you enable Write access.',
    },
    {
      title: 'Create a JSON key',
      detail: 'Open the service account → Keys → Add key → Create new key → JSON, and download it.',
    },
    {
      title: 'Paste it here',
      detail:
        'Paste the downloaded JSON into the Service-account JSON field, and set the project.',
    },
  ],
  docsUrl: 'https://cloud.google.com/iam/docs/service-accounts-create',
  docsLabel: 'Google Cloud service-account docs',
  toConfig: (v) => ({ project: trimmed(v.project) }),
}

const snowflake: WarehouseSpec = {
  type: 'snowflake',
  label: 'Snowflake',
  blurb: 'Snowflake cloud data platform. Connect with a role-scoped user.',
  fields: [
    {
      key: 'account',
      label: 'Account identifier',
      placeholder: 'orgname-account  ·  or  xy12345.us-east-1',
      required: true,
      mono: true,
    },
    { key: 'user', label: 'User', placeholder: 'DST', required: true, mono: true, half: true },
    {
      key: 'warehouse',
      label: 'Warehouse',
      placeholder: 'COMPUTE_WH',
      required: true,
      mono: true,
      half: true,
    },
    {
      key: 'database',
      label: 'Database',
      placeholder: 'ANALYTICS',
      required: true,
      mono: true,
      half: true,
    },
    {
      key: 'schema',
      label: 'Schema',
      placeholder: 'PUBLIC',
      defaultValue: 'PUBLIC',
      mono: true,
      half: true,
    },
  ],
  secret: { label: 'Password', placeholder: '••••••••', required: true },
  permissions: {
    read: [
      'USAGE on the warehouse, database & schema',
      'SELECT on the tables (or future tables)',
    ],
    write: ['CREATE TABLE on the schema — for the write probe'],
  },
  guide: [
    {
      title: 'Create a role and user',
      detail: 'Run in a Snowsight worksheet as ACCOUNTADMIN (or a role-admin).',
      code:
        "CREATE ROLE dst_reader;\nCREATE USER dst PASSWORD='••••••'\n  DEFAULT_ROLE = dst_reader;",
    },
    {
      title: 'Grant read access',
      code:
        'GRANT USAGE ON WAREHOUSE compute_wh TO ROLE dst_reader;\nGRANT USAGE ON DATABASE analytics TO ROLE dst_reader;\nGRANT USAGE ON SCHEMA analytics.public TO ROLE dst_reader;\nGRANT SELECT ON ALL TABLES IN SCHEMA analytics.public TO ROLE dst_reader;\nGRANT SELECT ON FUTURE TABLES IN SCHEMA analytics.public TO ROLE dst_reader;',
    },
    {
      title: 'Grant write access (optional)',
      detail: 'Only if you enable Write.',
      code: 'GRANT CREATE TABLE ON SCHEMA analytics.public TO ROLE dst_reader;',
    },
    {
      title: 'Assign the role & find your account',
      detail:
        'Assign the role, then copy the account identifier from Snowsight → bottom-left account menu → Account.',
      code: 'GRANT ROLE dst_reader TO USER dst;',
    },
  ],
  docsUrl: 'https://docs.snowflake.com/en/user-guide/admin-account-identifier',
  docsLabel: 'Snowflake account identifier docs',
  toConfig: (v) => ({
    account: trimmed(v.account),
    user: trimmed(v.user),
    warehouse: trimmed(v.warehouse),
    database: trimmed(v.database),
    schema: trimmed(v.schema) || 'PUBLIC',
  }),
}

const postgres: WarehouseSpec = {
  type: 'postgres',
  label: 'PostgreSQL',
  blurb: 'Any PostgreSQL database. Connect with a least-privilege login role.',
  fields: [
    {
      key: 'host',
      label: 'Host',
      placeholder: 'db.internal.example.com',
      required: true,
      mono: true,
    },
    {
      key: 'port',
      label: 'Port',
      placeholder: '5432',
      defaultValue: '5432',
      numeric: true,
      mono: true,
      half: true,
    },
    {
      key: 'database',
      label: 'Database',
      placeholder: 'analytics',
      required: true,
      mono: true,
      half: true,
    },
    { key: 'user', label: 'User', placeholder: 'dst', required: true, mono: true, half: true },
    {
      key: 'schema',
      label: 'Schema',
      placeholder: 'public',
      defaultValue: 'public',
      mono: true,
      half: true,
    },
  ],
  secret: { label: 'Password', placeholder: '••••••••', required: true },
  permissions: {
    read: ['CONNECT on the database', 'USAGE + SELECT on the schema (incl. future tables)'],
    write: ['CREATE on the schema — for the write probe'],
  },
  guide: [
    {
      title: 'Create a login role',
      detail: 'Connect as a superuser (psql) and create a dedicated role.',
      code: "CREATE ROLE dst LOGIN PASSWORD '••••••';",
    },
    {
      title: 'Grant read access',
      code:
        'GRANT CONNECT ON DATABASE analytics TO dst;\nGRANT USAGE ON SCHEMA public TO dst;\nGRANT SELECT ON ALL TABLES IN SCHEMA public TO dst;\nALTER DEFAULT PRIVILEGES IN SCHEMA public\n  GRANT SELECT ON TABLES TO dst;',
    },
    {
      title: 'Grant write access (optional)',
      detail: 'Only if you enable Write — lets dst create/drop its objects.',
      code: 'GRANT CREATE ON SCHEMA public TO dst;',
    },
    {
      title: 'Allow the network path',
      detail:
        'Make sure the host is reachable from dst (firewall / VPC allowlist) and SSL is enabled where required.',
    },
  ],
  docsUrl: 'https://www.postgresql.org/docs/current/sql-grant.html',
  docsLabel: 'PostgreSQL GRANT docs',
  toConfig: (v) => ({
    host: trimmed(v.host),
    port: Number(trimmed(v.port)) || 5432,
    database: trimmed(v.database),
    user: trimmed(v.user),
    schema: trimmed(v.schema) || 'public',
  }),
}

const mysql: WarehouseSpec = {
  type: 'mysql',
  label: 'MySQL',
  blurb: 'MySQL or MariaDB. Connect with a least-privilege user.',
  fields: [
    {
      key: 'host',
      label: 'Host',
      placeholder: 'db.internal.example.com',
      required: true,
      mono: true,
    },
    {
      key: 'port',
      label: 'Port',
      placeholder: '3306',
      defaultValue: '3306',
      numeric: true,
      mono: true,
      half: true,
    },
    {
      key: 'database',
      label: 'Database',
      placeholder: 'analytics',
      required: true,
      mono: true,
      half: true,
    },
    { key: 'user', label: 'User', placeholder: 'dst', required: true, mono: true, half: true },
  ],
  secret: { label: 'Password', placeholder: '••••••••', required: true },
  permissions: {
    read: ['SELECT on the database'],
    write: ['CREATE, INSERT, DROP on the database — for the write probe'],
  },
  guide: [
    {
      title: 'Create a user',
      detail: "Connect as root and create a dedicated user ('%' allows any host — scope it tighter if you can).",
      code: "CREATE USER 'dst'@'%' IDENTIFIED BY '••••••';",
    },
    {
      title: 'Grant read access',
      code: "GRANT SELECT ON analytics.* TO 'dst'@'%';\nFLUSH PRIVILEGES;",
    },
    {
      title: 'Grant write access (optional)',
      detail: 'Only if you enable Write.',
      code: "GRANT CREATE, INSERT, DROP ON analytics.* TO 'dst'@'%';\nFLUSH PRIVILEGES;",
    },
    {
      title: 'Allow the network path',
      detail: 'Ensure the host:port is reachable from dst (firewall / security-group allowlist).',
    },
  ],
  docsUrl: 'https://dev.mysql.com/doc/refman/8.0/en/grant.html',
  docsLabel: 'MySQL GRANT docs',
  toConfig: (v) => ({
    host: trimmed(v.host),
    port: Number(trimmed(v.port)) || 3306,
    database: trimmed(v.database),
    user: trimmed(v.user),
  }),
}

const duckdb: WarehouseSpec = {
  type: 'duckdb',
  label: 'DuckDB',
  blurb: 'In-process / local file. Great for the demo warehouse or a server-side file.',
  fields: [
    {
      key: 'path',
      label: 'Database file path',
      placeholder: 'fixtures/jaffle_shop.duckdb  (blank = built-in demo)',
      mono: true,
      help: 'A .duckdb file on the server. Leave blank to use the built-in jaffle demo warehouse.',
    },
  ],
  permissions: {
    read: ['Read access to the .duckdb file'],
    write: ['Write access to the .duckdb file on disk'],
  },
  guide: [
    {
      title: 'Point at a file',
      detail:
        'DuckDB is in-process — there is no server to provision. Give the path to a .duckdb file readable by the dst backend, or leave it blank to use the bundled jaffle demo.',
    },
    {
      title: 'Write access',
      detail:
        'Enable Write only if the file (and its directory) are writable by the server process — dst proves this by creating and dropping a temporary table.',
    },
  ],
  toConfig: (v) => (trimmed(v.path) ? { path: trimmed(v.path) } : {}),
}

/** Ordered list of warehouse types shown as connection tiles. */
export const WAREHOUSES: WarehouseSpec[] = [
  bigquery,
  snowflake,
  postgres,
  mysql,
  duckdb,
]

export const WAREHOUSES_BY_TYPE: Record<string, WarehouseSpec> = Object.fromEntries(
  WAREHOUSES.map((w) => [w.type, w]),
)

export type { WarehouseSpec, WarehouseField, GuideStep } from './types'
