import { useMutation, useQuery } from '@tanstack/react-query'
import { apiDelete, apiGet } from './client'

export interface Caller {
  id: string
  name: string
  type: string
  groups: string[]
}

export interface KeyRow {
  id: string
  prefix: string
  created_at: string
  revoked: boolean
}

export function useCallers() {
  return useQuery({ queryKey: ['callers-list'], queryFn: () => apiGet<Caller[]>('/mgmt/callers') })
}

export function useKeys(name: string) {
  return useQuery({
    queryKey: ['keys', name],
    queryFn: () => apiGet<KeyRow[]>(`/mgmt/callers/${name}/keys`),
    enabled: !!name,
  })
}

export function useRevokeKey(name: string) {
  return useMutation({
    mutationFn: (keyId: string) => apiDelete(`/mgmt/callers/${name}/keys/${keyId}`),
  })
}
