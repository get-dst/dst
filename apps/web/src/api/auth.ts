// Credential storage for the dashboard.
//
// Two kinds of credential, stored differently on purpose:
//
//   browser sign-in  the `dstsess_` token stays in the httpOnly cookie the API
//                    already sets at /auth/login. It is never written to
//                    localStorage, so script running on this origin cannot read
//                    it or copy it anywhere. localStorage holds a flag only —
//                    "a session exists" — which is a hint for rendering, not a
//                    credential: forging it authenticates nobody.
//   admin token      the `dstadm_…` token from `dst bootstrap`, pasted by hand.
//                    A pasted secret has nowhere else to live, so it does go to
//                    localStorage, and the shell labels it as the credential in
//                    play because it outranks any sign-in server-side.
const KEY = 'dst_admin_token'
const SESSION_FLAG = 'dst_session_active'
// The tenant switcher's registry (see the org-registry section below).
const ORGS_KEY = 'dst_orgs'

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

/** Record that /auth/login succeeded. The session itself rides the cookie. */
export function markSignedIn(): void {
  store()?.setItem(SESSION_FLAG, '1')
}

export function hasSession(): boolean {
  return store()?.getItem(SESSION_FLAG) === '1'
}

/** Anything at all to act with — the gate between the login form and the app. */
export function hasCredential(): boolean {
  return !!getToken() || hasSession()
}

/** Which credential every request carries. A pasted token outranks the session
 * cookie server-side, so when both exist the token is what is really in play —
 * and the shell says so. */
export type CredentialKind = 'session' | 'token' | 'none'

export function credentialKind(): CredentialKind {
  if (getToken()) return 'token'
  return hasSession() ? 'session' : 'none'
}

/** Drop every credential this browser holds: the pasted token, the session flag,
 * and the org registry with the admin tokens in it. Sign-out used to clear only
 * the active slot, which left the other orgs' `dstadm_` tokens sitting in
 * localStorage on a machine whose user had just said they were done. */
export function clearCredentials(): void {
  const s = store()
  if (!s) return
  s.removeItem(KEY)
  s.removeItem(SESSION_FLAG)
  s.removeItem(ORGS_KEY)
}

// --- Org registry (tenant switcher) ----------------------------------------
// An admin token is opaque — its org name can't be read out of it — so the
// browser remembers the orgs it holds tokens for and switches the active one.

/** How many orgs the switcher remembers. This list is long-lived admin tokens,
 * so it is bounded rather than allowed to accumulate every token the browser has
 * ever seen; past the bound the least recently registered drops off. */
export const MAX_ORGS = 8

export interface KnownOrg {
  name: string
  token: string
}

export function getOrgs(): KnownOrg[] {
  try {
    const raw = store()?.getItem(ORGS_KEY)
    const parsed = raw ? (JSON.parse(raw) as KnownOrg[]) : []
    return Array.isArray(parsed) ? parsed.slice(-MAX_ORGS) : []
  } catch {
    return []
  }
}

/** Add or update a known org (keyed by token); persists the newest MAX_ORGS. */
export function registerOrg(name: string, token: string): void {
  const s = store()
  if (!s || !token) return
  const orgs = getOrgs().filter((o) => o.token !== token)
  orgs.push({ name, token })
  s.setItem(ORGS_KEY, JSON.stringify(orgs.slice(-MAX_ORGS)))
}

/** Name of the org whose token is currently active, if known. */
export function getActiveOrgName(): string | undefined {
  const t = getToken()
  return getOrgs().find((o) => o.token === t)?.name
}
