import { useQuery } from '@tanstack/react-query'
import { credentialKind } from './auth'
import { API_BASE, apiGet } from './client'

// Local (Clerk-free) dashboard sessions: /auth/* on the API. Login sets the
// dstsess_ token as an httpOnly cookie, which is what the browser then rides;
// the token in the response body is for API clients, and the dashboard ignores it.

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
    queryKey: ['me', credentialKind()],
    queryFn: () => apiGet<Me>('/auth/me'),
    enabled: credentialKind() === 'session',
    retry: false,
  })
}

/** Revoke the active dstsess_ session server-side (best-effort). The cookie is
 * the credential and rides along on this same-origin POST, so there is no header
 * to attach. This call is what kills the session; clearing localStorage only
 * makes the browser forget it. */
export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, { method: 'POST' }).catch(() => undefined)
}
