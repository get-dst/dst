import { useMutation, useQuery } from '@tanstack/react-query'
import { apiGet, apiPost } from './client'

/** The wrong-vs-right delta a correction records on a ticket. */
export interface CorrectionDelta {
  kind: 'definition' | 'scope' | 'number' | 'freshness' | 'other'
  note: string
  corrected_answer?: string | null
  corrected_sql?: string | null
}

export interface ReviewTicket {
  ticket_id: string
  request_id: string
  lens: string
  caller: string
  state: string
  /** Who raised it: a person (caller/dashboard) or the lens's auto_review flagger. */
  origin: 'human' | 'ai'
  ai_verdict: string | null
  ai_reasoning: string | null
  human_verdict: string | null
  human_reasoning: string | null
  correction: CorrectionDelta | null
  /** Machine actor behind ai_verdict (the judge's model name); '' = no judge ran. */
  judged_by: string
  /** Who issued human_verdict: `human:<email>` or `token:<label>`; '' = unruled. */
  ruled_by: string
}

export function useReviewQueue(state?: string) {
  const qs = state ? `?state=${state}` : ''
  return useQuery({
    queryKey: ['reviews', state ?? 'all'],
    queryFn: () => apiGet<ReviewTicket[]>(`/mgmt/reviews${qs}`),
  })
}

export function useRuleReview() {
  return useMutation({
    mutationFn: (args: {
      ticketId: string
      verdict: string
      reasoning: string
      correction?: CorrectionDelta
    }) =>
      apiPost<ReviewTicket>(`/mgmt/reviews/${args.ticketId}/rule`, {
        verdict: args.verdict,
        reasoning: args.reasoning,
        correction: args.correction ?? null,
      }),
  })
}
