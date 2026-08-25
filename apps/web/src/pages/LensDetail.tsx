import { useEffect, useState, type ReactNode } from 'react'
import { formatCost, formatSql } from '../lib/format'
import { Answer, Markdown } from '../lib/markdown'
import { actorLabel } from '../lib/outcomes'
import { Link, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  useLensDetail,
  useValidateLens,
  type LensBundleShape,
  type ValidationReport,
} from '../api/lenses'
import {
  useCertified,
  useDeleteCertified,
  useGenerateCertified,
  type CertifiedAnswer,
  type VerifiedValue as CertifiedVerifiedValue,
} from '../api/certify'
import {
  useLensRepo,
  useLensFile,
  useLensVersions,
  useLensDiff,
  type RepoFile,
  type LensVersion,
} from '../api/repo'
import { useDistill, useReviewQueue, type ReviewTicket } from '../api/reviews'
import { ReviewDetail } from './Reviews'
import { API_BASE } from '../api/client'
import {
  useRequests,
  useCallerReport,
  useRequestDetail,
  type RequestSummary,
} from '../api/observe'
import {
  useEvalCases,
  useEvalRuns,
  useEvalRunResults,
  useRunEval,
  useInvalidateEvals,
  type EvalCase,
  type EvalRun,
  type EvalRunCaseResult,
  type EvalCaseSource,
  type EvalCaseStatus,
} from '../api/evals'
import { Button } from '../components/ui/Button'
import { Badge } from '../components/ui/Badge'
import { ConfidenceBadge } from '../components/ui/OutcomeBadges'
import { yamlLines } from '../lib/yaml'
import { Card } from '../components/ui/Card'
import { Tabs, TabList, TabTrigger, TabPanel } from '../components/ui/Tabs'
import { useJoinCandidates, useLensProfile, useLensProfileDrift } from '../api/profile'
import { Skeleton } from '../components/ui/Skeleton'
import { Page, PageHeader } from '../components/ui/Page'

type Tab =
  | 'activity'
  | 'reviews'
  | 'data'
  | 'certified'
  | 'ai'
  | 'access'
  | 'evals'
  | 'repo'

const TAB_LABEL: Record<Tab, string> = {
  activity: 'Activity',
  reviews: 'Reviews',
  data: 'Tables',
  certified: 'Certified definitions',
  ai: 'Answering',
  access: 'Access',
  evals: 'Evaluation',
  repo: 'Files',
}

export function LensDetail() {
  const { name = '' } = useParams()
  const [tab, setTab] = useState<Tab>('activity')
  const detail = useLensDetail(name)
  const validate = useValidateLens(name)
  const [report, setReport] = useState<ValidationReport | null>(null)
  const [connectOpen, setConnectOpen] = useState(false)

  // The compiled bundle to render: prefer the draft, fall back to the published one
  // (a file-applied lens may have no draft at all).
  const bundle = detail.data?.draft ?? detail.data?.published ?? null
  const live = detail.data?.status === 'live'
  const tabs: Tab[] = [
    'activity',
    'reviews',
    'data',
    'certified',
    'ai',
    'access',
    'evals',
    'repo',
  ]

  // Pending-review count for this lens — drives the Reviews tab badge. Shares the
  // ['reviews','all'] query key with ReviewsTab, so react-query dedupes the fetch.
  const reviewQueue = useReviewQueue()
  const pendingReviews = (reviewQueue.data ?? []).filter(
    (t) => t.lens === name && t.state === 'needs_human',
  ).length

  return (
    <Page width="data">
      <PageHeader
        back={{ to: '/', label: 'Lenses' }}
        mono
        title={detail.isLoading ? <Skeleton className="h-5 w-48" /> : name}
        accessory={
          detail.data ? (
            <span className="flex items-center gap-2">
              <Badge
                variant={live ? 'success' : detail.data.status === 'draft' ? 'warning' : 'default'}
                dot
              >
                {live ? 'Live' : detail.data.status}
              </Badge>
            </span>
          ) : undefined
        }
        description={detail.data?.display_name}
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={() => setConnectOpen(true)}>
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path d="M6.4 9.6 9.6 6.4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
                <path
                  d="M7.1 4.7 8.3 3.5a2.55 2.55 0 0 1 3.7 3.7L10.8 8.4"
                  stroke="currentColor"
                  strokeWidth="1.3"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <path
                  d="M8.9 11.3 7.7 12.5a2.55 2.55 0 0 1-3.7-3.7L5.2 7.6"
                  stroke="currentColor"
                  strokeWidth="1.3"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Connect
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => validate.mutate(undefined, { onSuccess: setReport })}
              disabled={validate.isPending}
            >
              {validate.isPending ? (
                <span className="flex items-center gap-1.5">
                  <span className="h-3 w-3 rounded-full border-2 border-border border-t-muted animate-spin" />
                  Validating
                </span>
              ) : (
                'Validate'
              )}
            </Button>
          </>
        }
      />

      {report && <ValidationPanel report={report} />}

      {/* Tab bar */}
      <div className="mt-5">
        <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
          <TabList>
            {tabs.map((t) => (
              <TabTrigger key={t} value={t}>
                {TAB_LABEL[t]}
                {t === 'reviews' && pendingReviews > 0 && (
                  <span className="ml-1.5 inline-flex min-w-[1.1rem] items-center justify-center rounded-full bg-accent px-1 py-px font-mono text-[10px] font-semibold tabular-nums text-bg">
                    {pendingReviews}
                  </span>
                )}
              </TabTrigger>
            ))}
          </TabList>

          <div className="mt-5">
            <TabPanel value="activity"><ActivityTab name={name} live={live} /></TabPanel>
            <TabPanel value="reviews"><ReviewsTab name={name} /></TabPanel>
            <TabPanel value="data"><DataProfileTab name={name} /></TabPanel>
            <TabPanel value="certified">
              <CertifiedDefsTab name={name} />
            </TabPanel>
            <TabPanel value="ai">
              <AnsweringTab name={name} bundle={bundle} />
            </TabPanel>
            <TabPanel value="access">
              <AccessTab name={name} bundle={bundle} />
            </TabPanel>
            <TabPanel value="evals">
              <EvalTab name={name} />
            </TabPanel>
            <TabPanel value="repo">
              <RepoTab name={name} />
            </TabPanel>
          </div>
        </Tabs>
      </div>

      {connectOpen && (
        <ConnectModal
          name={name}
          displayName={detail.data?.display_name}
          onClose={() => setConnectOpen(false)}
        />
      )}
    </Page>
  )
}

// ─── "authored in files" hint ───────────────────────────────────────────────
// The UI governs; files author. Wherever a surface renders state someone would
// want to change, this names the file that owns it and the verb that lands it —
// the same teaching idiom as ConnectionDeclareGuide and MintHint.
function AuthoredIn({
  file,
  field,
  children,
}: {
  file: string
  field?: string
  children?: ReactNode
}) {
  return (
    <div className="rounded-md border border-border bg-surface-2 px-3 py-2.5">
      <h4 className="panel-label">
        Authored in files
      </h4>
      <p className="mt-1 text-[12px] leading-relaxed text-muted">
        {children ? <>{children} </> : null}
        Edit <code className="font-mono text-[11.5px] text-text">{file}</code>
        {field && (
          <>
            {' '}
            (<code className="font-mono text-[11.5px] text-text">{field}</code>)
          </>
        )}{' '}
        and run <code className="font-mono text-[11.5px] text-text">dst apply</code> to land it.
      </p>
    </div>
  )
}

// Nothing compiled for this lens yet — say that rather than rendering defaults
// as though they were in force.
function NotCompiled({ name, field }: { name: string; field: string }) {
  return (
    <div className="space-y-4">
      <p className="text-[13px] text-muted">Nothing compiled for this lens yet.</p>
      <AuthoredIn file={`lenses/${name}/lens.yaml`} field={field} />
    </div>
  )
}

// ─── Data tab: table profiles + join candidates ─────────────────────────────
// Read-only: the lens's table membership is authored in files (dst apply);
// this tab renders the compiled scope — profiles, drift, and inferred joins.
function DataProfileTab({ name }: { name: string }) {
  const profile = useLensProfile(name)
  const candidates = useJoinCandidates(name)
  const drift = useLensProfileDrift(name)
  const tables = profile.data?.tables ?? []
  const pending = (candidates.data ?? []).filter((c) => c.status === 'candidate')
  const driftItems = drift.data?.drift ?? []

  return (
    <div>
      {driftItems.length > 0 && (
        <div className="mb-4 rounded-md border border-accent/40 bg-accent-fg px-4 py-3">
          <p className="text-[12px] font-semibold uppercase tracking-wider text-accent-dark">
            Profile drift
          </p>
          <ul className="mt-1.5 space-y-1">
            {driftItems.map((d, i) => (
              <li key={i} className="text-[13px] text-text">
                <code className="font-mono text-[12px]">{d.table}</code>
                <span className="text-muted"> · {d.kind.replace(/_/g, ' ')} — {d.detail}</span>
              </li>
            ))}
          </ul>
          {(drift.data?.eval_cases_needing_rebaseline.length ?? 0) > 0 && (
            <p className="mt-2 text-[12px] text-muted">
              {drift.data!.eval_cases_needing_rebaseline.length} approved eval case(s) touch the
              drifted tables — re-check them (Evaluation tab).
            </p>
          )}
        </div>
      )}

      {pending.length > 0 && (
        <div className="mb-4">
          <h3 className="text-[13px] font-bold uppercase tracking-wider text-muted">
            Suggested joins
          </h3>
          <p className="mt-1 text-[12px] text-muted">
            Inferred from keys and data overlap — adopt one by authoring the join in the
            lens&apos;s semantic files and running{' '}
            <code className="font-mono text-[11px] text-text">dst apply</code>.
          </p>
          <div className="mt-2 space-y-2">
            {pending.map((c) => (
              <div
                key={c.id}
                className="rounded-md border border-border bg-surface px-3.5 py-2.5"
              >
                <code className="font-mono text-[12px] text-text">
                  {c.left_table}.{c.left_columns.join('+')} = {c.right_table}.{c.right_columns.join('+')}
                </code>
                <span className="ml-2 text-[11px] text-muted">
                  {c.evidence}
                  {c.overlap_ratio != null && ` · overlap ${(c.overlap_ratio * 100).toFixed(0)}%`}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <h3 className="text-[13px] font-bold uppercase tracking-wider text-muted">Table profiles</h3>
      {profile.isLoading && <Skeleton className="mt-3 h-24 w-full" />}
      {!profile.isLoading && tables.length === 0 && (
        <p className="mt-2 text-[13px] text-muted">
          No stored profile yet — profiles are collected when a connection is added or
          refreshed from Data sources.
        </p>
      )}
      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
        {tables.map((t) => (
          <div
            key={t.table}
            className="rounded-lg border border-border bg-surface p-4"
            style={{ boxShadow: 'var(--shadow-card)' }}
          >
            <div className="flex items-center justify-between gap-2">
              <code className="font-mono text-[13px] font-semibold text-text">{t.table}</code>
              <div className="flex items-center gap-1.5">
                {t.partitioning?.column && (
                  <Badge variant="default">partitioned · {t.partitioning.column}</Badge>
                )}
                <Badge variant={t.source === 'catalog' ? 'default' : 'success'}>{t.source}</Badge>
              </div>
            </div>
            <p className="mt-1.5 font-mono text-[11px] text-muted tabular-nums">
              {t.row_count != null ? `${t.row_count.toLocaleString()} rows` : 'rows unknown'}
              {' · '}
              last update{' '}
              {(t.last_updated_logical ?? t.last_updated_physical)?.slice(0, 10) ?? 'unknown'}
            </p>
            <div className="mt-2.5 flex flex-wrap gap-1.5">
              {t.columns.slice(0, 12).map((c) => (
                <span
                  key={c.name}
                  title={[
                    c.type,
                    c.description ?? '',
                    c.top_values ? `values: ${c.top_values.join(', ')}` : '',
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                  className={[
                    'rounded border px-1.5 py-0.5 font-mono text-[11px]',
                    c.top_values
                      ? 'border-accent/30 bg-accent-fg text-accent-dark'
                      : c.description
                        ? 'border-border bg-surface-2 text-text'
                        : 'border-border bg-surface-2 text-muted',
                  ].join(' ')}
                >
                  {c.name}
                </span>
              ))}
              {t.columns.length > 12 && (
                <span className="text-[11px] text-muted self-center">
                  +{t.columns.length - 12} more
                </span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function ValidationPanel({ report }: { report: ValidationReport }) {
  return (
    <div
      className={`mt-4 rounded-lg border px-4 py-3 ${
        report.ok
          ? 'border-green/20 bg-green-bg'
          : 'border-red/20 bg-red-bg'
      }`}
    >
      <div className="flex items-center gap-2">
        {report.ok ? (
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
            <circle cx="7" cy="7" r="6" stroke="var(--color-green)" strokeWidth="1.25" />
            <path d="M4.5 7l2 2 3-3" stroke="var(--color-green)" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ) : (
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
            <circle cx="7" cy="7" r="6" stroke="var(--color-red)" strokeWidth="1.25" />
            <path d="M7 4.5v3M7 9.5v.5" stroke="var(--color-red)" strokeWidth="1.25" strokeLinecap="round" />
          </svg>
        )}
        <span className={`text-[13px] font-medium ${report.ok ? 'text-green' : 'text-red'}`}>
          {report.ok ? 'Validation passed' : 'Validation found errors'}
        </span>
      </div>
      {(report.issues.length > 0 || !report.ok) && (
        <ul className="mt-2 space-y-1 pl-5">
          {report.issues.map((issue, idx) => (
            <li key={idx} className="flex items-start gap-2 text-[13px]">
              <Badge
                variant={issue.severity === 'error' ? 'error' : 'warning'}
                className="mt-0.5 shrink-0"
              >
                {issue.severity}
              </Badge>
              <span className="text-text">{issue.message}</span>
            </li>
          ))}
          {report.issues.length === 0 && (
            <li className="text-[13px] text-muted">No issues found.</li>
          )}
        </ul>
      )}
    </div>
  )
}

// ─── activity (per-lens observe) ────────────────────────────────────────────────
function StatCard({ label, value, tone }: { label: string; value: string; tone?: 'red' | 'amber' }) {
  return (
    <div className="rounded-lg border border-border-strong bg-surface px-3.5 py-2.5" style={{ boxShadow: 'var(--shadow-card)' }}>
      <div className="text-[11px] font-semibold uppercase tracking-wider text-muted">{label}</div>
      <div
        className={[
          'mt-1 font-mono text-[20px] font-bold tabular-nums',
          tone === 'red' ? 'text-red' : tone === 'amber' ? 'text-amber' : 'text-text',
        ].join(' ')}
      >
        {value}
      </div>
    </div>
  )
}

function relTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso).getTime()
  const s = Math.max(0, (Date.now() - d) / 1000)
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function RequestDetailPanel({ id }: { id: string }) {
  const d = useRequestDetail(id)
  if (d.isLoading) return <Skeleton className="h-20 w-full" />
  if (!d.data) return null
  const t = d.data
  return (
    <div className="space-y-2 border-t border-border bg-surface-2 px-3.5 py-3 text-[12px]">
      {t.sql && (
        <pre className="overflow-x-auto rounded border border-border bg-surface px-2.5 py-2 font-mono text-[11px] leading-relaxed text-text">
          {formatSql(t.sql)}
        </pre>
      )}
      {t.answer && <Answer text={t.answer} className="text-text" />}
      {t.error && <p className="text-red">{t.error}</p>}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
        {t.definition_used && <span>definition: <span className="font-mono text-text">{t.definition_used}</span></span>}
        <span>rows: <span className="text-text">{t.row_count ?? '—'}</span></span>
        <span>AI: <span className="text-text">{formatCost(t.ai_cost_usd)}</span></span>
        <span>warehouse: <span className="text-text">{formatCost(t.wh_cost_usd)}</span></span>
      </div>
    </div>
  )
}

function ActivityTab({ name, live }: { name: string; live: boolean }) {
  const [flaggedOnly, setFlaggedOnly] = useState(false)
  const [openId, setOpenId] = useState<string | null>(null)
  const requests = useRequests(name)
  const callers = useCallerReport(name)

  const rows: RequestSummary[] = requests.data ?? []
  const isFlagged = (r: RequestSummary) =>
    r.status !== 'ok' || r.confidence === 'low' || r.confidence === 'unverified'
  const shown = flaggedOnly ? rows.filter(isFlagged) : rows

  const totalQueries = (callers.data ?? []).reduce((a, c) => a + c.queries, 0)
  const totalCost = (callers.data ?? []).reduce((a, c) => a + c.cost_usd, 0)
  const totalErrors = (callers.data ?? []).reduce((a, c) => a + c.errors, 0)
  const totalDeclined = (callers.data ?? []).reduce((a, c) => a + (c.declined ?? 0), 0)
  const flaggedCount = rows.filter(isFlagged).length

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="Requests" value={String(totalQueries)} />
        <StatCard label="Total cost" value={formatCost(totalCost)} />
        <StatCard
          label="Errors / declined"
          value={`${totalErrors} / ${totalDeclined}`}
          tone={totalErrors ? 'red' : undefined}
        />
        <StatCard
          label="Flagged (recent)"
          value={String(flaggedCount)}
          tone={flaggedCount ? 'amber' : undefined}
        />
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-[13px] font-semibold text-text">Recent requests</h3>
          <label className="flex items-center gap-1.5 text-[12px] text-muted cursor-pointer hover:text-text transition-colors select-none" style={{ transitionDuration: 'var(--duration-fast)' }}>
            <input
              type="checkbox"
              checked={flaggedOnly}
              onChange={(e) => setFlaggedOnly(e.target.checked)}
              className="h-3.5 w-3.5 accent-[var(--color-accent)] cursor-pointer"
            />
            Flagged only
          </label>
        </div>

        {requests.isLoading && <Skeleton className="h-32 w-full" />}
        {!requests.isLoading && shown.length === 0 && (
          <div className="rounded-lg border border-dashed border-border bg-surface-2 px-4 py-8 text-center">
            <p className="text-[13px] text-text">
              {flaggedOnly ? 'Nothing flagged.' : 'No requests yet.'}
            </p>
            <p className="mt-1 text-[12px] text-muted">
              {live
                ? 'Calls from agents and apps will appear here with their cost and confidence.'
                : 'Apply the lens and connect a caller — requests, cost, and flagged answers show up here.'}
            </p>
          </div>
        )}

        {shown.length > 0 && (
          <div className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface" style={{ boxShadow: 'var(--shadow-card)' }}>
            {shown.map((r) => (
              <div key={r.request_id}>
                <button
                  type="button"
                  onClick={() => setOpenId(openId === r.request_id ? null : r.request_id)}
                  className="flex w-full items-center gap-3 px-3.5 py-2.5 text-left cursor-pointer transition-colors hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
                >
                  <ConfidenceBadge confidence={r.confidence} status={r.status} />
                  <span className="min-w-0 flex-1 truncate text-[13px] text-text">
                    {r.question || <span className="text-muted">(no question recorded)</span>}
                  </span>
                  <span className="hidden shrink-0 font-mono text-[11px] text-muted sm:inline">
                    {r.caller}
                  </span>
                  <span className="shrink-0 font-mono text-[12px] text-text">{formatCost(r.cost_usd)}</span>
                  <span className="shrink-0 text-[11px] text-muted">{relTime(r.created_at)}</span>
                </button>
                {openId === r.request_id && <RequestDetailPanel id={r.request_id} />}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

// ─── reviews (per-lens slice of observe→reviews, ruled inline) ──────────────────
function ReviewsTab({ name }: { name: string }) {
  const [openId, setOpenId] = useState<string | null>(null)
  const [needsOnly, setNeedsOnly] = useState(true)
  const queue = useReviewQueue()
  const requests = useRequests(name)
  // Join to recent requests for the question text + age (the ticket carries neither).
  const byRequest = new Map((requests.data ?? []).map((r) => [r.request_id, r]))

  const mine = (queue.data ?? []).filter((t: ReviewTicket) => t.lens === name)
  const shown = needsOnly ? mine.filter((t) => t.state === 'needs_human') : mine

  // Inline ruling — the full review loop without leaving the lens.
  if (openId) return <ReviewDetail ticketId={openId} onBack={() => setOpenId(null)} />

  const stateVariant = (state: string): 'success' | 'error' | 'warning' | 'default' =>
    state === 'approved'
      ? 'success'
      : state === 'rejected'
        ? 'error'
        : state === 'needs_human'
          ? 'warning'
          : 'default'

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-[13px] font-semibold text-text">Reviews for this lens</h3>
        <label
          className="flex cursor-pointer select-none items-center gap-1.5 text-[12px] text-muted transition-colors hover:text-text"
          style={{ transitionDuration: 'var(--duration-fast)' }}
        >
          <input
            type="checkbox"
            checked={needsOnly}
            onChange={(e) => setNeedsOnly(e.target.checked)}
            className="h-3.5 w-3.5 cursor-pointer accent-[var(--color-accent)]"
          />
          Needs review only
        </label>
      </div>

      {queue.isLoading && <Skeleton className="h-32 w-full" />}
      {queue.isError && (
        <p className="rounded-md border border-red/20 bg-red-bg px-3 py-2 text-[13px] text-red">
          {(queue.error as Error).message}
        </p>
      )}

      {!queue.isLoading && shown.length === 0 && (
        <div className="rounded-lg border border-dashed border-border bg-surface-2 px-4 py-8 text-center">
          <p className="text-[13px] text-text">
            {needsOnly ? 'Nothing to review.' : 'No reviews for this lens.'}
          </p>
          <p className="mt-1 text-[12px] text-muted">
            Answers that fail verification — or that a caller flags via the API or MCP — land here
            for ruling.
          </p>
        </div>
      )}

      {shown.length > 0 && (
        <div
          className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface"
          style={{ boxShadow: 'var(--shadow-card)' }}
        >
          {shown.map((t) => {
            const req = byRequest.get(t.request_id)
            return (
              <button
                key={t.ticket_id}
                type="button"
                onClick={() => setOpenId(t.ticket_id)}
                className="flex w-full items-center gap-3 px-3.5 py-2.5 text-left cursor-pointer transition-colors hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
              >
                <Badge variant={stateVariant(t.state)}>{t.state.replace(/_/g, ' ')}</Badge>
                <span className="min-w-0 flex-1 truncate text-[13px] text-text">
                  {req?.question || <span className="text-muted">(no question recorded)</span>}
                </span>
                <span className="hidden shrink-0 font-mono text-[11px] text-muted sm:inline">
                  {t.caller}
                </span>
                <span className="shrink-0 text-[11px] text-muted">
                  {relTime(req?.created_at ?? null)}
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ─── collapsible tree branch — one first-class citizen of the lens ───────────
function TreeBranch({
  label,
  count,
  subtitle,
  defaultOpen = true,
  action,
  children,
}: {
  label: string
  count?: number
  subtitle?: string
  defaultOpen?: boolean
  action?: ReactNode
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="rounded-lg border border-border bg-surface overflow-hidden" style={{ boxShadow: 'var(--shadow-card)' }}>
      <div className="flex items-center justify-between gap-3 px-3.5 py-2.5">
        <button onClick={() => setOpen((o) => !o)} className="flex min-w-0 items-center gap-2 text-left cursor-pointer hover:text-accent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded" style={{ transitionDuration: 'var(--duration-fast)' }}>
          <svg
            width="12"
            height="12"
            viewBox="0 0 12 12"
            fill="none"
            aria-hidden="true"
            className={`shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`}
            style={{ transitionDuration: 'var(--duration-fast)' }}
          >
            <path d="M4 2.5L7.5 6L4 9.5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="text-[13px] font-bold text-text">{label}</span>
          {count !== undefined && <Badge variant="default">{count}</Badge>}
          {subtitle && <span className="truncate text-[11px] text-muted">{subtitle}</span>}
        </button>
        {action && <div className="shrink-0">{action}</div>}
      </div>
      {open && <div className="border-t border-border bg-bg px-3.5 py-3">{children}</div>}
    </div>
  )
}

// ─── the lens as a versioned repository — files + diffable history ───────────
// Render one materialized file by extension: markdown as formatted markdown, SQL
// pretty-printed in mono, everything else (semantic model JSON/YAML) as mono.
function FileContent({ path, content }: { path: string; content: string }) {
  const ext = path.toLowerCase().split('.').pop() ?? ''
  if (ext === 'md' || ext === 'markdown') {
    return <Markdown text={content} />
  }
  const body =
    ext === 'sql' ? formatSql(content)
    : ext === 'yaml' || ext === 'yml' ? yamlLines(content)
    : content
  return (
    <pre className="overflow-x-auto whitespace-pre rounded-md border border-border bg-surface px-3 py-2.5 font-mono text-[11px] leading-relaxed text-text">
      {body}
    </pre>
  )
}

function RepoTab({ name }: { name: string }) {
  const repo = useLensRepo(name)
  const versions = useLensVersions(name)
  const [selected, setSelected] = useState<string | null>(null)
  const file = useLensFile(name, selected)

  const files = repo.data?.files ?? []
  // Default to the first file once the tree loads, so the viewer is never blank.
  const active = selected ?? files[0]?.path ?? null
  const fileData = active === selected ? file.data : undefined

  const groups = new Map<string, RepoFile[]>()
  for (const f of files) {
    const folder = f.path.includes('/') ? f.path.split('/')[0] : '/'
    if (!groups.has(folder)) groups.set(folder, [])
    groups.get(folder)!.push(f)
  }

  return (
    <div className="space-y-3">
      <p className="text-[13px] text-muted">
        This lens as a versioned repository — its model, definitions, evals and audit
        materialized as files. Every apply that changes it is an immutable version you can diff.
      </p>

      {repo.isLoading && <Skeleton className="h-64 w-full" />}
      {!repo.isLoading && files.length === 0 && (
        <p className="rounded-lg border border-dashed border-border bg-surface-2 px-4 py-8 text-center text-[13px] text-muted">
          No files yet — run <code className="font-mono text-[12px] text-text">dst apply</code>{' '}
          to materialize this lens.
        </p>
      )}

      {files.length > 0 && (
        <div
          className="grid grid-cols-1 overflow-hidden rounded-lg border border-border bg-surface md:grid-cols-[minmax(0,15rem)_1fr]"
          style={{ boxShadow: 'var(--shadow-card)' }}
        >
          {/* File tree */}
          <div className="border-b border-border bg-surface-2 p-2 md:border-b-0 md:border-r">
            <div className="space-y-3">
              {[...groups.entries()].map(([folder, fs]) => (
                <div key={folder}>
                  {folder !== '/' && (
                    <p className="mb-1 px-1 font-mono text-[11px] uppercase tracking-wider text-muted-2">
                      {folder}/
                    </p>
                  )}
                  <div className="space-y-0.5">
                    {fs.map((f) => (
                      <button
                        key={f.path}
                        onClick={() => setSelected(f.path)}
                        className={`flex w-full items-center justify-between gap-2 rounded px-2 py-1 text-left transition-colors hover:bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent ${
                          active === f.path ? 'bg-surface font-medium text-accent' : 'text-text'
                        }`}
                        style={{ transitionDuration: 'var(--duration-fast)' }}
                      >
                        <span className="truncate font-mono text-[12px]">
                          {folder === '/' ? f.path : f.path.split('/').slice(1).join('/')}
                        </span>
                        <span className="shrink-0 font-mono text-[10px] text-muted tabular-nums">
                          {f.size} B
                        </span>
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Rendered file */}
          <div className="min-w-0 bg-bg">
            <div className="flex items-center justify-between gap-2 border-b border-border px-3.5 py-2">
              <span className="truncate font-mono text-[12px] text-text">{active}</span>
            </div>
            <div className="p-3.5">
              {active === selected && file.isLoading && <Skeleton className="h-24 w-full" />}
              {fileData && <FileContent path={fileData.path} content={fileData.content} />}
              {active !== selected && (
                <p className="text-[12px] text-muted">Select a file to view it.</p>
              )}
            </div>
          </div>
        </div>
      )}

      <HistoryBranch name={name} versions={versions.data ?? []} loading={versions.isLoading} />
    </div>
  )
}

function HistoryBranch({
  name,
  versions,
  loading,
}: {
  name: string
  versions: LensVersion[]
  loading: boolean
}) {
  // Default the diff to "previous → newest"; explicit selection overrides.
  const [fromSel, setFrom] = useState<number | null>(null)
  const [toSel, setTo] = useState<number | null>(null)
  const from = fromSel ?? versions[1]?.version ?? null
  const to = toSel ?? versions[0]?.version ?? null
  const diff = useLensDiff(name, from, to)

  return (
    <TreeBranch
      label="History"
      count={versions.length}
      subtitle="every applied change, diffable"
      defaultOpen={false}
    >
      {loading && <Skeleton className="h-16 w-full" />}
      {!loading && versions.length === 0 && (
        <p className="text-[12px] text-muted">No published versions yet.</p>
      )}
      <ul className="space-y-1">
        {versions.map((v) => (
          <li key={v.version} className="flex items-center gap-2 text-[12px]">
            <Badge variant="default">v{v.version}</Badge>
            <span className="font-mono text-[11px] text-muted tabular-nums">
              {new Date(v.created_at).toLocaleString()}
            </span>
            {v.created_by && (
              <span className="font-mono text-[11px] text-muted">
                by {actorLabel(v.created_by)}
              </span>
            )}
            {v.summary && (
              <span className="truncate text-muted">
                {/* The stored string is a provenance marker the server matches
                    exactly (lenses_from_files) — translate it for humans here,
                    never rewrite it in the DB. */}
                {v.summary === 'apply (files won)' ? 'applied from files' : v.summary}
              </span>
            )}
          </li>
        ))}
      </ul>

      {versions.length >= 2 && (
        <div className="mt-3 border-t border-border pt-3">
          <div className="flex items-center gap-2 text-[12px] text-muted">
            <span>diff</span>
            <VersionSelect value={from} versions={versions} onChange={setFrom} />
            <span>→</span>
            <VersionSelect value={to} versions={versions} onChange={setTo} />
          </div>
          {diff.isLoading && <Skeleton className="mt-2 h-20 w-full" />}
          {diff.data && diff.data.files.length === 0 && (
            <p className="mt-2 text-[12px] text-muted">No differences.</p>
          )}
          {diff.data?.files.map((f) => (
            <div key={f.path} className="mt-2">
              <p className="font-mono text-[11px] text-muted-2">{f.path}</p>
              <pre className="overflow-x-auto rounded border border-border bg-surface px-2.5 py-2 font-mono text-[11px] leading-relaxed">
                {f.diff.split('\n').map((line, i) => (
                  <div
                    key={i}
                    className={
                      line.startsWith('+') && !line.startsWith('+++')
                        ? 'text-green'
                        : line.startsWith('-') && !line.startsWith('---')
                          ? 'text-red'
                          : 'text-muted'
                    }
                  >
                    {line || ' '}
                  </div>
                ))}
              </pre>
            </div>
          ))}
        </div>
      )}
    </TreeBranch>
  )
}

function VersionSelect({
  value,
  versions,
  onChange,
}: {
  value: number | null
  versions: LensVersion[]
  onChange: (v: number) => void
}) {
  return (
    <select
      value={value ?? ''}
      onChange={(e) => onChange(Number(e.target.value))}
      className="rounded border border-border bg-surface px-2 py-1 font-mono text-[11px] text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      {versions.map((v) => (
        <option key={v.version} value={v.version}>
          v{v.version}
        </option>
      ))}
    </select>
  )
}

// ─── Certified definitions tab: generated question→SQL pairs, each with a verified value ──
// Generate from the lens's governed definitions (LLM + warehouse round-trip per
// definition), then list each as a card: question, formatted SQL, verified value.
function VerifiedValueView({ value }: { value: CertifiedVerifiedValue }) {
  if (value == null) return null
  // Scalar: a headline figure. Table: a small grid (columns + rows).
  if ('value' in value) {
    return (
      <div className="mt-3 rounded-md border border-accent/40 bg-accent-fg px-3 py-2">
        <div className="text-[10px] font-semibold uppercase tracking-wider text-accent-dark">
          Verified value
        </div>
        <div className="mt-0.5 font-mono text-[18px] font-bold tabular-nums text-text">
          {String(value.value)}
        </div>
      </div>
    )
  }
  return (
    <div className="mt-3 rounded-md border border-accent/40 bg-accent-fg px-3 py-2">
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-accent-dark">
        Verified value
      </div>
      <div className="overflow-x-auto rounded border border-border bg-surface">
        <table className="w-full border-collapse text-[12px]">
          <thead>
            <tr className="bg-surface-2">
              {value.columns.map((c, i) => (
                <th
                  key={i}
                  className="border-b border-border px-3 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-muted"
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {value.rows.map((r, ri) => (
              <tr key={ri} className={ri % 2 === 1 ? 'bg-surface-2/50' : 'bg-surface'}>
                {r.map((cell, ci) => (
                  <td key={ci} className="border-b border-border px-3 py-1.5 font-mono text-[11px] text-text">
                    {String(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function CertifiedAnswerCard({
  answer,
  onDelete,
  deleting,
}: {
  answer: CertifiedAnswer
  onDelete: () => void
  deleting: boolean
}) {
  return (
    <div className="rounded-lg border border-border bg-surface p-4" style={{ boxShadow: 'var(--shadow-card)' }}>
      <div className="flex items-start justify-between gap-3">
        <span className="text-[13px] font-medium text-text">{answer.question}</span>
        <button
          onClick={onDelete}
          disabled={deleting}
          className="shrink-0 text-[11px] text-muted transition-colors hover:text-red disabled:opacity-50"
          style={{ transitionDuration: 'var(--duration-fast)' }}
        >
          {deleting ? 'Removing…' : 'Remove'}
        </button>
      </div>
      <pre className="mt-2 overflow-x-auto rounded-md border border-border bg-bg px-3 py-2 font-mono text-[11px] leading-relaxed text-text">
        {formatSql(answer.sql)}
      </pre>
      <VerifiedValueView value={answer.verified_value} />
    </div>
  )
}

function CertifiedDefsTab({ name }: { name: string }) {
  const certified = useCertified(name)
  const generate = useGenerateCertified(name)
  const del = useDeleteCertified(name)
  const qc = useQueryClient()
  const refresh = () => qc.invalidateQueries({ queryKey: ['certified', name] })

  const answers = certified.data ?? []
  const failed = (generate.data?.results ?? []).filter((r) => r.error)

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-3">
        <p className="max-w-prose text-[13px] text-muted">
          Certified question→SQL pairs, each run once for a{' '}
          <span className="text-text">verified value</span> the lens is served from and graded
          against. Generate drafts one per governed definition — an AI + warehouse round-trip each,
          so it can take a while.
        </p>
        <Button
          variant="primary"
          size="sm"
          onClick={() => generate.mutate(undefined, { onSuccess: refresh })}
          disabled={generate.isPending}
          className="shrink-0"
        >
          {generate.isPending ? (
            <span className="flex items-center gap-1.5">
              <span className="h-3 w-3 rounded-full border-2 border-bg/30 border-t-bg animate-spin" />
              Generating…
            </span>
          ) : (
            'Generate certified definitions'
          )}
        </Button>
      </div>

      {generate.isPending && (
        <div className="rounded-md border border-accent/30 bg-accent-fg/40 px-3 py-2.5 text-[12px] text-accent-dark">
          Drafting a question + SQL per definition and running each read-only for a verified value.
          This runs an AI and warehouse round-trip per definition — leave the tab open.
        </div>
      )}
      {generate.isError && (
        <p className="rounded-md border border-red/20 bg-red-bg px-3 py-2 text-[13px] text-red">
          {String(generate.error)}
        </p>
      )}
      {generate.data && (
        <p className="text-[12px] text-muted">
          Generated {generate.data.generated} of {generate.data.results.length}.
          {failed.length > 0 && (
            <span className="text-amber"> {failed.length} couldn&apos;t be verified.</span>
          )}
        </p>
      )}
      {failed.length > 0 && (
        <div className="space-y-1.5">
          {failed.map((r) => (
            <div
              key={r.term}
              className="rounded border border-amber-strong/70 bg-amber-bg px-2.5 py-1.5 text-[12px]"
            >
              <span className="font-mono font-medium text-text">{r.term}</span>
              <span className="text-muted"> — {r.error}</span>
            </div>
          ))}
        </div>
      )}

      {certified.isLoading && <Skeleton className="h-40 w-full" />}

      {!certified.isLoading && answers.length === 0 && (
        <div className="rounded-lg border border-dashed border-border bg-surface-2 px-4 py-8 text-center">
          <p className="text-[13px] text-text">No certified definitions yet.</p>
          <p className="mt-1 text-[12px] text-muted">
            Generate them from the lens&apos;s definitions above, or certify an answer from a request.
          </p>
        </div>
      )}

      {answers.length > 0 && (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          {answers.map((a) => (
            <CertifiedAnswerCard
              key={a.id}
              answer={a}
              deleting={del.isPending}
              onDelete={() => del.mutate(a.id, { onSuccess: refresh })}
            />
          ))}
        </div>
      )}

      {certified.isError && <p className="text-[13px] text-red">{String(certified.error)}</p>}
    </div>
  )
}

type AnswerMode = 'strict' | 'balanced' | 'exploratory'

const ANSWER_MODES: { id: AnswerMode; label: string; blurb: string }[] = [
  {
    id: 'strict',
    label: 'Strict',
    blurb: 'Answers only when the data backs it cleanly — declines or flags more readily. Best for governed, high-stakes reporting.',
  },
  {
    id: 'balanced',
    label: 'Balanced',
    blurb: 'The default. Answers grounded questions and flags the uncertain ones — a sensible middle for most lenses.',
  },
  {
    id: 'exploratory',
    label: 'Exploratory',
    blurb: 'More willing to attempt an answer and surface partial results. Best for discovery, where a lead beats a refusal.',
  },
]

// The Answering tab — read-only. Answer mode and the model are lens
// config (lens.yaml `model:`): authored in files, landed by apply. The UI renders
// which mode is in force and teaches the file path; it never sets it.
function AnsweringTab({ name, bundle }: { name: string; bundle: LensBundleShape | null }) {
  if (!bundle) return <NotCompiled name={name} field="model:" />
  const model = bundle.config.model
  const current: AnswerMode = model.answer_mode ?? 'balanced'

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-[13px] font-semibold text-text">Answer mode</h3>
        <p className="mt-0.5 text-[13px] text-muted">
          How willing this lens is to answer — the one judgment call its owner declares. dst
          owns the thresholds; steering prose lives in the lens&apos;s instructions.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {ANSWER_MODES.map((m) => {
          const active = m.id === current
          return (
            <div
              key={m.id}
              aria-current={active ? 'true' : undefined}
              className={[
                'flex flex-col gap-1.5 rounded-lg border px-4 py-3',
                active ? 'border-accent bg-accent-fg' : 'border-border bg-surface opacity-70',
              ].join(' ')}
            >
              <span className="flex items-center gap-2">
                <span
                  className={[
                    'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full border',
                    active ? 'border-accent bg-accent' : 'border-border-strong',
                  ].join(' ')}
                  aria-hidden="true"
                >
                  {active && <span className="h-1.5 w-1.5 rounded-full bg-bg" />}
                </span>
                <span
                  className={[
                    'text-[13px] font-semibold',
                    active ? 'text-accent-dark' : 'text-text',
                  ].join(' ')}
                >
                  {m.label}
                </span>
                {active && (
                  <span className="font-mono text-[10px] font-semibold uppercase tracking-wider text-accent-dark">
                    in force
                  </span>
                )}
                {m.id === 'balanced' && !active && (
                  <span className="text-[10px] font-medium uppercase tracking-wider text-muted-2">
                    default
                  </span>
                )}
              </span>
              <span className="text-[12px] leading-relaxed text-muted">{m.blurb}</span>
            </div>
          )
        })}
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-surface">
        <div className="border-b border-border bg-surface-2 px-3.5 py-2">
          <h4 className="panel-label">
            Compiled model config
          </h4>
        </div>
        <dl className="divide-y divide-border">
          {[
            // Unset is the default and the good case: the lens follows this
            // install's own smart tier instead of pinning a vendor. Say that,
            // not a blank cell.
            ['provider', model.provider ?? "this install's tier"],
            ['model', model.model ?? "this install's tier"],
            ['answer_mode', current],
            ['max_rows_to_compose', String(model.max_rows_to_compose)],
          ].map(([k, v]) => (
            <div key={k} className="flex items-baseline justify-between gap-4 px-3.5 py-2">
              <dt className="font-mono text-[11px] text-muted">{k}</dt>
              <dd className="truncate font-mono text-[12px] text-text">{v}</dd>
            </div>
          ))}
        </dl>
      </div>

      <AuthoredIn file={`lenses/${name}/lens.yaml`} field="model:" />
    </div>
  )
}

// Access, read-only: the allow-list is deny-by-default policy declared
// in lens.yaml. This renders the compiled rules exactly as the file states them —
// granting or revoking access is a file edit plus apply.
function AccessTab({ name, bundle }: { name: string; bundle: LensBundleShape | null }) {
  if (!bundle) return <NotCompiled name={name} field="access.allow" />
  const rules = bundle.config.access.allow ?? []

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-[13px] font-semibold text-text">Allow-list</h3>
        <p className="mt-0.5 text-[13px] text-muted">
          Deny by default — a caller reaches this lens only through a matching rule.
        </p>
      </div>

      {rules.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border bg-surface-2 px-4 py-8 text-center text-[13px] text-muted">
          No rules — nobody but an admin token may query this lens.
        </p>
      ) : (
        <div
          className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface"
          style={{ boxShadow: 'var(--shadow-card)' }}
        >
          {rules.map((r, i) => (
            <div key={i} className="flex items-center gap-3 px-3.5 py-2.5">
              <Badge variant="default">{r.group ? 'group' : 'caller'}</Badge>
              <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-text">
                {r.group ?? r.caller ?? '—'}
              </span>
            </div>
          ))}
        </div>
      )}

      <AuthoredIn file={`lenses/${name}/lens.yaml`} field="access.allow" />
    </div>
  )
}

// ─── Connect modal ────────────────────────────────────────────────────────────
// One governed pipeline, three doors. Each persona reaches the same lens; only the
// connection shape differs. Humans + agents go through the remote MCP server; apps
// point an OpenAI client at the OpenAI-compatible adapter (/v1/chat/completions).

type Persona = 'human' | 'agent' | 'app'

function HumanIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="8" cy="5" r="2.6" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M3 13.4c0-2.5 2.2-4.1 5-4.1s5 1.6 5 4.1"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  )
}

function AgentIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <rect x="3.2" y="4.6" width="9.6" height="7.4" rx="2" stroke="currentColor" strokeWidth="1.3" />
      <path d="M8 2.3v2.3" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
      <circle cx="6.2" cy="8.3" r="0.95" fill="currentColor" />
      <circle cx="9.8" cy="8.3" r="0.95" fill="currentColor" />
    </svg>
  )
}

function AppIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M6 5.4 3.3 8 6 10.6"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M10 5.4 12.7 8 10 10.6"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

const PERSONAS: { id: Persona; label: string; door: string; icon: () => ReactNode }[] = [
  { id: 'human', label: 'Human', door: 'MCP', icon: HumanIcon },
  { id: 'agent', label: 'Agent', door: 'MCP', icon: AgentIcon },
  { id: 'app', label: 'App', door: 'OpenAI API', icon: AppIcon },
]

// The human's MCP door, as a conversational prompt: an "add this MCP server"
// instruction plus the config, to paste straight into an AI that can manage its own
// MCP servers (e.g. Claude Code) — no config-file editing.
function buildHumanPrompt(base: string, name: string, displayName: string | undefined): string {
  const title = displayName || name
  return [
    `Add this as an MCP server, then use its dst tools to answer my questions about "${title}":`,
    '',
    '{',
    '  "mcpServers": {',
    '    "dst": {',
    `      "url": "${base}/mcp"`,
    '    }',
    '  }',
    '}',
    '',
    "On first use it'll open a browser to sign in — no API key to paste.",
  ].join('\n')
}

// The service doors (agent/app) need a scoped dst_ key. Minting is CLI-only
// (`dst keys create <caller>` — shown once there); the snippets carry a
// placeholder to swap in.
const KEY_PLACEHOLDER = 'dst_YOUR_KEY'

function MintHint() {
  return (
    <p className="rounded-md border border-border bg-surface-2 px-3 py-2.5 text-[12px] leading-relaxed text-muted">
      Mint a scoped key from the CLI —{' '}
      <code className="font-mono text-[11px] text-text">
        dst keys create --caller &lt;name&gt;
      </code>{' '}
      (it&apos;s shown once) — and swap it in for{' '}
      <span className="font-mono text-text">{KEY_PLACEHOLDER}</span> below. The key inherits that
      caller&apos;s lens access.
    </p>
  )
}

function ConnectModal({
  name,
  displayName,
  onClose,
}: {
  name: string
  displayName?: string
  onClose: () => void
}) {
  // Same base the app uses for every API call (incl. ${API_BASE}/mcp). Local dev →
  // http://localhost:8000; prod's empty same-origin base falls back to the page origin,
  // so the copied snippets always point at a reachable host.
  const base = API_BASE || window.location.origin
  const [persona, setPersona] = useState<Persona>('human')
  const [humanMode, setHumanMode] = useState<'connector' | 'prompt'>('connector')

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Human MCP door: OAuth, no key in the config (the client runs the browser flow).
  const mcpConfig = `{
  "mcpServers": {
    "dst": {
      "url": "${base}/mcp"
    }
  }
}`
  const agentCli = `# Register the governed MCP server with any MCP-capable agent.
claude mcp add --transport http dst ${base}/mcp \\
  --header "Authorization: Bearer ${KEY_PLACEHOLDER}"`
  const appPy = `from openai import OpenAI

client = OpenAI(base_url="${base}/v1", api_key="${KEY_PLACEHOLDER}")

resp = client.chat.completions.create(
    model="dst/${name}",
    messages=[{"role": "user", "content": "your question"}],
)
print(resp.choices[0].message.content)`
  const restCurl = `curl -X POST ${base}/v1/lenses/${name}/query \\
  -H "Authorization: Bearer ${KEY_PLACEHOLDER}" \\
  -H "Content-Type: application/json" \\
  -d '{"q": "your question"}'`
  const humanPrompt = buildHumanPrompt(base, name, displayName)

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-auto bg-black/30 p-6 pt-[10vh]"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <Card elevated className="w-full max-w-2xl" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-start justify-between gap-4 px-5 pt-5">
          <div>
            <h3 className="text-[15px] font-bold leading-snug text-text">Connect to this lens</h3>
            <p className="mt-0.5 text-[12px] leading-relaxed text-muted">
              One governed pipeline, three doors — every caller gets a grounded, cited answer for{' '}
              <span className="font-mono text-text">{name}</span>.
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="-mr-1 -mt-1 inline-flex h-7 w-7 shrink-0 cursor-pointer items-center justify-center rounded-md text-muted hover:bg-surface-2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
            </svg>
          </button>
        </div>

        {/* Persona switcher */}
        <div className="grid grid-cols-3 gap-2 px-5 pt-4">
          {PERSONAS.map((p) => {
            const active = p.id === persona
            const Icon = p.icon
            return (
              <button
                key={p.id}
                onClick={() => setPersona(p.id)}
                className={[
                  'flex flex-col items-start gap-1 rounded-md border px-3 py-2.5 text-left',
                  'transition-all cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                  active
                    ? 'border-accent bg-accent/10'
                    : 'border-border bg-surface hover:border-border-strong hover:bg-surface-2',
                ].join(' ')}
                style={{ transitionDuration: 'var(--duration-fast)' }}
              >
                <span className="flex items-center gap-1.5">
                  <span className={active ? 'text-accent-dark' : 'text-muted'}>
                    <Icon />
                  </span>
                  <span className="text-[13px] font-semibold text-text">{p.label}</span>
                </span>
                <span className={['text-[11px]', active ? 'text-accent-dark' : 'text-muted'].join(' ')}>
                  via {p.door}
                </span>
              </button>
            )
          })}
        </div>

        {/* Persona panel */}
        <div className="space-y-3 px-5 py-4">
          {persona === 'human' && (
            <>
              <div className="flex gap-1.5">
                {(
                  [
                    ['connector', 'Add connector'],
                    ['prompt', 'Copy a prompt'],
                  ] as const
                ).map(([m, label]) => (
                  <button
                    key={m}
                    onClick={() => setHumanMode(m)}
                    className={[
                      'rounded-md border px-2.5 py-1 text-[11px] font-medium cursor-pointer transition-all',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                      humanMode === m
                        ? 'border-accent bg-accent/10 text-accent-dark'
                        : 'border-border bg-surface text-muted hover:border-border-strong hover:text-text',
                    ].join(' ')}
                    style={{ transitionDuration: 'var(--duration-fast)' }}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {humanMode === 'connector' ? (
                <>
                  <p className="text-[12px] leading-relaxed text-muted">
                    Add dst as a connector in{' '}
                    <strong className="font-semibold text-text">Claude Desktop</strong>,{' '}
                    <strong className="font-semibold text-text">Cursor</strong>, or{' '}
                    <strong className="font-semibold text-text">ChatGPT</strong>, then ask this lens
                    in plain language. Just the URL — on first use the client opens a browser to
                    sign you in, and lenses appear scoped to <em>your</em> access. No key to paste.
                  </p>
                  <Snippet
                    title="Claude Desktop / Cursor — connector config"
                    language="json"
                    body={mcpConfig}
                  />
                </>
              ) : (
                <>
                  <p className="text-[12px] leading-relaxed text-muted">
                    Rather than editing a config file, paste this into an AI that can add MCP
                    servers (e.g. <strong className="font-semibold text-text">Claude Code</strong>) —
                    it registers dst and answers from this lens. It signs you in via the browser
                    on first use; no key needed.
                  </p>
                  <Snippet title="Prompt — paste into an AI" language="prompt" body={humanPrompt} />
                </>
              )}
            </>
          )}
          {persona === 'agent' && (
            <>
              <p className="text-[12px] leading-relaxed text-muted">
                Register the same governed MCP server in your agent. It discovers the tools (
                <span className="font-mono text-text">list_lenses → describe_lens → query</span>) and
                decides when to call them. Works with the Claude Agent SDK, LangGraph, or any MCP
                client. Headless agents use a scoped key (no browser flow):
              </p>
              <MintHint />
              <Snippet title="Claude Agent SDK / any MCP client" language="bash" body={agentCli} />
            </>
          )}
          {persona === 'app' && (
            <>
              <p className="text-[12px] leading-relaxed text-muted">
                An app connects to exactly this lens — the{' '}
                <span className="font-mono text-text">model</span> field (
                <span className="font-mono text-text">dst/{name}</span>) binds it. Point an
                OpenAI client at dst and you get a governed, cited completion — no new SDK, no
                tool-call loop. Structured fields (sql, rows, citations) ride under a{' '}
                <span className="font-mono text-text">dst</span> key.
              </p>
              <MintHint />
              <Snippet title="Python — OpenAI SDK" language="python" body={appPy} />
              <Snippet title="or raw REST" language="bash" body={restCurl} />
            </>
          )}
        </div>

        {/* Footer — the shared prerequisite for every door */}
        <div className="flex items-start gap-2 border-t border-border bg-surface-2 px-5 py-3 text-[12px] leading-relaxed text-muted">
          <svg
            width="14"
            height="14"
            viewBox="0 0 16 16"
            fill="none"
            aria-hidden="true"
            className="mt-px shrink-0 text-muted"
          >
            <circle cx="6" cy="10" r="2.5" stroke="currentColor" strokeWidth="1.3" />
            <path d="M7.8 8.2 12.5 3.5M10.5 5.5l1.6 1.6M11.5 4.5l1.4 1.4" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span>
            Humans sign in via the browser (OAuth) — no key. Service callers use a scoped{' '}
            <span className="font-mono text-text">dst_…</span> key, minted from the CLI (
            <span className="font-mono text-text">dst keys create</span>) and revocable in{' '}
            <Link
              to="/settings"
              onClick={onClose}
              className="font-medium text-accent-dark underline-offset-2 hover:underline"
            >
              Settings
            </Link>
            . Either way, the identity must be on this lens's{' '}
            <span className="font-mono text-text">Access</span> allow-list — callers see only the
            lenses they're permitted.
          </span>
        </div>
      </Card>
    </div>
  )
}

// Minimal, language-agnostic coloring so the snippet boxes read as code (not bright body
// text): strings brightened to paper-white, comments muted, over a warm-ink ground —
// monochrome, no accent hue. Colors are inline because the paper theme has no
// dark-code token.
const CODE_STRING_RE = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g

function renderCodeLine(line: string): ReactNode {
  const trimmed = line.trimStart()
  if (trimmed.startsWith('#') || trimmed.startsWith('//')) {
    return <span style={{ color: '#8a8580' }}>{line}</span>
  }
  const out: ReactNode[] = []
  let last = 0
  let i = 0
  let m: RegExpExecArray | null
  CODE_STRING_RE.lastIndex = 0
  while ((m = CODE_STRING_RE.exec(line)) !== null) {
    if (m.index > last) out.push(<span key={i++}>{line.slice(last, m.index)}</span>)
    out.push(
      <span key={i++} style={{ color: '#fdfdfb' }}>
        {m[0]}
      </span>,
    )
    last = m.index + m[0].length
  }
  if (last < line.length) out.push(<span key={i}>{line.slice(last)}</span>)
  return out
}

function CodeBody({ body }: { body: string }) {
  const lines = body.split('\n')
  return (
    <pre
      className="overflow-x-auto px-4 py-3 font-mono text-[12px] leading-relaxed"
      style={{ background: '#201e1b', color: '#e7e4de' }}
    >
      <code>
        {lines.map((ln, idx) => (
          <span key={idx}>
            {renderCodeLine(ln)}
            {idx < lines.length - 1 ? '\n' : ''}
          </span>
        ))}
      </code>
    </pre>
  )
}

function Snippet({ title, language, body }: { title: string; language: string; body: string }) {
  const [copied, setCopied] = useState(false)

  const copy = () => {
    navigator.clipboard.writeText(body).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    })
  }

  return (
    <div className="rounded-md border border-border bg-surface overflow-hidden" style={{ boxShadow: 'var(--shadow-card)' }}>
      <div className="flex items-center justify-between border-b border-border bg-surface-2 px-4 py-2">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-medium text-text">{title}</span>
          <Badge variant="default">{language}</Badge>
        </div>
        <button
          onClick={copy}
          className={[
            'flex items-center gap-1.5 rounded px-2 py-1',
            'text-[11px] font-medium transition-colors cursor-pointer',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            copied ? 'text-green' : 'text-muted hover:text-text hover:bg-surface',
          ].join(' ')}
          style={{ transitionDuration: 'var(--duration-fast)' }}
        >
          {copied ? (
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
      <CodeBody body={body} />
    </div>
  )
}

// ─── EvalTab ────────────────────────────────────────────────────────────────

const SOURCE_LABEL: Record<EvalCaseSource, string> = {
  certified: 'certified',
  sample_query: 'sample',
  harvested: 'harvested',
  authored: 'authored',
}

// Hover text — "harvested" especially reads as jargon without it.
const SOURCE_TITLE: Record<EvalCaseSource, string> = {
  certified: 'Pinned from a certified answer — carries a real SQL oracle',
  sample_query: 'Promoted from a sample question',
  harvested: 'Distilled from a review ruling on real traffic',
  authored: 'Authored by hand in evals/cases.yaml',
}

const STATUS_VARIANT: Record<EvalCaseStatus, 'success' | 'warning' | 'default'> = {
  approved: 'success',
  candidate: 'warning',
  retired: 'default',
}

/** Inline spinner matching the existing pattern in LensDetail. */
function Spinner() {
  return (
    <span className="h-3 w-3 rounded-full border-2 border-border border-t-muted animate-spin" />
  )
}

/** A single eval case row — read-only; cases are authored in evals/cases.yaml. */
function EvalCaseRow({ c }: { c: EvalCase }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="rounded-md border border-border bg-surface overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-3 py-2.5">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
        >
          <svg
            width="12"
            height="12"
            viewBox="0 0 12 12"
            fill="none"
            aria-hidden="true"
            className={`shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`}
            style={{ transitionDuration: 'var(--duration-fast)' }}
          >
            <path
              d="M4 2.5L7.5 6L4 9.5"
              stroke="currentColor"
              strokeWidth="1.25"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <span className="min-w-0 flex-1 truncate text-[13px] text-text">{c.question}</span>
        </button>
        <span title={SOURCE_TITLE[c.source]}>
          <Badge variant="default">{SOURCE_LABEL[c.source]}</Badge>
        </span>
      </div>
      {open && c.expected_sql && (
        <div className="border-t border-border bg-bg px-3 py-2.5">
          <pre className="overflow-x-auto font-mono text-[11px] leading-relaxed text-muted">
            {c.expected_sql}
          </pre>
        </div>
      )}
    </div>
  )
}

/** Cases grouped under a collapsible section (candidate / approved / retired). */
function CasesGroup({ label, cases }: { label: string; cases: EvalCase[] }) {
  const [open, setOpen] = useState(true)
  if (cases.length === 0) return null
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="mb-2 flex items-center gap-2 cursor-pointer hover:text-accent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
        style={{ transitionDuration: 'var(--duration-fast)' }}
      >
        <svg
          width="12"
          height="12"
          viewBox="0 0 12 12"
          fill="none"
          aria-hidden="true"
          className={`shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`}
          style={{ transitionDuration: 'var(--duration-fast)' }}
        >
          <path
            d="M4 2.5L7.5 6L4 9.5"
            stroke="currentColor"
            strokeWidth="1.25"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">
          {label}
        </span>
        <Badge variant={STATUS_VARIANT[label.toLowerCase() as EvalCaseStatus] ?? 'default'}>
          {cases.length}
        </Badge>
      </button>
      {open && (
        <div className="space-y-1.5">
          {cases.map((c) => (
            <EvalCaseRow key={c.id} c={c} />
          ))}
        </div>
      )}
    </div>
  )
}

/** One persisted per-case outcome: verdict + question + reason, with the
 *  generated SQL behind the same chevron expand as EvalCaseRow. */
function EvalResultRow({ r }: { r: EvalRunCaseResult }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-md border border-border bg-surface overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-3 py-2.5">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
        >
          <svg
            width="12"
            height="12"
            viewBox="0 0 12 12"
            fill="none"
            aria-hidden="true"
            className={`shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`}
            style={{ transitionDuration: 'var(--duration-fast)' }}
          >
            <path
              d="M4 2.5L7.5 6L4 9.5"
              stroke="currentColor"
              strokeWidth="1.25"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <span className="min-w-0 flex-1 truncate text-[13px] text-text">
            {r.question || r.case_id}
          </span>
        </button>
        <Badge variant={r.passed ? 'success' : r.grade === 'errored' ? 'warning' : 'error'}>
          {r.passed ? 'pass' : r.grade === 'errored' ? 'errored' : 'fail'}
        </Badge>
      </div>
      {!r.passed && r.reason && (
        <p className="px-3 pb-2.5 pl-8 text-[12px] leading-relaxed text-muted">{r.reason}</p>
      )}
      {open && (
        <div className="border-t border-border bg-bg px-3 py-2.5">
          {r.actual_sql ? (
            <pre className="overflow-x-auto font-mono text-[11px] leading-relaxed text-muted">
              {formatSql(r.actual_sql)}
            </pre>
          ) : (
            <p className="text-[12px] text-muted-2">No SQL was generated for this case.</p>
          )}
        </div>
      )}
    </div>
  )
}

/** One run in the history — click to drill into its persisted per-case results. */
function RunRow({ lens, r }: { lens: string; r: EvalRun }) {
  const [open, setOpen] = useState(false)
  const results = useEvalRunResults(lens, open ? r.id : null)
  const pct = r.score != null ? `${Math.round(r.score * 100)}%` : '—'
  const tone =
    r.score == null
      ? 'text-muted'
      : r.score >= 0.9
      ? 'text-green'
      : r.score >= 0.7
      ? 'text-amber'
      : 'text-red'
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-4 px-3.5 py-2.5 text-[12px] text-left cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <svg
          width="12"
          height="12"
          viewBox="0 0 12 12"
          fill="none"
          aria-hidden="true"
          className={`shrink-0 text-muted transition-transform ${open ? 'rotate-90' : ''}`}
          style={{ transitionDuration: 'var(--duration-fast)' }}
        >
          <path
            d="M4 2.5L7.5 6L4 9.5"
            stroke="currentColor"
            strokeWidth="1.25"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span className={`w-10 shrink-0 font-mono font-bold tabular-nums ${tone}`}>
          {pct}
        </span>
        <Badge variant="default">{r.mode}</Badge>
        <span className="text-muted tabular-nums">
          {r.passed}p · {r.failed}f · {r.errored}e
        </span>
        {r.started_at && (
          <span className="ml-auto shrink-0 font-mono text-[11px] text-muted-2 tabular-nums">
            {new Date(r.started_at).toLocaleString()}
          </span>
        )}
      </button>
      {open && (
        <div className="border-t border-border bg-bg px-3.5 py-2.5">
          {results.isLoading && <Skeleton className="h-10 w-full" />}
          {results.isError && (
            <p className="text-[12px] text-red">{String(results.error)}</p>
          )}
          {results.data && results.data.length === 0 && (
            <p className="text-[12px] text-muted-2">
              This run recorded no per-case results.
            </p>
          )}
          {results.data && results.data.length > 0 && (
            <div className="space-y-1.5">
              {results.data.map((res) => (
                <EvalResultRow key={res.case_id} r={res} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** Compact accuracy trend: recent run scores, each expandable to its per-case results. */
function AccuracyTrend({ lens, runs }: { lens: string; runs: EvalRun[] }) {
  if (runs.length === 0) return null
  return (
    <div>
      <h3 className="mb-2 text-[13px] font-semibold text-text">Run history</h3>
      <div className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface" style={{ boxShadow: 'var(--shadow-card)' }}>
        {runs.map((r) => (
          <RunRow key={r.id} lens={lens} r={r} />
        ))}
      </div>
    </div>
  )
}

/** The Evaluation tab body. Cases are read-only (authored in evals/cases.yaml);
 *  running the suite and distilling verified history stay — both are governing. */
function EvalTab({ name }: { name: string }) {
  const qc = useQueryClient()
  const invalidate = useInvalidateEvals(name)
  const cases = useEvalCases(name)
  const runs = useEvalRuns(name)
  const distill = useDistill(name)
  const run = useRunEval(name)

  const allCases = cases.data ?? []
  const candidates = allCases.filter((c) => c.status === 'candidate')
  const approved = allCases.filter((c) => c.status === 'approved')
  const retired = allCases.filter((c) => c.status === 'retired')

  const latestRun = runs.data?.[0] ?? null
  const runResult = run.data ?? null
  // The persisted per-case outcomes for the latest run — unlike the mutation
  // result, these survive a refresh.
  const latestRunId = runResult?.run_id ?? latestRun?.id ?? null
  const latestResults = useEvalRunResults(name, latestRunId)

  return (
    <div className="space-y-6">
      {/* ── Actions ── */}
      <div className="flex items-center gap-2">
        <Button
          variant="secondary"
          size="sm"
          disabled={run.isPending || approved.length === 0}
          onClick={() =>
            run.mutate('health', { onSuccess: () => invalidate() })
          }
        >
          {run.isPending ? (
            <span className="flex items-center gap-1.5">
              <Spinner />
              Running
            </span>
          ) : (
            'Run health check'
          )}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={distill.isPending}
          onClick={() =>
            distill.mutate(undefined, {
              onSuccess: () => qc.invalidateQueries({ queryKey: ['patches', name] }),
            })
          }
        >
          {distill.isPending ? (
            <span className="flex items-center gap-1.5">
              <Spinner />
              Distilling
            </span>
          ) : (
            'Distill history'
          )}
        </Button>
        {run.isError && (
          <span className="text-[12px] text-red">{String(run.error)}</span>
        )}
        {distill.isError && (
          <span className="text-[12px] text-red">{String(distill.error)}</span>
        )}
        {distill.isSuccess && (
          <span className="text-[12px] text-muted">
            {distill.data.length === 0
              ? 'No new patterns in verified history.'
              : `${distill.data.length} candidate${distill.data.length === 1 ? '' : 's'} drafted — review under Reviews.`}
          </span>
        )}
      </div>

      {/* ── Latest run result ── */}
      {(runResult ?? latestRun) && (() => {
        const r = runResult ?? latestRun!
        const score = r.score
        const pct = score != null ? `${Math.round(score * 100)}%` : '—'
        const scoreTone =
          score == null
            ? undefined
            : score >= 0.9
            ? undefined
            : score >= 0.7
            ? 'amber' as const
            : 'red' as const
        const failing = (latestResults.data ?? []).filter((res) => !res.passed)
        return (
          <div>
            <h3 className="mb-2 text-[13px] font-semibold text-text">Latest run</h3>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatCard label="Score" value={pct} tone={scoreTone} />
              <StatCard label="Passed" value={String(r.passed)} />
              <StatCard label="Failed" value={String(r.failed)} tone={r.failed > 0 ? 'red' : undefined} />
              <StatCard label="Errored" value={String(r.errored)} tone={r.errored > 0 ? 'amber' : undefined} />
            </div>
            {failing.length > 0 && (
              <div className="mt-3 space-y-1">
                <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                  Failing cases
                </span>
                <div className="mt-1 space-y-1.5">
                  {failing.map((res) => (
                    <EvalResultRow key={res.case_id} r={res} />
                  ))}
                </div>
              </div>
            )}
          </div>
        )
      })()}

      {/* ── Cases list ── */}
      <div className="space-y-3">
        <h3 className="text-[13px] font-semibold text-text">Cases</h3>
        {cases.isLoading && (
          <div className="space-y-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        )}
        {cases.data && allCases.length === 0 && (
          <p className="rounded-lg border border-dashed border-border bg-surface-2 px-4 py-8 text-center text-[13px] text-muted">
            No eval cases yet.
          </p>
        )}
        {allCases.length > 0 && (
          <div className="space-y-4">
            <CasesGroup label="candidate" cases={candidates} />
            <CasesGroup label="approved" cases={approved} />
            <CasesGroup label="retired" cases={retired} />
          </div>
        )}
        <AuthoredIn file={`lenses/${name}/evals/cases.yaml`} field="status">
          Only <code className="font-mono text-[11.5px] text-text">status: approved</code> cases
          are scored (behavioral pins gate at apply; value verification lives in certified
          answers).
        </AuthoredIn>
      </div>

      {/* ── Accuracy trend ── */}
      {runs.data && <AccuracyTrend lens={name} runs={runs.data} />}
    </div>
  )
}
