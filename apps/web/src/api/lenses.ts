import { useMutation, useQuery } from '@tanstack/react-query'
import { apiGet, apiPost } from './client'

export interface LensSummary {
  name: string
  display_name: string
  description: string
  status: string
  /** Shape of the lens (published bundle if live, else draft). */
  entity_count: number
  definition_count: number
  question_count: number
  /** Usage, from the request log. */
  query_count: number
  last_queried_at: string | null
  /** False = created through the API, never applied from files — server-only until adopted. */
  from_files: boolean
}

export interface Citation {
  type: string
  ref: string
}

export interface VerificationCheck {
  name: string
  status: 'pass' | 'fail' | 'skip'
  reason: string | null
}

export interface VerificationReport {
  grade: 'verified' | 'partial' | 'unverified'
  checks: VerificationCheck[]
}

export interface CertifiedProvenance {
  cert_id: string
  certified_by: string
  certified_at: string
  /** Template serves only: the caller's values bound into the approved SQL shape. */
  bound_values: Record<string, string> | null
}

/** Signed record of a serve — POST it back to /v1/verify-receipt to check it. */
export interface Receipt {
  request_id: string
  lens: string
  served_at: string
  certification: 'certified' | 'assisted' | 'none'
  cert_id: string | null
  confidence: string | null
  sql_sha256: string | null
  data_as_of: string | null
  /** HMAC over the other fields; null = the server had no signing key. */
  digest: string | null
}

export interface QueryResponse {
  lens: string
  answer: string
  data: { columns: string[]; rows: unknown[][] } | null
  sql: string | null
  definition_used: string | null
  citations: Citation[]
  confidence: 'verified' | 'partial' | 'unverified' | null
  verification: VerificationReport | null
  certification: 'certified' | 'assisted' | 'none'
  certified_provenance: CertifiedProvenance | null
  trust_summary: string | null
  data_as_of: string | null
  receipt: Receipt | null
  request_id: string
}

export function useLenses() {
  return useQuery({ queryKey: ['lenses'], queryFn: () => apiGet<LensSummary[]>('/mgmt/lenses') })
}

export function useAskLens(name: string) {
  return useMutation({
    mutationFn: (q: string) => apiPost<QueryResponse>(`/v1/lenses/${name}/query`, { q }),
  })
}

// Publishing is what `dst apply` does — the UI never publishes a lens
// /mgmt/lenses/{name}/publish stays for the CLI and the cloud plugin.

export interface SemanticModelShape {
  dialect: string
  entities: { name: string; source: { table: string }; fields: { name: string; type: string }[] }[]
  definitions: { term: string; body: string; sql_expr?: string | null }[]
  sample_queries: { question: string }[]
  /** "Use this when…" — example asks that the router scores to route to this lens. */
  use_when?: string[]
  ai_instructions?: string | null
  [k: string]: unknown
}

export interface ModelConfigShape {
  /** null = unset, the default: the lens follows this install's own smart tier
   * rather than pinning a vendor the deployment may not have configured. */
  provider: string | null
  model: string | null
  temperature: number
  max_rows_to_compose: number
  /** Server-side directory of certified-definition pages for this lens. */
  certified_dir?: string | null
  /** How loosely the lens may answer — the one knob a lens owner sets (steering
   * prose lives in instructions). strict = answer only when well-grounded, decline/flag more;
   * balanced = default; exploratory = more willing to attempt, surfaces partials. */
  answer_mode?: 'strict' | 'balanced' | 'exploratory'
}

export interface AccessRuleShape {
  caller?: string | null
  group?: string | null
}

export interface LensConfigShape {
  access: { allow: AccessRuleShape[] }
  model: ModelConfigShape
  instructions?: string | null
  /** The warehouse connection(s) this lens reads — first is primary. */
  connections?: string[]
  [k: string]: unknown
}

export interface LensBundleShape {
  config: LensConfigShape
  semantic_model: SemanticModelShape
}

export interface LensDetail {
  name: string
  display_name: string
  description: string
  status: string
  draft: LensBundleShape | null
  published: LensBundleShape | null
}

export function useLensDetail(name: string) {
  return useQuery({
    queryKey: ['lens', name],
    queryFn: () => apiGet<LensDetail>(`/mgmt/lenses/${name}`),
    enabled: !!name,
  })
}

export interface ValidationIssue {
  severity: 'error' | 'warning'
  code: string
  message: string
}

export interface ValidationReport {
  ok: boolean
  issues: ValidationIssue[]
  probes: { question: string; ok: boolean; reason: string | null }[]
  conflicts: { term: string; source: string }[]
}

export function useValidateLens(name: string) {
  return useMutation({
    mutationFn: () => apiPost<ValidationReport>(`/mgmt/lenses/${name}/validate`, {}),
  })
}

// A lens bundle is authored in `lenses/<name>/` and landed by apply; the SPA has
// no PUT path onto it.
