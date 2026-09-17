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
  // Governed basis: 2390 of 3727 graded (3847 answered − 120 pre-ledger rows).
  declared_pct: 64.1,
  declared_delta_pp: null,
  resolution_unknown: 120,
  resolution_histogram: { certified: 800, declared: 1590, mixed: 400, inferred: 937, unknown: 120 },
  // Typed: share over the 3547 answers served since typed serving existed
  // (3847 answered − 300 predating rows). Delta null — keeps "one delta" true.
  typed_pct: 78.2,
  typed_delta_pp: null,
  typed_unknown: 300,
  unresolved_slots: [
    { slot: 'region', count: 41 },
    { slot: 'product_line', count: 9 },
  ],
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
      declared_pct: 88.0,
      typed_pct: 91.0,
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
      declared_pct: null, // every answer predates the ledger — untested, not 0%
      typed_pct: null, // every answer predates typed serving — blank, not 0%
      cost_usd: 31.44,
      owner: '',
      degraded: 'DEGRADED: schema drift on connection x',
      gate_score: 1.0,
    },
  ],
  certified_active: 61,
  open_incident_tickets: 1,
}

function stubStatement(statement: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(JSON.stringify(statement), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    ),
  )
}

beforeEach(() => stubStatement(STATEMENT))
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
  const flagged = screen.getByText('served, but a figure could not be traced to the result rows')
  const cell = flagged.parentElement!
  expect(cell.textContent).toContain('193')
  expect(cell.querySelector('.text-red')).not.toBeNull()
})

test('governed basis: the declared share over graded answers, pre-ledger rows named beside it', async () => {
  const { container } = renderPage()
  expect(await screen.findByText('64.1')).toBeInTheDocument()
  expect(
    screen.getByText(/2,390 of 3,727 graded answers computed from a certified or declared/),
  ).toBeInTheDocument()
  // The ungraded rows are counted, not folded into a 0% — and never "not governed".
  expect(screen.getByText('120 answers predate the ledger — not graded.')).toBeInTheDocument()
  expect(container.textContent).not.toMatch(/not governed|ungoverned/i)
  // Band legend: percentages over the 3727 graded; unknown as a count.
  expect(
    screen.getByText(/21% certified · 43% declared · 11% mixed · 25% inferred/),
  ).toBeInTheDocument()
  expect(screen.getByText(/· 120 unknown/)).toBeInTheDocument()
  // The declared/inferred split is a source story, never an alarm: no red in the cell.
  const cell = screen.getByText('Governed basis').closest('div')!.parentElement!
  expect(cell.querySelector('.text-red, .bg-red')).toBeNull()
})

test('a window the ledger never graded renders — for the basis, not 0%', async () => {
  stubStatement({
    ...STATEMENT,
    declared_pct: null,
    declared_delta_pp: null,
    resolution_unknown: 0,
    resolution_histogram: {},
  })
  renderPage()
  await screen.findByText('93.4')
  const label = screen.getByText('Governed basis').closest('div')!.parentElement!
  expect(label.textContent).toContain('—%')
  expect(screen.queryByText(/predate the ledger/)).toBeNull()
})

test('the lens ledger carries a Declared column, blank where nothing was graded', async () => {
  renderPage()
  await screen.findByRole('link', { name: 'finance' })
  expect(screen.getByText('Declared', { selector: 'th' })).toBeInTheDocument()
  const finance = screen.getByRole('link', { name: 'finance' }).closest('tr')!
  expect(finance.textContent).toContain('88%')
  const board = screen.getByRole('link', { name: 'board_metrics' }).closest('tr')!
  expect(board.textContent).toContain('—')
})

test('typed: the share over answers served since typed serving, predating rows named apart', async () => {
  const { container } = renderPage()
  expect(await screen.findByText('78.2')).toBeInTheDocument()
  expect(screen.getByText('78.2% of 3,547 answers typed end to end.')).toBeInTheDocument()
  expect(screen.getByText('300 answers predate typed serving.')).toBeInTheDocument()
  // Vocabulary pin: a predating row is counted apart, never "untyped". The
  // hint's own sentence that says so is the one place the word may appear.
  const visible = container.cloneNode(true) as HTMLElement
  visible.querySelectorAll('[role="tooltip"]').forEach((el) => el.remove())
  expect(visible.textContent).not.toMatch(/untyped/i)
  // The typed cell is a share story, never an alarm: no red in it.
  const cell = screen.getByText('Typed', { selector: 'div' }).closest('div')!.parentElement!
  expect(cell.querySelector('.text-red, .bg-red')).toBeNull()
})

test('typed reads blank, not 0%, when nothing was served since typed serving', async () => {
  stubStatement({ ...STATEMENT, typed_pct: null, typed_delta_pp: null, typed_unknown: 0 })
  renderPage()
  await screen.findByText('93.4')
  const cell = screen.getByText('Typed', { selector: 'div' }).closest('div')!.parentElement!
  expect(cell.textContent).toContain('—%')
  expect(screen.queryByText(/predate typed serving/)).toBeNull()
})

test('dictionary gaps list the slots that clarified, with counts, and hide when empty', async () => {
  renderPage()
  expect(await screen.findByText('Dictionary gaps')).toBeInTheDocument()
  expect(
    screen.getByText('columns that clarified for want of a complete value dictionary — profile them'),
  ).toBeInTheDocument()
  const region = screen.getByText('region').closest('li')!
  expect(region.textContent).toContain('×41')
  expect(screen.getByText('product_line').closest('li')!.textContent).toContain('×9')
})

test('no dictionary gaps — no list', async () => {
  stubStatement({ ...STATEMENT, unresolved_slots: [] })
  renderPage()
  await screen.findByText('93.4')
  expect(screen.queryByText('Dictionary gaps')).toBeNull()
})

test('the lens ledger carries a Typed column, blank where nothing was served since typed serving', async () => {
  renderPage()
  await screen.findByRole('link', { name: 'finance' })
  expect(screen.getByText('Typed', { selector: 'th' })).toBeInTheDocument()
  const finance = screen.getByRole('link', { name: 'finance' }).closest('tr')!
  expect(finance.textContent).toContain('91%')
  const board = screen.getByRole('link', { name: 'board_metrics' }).closest('tr')!
  // verified 91%, declared —, typed — : two blanks in the row
  expect(board.textContent!.match(/—/g)).toHaveLength(2)
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
