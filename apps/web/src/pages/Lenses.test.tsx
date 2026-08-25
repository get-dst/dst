import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Lenses } from './Lenses'

// The empty state only renders with a token present; localStorage isn't available
// in this test env, so stub the auth module instead.
vi.mock('../api/auth', () => ({ getToken: () => 'dstadm_test' }))

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    ),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Lenses />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('the ledger weights by evidence and the fascia carries the readout', async () => {
  // an active lens expands (description shown), an empty
  // one compresses; the header readout states the live counts. A uniform grid
  // would render the two identically, which is the thing this pins against.
  const lenses = JSON.stringify([
    {
      name: 'busy',
      display_name: 'Busy',
      description: 'Serves the finance agent.',
      status: 'live',
      entity_count: 2,
      definition_count: 1,
      question_count: 0,
      query_count: 400,
      last_queried_at: new Date().toISOString(),
    },
    {
      name: 'idle',
      display_name: 'Idle',
      description: 'Never queried.',
      status: 'draft',
      entity_count: 1,
      definition_count: 0,
      question_count: 0,
      query_count: 0,
      last_queried_at: null,
    },
  ])
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(lenses, { status: 200, headers: { 'Content-Type': 'application/json' } }),
    ),
  )
  renderPage()
  expect(await screen.findByText('busy')).toBeInTheDocument()
  // Weight follows evidence: the active lens shows its description…
  expect(screen.getByText('Serves the finance agent.')).toBeInTheDocument()
  // …the idle one compresses to a single line (no description rendered).
  expect(screen.queryByText('Never queried.')).toBeNull()
  expect(screen.getByText('no queries')).toBeInTheDocument()
  // The fascia readout states the live counts.
  expect(screen.getByText('2 lenses')).toBeInTheDocument()
  expect(screen.getByText('1 live')).toBeInTheDocument()
})

test('the empty state teaches the file loop — no create affordance anywhere', async () => {
  renderPage()
  expect(await screen.findByText('No lenses yet')).toBeInTheDocument()
  // Points at the authoring loop: apply, AGENTS.md, introspect, the scaffolded skill.
  expect(screen.getAllByText(/dst apply/).length).toBeGreaterThan(0)
  expect(screen.getByText('AGENTS.md')).toBeInTheDocument()
  expect(screen.getByText('dst introspect')).toBeInTheDocument()
  expect(screen.getByText('dst-semantic')).toBeInTheDocument()
  // The wizard's entry points are gone.
  expect(screen.queryByText('New lens')).toBeNull()
  expect(screen.queryByText('Create a lens')).toBeNull()
})
