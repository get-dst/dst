import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { DataSources } from './DataSources'

// The page fires read queries on mount; stub fetch so they resolve to empty.
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
        <DataSources />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('a warehouse tile opens the declare-connection guide, not a create form', () => {
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: /BigQuery/ }))

  // The guide renders the copy-paste declaration…
  expect(screen.getByText('Declare a BigQuery connection')).toBeInTheDocument()
  expect(screen.getByText(/type: bigquery/)).toBeInTheDocument()
  expect(screen.getAllByText(/DST_API_KEY_BIGQUERY/).length).toBeGreaterThan(0)
  // …including the @path .env file-ref convention for the SA JSON…
  expect(screen.getByText(/@\/path\/to\/service-account\.json/)).toBeInTheDocument()
  // …and the apply step (server probes credentials before accepting).
  expect(screen.getAllByText(/dst apply/).length).toBeGreaterThan(0)

  // No credential form: connections are declared in files, never typed into the UI.
  expect(screen.queryByLabelText(/Service-account JSON/)).toBeNull()
  expect(screen.queryByRole('button', { name: /Verify|Add connection/ })).toBeNull()
})

test('the roles + provider setup guide content survives the repurposing', () => {
  renderPage()
  fireEvent.click(screen.getByRole('button', { name: /Snowflake/ }))
  expect(screen.getByText('Credential roles')).toBeInTheDocument()
  expect(screen.getByText(/USAGE on the warehouse, database & schema/)).toBeInTheDocument()
  expect(screen.getByText('Create a role and user')).toBeInTheDocument()
  expect(screen.getByText(/GRANT USAGE ON WAREHOUSE/)).toBeInTheDocument()
})
