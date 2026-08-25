import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { DriftAuditPanel } from './Audit'

const finding = {
  metric_intent: 'Monthly net revenue',
  severity: 'conflict',
  blast_radius: 47,
  canon_index: 0,
  canon_rationale:
    'Reads from the gold layer (gold.fct_revenue_monthly), the business-ready curated source.',
  variants: [
    {
      statement: 'SELECT SUM(net_revenue_eur) AS total FROM gold.fct_revenue_monthly',
      source_tables: ['gold.fct_revenue_monthly'],
      run_count: 25,
      principals: ['CFO'],
      source_tools: ['Omni'],
      distinguishing: 'SUM(net_revenue_eur); over gold.fct_revenue_monthly',
      observed_columns: ['total'],
      observed_rows: [[1000000]],
      observed_error: null,
      tier: 'gold',
    },
    {
      statement: 'SELECT SUM(revenue_eur) AS total FROM gold.sales_revenue_monthly',
      source_tables: ['gold.sales_revenue_monthly'],
      run_count: 22,
      principals: ['ANALYST'],
      source_tools: ['Tableau'],
      distinguishing: 'SUM(revenue_eur); over gold.sales_revenue_monthly',
      observed_columns: ['total'],
      observed_rows: [[1180000]],
      observed_error: null,
      tier: 'gold',
    },
  ],
}

const auditResult = {
  id: 'a1',
  connection: 'snowflake-prod',
  days: 30,
  records_scanned: 412,
  findings: [finding],
  status: 'ok',
  created_at: new Date().toISOString(),
}

const summary = {
  connection: 'snowflake-prod',
  answer_accuracy: 0.92,
  accuracy_lenses: 2,
  governed_metrics: 3,
  ungoverned_metrics: 1,
  governed_share: 0.75,
  conflicts: 1,
  duplications: 0,
  has_run: true,
}

const candidate = {
  id: 'p1',
  ticket_id: null,
  lens: 'finance',
  kind: 'definition',
  target: 'Monthly net revenue',
  owner: 'lens-owner',
  diff_before: null,
  diff_after: 'Canonical reading …',
  status: 'candidate',
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
    if (url.includes('/audit/draft-definition')) return json(candidate)
    if (url.includes('/audit/summary')) return json(summary)
    if (url.includes('/audit/latest')) return json({ found: false }) // never audited → empty state
    if (url.includes('/audit')) return json(auditResult) // POST refresh → the fresh run

    if (url.includes('/mgmt/connections')) return json([{ name: 'snowflake-prod', type: 'snowflake', config: {}, has_secret: true }])
    if (url.includes('/mgmt/lenses')) return json([{ name: 'finance' }, { name: 'sales' }])
    return json({})
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderAudit() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/audit']}>
        <DriftAuditPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('the KPI band leads with answer accuracy, governed coverage, and open conflicts', async () => {
  renderAudit()
  await screen.findByRole('option', { name: 'snowflake-prod' }) // connections loaded
  expect(await screen.findByText('Answer accuracy')).toBeInTheDocument()
  expect(screen.getByText('92%')).toBeInTheDocument() // 0.92
  expect(screen.getByText('Governed coverage')).toBeInTheDocument()
  expect(screen.getByText('75%')).toBeInTheDocument()
  expect(screen.getByText('Open conflicts')).toBeInTheDocument()
})

test('a finding shows as a colored summary, then drills down to numbers + canon', async () => {
  renderAudit()
  await screen.findByRole('option', { name: 'snowflake-prod' }) // connections loaded
  expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

  // the high-level summary: title, severity, divergence — and the worst-split verdict
  expect(await screen.findByText('Monthly net revenue')).toBeInTheDocument()
  expect(screen.getByText('conflict')).toBeInTheDocument()
  expect(screen.getByText(/180,000 · 18\.0% apart/)).toBeInTheDocument()

  // collapsed by default — the diverging numbers are NOT shown yet
  expect(screen.queryByText('1,000,000')).toBeNull()

  // drill in: click the summary header → numbers + proposed canon appear
  fireEvent.click(screen.getByRole('button', { name: /Monthly net revenue/ }))
  expect(await screen.findByText('1,000,000')).toBeInTheDocument()
  expect(screen.getByText('1,180,000')).toBeInTheDocument()
  expect(screen.getByText('Proposed canon')).toBeInTheDocument()
  expect(screen.getByText(/blast radius 47 runs/)).toBeInTheDocument()
  expect(screen.getAllByText('show SQL').length).toBeGreaterThan(0)

  // A finding never offers to build a lens: lenses are authored in files. The
  // definition PatchCandidate bridge is the only route out of a finding.
  expect(screen.queryByText('Build a lens to fix this')).toBeNull()

  // the audit POST carried the window
  const calls = fetchMock.mock.calls.map((c) => String(c[0]))
  expect(calls.some((u) => u.includes('/mgmt/connections/snowflake-prod/audit?days=30'))).toBe(true)
})

test('draft definition posts the finding + lens and links to the patch queue', async () => {
  renderAudit()
  await screen.findByRole('option', { name: 'snowflake-prod' }) // connections loaded
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

  // expand the finding to reach its draft-definition bridge
  fireEvent.click(await screen.findByRole('button', { name: /Monthly net revenue/ }))
  const lensSelect = await screen.findByLabelText('Target lens')
  await screen.findByRole('option', { name: 'finance' }) // lenses loaded
  fireEvent.change(lensSelect, { target: { value: 'finance' } })
  fireEvent.click(screen.getByRole('button', { name: 'Draft definition' }))

  expect(await screen.findByText('Review in the patch queue')).toBeInTheDocument()
  const draftCall = fetchMock.mock.calls.find((c) => String(c[0]).includes('/audit/draft-definition'))
  expect(draftCall).toBeTruthy()
  const body = JSON.parse(String((draftCall![1] as RequestInit).body))
  expect(body.lens).toBe('finance')
  expect(body.finding.metric_intent).toBe('Monthly net revenue')
})
