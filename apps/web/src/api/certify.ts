import { useMutation, useQuery } from '@tanstack/react-query'
import { apiDelete, apiGet, apiPost } from './client'

/** The known-good result a certified answer was verified against: a scalar
 * (`{value}`) or a small table (`{columns, rows}`); null when never run. */
export type VerifiedValue =
  | { value: unknown }
  | { columns: string[]; rows: unknown[][] }
  | null

export interface CertifiedAnswer {
  id: string
  question: string
  sql: string
  created_by: string
  created_at: string
  verified_value: VerifiedValue
  /** Where the pair came from (e.g. `review:<ticket>`, `file:certified_answers.yaml`). */
  source: string | null
  verified_by: string | null
  /** `active` serves; `pending_embedding` awaits reindex; `retired` is history-only. */
  status: 'active' | 'pending_embedding' | 'retired'
}

/** The approved question→SQL pairs for a lens. */
export function useCertified(lens: string) {
  return useQuery({
    queryKey: ['certified', lens],
    queryFn: () => apiGet<CertifiedAnswer[]>(`/mgmt/lenses/${encodeURIComponent(lens)}/certified`),
    enabled: !!lens,
  })
}

/** One generated certified pair — stored ones carry an id + verified value; a
 * failed/skipped one carries an error instead. */
export interface GeneratedCertified {
  term: string
  question: string
  sql: string
  id: string | null
  verified_value: VerifiedValue
  error: string | null
}

export interface GenerateCertifiedResult {
  lens: string
  generated: number
  results: GeneratedCertified[]
}

/** Generate certified question→SQL pairs from the lens's governed definitions —
 * an LLM + read-only warehouse round-trip per definition, so it can take a while. */
export function useGenerateCertified(lens: string) {
  return useMutation({
    mutationFn: () =>
      apiPost<GenerateCertifiedResult>(
        `/mgmt/lenses/${encodeURIComponent(lens)}/certified/generate`,
        {},
      ),
  })
}

export function useCreateCertified(lens: string) {
  return useMutation({
    mutationFn: (body: { question: string; sql: string }) =>
      apiPost<{ id: string }>(`/mgmt/lenses/${encodeURIComponent(lens)}/certified`, body),
  })
}

/** Certify a prior answer by its request_id (snapshots its question + SQL). */
export function useCertifyRequest(lens: string) {
  return useMutation({
    mutationFn: (requestId: string) =>
      apiPost<{ id: string }>(
        `/mgmt/lenses/${encodeURIComponent(lens)}/certified/from-request/${requestId}`,
        {},
      ),
  })
}

export function useDeleteCertified(lens: string) {
  return useMutation({
    mutationFn: (id: string) =>
      apiDelete(`/mgmt/lenses/${encodeURIComponent(lens)}/certified/${id}`),
  })
}
