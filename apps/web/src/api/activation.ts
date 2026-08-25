import { useQuery } from '@tanstack/react-query'
import { apiGet } from './client'

/** The org's place in the connect→context→audit→lens funnel. */
export type ActivationStep =
  | 'empty'
  | 'connected'
  | 'auditing'
  | 'findings_ready'
  | 'drafting'
  | 'activated'

export interface Activation {
  step: ActivationStep
  connections: number
  lenses: number
  published_lenses: number
  auditing: boolean
  /** Open conflicts on the focus connection. */
  findings: number
  /** The warehouse the next step acts on (most contested, else mid-audit, else first). */
  focus_connection: string | null
}

/** Derived from already-persisted state — the first-run home reads this to name the
 * single next action. An `activated` org shows nothing. */
export function useActivation() {
  return useQuery({
    queryKey: ['activation'],
    queryFn: () => apiGet<Activation>('/mgmt/activation'),
  })
}
