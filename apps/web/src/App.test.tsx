import { test, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import App from './App'

function renderApp() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('renders the app shell with brand + nav', () => {
  renderApp()
  expect(screen.getByText('dst')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Certify' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Data sources' })).toBeInTheDocument()
})
