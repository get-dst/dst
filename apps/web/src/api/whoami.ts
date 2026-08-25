import { useQuery } from '@tanstack/react-query'
import { getToken } from './auth'
import { apiGet } from './client'

export interface WhoAmI {
  org_id: string
  org_name: string | null
}

/** The org the active admin token belongs to — used to label the current tenant. */
export function useWhoami() {
  return useQuery({
    queryKey: ['whoami', getToken()],
    queryFn: () => apiGet<WhoAmI>('/mgmt/whoami'),
    enabled: !!getToken(),
  })
}
