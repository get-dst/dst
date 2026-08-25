/**
 * Pins the Observe behaviors:
 * - declines never wear the error style (red is status='error' only),
 * - clicking a request row opens its trace INLINE under the row,
 * - the callers table splits AI vs warehouse cost and surfaces unpriced requests,
 * - the trace shows governed scope and human-readable warehouse usage.
 */
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Observe } from './Observe'

const kpis = {
  queries: 5,
  ai_cost_usd: 0.0023,
  warehouse_cost_usd: 0,
  input_tokens: 1200,
  output_tokens: 400,
  errors: 1,
  declined: 3,
  unpriced: 2,
  outcomes: { ok: 1, refused: 3, clarification: 0, rejected: 0, error: 1 },
}

const callers = [
  {
    caller: 'anders',
    queries: 4,
    cost_usd: 0.003,
    ai_cost_usd: 0.002,
    wh_cost_usd: 0.001,
    unpriced: 2,
    errors: 0,
    declined: 3,
  },
]

const requests = [
  {
    request_id: 'req_ok',
    lens: 'reporting',
    caller: 'anders',
    status: 'ok',
    row_count: 1,
    confidence: 'verified',
    cost_usd: 0.0001,
    created_at: '2026-08-13T20:55:48Z',
    question: 'average ticket satisfaction',
  },
  {
    request_id: 'req_declined',
    lens: 'reporting',
    caller: 'anders',
    status: 'refused',
    row_count: null,
    confidence: null,
    cost_usd: 0,
    created_at: '2026-08-13T20:50:00Z',
    question: 'signups by country',
  },
]

const trace = {
  ...requests[0],
  sql: 'SELECT AVG(satisfaction) FROM support.tickets',
  scope: { tables: ['support.tickets'], fields: ['satisfaction'], filters: [], order_by: [] },
  answer: 'The average is 3.97.',
  citations: null,
  definition_used: null,
  verification: {
    grade: 'verified',
    checks: [{ name: 'has_rows', status: 'pass', reason: null }],
  },
  certification: 'none',
  latency: null,
  ai_input_tokens: 900,
  ai_output_tokens: 100,
  ai_cost_usd: 0.0001,
  wh_bytes: 1234,
  wh_cost_usd: 0.0005,
  error: null,
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const json = (data: unknown) =>
      new Response(JSON.stringify(data), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    if (url.includes('/mgmt/observe/kpis')) return json(kpis)
    if (url.includes('/mgmt/observe/callers')) return json(callers)
    if (url.includes('/mgmt/observe/evals')) return json([])
    if (url.includes('/mgmt/observe/requests/')) return json(trace)
    if (url.includes('/mgmt/observe/requests')) return json(requests)
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderObserve() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/observe']}>
        {/* These tests pin the Cost & requests panel; the page itself lands
            on the Audit statement tab. */}
        <Observe initialTab="requests" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('declines are neutral, never error-red', async () => {
  renderObserve()
  const refused = await screen.findByText('refused')
  expect(refused.className).not.toContain('text-red')
  // the KPI still reports the real fault count with declines split out
  expect(await screen.findByText('3 declined (refused / clarify)')).toBeInTheDocument()
})

test('callers table splits AI vs warehouse cost and flags unpriced spend', async () => {
  renderObserve()
  expect(await screen.findByText('AI cost', { selector: 'th' })).toBeInTheDocument()
  expect(screen.getByText('Warehouse cost', { selector: 'th' })).toBeInTheDocument()
  expect(await screen.findByText('$0.0020')).toBeInTheDocument()
  expect(screen.getByText('$0.0010')).toBeInTheDocument()
  expect(
    screen.getByText(/some requests used a model with no configured price/),
  ).toBeInTheDocument()
})

test('clicking a request row opens its trace inline, with scope and readable bytes', async () => {
  renderObserve()
  // the table has no question column — find the ok row by its status badge
  const row = (await screen.findByText('ok')).closest('tr')!
  fireEvent.click(row)

  expect(await screen.findByText('Trace')).toBeInTheDocument()
  expect(screen.getByText('average ticket satisfaction')).toBeInTheDocument()
  // governed scope renders (it was fetched-but-dropped before)
  expect(screen.getByText(/support\.tickets · fields: satisfaction/)).toBeInTheDocument()
  // warehouse row is human bytes + cost, not a raw byte integer
  expect(screen.getByText(/1\.2 KB scanned · \$0\.0005/)).toBeInTheDocument()

  // the trace row sits inside the table, directly under the clicked row
  const traceCell = screen.getByText('Trace').closest('td')!
  expect(traceCell.closest('table')).toBe(row.closest('table'))

  // clicking the row again closes it
  fireEvent.click(row)
  expect(screen.queryByText('Trace')).toBeNull()
})
