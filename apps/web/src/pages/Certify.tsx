/**
 * Certify — is what dst serves provably right?
 *
 * The certification hub across every lens: the certified question→SQL pairs
 * answers are served from (and regression-tested against — the certified
 * corpus IS the regression suite), the behavioral eval pins beside them, and
 * the published lens versions (deployments) that carry them live. The drift audit —
 * the warehouse's own query history mined for contradictions — sits alongside
 * as the finder that feeds certification; /audit deep-links to that tab.
 *
 * Read-only overview: certifying, approving cases, and publishing all happen on
 * the lens (its Certified/Evals tabs, `dst apply`) — every row links there.
 */
import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useQueries } from '@tanstack/react-query'
import { apiGet } from '../api/client'
import { useLenses } from '../api/lenses'
import type { CertifiedAnswer, VerifiedValue } from '../api/certify'
import type { EvalCase, EvalRun } from '../api/evals'
import type { LensVersion } from '../api/repo'
import { Badge } from '../components/ui/Badge'
import { EmptyState } from '../components/ui/EmptyState'
import { Page, PageHeader } from '../components/ui/Page'
import { Skeleton } from '../components/ui/Skeleton'
import { Tabs, TabList, TabTrigger, TabPanel } from '../components/ui/Tabs'
import { actorLabel } from '../lib/outcomes'
import { DriftAuditPanel } from './Audit'

const enc = encodeURIComponent

/** One query per lens, keyed exactly like the per-lens hooks so caches are shared
 * with the LensDetail tabs. */
function usePerLens<T>(names: string[], key: (n: string) => unknown[], path: (n: string) => string) {
  return useQueries({
    queries: names.map((n) => ({ queryKey: key(n), queryFn: () => apiGet<T>(path(n)) })),
  })
}

const fmtNumber = (v: number) =>
  Number.isInteger(v)
    ? v.toLocaleString('en-US')
    : v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

const date = (iso: string) => new Date(iso).toLocaleDateString()

const n = (count: number, singular: string, plural?: string) =>
  `${count} ${count === 1 ? singular : (plural ?? singular + 's')}`

/** Small mono link back to the lens the row belongs to. */
function LensChip({ name }: { name: string }) {
  return (
    <Link
      to={`/lenses/${enc(name)}`}
      className="rounded border border-border bg-surface-2 px-1.5 py-px font-mono text-[11px] text-accent-dark outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
    >
      {name}
    </Link>
  )
}

/** The count line above each list — what the tab covers, in the meta voice. */
function MetaLine({ children }: { children: ReactNode }) {
  return <div className="border-b border-border-strong pb-2 font-mono text-[11px] text-muted">{children}</div>
}

// ─── Certified answers: every question→SQL pair, across lenses ────────────────

function Verified({ value }: { value: VerifiedValue }) {
  if (!value) return <span className="font-mono text-[11px] text-muted-2">unverified</span>
  if ('value' in value) {
    return (
      <span className="font-mono text-[16px] font-semibold tabular-nums text-text">
        {typeof value.value === 'number' ? fmtNumber(value.value) : String(value.value)}
      </span>
    )
  }
  return (
    <span className="font-mono text-[11px] text-muted">
      {value.rows.length}×{value.columns.length} table
    </span>
  )
}

function CertifiedPanel() {
  const lenses = useLenses()
  const names = (lenses.data ?? []).map((l) => l.name)
  const queries = usePerLens<CertifiedAnswer[]>(
    names,
    (l) => ['certified', l],
    (l) => `/mgmt/lenses/${enc(l)}/certified`,
  )

  if (lenses.isLoading || queries.some((q) => q.isLoading)) return <Skeleton className="h-40 w-full" />
  if (lenses.isError) return <p className="text-[13px] text-red">{String(lenses.error)}</p>
  if (lenses.data && names.length === 0) return <NoLenses what="Certified answers" />

  const rows = names
    .flatMap((l, i) => (queries[i].data ?? []).map((a) => ({ lens: l, a })))
    .sort((x, y) => y.a.created_at.localeCompare(x.a.created_at))

  if (rows.length === 0) {
    return (
      <EmptyState
        title="No certified answers yet"
        description="Certify a verified answer from a request, or generate them per lens on its Certified tab — each becomes a question→SQL pair with a known-good value."
      />
    )
  }

  return (
    <div>
      <MetaLine>
        {n(rows.length, 'certified answer')} across {n(names.length, 'lens', 'lenses')}
      </MetaLine>
      <ul className="divide-y divide-border">
        {rows.map(({ lens, a }) => (
          <li key={a.id} className="flex items-start gap-4 py-3">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[13.5px] font-medium leading-snug text-text">{a.question}</span>
                <LensChip name={lens} />
              </div>
              <div className="mt-1 font-mono text-[10.5px] text-muted-2">
                certified by {actorLabel(a.created_by)} · {date(a.created_at)}
                {a.source ? ` · ${a.source}` : ''}
              </div>
              <details className="group mt-1.5">
                <summary className="cursor-pointer list-none font-mono text-[10.5px] text-accent-dark outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent">
                  <span className="group-open:hidden">show SQL</span>
                  <span className="hidden group-open:inline">hide SQL</span>
                </summary>
                <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words border-l-2 border-border-strong bg-surface-2 px-3 py-2 font-mono text-[10.5px] leading-relaxed text-muted">
                  {a.sql}
                </pre>
              </details>
            </div>
            <div className="shrink-0 pt-0.5 text-right">
              <Verified value={a.verified_value} />
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}

// ─── Eval cases: per-lens coverage and the latest score ───────────────────────

const scoreTone = (s: number | null) =>
  s == null ? 'text-muted-2' : s >= 0.9 ? 'text-green' : s >= 0.7 ? 'text-amber' : 'text-red'

function EvalsPanel() {
  const lenses = useLenses()
  const names = (lenses.data ?? []).map((l) => l.name)
  const cases = usePerLens<EvalCase[]>(
    names,
    (l) => ['eval-cases', l, 'all'],
    (l) => `/mgmt/lenses/${enc(l)}/evals/cases`,
  )
  const runs = usePerLens<EvalRun[]>(
    names,
    (l) => ['eval-runs', l],
    (l) => `/mgmt/lenses/${enc(l)}/evals/runs`,
  )

  if (lenses.isLoading || cases.some((q) => q.isLoading) || runs.some((q) => q.isLoading))
    return <Skeleton className="h-40 w-full" />
  if (lenses.isError) return <p className="text-[13px] text-red">{String(lenses.error)}</p>
  if (lenses.data && names.length === 0) return <NoLenses what="Eval cases" />

  const perLens = names.map((l, i) => {
    const all = cases[i].data ?? []
    const latest = runs[i].data?.[0] ?? null
    return {
      lens: l,
      approved: all.filter((c) => c.status === 'approved').length,
      candidates: all.filter((c) => c.status === 'candidate').length,
      latest,
    }
  })
  const approved = perLens.reduce((acc, r) => acc + r.approved, 0)
  const candidates = perLens.reduce((acc, r) => acc + r.candidates, 0)

  return (
    <div>
      <MetaLine>
        {n(approved, 'approved case')} · {n(candidates, 'candidate')} across{' '}
        {n(names.length, 'lens', 'lenses')} — a zero-case lens ships unprotected
      </MetaLine>
      <div className="divide-y divide-border">
        <div className="grid grid-cols-[minmax(0,1fr)_repeat(3,auto)] gap-x-6 bg-surface-2 px-3 py-1.5">
          {['Lens', 'Cases', 'Latest score', 'Last run'].map((h) => (
            <span key={h} className="font-mono text-[9.5px] font-semibold uppercase tracking-[0.12em] text-muted-2">
              {h}
            </span>
          ))}
        </div>
        {perLens.map((r) => (
          <div key={r.lens} className="grid grid-cols-[minmax(0,1fr)_repeat(3,auto)] items-baseline gap-x-6 px-3 py-2.5">
            <LensChip name={r.lens} />
            <span className="font-mono text-[12px] tabular-nums text-text">
              {r.approved} approved
              {r.candidates > 0 && <span className="text-muted"> · {r.candidates} candidate{r.candidates === 1 ? '' : 's'}</span>}
            </span>
            <span className={['font-mono text-[15px] font-semibold tabular-nums', scoreTone(r.latest?.score ?? null)].join(' ')}>
              {r.latest?.score != null ? `${Math.round(r.latest.score * 100)}%` : '—'}
            </span>
            <span className="font-mono text-[11px] tabular-nums text-muted">
              {r.latest
                ? `${r.latest.passed} passed · ${r.latest.failed} failed · ${r.latest.errored} errored`
                : 'never run'}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Deployments: what's live, and the published history behind it ────────────

function DeploymentsPanel() {
  const lenses = useLenses()
  const summaries = lenses.data ?? []
  const names = summaries.map((l) => l.name)
  const versions = usePerLens<LensVersion[]>(
    names,
    (l) => ['versions', l],
    (l) => `/mgmt/lenses/${enc(l)}/versions`,
  )

  if (lenses.isLoading || versions.some((q) => q.isLoading)) return <Skeleton className="h-40 w-full" />
  if (lenses.isError) return <p className="text-[13px] text-red">{String(lenses.error)}</p>
  if (lenses.data && names.length === 0) return <NoLenses what="Deployments" />

  const live = summaries.filter((l) => l.status === 'live').length
  const history = names
    .flatMap((l, i) => (versions[i].data ?? []).map((v) => ({ lens: l, v })))
    .sort((x, y) => y.v.created_at.localeCompare(x.v.created_at))

  return (
    <div>
      <MetaLine>
        {live}/{n(names.length, 'lens', 'lenses')} live · {n(history.length, 'published version')} —
        publishing happens with dst apply
      </MetaLine>

      {/* What each lens is serving right now. */}
      <div className="divide-y divide-border">
        {summaries.map((l, i) => {
          const latest = versions[i].data?.[0] ?? null
          return (
            <div key={l.name} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-3 py-2.5">
              <LensChip name={l.name} />
              <Badge variant={l.status === 'live' ? 'success' : 'default'} dot={l.status === 'live'}>
                {l.status}
              </Badge>
              {latest ? (
                <span className="font-mono text-[12px] tabular-nums text-text">
                  v{latest.version}
                  <span className="text-muted-2"> · published {date(latest.created_at)}</span>
                </span>
              ) : (
                <span className="font-mono text-[12px] text-muted-2">never published</span>
              )}
              {latest?.summary && (
                <span className="min-w-0 flex-1 truncate text-[12px] text-muted">{latest.summary}</span>
              )}
            </div>
          )
        })}
      </div>

      {/* The full publish feed, newest first — the deploy log. */}
      {history.length > 0 && (
        <section className="mt-7">
          <h2 className="font-mono text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-2">
            Published history
          </h2>
          <ul className="mt-2 divide-y divide-border">
            {history.map(({ lens, v }) => (
              <li key={`${lens}-${v.version}`} className="flex items-baseline gap-3 py-2 text-[13px]">
                <span className="w-20 shrink-0 font-mono text-[10.5px] tabular-nums text-muted-2">
                  {date(v.created_at)}
                </span>
                <span className="shrink-0 font-mono text-[12px] font-semibold tabular-nums text-text">
                  {lens} v{v.version}
                </span>
                <span className="min-w-0 flex-1 truncate text-muted">{v.summary}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

// ─── shared empty state ───────────────────────────────────────────────────────

function NoLenses({ what }: { what: string }) {
  return (
    <EmptyState
      title="No lenses yet"
      description={`${what} hang off lenses. Author one in files and land it with dst apply — this page fills in as lenses certify, test, and publish.`}
    />
  )
}

// ─── the page ─────────────────────────────────────────────────────────────────

type CertifyTab = 'certified' | 'evals' | 'deployments' | 'drift'

export function Certify({ initialTab = 'certified' }: { initialTab?: CertifyTab }) {
  const [tab, setTab] = useState<CertifyTab>(initialTab)
  return (
    <Page width="data">
      <PageHeader
        title="Certify"
        description="What dst serves, proven. Certified answers are the regression suite; behavioral pins sit beside them; the drift audit finds what still needs certifying."
      />
      <Tabs value={tab} onValueChange={(v) => setTab(v as CertifyTab)}>
        <TabList>
          <TabTrigger value="certified">Certified answers</TabTrigger>
          <TabTrigger value="evals">Eval cases</TabTrigger>
          <TabTrigger value="deployments">Deployments</TabTrigger>
          <TabTrigger value="drift">Drift audit</TabTrigger>
        </TabList>
        <div className="mt-5">
          <TabPanel value="certified"><CertifiedPanel /></TabPanel>
          <TabPanel value="evals"><EvalsPanel /></TabPanel>
          <TabPanel value="deployments"><DeploymentsPanel /></TabPanel>
          <TabPanel value="drift"><DriftAuditPanel /></TabPanel>
        </div>
      </Tabs>
    </Page>
  )
}
