import { describe, expect, it } from 'vitest'
import { API_BASE } from './client'

describe('API_BASE', () => {
  it('defaults to the browser origin, never a hardcoded port', () => {
    // `dst serve` mounts this bundle on the API's own origin. A hardcoded port
    // would be right for exactly one deployment and silently wrong for every
    // other: the dashboard would ask a host that has never seen its token — no
    // error, no console failure, just "resolving org…" forever.
    expect(API_BASE).toBe(window.location.origin)
    expect(API_BASE).not.toContain('localhost:8000')
  })
})
