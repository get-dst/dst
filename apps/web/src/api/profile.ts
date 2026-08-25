import { useMutation, useQuery } from '@tanstack/react-query'
import { apiGet, apiPost } from './client'

export interface ColumnProfile {
  name: string
  type: string
  nullable: boolean
  description: string | null
  description_source: 'warehouse' | 'dbt' | 'sampled' | null
  null_rate: number | null
  distinct_count: number | null
  is_low_cardinality: boolean
  top_values: string[] | null
  min: string | null
  max: string | null
}

export interface TableProfile {
  connection: string
  table: string
  description: string | null
  row_count: number | null
  partitioning: { column: string | null; kind: string | null; latest_partition: string | null } | null
  clustering: string[]
  last_updated_physical: string | null
  last_updated_logical: string | null
  profiled_at: string
  source: 'catalog' | 'sampled' | 'mixed'
  columns: ColumnProfile[]
}

export interface JoinCandidate {
  id: string
  left_table: string
  left_columns: string[]
  right_table: string
  right_columns: string[]
  evidence: 'fk' | 'dbt' | 'name+overlap'
  overlap_ratio: number | null
  status: 'candidate' | 'approved' | 'rejected'
}

/** The stored profiles for the lens's scope tables. */
export function useLensProfile(lens: string) {
  return useQuery({
    queryKey: ['lens-profile', lens],
    queryFn: () =>
      apiGet<{ lens: string; connection: string; tables: TableProfile[] }>(
        `/mgmt/lenses/${encodeURIComponent(lens)}/profile`,
      ),
    enabled: !!lens,
  })
}

export function useJoinCandidates(lens: string) {
  return useQuery({
    queryKey: ['join-candidates', lens],
    queryFn: () =>
      apiGet<JoinCandidate[]>(`/mgmt/lenses/${encodeURIComponent(lens)}/join-candidates`),
    enabled: !!lens,
  })
}

export function useInferJoinCandidates(lens: string) {
  return useMutation({
    mutationFn: () =>
      apiPost<JoinCandidate[]>(`/mgmt/lenses/${encodeURIComponent(lens)}/join-candidates/infer`, {}),
  })
}

export interface ProfileDriftItem {
  table: string
  kind: string
  detail: string
}

export function useLensProfileDrift(lens: string) {
  return useQuery({
    queryKey: ['lens-profile-drift', lens],
    queryFn: () =>
      apiGet<{ lens: string; drift: ProfileDriftItem[]; eval_cases_needing_rebaseline: string[] }>(
        `/mgmt/lenses/${encodeURIComponent(lens)}/profile-drift`,
      ),
    enabled: !!lens,
  })
}
