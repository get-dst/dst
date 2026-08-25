import { useMutation, useQuery } from '@tanstack/react-query'
import { apiGet, apiPost } from './client'
import type { PatchCandidate } from './reviews'

/** Medallion tier of a variant's source tables — drives canon + coloring. */
export type Tier = 'gold' | 'silver' | 'bronze' | 'unknown'

/** One distinct definition of a metric, with where and how often it runs. */
export interface DriftVariant {
  statement: string
  source_tables: string[]
  run_count: number
  principals: string[]
  source_tools: string[]
  distinguishing: string
  observed_columns: string[] | null
  observed_rows: unknown[][] | null
  observed_error: string | null
  /** The medallion tier of this reading's source tables. */
  tier: Tier | null
}

/**
 * How a finding squares against a declared standard (e.g. a dbt metric).
 * `agrees` = the proposed-canon reading matches the declared one; `contradicts` = a
 * *different* reading matches the standard (so canon disagrees with it);
 * `undetermined` = a standard exists but couldn't be aligned to a variant. `null` =
 * no declared standard for this metric.
 */
export interface CanonMatch {
  term: string
  state: 'agrees' | 'contradicts' | 'undetermined'
}

/** One metric computed several ways — the unit of the Definition Drift audit. */
export interface DriftFinding {
  metric_intent: string
  variants: DriftVariant[]
  blast_radius: number
  severity: 'conflict' | 'duplication'
  /** The convention-aware canon (medallion tier, not a vote) + its reason. */
  canon_index: number | null
  canon_rationale: string | null
  /** How this finding squares against a declared standard (null = none). */
  canon_match?: CanonMatch | null
}

/**
 * A persisted audit run — the standing Definition Drift result the page examines.
 * `created_at` is an ISO timestamp; `id` keys the run in a timeline.
 */
export interface AuditRun {
  id: string
  connection: string
  days: number
  records_scanned: number
  findings: DriftFinding[]
  status: string
  created_at: string
}

/** GET …/audit/latest → the latest persisted run, or {found:false} if never run. */
export type LatestAudit = ({ found: true } & AuditRun) | { found: false }

/** The Audit's headline KPIs — answer accuracy, governed coverage, open drift. */
export interface AuditSummary {
  connection: string
  /** "unsupported" = no query history to score (the key below is then ABSENT —
   * an accuracy over 0 records is a vacuous pass, never emitted). */
  accuracy_status: 'scored' | 'unscored' | 'unsupported'
  answer_accuracy?: number | null
  accuracy_lenses: number
  governed_metrics: number
  ungoverned_metrics: number
  governed_share: number | null
  conflicts: number
  duplications: number
  has_run: boolean
  /** Metric label → the lens that governs it (null = ungoverned gap). */
  metric_governance: Record<string, string | null>
}

/** The KPI band data for a connection (accuracy + coverage + conflicts), one call. */
export function useAuditSummary(connection: string) {
  return useQuery({
    queryKey: ['audit', 'summary', connection],
    enabled: connection.length > 0,
    queryFn: () =>
      apiGet<AuditSummary>(`/mgmt/connections/${encodeURIComponent(connection)}/audit/summary`),
  })
}

/**
 * The standing result: default-load the latest persisted audit for a connection.
 * `enabled` gates the query until a connection is selected. Returns {found:false}
 * (not an error) when the connection has never been audited — the empty state.
 */
export function useLatestAudit(connection: string) {
  return useQuery({
    queryKey: ['audit', 'latest', connection],
    enabled: connection.length > 0,
    queryFn: () =>
      apiGet<LatestAudit>(`/mgmt/connections/${encodeURIComponent(connection)}/audit/latest`),
  })
}

/**
 * Refresh: re-run the audit (the page's secondary action) and persist it.
 * Returns the freshly stored AuditRun. Typically 10–30 s against a live warehouse.
 */
export function useRunAudit() {
  return useMutation({
    mutationFn: (args: { connection: string; days: number }) =>
      apiPost<AuditRun>(
        `/mgmt/connections/${encodeURIComponent(args.connection)}/audit?days=${args.days}`,
        {},
      ),
  })
}

/** Bridge a conflict finding into a definition PatchCandidate on the chosen lens. */
export function useDraftDefinition() {
  return useMutation({
    mutationFn: (args: { connection: string; finding: DriftFinding; lens: string }) =>
      apiPost<PatchCandidate>(
        `/mgmt/connections/${encodeURIComponent(args.connection)}/audit/draft-definition`,
        { finding: args.finding, lens: args.lens },
      ),
  })
}
