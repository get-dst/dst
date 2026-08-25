import { useQuery } from '@tanstack/react-query'
import { apiGet } from './client'

export interface OrgStandard {
  term: string
  body: string
  sql_expr: string | null
}

export function useStandards() {
  return useQuery({
    queryKey: ['standards'],
    queryFn: () => apiGet<OrgStandard[]>('/mgmt/standards'),
  })
}

export interface DriftConflict {
  term: string
  kind: string
  source: string
  lens_body: string
  other_body: string
}

export function useDrift(name: string) {
  return useQuery({
    queryKey: ['drift', name],
    queryFn: () => apiGet<{ conflicts: DriftConflict[] }>(`/mgmt/lenses/${name}/drift`),
    enabled: !!name,
  })
}
