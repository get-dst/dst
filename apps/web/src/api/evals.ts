import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost } from './client'

// ─── Types ───────────────────────────────────────────────────────────────────

export type EvalCaseStatus = 'candidate' | 'approved' | 'retired'
export type EvalCaseSource = 'certified' | 'sample_query' | 'harvested' | 'authored'

export interface EvalCase {
  id: string
  question: string
  /** Legacy value-case oracle — behavioral pins carry none (value verification
   *  lives in certified answers; `dst evals migrate` converts old cases). */
  expected_sql?: string | null
  source: EvalCaseSource
  status: EvalCaseStatus
  created_by: string
}

export interface EvalRun {
  id: string
  mode: 'regression' | 'health' | 'certified' | string
  score: number | null
  passed: number
  failed: number
  errored: number
  started_at: string | null
}

export interface RunResult extends EvalRun {
  run_id: string
  failing: string[]
  detail: Record<string, unknown>
}

// ─── Cases ───────────────────────────────────────────────────────────────────

const base = (lens: string) => `/mgmt/lenses/${encodeURIComponent(lens)}/evals`

/** List eval cases for a lens, optionally filtered by status. */
export function useEvalCases(lens: string, status?: EvalCaseStatus) {
  const qs = status ? `?status=${status}` : ''
  return useQuery({
    queryKey: ['eval-cases', lens, status ?? 'all'],
    queryFn: () => apiGet<EvalCase[]>(`${base(lens)}/cases${qs}`),
    enabled: !!lens,
  })
}

// Case generate/approve/retire/delete are authoring — they land in
// `lenses/<lens>/evals/cases.yaml` via apply, never from the UI. The
// endpoints stay in core for the CLI and the cloud plugin.

// ─── Runs ────────────────────────────────────────────────────────────────────

/** Trigger a health run (judge-scored live checks). Value regression is a
 *  separate lane: certified answers are the regression suite (`dst test`). */
export function useRunEval(lens: string) {
  return useMutation({
    mutationFn: (mode: 'health') => apiPost<RunResult>(`${base(lens)}/run?mode=${mode}`, {}),
  })
}

/** List recent eval runs for a lens (newest first — the accuracy trend). */
export function useEvalRuns(lens: string) {
  return useQuery({
    queryKey: ['eval-runs', lens],
    queryFn: () => apiGet<EvalRun[]>(`${base(lens)}/runs`),
    enabled: !!lens,
  })
}

export interface EvalRunCaseResult {
  case_id: string
  question: string
  passed: boolean
  grade: string | null
  checks: Record<string, unknown> | null
  actual_sql: string | null
  actual_value: string | null
  reason: string | null
}

/**
 * One run's per-case outcomes, failing first — the drill-down behind a trend
 * point. Empty for runs recorded before per-case persistence.
 */
export function useEvalRunResults(lens: string, runId: string | null) {
  return useQuery({
    queryKey: ['eval-run-results', lens, runId],
    queryFn: () =>
      apiGet<EvalRunCaseResult[]>(`${base(lens)}/runs/${encodeURIComponent(runId!)}/results`),
    enabled: !!lens && !!runId,
  })
}

// ─── Quickcheck (wizard "Validate") ──────────────────────────────────────────

export interface QuickcheckCase {
  question: string
  passed: boolean
  reason: string
}

export interface QuickcheckResult {
  score: number
  results: QuickcheckCase[]
}

/**
 * Lightweight health check over a lens's accepted (sample) questions — runs each
 * live and judges it, persisting nothing (no cases/runs are stored). Powers the
 * wizard's optional "Validate" affordance on the Draft/Publish step.
 */
export function useQuickcheck(lens: string) {
  return useMutation({
    mutationFn: (body?: { limit?: number }) =>
      apiPost<QuickcheckResult>(`${base(lens)}/quickcheck`, body ?? {}),
  })
}

// ─── Convenience invalidation helper ─────────────────────────────────────────

/** Returns a function that invalidates all eval queries for a lens. */
export function useInvalidateEvals(lens: string) {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: ['eval-cases', lens] })
    qc.invalidateQueries({ queryKey: ['eval-runs', lens] })
    qc.invalidateQueries({ queryKey: ['eval-run-results', lens] })
  }
}
