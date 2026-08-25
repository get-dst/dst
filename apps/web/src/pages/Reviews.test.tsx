import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReviewsPanel } from './Reviews'

const ticket = {
  ticket_id: 'rev_abc123',
  request_id: 'req_1',
  lens: 'sales',
  caller: 'cs-bot',
  state: 'needs_human',
  origin: 'ai',
  ai_verdict: 'fail',
  ai_reasoning: 'The SQL ignores refunds.',
  human_verdict: null,
  human_reasoning: null,
  correction: {
    kind: 'definition',
    note: 'Revenue must exclude refunds',
    corrected_sql: 'SELECT revenue - refunds FROM sales',
    corrected_answer: null,
  },
}

const request = {
  request_id: 'req_1',
  lens: 'sales',
  caller: 'cs-bot',
  status: 'ok',
  row_count: 1,
  confidence: 'partial',
  cost_usd: 0.01,
  created_at: new Date().toISOString(),
  question: 'What was Q3 revenue?',
}

const trace = {
  ...request,
  sql: 'SELECT revenue FROM sales',
  scope: {
    tables: ['NORTHWIND.GOLD.SALES'],
    fields: ['revenue'],
    filters: [],
    order_by: [],
  },
  answer: 'Q3 revenue was $1.2M.',
  citations: null,
  definition_used: null,
  verification: {
    grade: 'partial',
    checks: [{ name: 'row_count', status: 'pass', reason: null }],
  },
  certification: null,
  latency: null,
  ai_input_tokens: null,
  ai_output_tokens: null,
  ai_cost_usd: null,
  wh_bytes: null,
  wh_cost_usd: null,
  error: null,
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      const json = (data: unknown) =>
        new Response(JSON.stringify(data), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      if (url.includes('/patches')) return json([])
      if (url.includes('/mgmt/observe/requests/')) return json(trace)
      if (url.includes('/mgmt/observe/requests')) return json([request])
      if (url.includes('/mgmt/reviews')) return json([ticket])
      return json({})
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderPanel(initialPath = '/reviews') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route path="/reviews" element={<ReviewsPanel />} />
          <Route path="/reviews/:id" element={<ReviewsPanel />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('queue renders clickable rows: question, lens, state, origin + correction chips', async () => {
  renderPanel()
  expect(await screen.findByText('What was Q3 revenue?')).toBeInTheDocument()
  expect(screen.getByText('sales')).toBeInTheDocument()
  expect(screen.getByText('needs human')).toBeInTheDocument()
  expect(screen.getByText('AI-flagged')).toBeInTheDocument()
  expect(screen.getByText('Δ definition')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Open review rev_abc123' })).toBeInTheDocument()
})

test('row click opens the detail: story, correction diff, and the action rail', async () => {
  renderPanel()
  fireEvent.click(await screen.findByRole('button', { name: 'Open review rev_abc123' }))

  // The story: question → answer → governed scope → SQL → AI judge. The recorded
  // SQL shows up twice by design: the story's SQL block and the correction diff's − line.
  expect(await screen.findAllByText('SELECT revenue FROM sales')).toHaveLength(2)
  expect(screen.getByText('SELECT revenue - refunds FROM sales')).toBeInTheDocument()
  expect(screen.getByText('Q3 revenue was $1.2M.')).toBeInTheDocument()
  expect(screen.getByText('The SQL ignores refunds.')).toBeInTheDocument()

  // Governed scope: the SQL decomposed so it can be audited without reading SQL.
  expect(screen.getByText('Governed scope')).toBeInTheDocument()
  expect(screen.getByText('NORTHWIND.GOLD.SALES')).toBeInTheDocument()
  expect(screen.getByText('no filter')).toBeInTheDocument()

  // The rail: the loop appears because a correction is recorded, with a draftable patch
  expect(screen.getByText('The loop')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Draft patch' })).toBeInTheDocument()

  // Human ruling stays available, secondary
  expect(screen.getByText('Human ruling')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument()
})

test('deep link /reviews/:id renders the detail directly', async () => {
  renderPanel('/reviews/rev_abc123')
  expect(await screen.findByText('rev_abc123')).toBeInTheDocument()
  expect(await screen.findByText('What was Q3 revenue?')).toBeInTheDocument()
})
