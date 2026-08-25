import { useQuery } from '@tanstack/react-query'
import { apiGet } from './client'

/** One persisted routing decision — a route to a lens, or a decline. */
export interface RoutingDecision {
  id: string
  question: string
  routed_lens: string | null
  score: number
  covered: boolean
  created_at: string
}

export interface UncoveredCluster {
  label: string
  count: number
  examples: string[]
}

/** The router-lens's surface area over a window + the gap candidates + recent decisions. */
export interface SurfaceReport {
  window_days: number
  asked: number
  routed: number
  declined: number
  surface_area: number | null
  trend: number | null
  uncovered_clusters: UncoveredCluster[]
  recent: RoutingDecision[]
}

export function useSurface(days: number) {
  return useQuery({
    queryKey: ['surface', days],
    queryFn: () => apiGet<SurfaceReport>(`/mgmt/surface?days=${days}`),
  })
}
