/**
 * Outcome vocabulary → badge variant, defined once so every page agrees.
 *
 * - status: 'error' is the only red — refused/clarification/rejected are
 *   governed declines (the product doing its job), rendered neutral.
 * - confidence: the verification grade. 'unverified' is red because it means
 *   a grounding/intent check FAILED, not "not yet checked" (checks that merely
 *   couldn't run grade 'partial'). 'high'/'low' are legacy grades that still
 *   appear on older traces.
 */

export function statusVariant(s: string): 'success' | 'error' | 'default' {
  return s === 'ok' ? 'success' : s === 'error' ? 'error' : 'default'
}

export function confidenceVariant(
  c: string | null
): 'success' | 'warning' | 'error' | 'default' {
  if (c === 'verified' || c === 'high') return 'success'
  if (c === 'unverified') return 'error'
  if (c === 'partial' || c === 'low') return 'warning'
  return 'default'
}

/**
 * Prose form of an actor string (`human:ana@corp`, `token:ci-deploy`, or a
 * model name). The stored prefix is the trust tier — a raw admin token is not
 * provably a person, so it must never render as one.
 */
export function actorLabel(actor: string): string {
  if (actor.startsWith('human:')) return actor.slice('human:'.length)
  if (actor.startsWith('token:')) return `admin token '${actor.slice('token:'.length)}'`
  return actor
}
