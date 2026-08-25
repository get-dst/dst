import { getToken } from './auth'
import { getClerkToken } from './clerkToken'

/** Same-origin by DEFAULT: `dst serve` mounts this bundle on the API's own
 * origin, so relative fetches are correct wherever it is served from.
 *
 * Never bake an absolute host in here. A hard-coded port is right for exactly one
 * deployment and silently wrong for every other: served on any other port, the
 * dashboard asks a host that has never seen its token — no error, no console
 * failure, just "resolving org…" forever.
 *
 * Dev against a separate API (vite on 5173) sets VITE_API_URL explicitly; the vite
 * proxy in vite.config.ts makes even that unnecessary for the default setup. */
const BASE = import.meta.env.VITE_API_URL ?? ''

/** The dst API base URL — also the host of the remote MCP endpoint (`${BASE}/mcp`).
 * Relative ("") for same-origin fetches; anything SHOWN to the user (MCP setup
 * snippets, curl examples) must be absolute, so fall back to the browser origin —
 * which under same-origin serving IS the API. */
export const API_BASE = BASE || window.location.origin

/** The credential for one request, in precedence order: Clerk's per-request JWT,
 * then a pasted admin token. A browser sign-in adds NO header — its `dstsess_`
 * token lives in an httpOnly cookie, which fetch attaches by itself on these
 * same-origin requests (`BASE` is relative; see above). That is also why the
 * cookie is same-origin-only in practice: a split deploy that points
 * `VITE_API_URL` at another host has no cookie to send and needs a pasted token. */
async function authHeaders(): Promise<Record<string, string>> {
  const clerk = await getClerkToken()
  if (clerk) return { Authorization: `Bearer ${clerk}` }
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function unwrap<T>(res: Response, path: string): Promise<T> {
  if (!res.ok) {
    let detail = `${res.status}`
    try {
      const body = await res.json()
      detail = body.detail ?? body.error?.message ?? detail
    } catch {
      /* non-JSON error */
    }
    throw new Error(`${path}: ${detail}`)
  }
  return (await res.json()) as T
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: { ...(await authHeaders()) } })
  return unwrap<T>(res, path)
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(await authHeaders()) },
    body: JSON.stringify(body),
  })
  return unwrap<T>(res, path)
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...(await authHeaders()) },
    body: JSON.stringify(body),
  })
  return unwrap<T>(res, path)
}

export async function apiDelete(path: string): Promise<void> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'DELETE',
    headers: { ...(await authHeaders()) },
  })
  if (!res.ok && res.status !== 204) throw new Error(`${path}: ${res.status}`)
}

export async function apiUpload<T>(path: string, file: File): Promise<T> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { ...(await authHeaders()) }, // no Content-Type: browser sets the multipart boundary
    body: form,
  })
  return unwrap<T>(res, path)
}
