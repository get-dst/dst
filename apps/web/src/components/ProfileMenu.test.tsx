import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ProfileMenu } from './ProfileMenu'

// The real auth module reads localStorage; give it one so credentialKind() is
// exercised for real rather than mocked away.
function useStoredToken(token: string) {
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => (k === 'dst_admin_token' ? token : null),
    setItem: vi.fn(),
    removeItem: vi.fn(),
  })
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const path = String(url)
      const body = path.endsWith('/auth/me')
        ? { email: 'alex@example.com', role: 'admin', org_id: '8f21bd44-0000-4000-8000-000000000001' }
        : path.endsWith('/mgmt/whoami')
          ? { org_id: '8f21bd44-0000-4000-8000-000000000001', org_name: 'Tammiketju Oy' }
          : {}
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderMenu() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ProfileMenu />
    </QueryClientProvider>,
  )
}

test('a pasted admin token is labelled, explained, and clearable', async () => {
  useStoredToken('dstadm_pasted')
  renderMenu()

  // The shell says which credential is in play before the menu is even opened.
  expect(screen.getByText('token')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Settings & organization' }))
  // No user behind a pasted token — it names the org it acts for instead.
  expect((await screen.findAllByText('Tammiketju Oy')).length).toBeGreaterThan(0)
  expect(screen.getByText('Org admin (no user)')).toBeInTheDocument()
  expect(screen.getByText('8f21bd44')).toBeInTheDocument()
  expect(screen.getByText('pasted admin token')).toBeInTheDocument()
  expect(screen.getByText(/outranks any sign-in/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Clear token & sign in' })).toBeInTheDocument()
})

test('a signed-in session names the user and offers no token to clear', async () => {
  useStoredToken('dstsess_live')
  renderMenu()

  expect(screen.getByText('session')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Settings & organization' }))
  expect(await screen.findByText('alex@example.com')).toBeInTheDocument()
  expect(screen.getByText('browser sign-in')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Clear token & sign in' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
})
