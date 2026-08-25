import { useQuery } from '@tanstack/react-query'
import { apiGet } from './client'

export interface Kpis {
  queries: number
  ai_cost_usd: number
  warehouse_cost_usd: number
  input_tokens: number
  output_tokens: number
  /** status='error' only — faults. Declines are NOT errors. */
  errors: number
  /** refused + clarification + rejected: governed declines (authoring/scope signal). */
  declined: number
  /** Requests whose model had no configured price — AI spend unknown, not $0. */
  unpriced: number
  outcomes: {
    ok: number
    refused: number
    clarification: number
    rejected: number
    error: number
  }
}

export interface CallerRow {
  caller: string
  queries: number
  /** ai + warehouse, blended — kept for totals. */
  cost_usd: number
  ai_cost_usd: number
  wh_cost_usd: number
  /** Requests whose model had no configured price — their AI spend is unknown, not $0. */
  unpriced: number
  /** Faults only (status='error'), matching Kpis.errors. */
  errors: number
  declined: number
}

export function useKpis() {
  return useQuery({ queryKey: ['kpis'], queryFn: () => apiGet<Kpis>('/mgmt/observe/kpis') })
}

export interface AuditLensRow {
  lens: string
  asked: number
  answered: number
  /** Verified share of SERVED answers — null when the lens served nothing. */
  verified_pct: number | null
  cost_usd: number
  owner: string
  /** The standing drift mark, when set — the row's state chip reads it. */
  degraded: string | null
  /** Latest regression-gate score, when a gated run exists. */
  gate_score: number | null
}

export interface AuditStatement {
  window_days: number
  asked: number
  answered: number
  clarified: number
  refused: number
  faults: number
  yield_pct: number | null
  verified_pct: number | null
  /** Deltas exist only when both windows carried traffic — never invented. */
  yield_delta_pp: number | null
  verified_delta_pp: number | null
  ai_cost_usd: number
  wh_cost_usd: number
  cost_per_answer_usd: number | null
  confidence_histogram: Record<string, number>
  series: { day: string; asked: number }[]
  lenses: AuditLensRow[]
  certified_active: number
  open_incident_tickets: number
}

export function useAuditStatement(days: number) {
  return useQuery({
    queryKey: ['audit-statement', days],
    queryFn: () => apiGet<AuditStatement>(`/mgmt/observe/audit?days=${days}`),
  })
}

export function useCallerReport(lens?: string) {
  const qs = lens ? `?lens=${encodeURIComponent(lens)}` : ''
  return useQuery({
    queryKey: ['callers', lens ?? ''],
    queryFn: () => apiGet<CallerRow[]>(`/mgmt/observe/callers${qs}`),
  })
}

export interface EvalRunPoint {
  mode: 'regression' | 'health' | string
  score: number | null
  passed: number
  failed: number
  errored: number
  started_at: string | null
}

export interface LensEvalTrend {
  lens: string
  latest_score: number | null
  runs: EvalRunPoint[]
}

/** Per-lens accuracy trend from eval runs. */
export function useEvalTrend() {
  return useQuery({
    queryKey: ['eval-trend'],
    queryFn: () => apiGet<LensEvalTrend[]>('/mgmt/observe/evals'),
  })
}

export interface RequestSummary {
  request_id: string
  lens: string
  caller: string
  status: string
  row_count: number | null
  confidence: string | null
  cost_usd: number
  created_at: string | null
  question: string | null
}

/** The governed decomposition of a query's SQL (services/runtime/scope.py). */
export interface QueryScope {
  tables: string[]
  fields: string[]
  filters: string[]
  order_by: string[]
}

export interface RequestTrace extends RequestSummary {
  question: string
  sql: string | null
  scope: QueryScope | null
  answer: string | null
  citations: { type: string; ref: string }[] | null
  definition_used: string | null
  verification: {
    grade: 'verified' | 'partial' | 'unverified'
    checks: { name: string; status: 'pass' | 'fail' | 'skip'; reason: string | null }[]
  } | null
  certification: string | null
  latency: Record<string, number> | null
  ai_input_tokens: number | null
  ai_output_tokens: number | null
  ai_cost_usd: number | null
  wh_bytes: number | null
  wh_cost_usd: number | null
  error: string | null
}

export function useRequests(lens?: string, status?: string) {
  const params = new URLSearchParams()
  if (lens) params.set('lens', lens)
  if (status) params.set('status', status)
  const qs = params.toString() ? `?${params}` : ''
  return useQuery({
    queryKey: ['requests', lens ?? '', status ?? ''],
    queryFn: () => apiGet<RequestSummary[]>(`/mgmt/observe/requests${qs}`),
  })
}

export function useRequestDetail(id: string | null) {
  return useQuery({
    queryKey: ['request', id],
    queryFn: () => apiGet<RequestTrace>(`/mgmt/observe/requests/${id}`),
    enabled: !!id,
  })
}
