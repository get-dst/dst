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
    resolution_tag: 'mixed',
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
    resolution_tag: null,
  },
]

const trace = {
  ...requests[0],
  sql: 'SELECT AVG(satisfaction) FROM support.tickets',
  scope: { tables: ['support.tickets'], fields: ['satisfaction'], filters: [], order_by: [] },
  resolution: {
    method: 'attributed',
    tag: 'mixed',
    typed: true,
    slots: [
      { kind: 'metric', name: 'avg_satisfaction', source: 'declared', value: null },
      { kind: 'filter', name: "status = 'closed'", source: 'inferred', value: "status = 'closed'" },
    ],
    decisions: [
      {
        slot: 'metric',
        chosen: 'avg_satisfaction',
        p: 0.94,
        runner_up: 0.04,
        margin: 0.9,
        verdict: 'act',
        provider: 'typed',
      },
      {
        slot: 'grain',
        chosen: null,
        p: 0.41,
        runner_up: 0.39,
        margin: 0.02,
        verdict: 'clarify',
        provider: 'typed',
      },
    ],
  },
  typed: true,
  clarification: null,
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

// A clarification trace: the typed resolver could not type one slot and
// named it, with the values the column holds. No resolution — nothing served.
const clarifyRequest = {
  request_id: 'req_clarify',
  lens: 'reporting',
  caller: 'anders',
  status: 'clarification',
  row_count: null,
  confidence: null,
  cost_usd: 0,
  created_at: '2026-08-13T20:40:00Z',
  question: 'tickets in the north region',
  resolution_tag: null,
}
const clarifyTrace = {
  ...clarifyRequest,
  sql: null,
  scope: null,
  resolution: null,
  typed: null,
  clarification: {
    kind: 'unresolved_slot',
    term: 'region',
    question: 'Which region? The column holds: NORTH, SOUTH.',
    options: ['NORTH', 'SOUTH'],
  },
  answer: null,
  citations: null,
  definition_used: null,
  verification: null,
  certification: 'none',
  latency: null,
  ai_input_tokens: null,
  ai_output_tokens: null,
  ai_cost_usd: null,
  wh_bytes: null,
  wh_cost_usd: null,
  error: null,
}

// The accuracy tab's eval trend: one lens with a calibration table whose
// rows span the three statuses — only the measured one may carry a number.
const evals = [
  {
    lens: 'reporting',
    latest_score: 1.0,
    runs: [
      { mode: 'regression', score: 1.0, passed: 12, failed: 0, errored: 0, started_at: null },
    ],
    calibration: {
      'typed [metric]': {
        n: 40,
        accuracy: 0.95,
        uncalibrated_n: 0,
        status: 'measured',
        brier: 0.041,
        ece: 0.032,
        bins: [],
        curve: [],
      },
      'typed [grain]': {
        n: 6,
        accuracy: 1.0,
        uncalibrated_n: 0,
        status: 'n too small',
        reason: '6 measured decisions, need 20',
      },
      'router [lens]': {
        n: 12,
        accuracy: 0.83,
        uncalibrated_n: 12,
        status: 'UNTESTED',
        reason: 'no probabilities measured',
      },
    },
  },
]

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
    if (url.includes('/mgmt/observe/evals')) return json(evals)
    if (url.includes('/mgmt/observe/requests/req_clarify')) return json(clarifyTrace)
    if (url.includes('/mgmt/observe/requests/')) return json(trace)
    if (url.includes('/mgmt/observe/requests')) return json([...requests, clarifyRequest])
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderObserve(initialTab: 'requests' | 'accuracy' = 'requests') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/observe']}>
        {/* These tests pin the Cost & requests panel (and the accuracy tab);
            the page itself lands on the Audit statement tab. */}
        <Observe initialTab={initialTab} />
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

test('the ledger tag rides the row and the trace names every slot, inferred marked but never red', async () => {
  renderObserve()
  const okRow = (await screen.findByText('ok')).closest('tr')!
  expect(okRow.textContent).toContain('mixed')
  // a decline has no resolution — nothing beside its status
  const refusedRow = screen.getByText('refused').closest('tr')!
  expect(refusedRow.textContent).not.toMatch(/mixed|inferred|declared|unknown/)

  fireEvent.click(okRow)
  expect(await screen.findByText('Resolution')).toBeInTheDocument()
  const resolution = screen.getByText('Resolution').parentElement!
  expect(resolution.textContent).toContain('mixed · attributed')
  expect(resolution.textContent).toContain('metric avg_satisfaction [declared]')
  expect(resolution.textContent).toContain("filter status = 'closed' = status = 'closed' [inferred]")
  const inferred = screen.getByTitle(/^inferred:/)
  expect(inferred.className).toContain('decoration-dotted')
  expect(resolution.querySelector('.text-red')).toBeNull()
})

test('the trace says typed on the meta line and lists every measured decision', async () => {
  renderObserve()
  fireEvent.click((await screen.findByText('ok')).closest('tr')!)
  const resolution = (await screen.findByText('Resolution')).parentElement!
  expect(resolution.textContent).toContain('mixed · attributed · typed')
  const decisions = screen.getByRole('list', { name: 'Decisions' })
  const lines = Array.from(decisions.children).map((el) => el.textContent)
  expect(lines[0]).toBe('metric avg_satisfaction p=0.94 act')
  expect(lines[1]).toBe('grain — p=0.41 clarify')
  // a clarify verdict is a governed outcome: muted, never red
  expect(decisions.children[1].className).toContain('text-muted')
  expect(resolution.querySelector('.text-red')).toBeNull()
})

test('a clarification trace names the slot, its kind, and the values it holds', async () => {
  renderObserve()
  fireEvent.click((await screen.findByText('clarification')).closest('tr')!)
  const row = (await screen.findByText('Clarification')).parentElement!
  expect(row.textContent).toContain('unresolved_slot · region')
  expect(row.textContent).toContain('NORTH')
  expect(row.textContent).toContain('SOUTH')
  // nothing was served: no resolution row, and a null typed prints nothing
  expect(screen.queryByText('Resolution')).toBeNull()
  expect(row.closest('table')!.textContent).not.toMatch(/untyped/i)
})

test('the accuracy tab shows calibration per decider and slot, words where nothing was measured', async () => {
  renderObserve('accuracy')
  const table = await screen.findByRole('table', { name: 'Calibration' })
  expect(table.textContent).toContain('typed [metric]')
  expect(table.textContent).toContain('0.041 · 0.032')
  const untested = screen.getByText('router [lens]').closest('tr')!
  expect(untested.textContent).toContain('UNTESTED — no probabilities measured')
  expect(untested.textContent).not.toMatch(/0\.\d{3}/)
  const small = screen.getByText('typed [grain]').closest('tr')!
  expect(small.textContent).toContain('n too small — 6 measured decisions, need 20')
  expect(small.textContent).not.toMatch(/0\.\d{3}/)
})
