/**
 * Warehouse data-connection specs.
 *
 * One spec per warehouse type drives the connection wizard: the credential form
 * (fields + secret), the required privileges panel, and the side-by-side setup
 * guide (how to create the service account / role, which grants, where to click).
 * The backend mirror is services/connectors/<type>.py + the create-time evaluation.
 */

export interface WarehouseField {
  key: string
  label: string
  placeholder?: string
  required?: boolean
  mono?: boolean
  numeric?: boolean
  /** Render two-per-row instead of full width. */
  half?: boolean
  defaultValue?: string
  /** One-line helper under the field. */
  help?: string
}

export interface WarehouseSecret {
  label: string
  placeholder: string
  required?: boolean
  /** Service-account JSON etc. — render a textarea. */
  multiline?: boolean
}

/** A numbered step in the right-hand setup guide. */
export interface GuideStep {
  title: string
  detail?: string
  /** Optional copy-able snippet (SQL / shell). */
  code?: string
}

export interface WarehouseSpec {
  /** Stable key — matches the backend connection `type`. */
  type: string
  label: string
  blurb: string

  /** Non-secret connection fields (host, project, …). */
  fields: WarehouseField[]
  /** The encrypted credential, if any (duckdb has none). */
  secret?: WarehouseSecret

  /**
   * Privileges the credential needs, split by access level. The write rows are
   * highlighted in the panel when the user enables Write access.
   */
  permissions: { read: string[]; write: string[] }

  /** Step-by-step provider setup, shown alongside the form. */
  guide: GuideStep[]
  /** "Open provider docs" deep link. */
  docsUrl?: string
  docsLabel?: string

  /** Map collected field values → the backend `config` object. */
  toConfig: (values: Record<string, string>) => Record<string, unknown>
}
