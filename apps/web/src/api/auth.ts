// Admin-token storage (localStorage), the credential escape hatch beside the
// login form: paste the `dstadm_…` token `dst bootstrap` mints.
const KEY = 'dst_admin_token'

function store(): Storage | undefined {
  return typeof localStorage !== 'undefined' ? localStorage : undefined
}

export function getToken(): string {
  return store()?.getItem(KEY) ?? ''
}

export function setToken(token: string): void {
  const s = store()
  if (!s) return
  if (token) s.setItem(KEY, token)
  else s.removeItem(KEY)
}

/** Which credential every request carries. One localStorage slot holds both the
 * `dstsess_` token minted by the login form and a hand-pasted `dstadm_` admin token —
 * and the header always outranks the session cookie server-side, so a pasted
 * token silently acts for ITS org. The shell labels which one is in play. */
export type CredentialKind = 'session' | 'token' | 'none'

export function credentialKind(): CredentialKind {
  const t = getToken()
  if (!t) return 'none'
  return t.startsWith('dstsess_') ? 'session' : 'token'
}

// --- Org registry (tenant switcher) ----------------------------------------
// An admin token is opaque — its org name can't be read out of it — so the
// browser remembers the orgs it holds tokens for and switches the active one.
const ORGS_KEY = 'dst_orgs'

export interface KnownOrg {
  name: string
  token: string
}

export function getOrgs(): KnownOrg[] {
  try {
    const raw = store()?.getItem(ORGS_KEY)
    return raw ? (JSON.parse(raw) as KnownOrg[]) : []
  } catch {
    return []
  }
}

/** Add or update a known org (keyed by token); persists the list. */
export function registerOrg(name: string, token: string): void {
  const s = store()
  if (!s || !token) return
  const orgs = getOrgs().filter((o) => o.token !== token)
  orgs.push({ name, token })
  s.setItem(ORGS_KEY, JSON.stringify(orgs))
}

/** Name of the org whose token is currently active, if known. */
export function getActiveOrgName(): string | undefined {
  const t = getToken()
  return getOrgs().find((o) => o.token === t)?.name
}
