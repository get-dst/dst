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
  /** Served queries whose connector reports no bytes/cost — unmetered, not free. */
  wh_unpriced: number
  outcomes: {
    ok: number
    refused: number
    clarification: number
    rejected: number
    error: number
  }
  /** Served answers by resolution tag — see AuditStatement.resolution_histogram. */
  resolution_histogram: Record<string, number>
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
  /** Certified + declared share of the answers the ledger GRADED — null when
   * none were (a pre-ledger lens reads untested, never 0% declared). */
  declared_pct: number | null
  cost_usd: number
  owner: string
  /** The standing drift mark, when set — the row's state chip reads it. */
  degraded: string | null
  /** Latest regression-gate score, when a gated run exists. */
  gate_score: number | null
  /** Typed share of the answers served since typed serving existed — null
   * when the lens served none of those. */
  typed_pct: number | null
}

/** A slot (column) that keeps clarifying for want of a value the question
 * could not type — a dictionary gap to profile, not a defect. */
export interface UnresolvedSlot {
  slot: string
  count: number
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
  /** Governed basis: % of GRADED answers whose figure came from a certified or
   * declared definition. The denominator excludes `unknown`. */
  declared_pct: number | null
  declared_delta_pp: number | null
  /** Answers the ledger never graded — recorded before the ledger existed.
   * Never "not governed": an ungraded row says nothing about its basis. */
  resolution_unknown: number
  /** Served answers by tag: certified | declared | mixed | inferred | unknown. */
  resolution_histogram: Record<string, number>
  /** Typed: % of answers where every slot was a closed-set decision the policy
   * acted on, a deterministic parse, or a caller-supplied binding — no
   * raw-SQL escalation ran. The denominator excludes `typed_unknown`. */
  typed_pct: number | null
  typed_delta_pp: number | null
  /** Answers served before typed serving existed. Counted apart — never
   * "untyped": a predating row says nothing about how it typed. */
  typed_unknown: number
  /** The slots that clarified for want of a complete value dictionary, most
   * frequent first (up to 8) — the profiling gaps to close. */
  unresolved_slots: UnresolvedSlot[]
  ai_cost_usd: number
  wh_cost_usd: number
  /** Calls whose model had no configured price — the spend totals are a FLOOR. */
  unpriced: number
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

/** One decider's calibration on one slot, from the latest `dst test` sweep
 * that recorded it (services/evals/calibration.py). Only `measured` rows
 * carry brier/ece; UNTESTED and "n too small" say so in `reason` — no number
 * is derived for them, and none comes from production traffic. */
export interface CalibrationEntry {
  n: number
  accuracy: number
  /** Decisions that carried no measured probability. */
  uncalibrated_n: number
  status: 'measured' | 'UNTESTED' | 'n too small'
  reason?: string
  brier?: number
  ece?: number
  bins?: { lo: number; hi: number; n: number; conf: number; acc: number }[]
  curve?: { threshold: number; coverage: number; error_among_acted: number }[]
}

export interface LensEvalTrend {
  lens: string
  latest_score: number | null
  runs: EvalRunPoint[]
  /** Keyed "<decider> [<slot>]"; null when no run recorded a calibration. */
  calibration: Record<string, CalibrationEntry> | null
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
  /** The ledger's headline tag; null for declines and faults. `unknown` means
   * recorded before the ledger existed (or attribution could not run). */
  resolution_tag: string | null
}

/** One part of an answer's meaning and where it came from
 * (services/contracts/resolution.py). `inferred` is the generator's own choice,
 * not a defect; `unknown` is "attribution could not run", never a guess. */
export interface ResolutionSlot {
  kind: 'metric' | 'definition' | 'dimension' | 'grain' | 'filter' | 'window'
  name: string
  source: 'certified' | 'declared' | 'inferred' | 'unknown'
  value: string | null
}

/** One measured closed-set decision on the way to the answer: the slot, what
 * was chosen, the probability the decider measured (null = nothing measured)
 * and the policy verdict. Clarify and decline are governed outcomes. */
export interface DecisionRecord {
  slot: string
  chosen: string | null
  p: number | null
  runner_up: number | null
  margin: number | null
  verdict: 'act' | 'clarify' | 'decline' | string
  provider: string
}

export interface Resolution {
  /** construction = the intent tier picked names from the model; attributed =
   * read back from the served SQL. */
  method: 'construction' | 'attributed'
  slots: ResolutionSlot[]
  tag: string
  /** Every measured decision that shaped this answer — on the ledger, not
   * beside it. Empty on attributed traces. */
  decisions?: DecisionRecord[]
  /** True when every slot typed and no raw-SQL escalation ran; null on
   * answers served before typed serving existed. */
  typed?: boolean | null
}

/** The question could not be answered as asked. `unresolved_slot` names the
 * slot the typed resolver could not type — `term` is the slot, `options` the
 * candidates or the value grammar; the agent re-asks with bindings. */
export interface Clarification {
  kind: 'ambiguous_term' | 'unknown_value' | 'unresolved_slot' | string
  term: string
  question: string
  options: string[]
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
  /** Null on declines/faults and on rows recorded before the ledger. */
  resolution: Resolution | null
  /** Mirrors resolution.typed at the row level; null before typed serving. */
  typed?: boolean | null
  /** Set on status='clarification' rows only. */
  clarification?: Clarification | null
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
