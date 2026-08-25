import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { LensDetail } from './LensDetail'

vi.mock('../api/auth', () => ({ getToken: () => 'dstadm_test' }))

const BUNDLE = {
  config: {
    access: { allow: [{ group: 'analysts' }] },
    model: { provider: 'anthropic', model: 'claude-sonnet-4-6', max_rows_to_compose: 200, answer_mode: 'strict' },
  },
  semantic_model: { dialect: 'postgres', entities: [], definitions: [], sample_queries: [] },
}

const DETAIL = {
  name: 'sales',
  display_name: 'Sales',
  description: '',
  status: 'live',
  draft: null,
  published: BUNDLE,
}

const CASES = [
  { id: 'c1', question: 'how many repeat customers?', expected_sql: 'select 1', source: 'certified', status: 'candidate', created_by: 'ai' },
]

// Route reads by path; everything unmatched resolves to an empty list.
function payload(url: string): unknown {
  if (url.endsWith('/mgmt/lenses/sales')) return DETAIL
  if (url.includes('/evals/cases')) return CASES
  if (url.includes('/context'))
    return { lens: 'sales', sources: [{ source: 'handbook', chunks: 3, last_indexed: null }] }
  return []
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      return new Response(JSON.stringify(payload(String(url))), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
})
afterEach(() => vi.unstubAllGlobals())

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/lenses/sales']}>
        <Routes>
          <Route path="/lenses/:name" element={<LensDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const openTab = (label: string) => fireEvent.click(screen.getByRole('tab', { name: label }))

test('the header publishes nothing — Validate (a read-only check) survives', async () => {
  renderDetail()
  expect(await screen.findByRole('button', { name: 'Validate' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Publish/ })).toBeNull()
})

test('Answering renders the mode in force read-only and names the file', async () => {
  renderDetail()
  openTab('Answering')
  // The applied mode is marked, and no mode is clickable.
  expect(await screen.findByText('in force')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Exploratory/ })).toBeNull()
  expect(screen.getByText('lenses/sales/lens.yaml')).toBeInTheDocument()
  expect(screen.getAllByText('dst apply').length).toBeGreaterThan(0)
})

test('Access lists the compiled allow-rules; granting is a file edit', async () => {
  renderDetail()
  openTab('Access')
  expect(await screen.findByText('analysts')).toBeInTheDocument()
  expect(screen.getByText('access.allow')).toBeInTheDocument()
  // The people/groups editor and its AI resolver are gone.
  expect(screen.queryByPlaceholderText(/Describe who should have access/)).toBeNull()
})

test('Evaluation lists cases read-only — running the suite stays', async () => {
  renderDetail()
  openTab('Evaluation')
  expect(await screen.findByText('how many repeat customers?')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /Run health check/ })).toBeInTheDocument()
  for (const gone of ['Generate cases', 'Approve', 'Retire', 'Delete']) {
    expect(screen.queryByRole('button', { name: gone })).toBeNull()
  }
  expect(screen.getByText('lenses/sales/evals/cases.yaml')).toBeInTheDocument()
})

test('Context sources are read-only — no note form, no upload, no sync', async () => {
  renderDetail()
  openTab('Context')
  // The indexed source renders…
  expect(await screen.findByText('handbook')).toBeInTheDocument()
  // …and the tab offers no write affordance.
  expect(screen.queryByText('Add a note')).toBeNull()
  expect(screen.queryByText('or upload a file')).toBeNull()
  expect(screen.queryByRole('button', { name: 'Sync into lens' })).toBeNull()
  // Certifying an answer is governing — that one stays (its branch starts collapsed).
  fireEvent.click(screen.getByRole('button', { name: /Certified answers/ }))
  expect(screen.getByRole('button', { name: /Add certified answer/ })).toBeInTheDocument()
})
