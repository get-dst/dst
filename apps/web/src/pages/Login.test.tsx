import { test, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Login } from './Login'
import { credentialKind, getToken } from '../api/auth'
import { stubLocalStorage } from '../test/storage'

let cells: Map<string, string>
beforeEach(() => {
  cells = stubLocalStorage()
  // jsdom has no navigation; the form hard-reloads on success.
  vi.stubGlobal('location', { ...window.location, assign: vi.fn() })
})
afterEach(() => vi.unstubAllGlobals())

test('renders email+password sign-in with the token escape hatch', () => {
  render(<Login />)
  expect(screen.getByText('dst')).toBeInTheDocument()
  expect(screen.getByLabelText('Email')).toBeInTheDocument()
  expect(screen.getByLabelText('Password')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Use an admin token instead' }))
  expect(screen.getByLabelText('Admin token')).toBeInTheDocument()
  expect(screen.getByPlaceholderText('dstadm_…')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Sign in with email instead' }))
  expect(screen.getByLabelText('Email')).toBeInTheDocument()
})

test('signing in keeps the session token out of localStorage', async () => {
  // The API returns the token in the body AND as an httpOnly cookie. The browser
  // must take the cookie and drop the body token: a token localStorage can hold is
  // a token any script on this origin can read and send somewhere else.
  const token = 'dstsess_secret-session-token'
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify({ token, email: 'a@b.c', role: 'admin', org_id: 'o' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    ),
  )

  render(<Login />)
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'a@b.c' } })
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'pw' } })
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))

  await waitFor(() => expect(credentialKind()).toBe('session'))
  expect(getToken()).toBe('')
  expect([...cells.values()].join()).not.toContain(token)
})

test('the pasted-token escape hatch still stores the token it is given', async () => {
  render(<Login />)
  fireEvent.click(screen.getByRole('button', { name: 'Use an admin token instead' }))
  fireEvent.change(screen.getByLabelText('Admin token'), { target: { value: 'dstadm_pasted' } })
  fireEvent.click(screen.getByRole('button', { name: 'Continue with token' }))

  // A pasted secret has nowhere else to live — this path is deliberately unchanged.
  await waitFor(() => expect(getToken()).toBe('dstadm_pasted'))
  expect(credentialKind()).toBe('token')
})
