import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Certify } from './Certify'

// Two lenses: finance certifies/tests/publishes, sales has nothing yet — the
// hub must show both the substance and the gap.
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
      if (url.includes('/mgmt/lenses/finance/certified'))
        return json([
          {
            id: 'c1',
            question: 'What was monthly net revenue in June?',
            sql: 'SELECT SUM(net_revenue_eur) FROM gold.fct_revenue_monthly',
            created_by: 'alex',
            created_at: '2026-08-01T10:00:00Z',
            verified_value: { value: 1000000 },
          },
        ])
      if (url.includes('/certified')) return json([])
      if (url.includes('/mgmt/lenses/finance/evals/cases'))
        return json([
          { id: 'e1', question: 'q', expected_sql: 's', source: 'certified', status: 'approved', created_by: 'alex' },
          { id: 'e2', question: 'q2', expected_sql: 's2', source: 'harvested', status: 'candidate', created_by: 'alex' },
        ])
      if (url.includes('/evals/cases')) return json([])
      if (url.includes('/mgmt/lenses/finance/evals/runs'))
        return json([{ id: 'r1', mode: 'regression', score: 0.95, passed: 19, failed: 1, errored: 0 }])
      if (url.includes('/evals/runs')) return json([])
      if (url.includes('/mgmt/lenses/finance/versions'))
        return json([
          { version: 3, summary: 'net revenue definition made canon', created_at: '2026-08-02T10:00:00Z' },
          { version: 2, summary: 'first eval cases', created_at: '2026-07-20T10:00:00Z' },
        ])
      if (url.includes('/versions')) return json([])
      if (url.includes('/mgmt/connections')) return json([])
      if (url.includes('/mgmt/lenses'))
        return json([
          { name: 'finance', status: 'live' },
          { name: 'sales', status: 'draft' },
        ])
      return json({})
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderCertify(initialTab?: 'drift') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Certify initialTab={initialTab} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('certified answers lead, aggregated across lenses with their verified values', async () => {
  renderCertify()
  expect(await screen.findByText('What was monthly net revenue in June?')).toBeInTheDocument()
  expect(screen.getByText('1,000,000')).toBeInTheDocument() // the verified value
  expect(screen.getByText('1 certified answer across 2 lenses')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'finance' })).toHaveAttribute('href', '/lenses/finance')
})

test('eval cases and deployments tabs aggregate per lens, gaps visible', async () => {
  renderCertify()

  fireEvent.click(screen.getByRole('tab', { name: 'Eval cases' }))
  expect(await screen.findByText('95%')).toBeInTheDocument()
  expect(screen.getByText(/19 passed · 1 failed · 0 errored/)).toBeInTheDocument()
  expect(screen.getByText('never run')).toBeInTheDocument() // sales ships unprotected

  fireEvent.click(screen.getByRole('tab', { name: 'Deployments' }))
  expect(await screen.findByText(/1\/2 lenses live · 2 published versions/)).toBeInTheDocument()
  expect(screen.getByText('never published')).toBeInTheDocument() // sales again
  expect(screen.getByText('finance v3')).toBeInTheDocument()
  // the latest summary shows twice: on the live-now row and in the history feed
  expect(screen.getAllByText('net revenue definition made canon')).toHaveLength(2)
})

test('the drift audit survives as a tab, and /audit deep-links into it', async () => {
  renderCertify('drift')
  // The drift panel owns the warehouse selector; with no connections it shows its
  // declare-first empty state pointing at Data sources.
  expect(await screen.findByText('No warehouse connections')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /Go to Data sources/ })).toHaveAttribute(
    'href',
    '/data-sources',
  )
})
