import { useMutation, useQuery } from '@tanstack/react-query'
import { apiDelete, apiGet, apiPost } from './client'

export interface Connection {
  name: string
  type: string
  config: Record<string, unknown>
  has_secret: boolean
}

export function useConnections() {
  return useQuery({
    queryKey: ['connections'],
    queryFn: () => apiGet<Connection[]>('/mgmt/connections'),
  })
}

export function useTestConnection() {
  return useMutation({
    mutationFn: (name: string) =>
      apiPost<{ name: string; ok: boolean; tables: number }>(
        `/mgmt/connections/${name}/test`,
        {},
      ),
  })
}

export function useDeleteConnection() {
  return useMutation({ mutationFn: (name: string) => apiDelete(`/mgmt/connections/${name}`) })
}

export interface DependentLens {
  name: string
  display_name: string
  description: string
  status: string
}

/** Lenses that reference a connection — drives the delete-time dependency warning. */
export function useConnectionDependents(name: string | null) {
  return useQuery({
    queryKey: ['connection-dependents', name],
    queryFn: () =>
      apiGet<{ name: string; lenses: DependentLens[] }>(`/mgmt/connections/${name}/dependents`),
    enabled: !!name,
  })
}
