import { Fragment, useState } from 'react'
import { getToken } from '../api/auth'
import {
  useCallerReport,
  useEvalTrend,
  useKpis,
  useRequestDetail,
  useRequests,
  type LensEvalTrend,
  type RequestTrace,
} from '../api/observe'
import { Badge } from '../components/ui/Badge'
import { Skeleton } from '../components/ui/Skeleton'
import { Page, PageHeader } from '../components/ui/Page'
import { Readout } from '../components/ui/Readout'
import { Tabs, TabList, TabTrigger, TabPanel } from '../components/ui/Tabs'
import { ReviewsPanel } from './Reviews'
import { ConfidenceBadge } from '../components/ui/OutcomeBadges'
import { formatBytes, formatCost } from '../lib/format'
import { statusVariant } from '../lib/outcomes'

// One color per eval-run mode, shared by the bars and the legend. Monochrome
// except certified-green: regression is ink, health a light neutral — the gap
// between them is what keeps the series readable without an accent hue.
const MODE_BAR: Record<string, string> = {
  certified: 'bg-green',
  regression: 'bg-accent',
  health: 'bg-[var(--color-border-strong)]',
}
const MODE_LEGEND: [string, string][] = [
  ['certified', 'standing suite'],
  ['regression', 'publish gate (certified + behavioral)'],
  ['health', 'live spot-check'],
]

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div
      className="rounded-lg border border-border bg-surface p-5"
      style={{ boxShadow: 'var(--shadow-card)' }}
    >
      <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div className="mt-3 font-mono text-[26px] font-semibold tracking-tight text-text tabular-nums leading-none">
        {value}
      </div>
      {sub && (
        <div className="mt-2 font-mono text-[11px] text-muted tabular-nums">{sub}</div>
      )}
    </div>
  )
}

export function Observe({ initialTab = 'requests' }: { initialTab?: 'requests' | 'reviews' }) {
  const [tab, setTab] = useState<'requests' | 'reviews'>(initialTab)
  // Same query the KPI grid runs — react-query dedupes; the fascia readout is
  // the headline instruments, the grid below is the detail.
  const kpis = useKpis()
  return (
    <Page width="data">
      <PageHeader
        title="Observe"
        description="Cost, activity, and the verification queue."
        readout={
          kpis.data ? (
            <Readout
              items={[
                { label: 'queries', value: kpis.data.queries.toLocaleString() },
                { label: 'errors', value: kpis.data.errors.toLocaleString() },
                { label: 'ai', value: formatCost(kpis.data.ai_cost_usd) },
                { label: 'wh', value: formatCost(kpis.data.warehouse_cost_usd) },
              ]}
            />
          ) : undefined
        }
      />
      <div className="mt-5">
        <Tabs value={tab} onValueChange={(v) => setTab(v as 'requests' | 'reviews')}>
          <TabList>
            <TabTrigger value="requests">Cost &amp; requests</TabTrigger>
            <TabTrigger value="reviews">Reviews</TabTrigger>
          </TabList>
          <div className="mt-5">
            <TabPanel value="requests"><CostAndRequests /></TabPanel>
            <TabPanel value="reviews"><ReviewsPanel /></TabPanel>
          </div>
        </Tabs>
      </div>
    </Page>
  )
}

function AccuracyTrendCard({ trend }: { trend: LensEvalTrend }) {
  const scored = trend.runs.filter((r) => r.score != null)
  const last = scored[scored.length - 1]
  const pct = (s: number | null) => (s == null ? '—' : `${Math.round(s * 100)}%`)
  return (
    <div
      className="rounded-lg border border-border bg-surface p-4"
      style={{ boxShadow: 'var(--shadow-card)' }}
    >
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono text-[13px] font-semibold text-text">{trend.lens}</span>
        <span
          className={[
            'font-mono text-[20px] font-semibold tabular-nums leading-none',
            trend.latest_score == null
              ? 'text-muted'
              : trend.latest_score >= 0.9
                ? 'text-green'
                : trend.latest_score >= 0.7
                  ? 'text-amber'
                  : 'text-red',
          ].join(' ')}
        >
          {pct(trend.latest_score)}
        </span>
      </div>
      {/* run-by-run bars: the "is this lens getting better?" chart */}
      <div className="mt-3 flex h-10 items-end gap-1" aria-hidden="true">
        {scored.slice(-24).map((r, i) => (
          <span
            key={i}
            title={`${r.mode} · ${pct(r.score)}${r.started_at ? ` · ${new Date(r.started_at).toLocaleString()}` : ''}`}
            className={['w-2 rounded-sm', MODE_BAR[r.mode] ?? 'bg-green'].join(' ')}
            style={{ height: `${Math.max(8, Math.round((r.score ?? 0) * 100))}%` }}
          />
        ))}
        {scored.length === 0 && (
          <span className="text-[12px] text-muted">No scored runs yet.</span>
        )}
      </div>
      {last && (
        <p className="mt-2 font-mono text-[11px] text-muted tabular-nums">
          last {last.mode}: {last.passed} pass · {last.failed} fail
          {last.errored > 0 ? ` · ${last.errored} error` : ''}
          {last.started_at ? ` · ${new Date(last.started_at).toLocaleString()}` : ''}
        </p>
      )}
    </div>
  )
}

function CostAndRequests() {
  const hasToken = !!getToken()
  const kpis = useKpis()
  const callers = useCallerReport()
  const evals = useEvalTrend()

  return (
    <>

      {/* Auth prompt */}
      {!hasToken && (
        <div className="mt-4 flex items-center gap-3 rounded-md border border-border bg-surface-2 px-4 py-3">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true" className="shrink-0 text-muted">
            <circle cx="7" cy="7" r="6" stroke="currentColor" strokeWidth="1.25" />
            <path d="M7 4.5v3M7 9.5v.5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
          </svg>
          <p className="text-[13px] text-muted">Set your admin token (top-right) to load observability.</p>
        </div>
      )}

      {/* KPI grid */}
      {kpis.isLoading && (
        <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="rounded-md border border-border bg-surface p-4">
              <Skeleton className="h-3 w-20 mb-3" />
              <Skeleton className="h-7 w-28" />
            </div>
          ))}
        </div>
      )}
      {kpis.data && (
        <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Kpi
            label="Queries"
            value={kpis.data.queries.toLocaleString()}
          />
          <Kpi
            label="AI cost"
            value={formatCost(kpis.data.ai_cost_usd)}
            sub={`${kpis.data.input_tokens.toLocaleString()} in / ${kpis.data.output_tokens.toLocaleString()} out${
              (kpis.data.unpriced ?? 0) > 0 ? ` · ${kpis.data.unpriced} unpriced` : ''
            }`}
          />
          <Kpi
            label="Warehouse cost"
            value={formatCost(kpis.data.warehouse_cost_usd)}
          />
          <Kpi
            label="Errors"
            value={kpis.data.errors.toLocaleString()}
            sub={`${(kpis.data.declined ?? 0).toLocaleString()} declined (refused / clarify)`}
          />
        </div>
      )}

      {/* Accuracy section — the per-lens trend */}
      {evals.data && evals.data.length > 0 && (
        <div className="mt-8">
          <h2 className="text-[13px] font-bold uppercase tracking-wider text-muted">Accuracy</h2>
          <p className="mt-1 text-[12px] text-muted">
            Each bar is one eval run, oldest to newest, read from dst&apos;s own run
            history — nothing is written to your warehouse.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted">
            {MODE_LEGEND.map(([mode, what]) => (
              <span key={mode} className="flex items-center gap-1.5">
                <span className={`h-2 w-2 rounded-sm ${MODE_BAR[mode]}`} aria-hidden="true" />
                <span className="font-mono">{mode}</span> — {what}
              </span>
            ))}
          </div>
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {evals.data.map((t) => (
              <AccuracyTrendCard key={t.lens} trend={t} />
            ))}
          </div>
        </div>
      )}

      {/* Callers section */}
      <div className="mt-8">
        <h2 className="text-[13px] font-bold uppercase tracking-wider text-muted">Callers</h2>
        {callers.isLoading && (
          <div className="mt-3 space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-3/4" />
          </div>
        )}
        {callers.data && callers.data.length > 0 ? (
          <div
            className="mt-3 rounded-md border border-border overflow-hidden"
            style={{ boxShadow: 'var(--shadow-card)' }}
          >
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="bg-surface-2">
                  {['Caller', 'Queries', 'AI cost', 'Warehouse cost', 'Declined', 'Errors'].map((h) => (
                    <th
                      key={h}
                      className="border-b border-border px-4 py-2.5 text-left text-[11px] font-medium uppercase tracking-wider text-muted"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {callers.data.map((r) => (
                  <tr key={r.caller} className="bg-surface hover:bg-surface-2 transition-colors" style={{ transitionDuration: 'var(--duration-fast)' }}>
                    <td className="px-4 py-2.5 font-mono text-[12px] text-text">{r.caller}</td>
                    <td className="px-4 py-2.5 tabular-nums text-text">{r.queries.toLocaleString()}</td>
                    <td className="px-4 py-2.5 font-mono tabular-nums text-text">
                      {formatCost(r.ai_cost_usd)}
                      {(r.unpriced ?? 0) > 0 && (
                        <span
                          className="text-muted"
                          title={`${r.unpriced} request(s) used a model with no configured price — that AI spend is uncounted, not $0`}
                        >
                          {' '}*
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 font-mono tabular-nums text-text">{formatCost(r.wh_cost_usd)}</td>
                    <td className="px-4 py-2.5 tabular-nums">
                      {/* Declines are governed outcomes (coverage signal), never red. */}
                      {(r.declined ?? 0) > 0 ? (
                        <span className="text-muted tabular-nums">{r.declined}</span>
                      ) : (
                        <span className="text-muted-2">0</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 tabular-nums">
                      {r.errors > 0 ? (
                        <span className="text-red tabular-nums">{r.errors}</span>
                      ) : (
                        <span className="text-muted-2">0</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : callers.data ? (
          <p className="mt-3 text-[13px] text-muted">No queries yet.</p>
        ) : null}
        {callers.data?.some((r) => (r.unpriced ?? 0) > 0) && (
          <p className="mt-2 text-[11px] text-muted">
            * some requests used a model with no configured price — their AI spend is
            uncounted, not $0.
          </p>
        )}
      </div>

      <RequestExplorer />
    </>
  )
}

function RequestExplorer() {
  const requests = useRequests()
  const [selected, setSelected] = useState<string | null>(null)
  const detail = useRequestDetail(selected)

  return (
    <div className="mt-8">
      <div className="flex items-baseline gap-3">
        <h2 className="text-[13px] font-bold uppercase tracking-wider text-muted">Requests</h2>
        <span className="text-[12px] text-muted-2">Click a row to inspect its full trace.</span>
      </div>

      {requests.isLoading && (
        <div className="mt-3 space-y-2">
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-full" />
          <Skeleton className="h-8 w-5/6" />
        </div>
      )}

      {requests.data && requests.data.length > 0 ? (
        <div
          className="mt-3 rounded-md border border-border overflow-hidden"
          style={{ boxShadow: 'var(--shadow-card)' }}
        >
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="bg-surface-2">
                {['Time', 'Lens', 'Caller', 'Status', 'Rows', 'Cost'].map((h) => (
                  <th
                    key={h}
                    className="border-b border-border px-4 py-2.5 text-left text-[11px] font-medium uppercase tracking-wider text-muted"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {requests.data.map((r) => (
                <Fragment key={r.request_id}>
                <tr
                  tabIndex={0}
                  role="button"
                  onClick={() => setSelected(r.request_id === selected ? null : r.request_id)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      setSelected(r.request_id === selected ? null : r.request_id)
                    }
                  }}
                  className={[
                    'cursor-pointer transition-colors outline-none',
                    'focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent',
                    selected === r.request_id
                      ? 'bg-accent-fg border-l-2 border-l-accent'
                      : 'bg-surface hover:bg-surface-2',
                  ].join(' ')}
                  style={{ transitionDuration: 'var(--duration-fast)' }}
                >
                  <td className="px-4 py-2.5 font-mono text-[11px] text-muted tabular-nums">
                    {r.created_at ? new Date(r.created_at).toLocaleTimeString() : '—'}
                  </td>
                  <td className="px-4 py-2.5 font-mono text-[12px] font-medium text-text">{r.lens}</td>
                  <td className="px-4 py-2.5 font-mono text-[11px] text-muted">{r.caller}</td>
                  <td className="px-4 py-2.5">
                    <Badge variant={statusVariant(r.status)} dot>
                      {r.status}
                    </Badge>
                  </td>
                  <td className="px-4 py-2.5 text-[13px] tabular-nums text-text">
                    {r.row_count ?? <span className="text-muted-2">—</span>}
                  </td>
                  <td className="px-4 py-2.5 font-mono text-[12px] tabular-nums text-text">
                    {formatCost(r.cost_usd)}
                  </td>
                </tr>
                {/* The trace opens right under its row: on a long table a panel
                    below the whole list would open off-screen. */}
                {selected === r.request_id && (
                  <tr>
                    <td colSpan={6} className="bg-surface-2 px-3 pb-3">
                      {detail.isLoading && (
                        <div className="mt-3 rounded-md border border-border bg-surface p-4 space-y-3">
                          <Skeleton className="h-4 w-64" />
                          <Skeleton className="h-16 w-full" />
                        </div>
                      )}
                      {detail.isError && (
                        <p className="mt-3 rounded-md border border-red/25 bg-red-bg px-4 py-3 text-[13px] text-red">
                          Couldn't load this trace — {(detail.error as Error)?.message ?? 'request failed'}.
                        </p>
                      )}
                      {detail.data && (
                        <TraceDetail trace={detail.data} onClose={() => setSelected(null)} />
                      )}
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      ) : requests.data ? (
        <p className="mt-3 text-[13px] text-muted">No requests logged yet.</p>
      ) : null}
    </div>
  )
}

function TraceDetail({ trace, onClose }: { trace: RequestTrace; onClose: () => void }) {
  const [sqlCopied, setSqlCopied] = useState(false)

  const copySql = () => {
    if (!trace.sql) return
    navigator.clipboard.writeText(trace.sql).then(() => {
      setSqlCopied(true)
      setTimeout(() => setSqlCopied(false), 1800)
    })
  }

  const metaRow = (label: string, content: React.ReactNode) => (
    <div className="grid grid-cols-[140px_1fr] gap-3 py-2 border-b border-border last:border-0">
      <span className="text-[11px] font-semibold uppercase tracking-wider text-muted self-start pt-0.5">
        {label}
      </span>
      <span className="text-[13px] text-text break-words">{content}</span>
    </div>
  )

  const latencyDisplay = trace.latency
    ? (() => {
        try {
          const parsed = typeof trace.latency === 'string' ? JSON.parse(trace.latency) : trace.latency
          if (parsed && typeof parsed === 'object') {
            return Object.entries(parsed as Record<string, unknown>)
              .map(([k, v]) => `${k}: ${v}`)
              .join(' · ')
          }
          return JSON.stringify(trace.latency)
        } catch {
          return String(trace.latency)
        }
      })()
    : null

  return (
    <div
      className="mt-3 rounded-md border border-accent/40 bg-surface overflow-hidden"
      style={{ boxShadow: 'var(--shadow-popover)' }}
    >
      {/* Trace header */}
      <div className="flex items-center justify-between border-b border-border bg-surface-2 px-4 py-2.5">
        <div className="flex items-center gap-3">
          <span className="text-[11px] font-medium uppercase tracking-wider text-muted">Trace</span>
          <code className="font-mono text-[11px] text-text">{trace.request_id}</code>
          <Badge variant={statusVariant(trace.status)} dot>
            {trace.status}
          </Badge>
        </div>
        <button
          onClick={onClose}
          className={[
            'flex items-center gap-1 rounded px-2 py-1',
            'text-[12px] text-muted hover:text-text hover:bg-surface-3 transition-colors',
            'outline-none focus-visible:ring-2 focus-visible:ring-accent',
          ].join(' ')}
          style={{ transitionDuration: 'var(--duration-fast)' }}
        >
          <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true">
            <path d="M1.5 1.5l8 8M9.5 1.5l-8 8" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
          </svg>
          Close
        </button>
      </div>

      {/* Meta rows */}
      <div className="px-4 divide-y divide-border">
        {trace.question && metaRow('Question', trace.question)}
        {trace.definition_used && metaRow(
          'Definition',
          <code className="font-mono text-[12px] text-text bg-surface-2 px-1.5 py-0.5 rounded border border-border">
            {trace.definition_used}
          </code>
        )}
        {trace.answer && metaRow('Answer', trace.answer)}
        {trace.confidence && metaRow(
          'Confidence',
          <ConfidenceBadge confidence={trace.confidence} dot />
        )}
        {trace.verification?.checks?.length ? metaRow(
          'Verification',
          <span className="flex flex-wrap gap-1.5">
            {trace.verification.checks.map((c) => (
              <span
                key={c.name}
                title={c.reason ?? undefined}
                className={[
                  'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[11px]',
                  c.status === 'pass'
                    ? 'border-green/25 bg-green-bg text-green'
                    : c.status === 'fail'
                      ? 'border-red/25 bg-red-bg text-red'
                      : 'border-border bg-surface-2 text-muted',
                ].join(' ')}
              >
                {c.status === 'pass' ? '✓' : c.status === 'fail' ? '✗' : '·'} {c.name}
              </span>
            ))}
          </span>
        ) : null}
        {trace.scope && trace.scope.tables.length > 0 && metaRow(
          'Scope',
          <span className="font-mono text-[12px] text-text break-words">
            {trace.scope.tables.join(', ')}
            {trace.scope.fields.length > 0 ? ` · fields: ${trace.scope.fields.join(', ')}` : ''}
            {trace.scope.filters.length > 0 ? ` · filters: ${trace.scope.filters.join(' AND ')}` : ''}
            {trace.scope.order_by.length > 0 ? ` · order: ${trace.scope.order_by.join(', ')}` : ''}
          </span>
        )}
        {trace.certification && trace.certification !== 'none' && metaRow(
          'Certification',
          <Badge variant={trace.certification === 'certified' ? 'success' : 'default'} dot>
            {trace.certification}
          </Badge>
        )}
        {trace.row_count != null && metaRow(
          'Rows',
          <span className="tabular-nums font-mono text-[13px]">{trace.row_count}</span>
        )}
        {(trace.ai_input_tokens != null || trace.ai_output_tokens != null) && metaRow(
          'AI tokens',
          <span className="tabular-nums font-mono text-[13px]">
            {(trace.ai_input_tokens ?? 0).toLocaleString()} in
            {' / '}
            {(trace.ai_output_tokens ?? 0).toLocaleString()} out
          </span>
        )}
        {trace.ai_cost_usd != null && metaRow(
          'AI cost',
          <span className="tabular-nums font-mono text-[13px]">{formatCost(trace.ai_cost_usd)}</span>
        )}
        {(trace.wh_bytes != null || trace.wh_cost_usd != null) && metaRow(
          'Warehouse',
          <span className="tabular-nums font-mono text-[13px]">
            {trace.wh_bytes != null ? `${formatBytes(trace.wh_bytes)} scanned` : ''}
            {trace.wh_bytes != null && trace.wh_cost_usd != null ? ' · ' : ''}
            {trace.wh_cost_usd != null ? formatCost(trace.wh_cost_usd) : ''}
          </span>
        )}
        {latencyDisplay && metaRow(
          'Latency',
          <span className="font-mono text-[12px] text-text">{latencyDisplay}</span>
        )}
        {trace.error && metaRow(
          'Error',
          <span className="text-red text-[13px]">{trace.error}</span>
        )}
      </div>

      {/* SQL block */}
      {trace.sql && (
        <div className="border-t border-border">
          <div className="flex items-center justify-between border-b border-border bg-surface-2 px-4 py-2">
            <div className="flex items-center gap-2">
              <span className="text-[11px] font-medium uppercase tracking-wider text-muted">SQL</span>
              <Badge variant="default">generated</Badge>
            </div>
            <button
              onClick={copySql}
              className={[
                'flex items-center gap-1.5 rounded px-2 py-1',
                'text-[11px] font-medium transition-colors',
                'outline-none focus-visible:ring-2 focus-visible:ring-accent',
                sqlCopied ? 'text-green' : 'text-muted hover:text-text hover:bg-surface-3',
              ].join(' ')}
              style={{ transitionDuration: 'var(--duration-fast)' }}
            >
              {sqlCopied ? (
                <>
                  <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true">
                    <path d="M1.5 5.5l3 3 5-5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  Copied
                </>
              ) : (
                <>
                  <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true">
                    <rect x="3" y="3" width="7" height="7" rx="1" stroke="currentColor" strokeWidth="1.1" />
                    <path d="M1.5 7.5V1.5h6" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
                  </svg>
                  Copy
                </>
              )}
            </button>
          </div>
          <pre className="overflow-x-auto px-4 py-3 font-mono text-[12px] leading-relaxed text-text bg-surface">
            {trace.sql}
          </pre>
        </div>
      )}
    </div>
  )
}
