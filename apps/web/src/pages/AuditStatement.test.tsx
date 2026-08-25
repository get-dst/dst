// The Statement: two headline figures, the honest outcome split, the lens
// ledger. Vocabulary pins: declines are never framed as errors, deltas are
// absent when the prior window carried no traffic (never invented), and no
// comparisons to figures we never measured.
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AuditStatementPanel } from './AuditStatement'

vi.mock('../api/auth', () => ({ getToken: () => 'dstadm_test' }))

const STATEMENT = {
  window_days: 30,
  asked: 4118,
  answered: 3847,
  clarified: 214,
  refused: 41,
  faults: 16,
  yield_pct: 93.4,
  verified_pct: 71.0,
  yield_delta_pp: 2.1,
  verified_delta_pp: null, // no prior traffic for this one — must not render
  ai_cost_usd: 214.6,
  wh_cost_usd: 89.32,
  cost_per_answer_usd: 0.079,
  confidence_histogram: { verified: 2731, partial: 923, unverified: 193 },
  series: [
    { day: '2026-08-20', asked: 120 },
    { day: '2026-08-21', asked: 189 },
    { day: '2026-08-22', asked: 140 },
  ],
  lenses: [
    {
      lens: 'finance',
      asked: 1204,
      answered: 1180,
      verified_pct: 98.0,
      cost_usd: 96.2,
      owner: 'ravi',
      degraded: null,
      gate_score: 1.0,
    },
    {
      lens: 'board_metrics',
      asked: 212,
      answered: 200,
      verified_pct: 91.0,
      cost_usd: 31.44,
      owner: '',
      degraded: 'DEGRADED: schema drift on connection x',
      gate_score: 1.0,
    },
  ],
  certified_active: 61,
  open_incident_tickets: 1,
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify(STATEMENT), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    ),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <AuditStatementPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('the two headline figures and the honest outcome split render', async () => {
  renderPage()
  expect(await screen.findByText('93.4')).toBeInTheDocument()
  expect(screen.getByText('71')).toBeInTheDocument()
  // declines carry their own vocabulary, never the error frame
  expect(screen.getByText('asked which meaning — not errors')).toBeInTheDocument()
  expect(screen.getByText('governed boundary held')).toBeInTheDocument()
  expect(screen.getByText('dst defects — each one ticketed')).toBeInTheDocument()
})

test('a delta renders only when the prior window carried traffic', async () => {
  renderPage()
  expect(await screen.findByText(/\+2\.1 pp vs prior window/)).toBeInTheDocument()
  // verified_delta_pp is null → exactly one delta on the page
  expect(screen.getAllByText(/pp vs prior window/)).toHaveLength(1)
})

test('the lens ledger links lenses and shows the degraded state', async () => {
  renderPage()
  const finance = await screen.findByRole('link', { name: 'finance' })
  expect(finance).toHaveAttribute('href', '/lenses/finance')
  expect(screen.getByText('healthy')).toBeInTheDocument()
  expect(screen.getByTitle(/schema drift/)).toHaveTextContent('degraded')
})

test('no invented comparisons anywhere', async () => {
  const { container } = renderPage()
  await screen.findByText('93.4')
  expect(container.textContent).not.toMatch(/analyst|\$\d+\/hr|vs \$/)
})

test('the confidence split is a visible three-band story, and flagged is the alarm', async () => {
  renderPage()
  // 2731 / 923 / 193 of 3847 served → the band legend states each share.
  expect(await screen.findByText(/71% verified · 24% caveated ·/)).toBeInTheDocument()
  // Flagged rides the outcome strip as a count, red because it is nonzero.
  const flagged = screen.getByText('failed verification — said so on its face')
  const cell = flagged.parentElement!
  expect(cell.textContent).toContain('193')
  expect(cell.querySelector('.text-red')).not.toBeNull()
})

test('the panel never claims "not wrong" — caveated is not knowledge', async () => {
  // A skipped check knows nothing: the honest claim is "nothing is wrong
  // silently", never "we know these weren't wrong". This pins the vocabulary
  // so the reassuring-but-unearned framing cannot creep back in.
  const { container } = renderPage()
  await screen.findByText('93.4')
  expect(container.textContent).not.toMatch(/not (outright )?wrong|known[- ]good|never wrong/i)
  expect(container.textContent).toContain('Nothing is wrong silently.')
})
