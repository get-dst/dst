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

// ─── drafted patches (the auto-drafted, approvable fixes) ────────────────────

export interface PatchCandidate {
  id: string
  ticket_id: string | null
  lens: string
  kind: 'definition' | 'instruction' | 'certified' | 'reindex' | 'reprofile'
  target: string
  owner: string
  diff_before: string | null
  diff_after: string
  status: 'candidate' | 'approved' | 'rejected'
}

/** The file an approved ruling asks for — whole new content + diff. Not live
 *  until it is committed and `dst apply` runs: files author, the UI governs. */
export interface ProposedFile {
  path: string
  content: string
  diff: string
}

export interface PatchApproval {
  id: string
  status: string
  kind: PatchCandidate['kind']
  target: string
  /** Is the ruling in effect on what the lens serves right now? */
  live: boolean
  /** Server-side state the ruling actually wrote (certified pair). */
  applied: Record<string, unknown>
  proposed_file: ProposedFile | null
  /** The one act that makes the ruling real. */
  next_step: string
  eval_case_id: string | null
}

export function useLensPatches(lens: string) {
  return useQuery({
    queryKey: ['patches', lens],
    queryFn: () => apiGet<PatchCandidate[]>(`/mgmt/lenses/${encodeURIComponent(lens)}/patches`),
    enabled: !!lens,
  })
}

/** Distill the lens's verified request history into patch candidates. */
export function useDistill(lens: string) {
  return useMutation({
    mutationFn: () =>
      apiPost<PatchCandidate[]>(`/mgmt/lenses/${encodeURIComponent(lens)}/distill`, {}),
  })
}

export function useDraftPatch() {
  return useMutation({
    mutationFn: (ticketId: string) =>
      apiPost<PatchCandidate>(`/mgmt/reviews/${ticketId}/draft-patch`, {}),
  })
}

export function useApprovePatch() {
  return useMutation({
    mutationFn: (patchId: string) =>
      apiPost<PatchApproval>(`/mgmt/reviews/patches/${patchId}/approve`, {}),
  })
}

export function useRejectPatch() {
  return useMutation({
    mutationFn: (patchId: string) =>
      apiPost<{ id: string; status: string }>(`/mgmt/reviews/patches/${patchId}/reject`, {}),
  })
}
