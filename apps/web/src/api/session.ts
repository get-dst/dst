import { useQuery } from '@tanstack/react-query'
import { credentialKind, getToken } from './auth'
import { API_BASE, apiGet } from './client'

// Local (Clerk-free) dashboard sessions: /auth/* on the API. Login mints a
// dstsess_ token (also set as an httpOnly cookie); we keep the token in the same
// localStorage slot as the dev admin-token fallback so the app is auth-agnostic.

export interface Session {
  token: string
  email: string
  role: string
  org_id: string
  expires_at: string | null
}

export async function login(email: string, password: string): Promise<Session> {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!res.ok) {
    let detail = `login failed (${res.status})`
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* non-JSON error */
    }
    throw new Error(detail)
  }
  return (await res.json()) as Session
}

export interface Me {
  email: string
  role: string
  org_id: string
}

/** The signed-in user behind the active `dstsess_` session. Only a session token
 * resolves here — a pasted admin token names no user, which is exactly the
 * distinction the shell's identity card renders. */
export function useMe() {
  return useQuery({
    queryKey: ['me', getToken()],
    queryFn: () => apiGet<Me>('/auth/me'),
    enabled: credentialKind() === 'session',
    retry: false,
  })
}

/** Revoke the active dstsess_ session server-side (best-effort). */
export async function logout(): Promise<void> {
  const token = getToken()
  await fetch(`${API_BASE}/auth/logout`, {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  }).catch(() => undefined)
}
