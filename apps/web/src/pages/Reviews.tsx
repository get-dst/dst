/**
 * Reviews — the self-healing loop, rebuilt as queue → detail → patch rail.
 *
 * Queue (/reviews): every flagged answer as a clickable row — question, lens,
 * caller, state, age, and a Δ chip when a correction is attached.
 *
 * Detail (/reviews/:id): the story of one answer top-to-bottom (question → SQL →
 * answer → verification → AI judge) on the left, and beside it the human's act —
 * the VERDICT card (rule + attach the correction) on top, the primary decision,
 * with the patch loop (draft → apply → publish) below it as the consequence.
 *
 * UI only — all data flows through the existing hooks in ../api/reviews.
 */
import { useMemo, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useCertifyRequest } from '../api/certify'
import { useRequestDetail, useRequests, type RequestTrace } from '../api/observe'
import {
  useApprovePatch,
  useDraftPatch,
  useLensPatches,
  useRejectPatch,
  useReviewQueue,
  useRuleReview,
  type CorrectionDelta,
  type PatchApproval,
  type PatchCandidate,
  type ReviewTicket,
} from '../api/reviews'
import { Button } from '../components/ui/Button'
import { Badge } from '../components/ui/Badge'
import { ConfidenceBadge } from '../components/ui/OutcomeBadges'
import { Skeleton, SkeletonText } from '../components/ui/Skeleton'
import { EmptyState } from '../components/ui/EmptyState'
import { formatSql } from '../lib/format'
import { actorLabel } from '../lib/outcomes'
import { Answer } from '../lib/markdown'

// ─── small shared pieces ──────────────────────────────────────────────────────

function relTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return `${Math.floor(s)}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function StateChip({ state }: { state: string }) {
  const variant =
    state === 'approved'
      ? 'success'
      : state === 'rejected'
        ? 'error'
        : state === 'needs_human'
          ? 'warning'
          : 'default'
  return (
    <Badge variant={variant} dot>
      {state.replace(/_/g, ' ')}
    </Badge>
  )
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">
      {children}
    </span>
  )
}

/** Spinner-in-button, inheriting the button's text color. */
function Busy({ label }: { label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span
        className="h-2.5 w-2.5 animate-spin rounded-full border-2 border-current/25 border-t-current"
        aria-hidden="true"
      />
      {label}…
    </span>
  )
}

/** Origin chip — who raised the ticket: the lens's auto_review flagger or a person. */
function OriginChip({ origin }: { origin: string }) {
  const ai = origin === 'ai'
  return (
    <span
      title={ai ? 'Auto-flagged by the lens (auto_review)' : 'Raised by a caller or reviewer'}
      className={[
        'inline-flex shrink-0 items-center rounded border px-1.5 py-px font-mono text-[10px]',
        ai
          ? 'border-accent/30 bg-accent-fg text-accent-dark'
          : 'border-border bg-surface-2 text-muted',
      ].join(' ')}
    >
      {ai ? 'AI-flagged' : 'human'}
    </span>
  )
}

/** The Δ chip marking a ticket that carries a correction (the loop's fuel). */
function CorrectionChip({ correction }: { correction: CorrectionDelta }) {
  return (
    <span
      title={correction.note}
      className="inline-flex shrink-0 items-center gap-1 rounded border border-accent/30 bg-accent-fg px-1.5 py-px font-mono text-[10px] text-accent-dark"
    >
      Δ {correction.kind}
    </span>
  )
}

// ─── line diff (LCS) + the unified diff block ─────────────────────────────────

type DiffLine = { type: 'same' | 'add' | 'del'; text: string }

function lineDiff(before: string, after: string): DiffLine[] {
  const a = before ? before.split('\n') : []
  const b = after ? after.split('\n') : []
  const n = a.length
  const m = b.length
  // LCS table — inputs here are short (definition bodies, SQL snippets).
  const dp: number[][] = []
  for (let i = 0; i <= n; i++) dp.push(new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out: DiffLine[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ type: 'same', text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: 'del', text: a[i] })
      i++
    } else {
      out.push({ type: 'add', text: b[j] })
      j++
    }
  }
  while (i < n) out.push({ type: 'del', text: a[i++] })
  while (j < m) out.push({ type: 'add', text: b[j++] })
  return out
}

/** Unified before/after diff — mono, gutter signs, subtle in-identity tinting. */
function DiffBlock({
  before,
  after,
  wrap = false,
}: {
  before: string | null
  after: string
  wrap?: boolean
}) {
  const lines = useMemo(() => lineDiff(before ?? '', after), [before, after])
  return (
    <div className="overflow-hidden rounded-md border border-border bg-surface">
      <div className={wrap ? '' : 'overflow-x-auto'}>
        {lines.map((l, idx) => (
          <div
            key={idx}
            className={[
              'flex font-mono text-[11px] leading-[1.7]',
              l.type === 'add' ? 'bg-green-bg' : l.type === 'del' ? 'bg-red-bg' : '',
            ].join(' ')}
          >
            <span
              aria-hidden="true"
              className={[
                'w-5 shrink-0 select-none text-center',
                l.type === 'add' ? 'text-green' : l.type === 'del' ? 'text-red' : 'text-muted-2',
              ].join(' ')}
            >
              {l.type === 'add' ? '+' : l.type === 'del' ? '−' : ''}
            </span>
            <span
              className={[
                'flex-1 pr-3 text-text',
                wrap ? 'whitespace-pre-wrap break-words' : 'whitespace-pre',
              ].join(' ')}
            >
              {l.text || ' '}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** A server-rendered unified diff (the proposed file change), rendered like the
 *  lens version history's — the file loop looks the same wherever it shows up. */
function UnifiedDiff({ diff }: { diff: string }) {
  return (
    <pre className="overflow-x-auto rounded-md border border-border bg-surface px-2.5 py-2 font-mono text-[11px] leading-[1.7]">
      {diff.split('\n').map((line, i) => (
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
          {line || ' '}
        </div>
      ))}
    </pre>
  )
}

// ─── the panel: queue or detail, by route ─────────────────────────────────────

export function ReviewsPanel() {
  const { id } = useParams<{ id: string }>()
  return id ? <ReviewDetail ticketId={id} /> : <ReviewQueue />
}

// ─── queue ────────────────────────────────────────────────────────────────────

const FILTERS = [
  { key: 'needs_human', label: 'Needs review' },
  { key: 'approved', label: 'Approved' },
  { key: 'changes_requested', label: 'Changes' },
  { key: 'rejected', label: 'Rejected' },
  { key: 'all', label: 'All' },
] as const

const EMPTY_COPY: Record<string, { title: string; description: string }> = {
  needs_human: {
    title: 'Queue is clear',
    description: 'Answers that fail verification — or that a caller flags — land here for ruling.',
  },
  approved: {
    title: 'No approved reviews',
    description: 'Approve a flagged answer and it shows up here, ready to certify.',
  },
  changes_requested: {
    title: 'No change requests',
    description: 'Rule “request changes” on a ticket and it is tracked here.',
  },
  rejected: {
    title: 'No rejected reviews',
    description: 'Rejected answers stay here for the record.',
  },
  all: {
    title: 'No reviews yet',
    description: 'Send an answer for review from the API or MCP tools and it lands here.',
  },
}

function ReviewQueue() {
  const [filter, setFilter] = useState<string>('needs_human')
  const navigate = useNavigate()
  // One fetch for all states: client-side filtering gives every tab a live count.
  const queue = useReviewQueue()
  // Join against recent requests for the question text + age (no extra API surface).
  const requests = useRequests()
  const byRequest = useMemo(
    () => new Map((requests.data ?? []).map((r) => [r.request_id, r])),
    [requests.data],
  )

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: queue.data?.length ?? 0 }
    for (const t of queue.data ?? []) c[t.state] = (c[t.state] ?? 0) + 1
    return c
  }, [queue.data])

  const tickets = (queue.data ?? []).filter((t) => filter === 'all' || t.state === filter)
  const open = (id: string) => navigate(`/reviews/${id}`)

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        {/* Filter tabs */}
        <div
          className="inline-flex items-center gap-px rounded-lg border border-border bg-surface-2 p-0.5"
          role="tablist"
          aria-label="Filter by state"
        >
          {FILTERS.map((f) => (
            <button
              key={f.key}
              role="tab"
              aria-selected={filter === f.key}
              onClick={() => setFilter(f.key)}
              className={[
                'cursor-pointer rounded-md px-3 py-1.5 text-[12px] font-medium',
                'transition-[background-color,color,box-shadow,transform] active:scale-[0.97]',
                'outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1',
                filter === f.key
                  ? 'bg-surface text-text shadow-[var(--shadow-card)] ring-1 ring-border'
                  : 'text-muted hover:bg-surface/60 hover:text-text hover:shadow-sm',
              ].join(' ')}
              style={{ transitionDuration: 'var(--duration-fast)' }}
            >
              {f.label}
              {queue.data && (
                <span className="ml-1.5 font-mono text-[10px] tabular-nums text-muted-2">
                  {counts[f.key] ?? 0}
                </span>
              )}
            </button>
          ))}
        </div>
        {/* The loop, spelled out — the mental model for this whole surface. */}
        <p className="font-mono text-[11px] text-muted-2">
          flagged answer → correction → patch → ruling → applied fix
        </p>
      </div>

      {queue.isLoading && (
        <div className="mt-4 space-y-2">
          {[...Array(4)].map((_, i) => (
            <Skeleton key={i} className="h-9 w-full" />
          ))}
        </div>
      )}

      {queue.isError && (
        <div className="mt-4 rounded-md border border-red/20 bg-red-bg px-4 py-3 text-[13px] text-red">
          {(queue.error as Error).message}
        </div>
      )}

      {queue.data && tickets.length > 0 && (
        <div
          className="mt-4 overflow-hidden rounded-lg border border-border bg-surface"
          style={{ boxShadow: 'var(--shadow-card)' }}
        >
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="bg-surface-2">
                {['State', 'Question', 'Lens', 'Caller', 'Age', ''].map((h, i) => (
                  <th
                    key={i}
                    className={[
                      'border-b border-border px-4 py-2.5 text-left text-[11px] font-medium uppercase tracking-wider text-muted',
                      h === 'Caller' ? 'hidden md:table-cell' : '',
                    ].join(' ')}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {tickets.map((t) => {
                const req = byRequest.get(t.request_id)
                return (
                  <tr
                    key={t.ticket_id}
                    role="button"
                    tabIndex={0}
                    aria-label={`Open review ${t.ticket_id}`}
                    onClick={() => open(t.ticket_id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        open(t.ticket_id)
                      }
                    }}
                    className={[
                      'cursor-pointer bg-surface transition-colors outline-none',
                      'hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent',
                    ].join(' ')}
                    style={{ transitionDuration: 'var(--duration-fast)' }}
                  >
                    <td className="whitespace-nowrap px-4 py-2.5">
                      <span className="flex items-center gap-1.5">
                        <StateChip state={t.state} />
                        <OriginChip origin={t.origin} />
                      </span>
                    </td>
                    <td className="w-full max-w-0 px-4 py-2.5">
                      <span className="flex items-center gap-2">
                        <span className="truncate text-[13px] text-text">
                          {req?.question ?? (
                            <code className="font-mono text-[11px] text-muted">
                              {t.request_id}
                            </code>
                          )}
                        </span>
                        {t.correction && <CorrectionChip correction={t.correction} />}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 font-mono text-[12px] font-medium text-text">
                      {t.lens}
                    </td>
                    <td className="hidden whitespace-nowrap px-4 py-2.5 font-mono text-[11px] text-muted md:table-cell">
                      {t.caller}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-[11px] tabular-nums text-muted">
                      {req ? relTime(req.created_at) : '—'}
                    </td>
                    <td className="py-2.5 pr-3 text-muted-2">
                      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                        <path
                          d="M4.5 2.5L8 6l-3.5 3.5"
                          stroke="currentColor"
                          strokeWidth="1.25"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {queue.data && tickets.length === 0 && (
        <div className="mt-4">
          <EmptyState
            icon={
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M20 12a8 8 0 1 1-2.34-5.66"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
                <path
                  d="M18 3v4h-4"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <path
                  d="M9.5 12l1.8 1.8 3.2-3.6"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            }
            title={EMPTY_COPY[filter].title}
            description={EMPTY_COPY[filter].description}
          />
        </div>
      )}
    </div>
  )
}

// ─── detail ───────────────────────────────────────────────────────────────────

export function ReviewDetail({
  ticketId,
  onBack,
}: {
  ticketId: string
  onBack?: () => void
}) {
  const navigate = useNavigate()
  // Embedded in a lens (onBack) the back button returns to the lens-local list;
  // on the standalone /reviews route it navigates to the global queue.
  const back = onBack ?? (() => navigate('/reviews'))
  const queue = useReviewQueue()
  const ticket = queue.data?.find((t) => t.ticket_id === ticketId)
  const trace = useRequestDetail(ticket?.request_id ?? null)
  const patches = useLensPatches(ticket?.lens ?? '')

  if (queue.isLoading) {
    return (
      <div>
        <Skeleton className="h-4 w-24" />
        <Skeleton className="mt-4 h-6 w-80" />
        <div className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
          <Skeleton className="h-72 w-full rounded-lg" />
          <Skeleton className="h-72 w-full rounded-lg" />
        </div>
      </div>
    )
  }

  if (queue.isError) {
    return (
      <div className="rounded-md border border-red/20 bg-red-bg px-4 py-3 text-[13px] text-red">
        {(queue.error as Error).message}
      </div>
    )
  }

  if (!ticket) {
    return (
      <EmptyState
        title="Review not found"
        description={`No ticket “${ticketId}” in the queue — it may have been opened by another org.`}
        action={
          <Button variant="secondary" size="sm" onClick={back}>
            Back to queue
          </Button>
        }
      />
    )
  }

  const ticketPatches = (patches.data ?? []).filter((p) => p.ticket_id === ticket.ticket_id)

  return (
    <div>
      {/* Back to the queue */}
      <button
        onClick={back}
        className={[
          'inline-flex items-center gap-1 rounded text-[12px] text-muted',
          'transition-colors hover:text-text',
          'outline-none focus-visible:ring-2 focus-visible:ring-accent',
        ].join(' ')}
        style={{ transitionDuration: 'var(--duration-fast)' }}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
          <path
            d="M7.5 9L4.5 6L7.5 3"
            stroke="currentColor"
            strokeWidth="1.25"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        Queue
      </button>

      {/* Ticket header */}
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <code className="font-mono text-[15px] font-semibold text-text">{ticket.ticket_id}</code>
        <StateChip state={ticket.state} />
        <OriginChip origin={ticket.origin} />
        <span aria-hidden="true" className="text-border-strong">·</span>
        <Link
          to={`/lenses/${encodeURIComponent(ticket.lens)}`}
          className="rounded font-mono text-[12px] font-medium text-accent-dark hover:underline outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {ticket.lens}
        </Link>
        <span className="text-[12px] text-muted">
          caller <span className="font-mono font-medium text-text">{ticket.caller}</span>
        </span>
        {trace.data?.created_at && (
          <span className="text-[11px] tabular-nums text-muted-2">
            {relTime(trace.data.created_at)}
          </span>
        )}
      </div>

      <div className="mt-5 grid items-start gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
        {/* The story of this answer, top to bottom */}
        <div className="min-w-0 space-y-4">
          <StoryCard ticket={ticket} trace={trace.data} traceLoading={trace.isLoading} />
          {ticket.correction && (
            <CorrectionCard correction={ticket.correction} originalSql={trace.data?.sql ?? null} />
          )}
        </div>

        {/* The action rail: the human ruling is the act; the patch loop is its
            consequence — so the loop only appears once a correction exists to drive it. */}
        <div className="space-y-4 lg:sticky lg:top-6">
          <VerdictCard ticket={ticket} />
          {(ticket.correction || ticketPatches.length > 0) && (
            <ActionRail
              ticket={ticket}
              ticketPatches={ticketPatches}
              patchesLoading={patches.isLoading}
            />
          )}
        </div>
      </div>
    </div>
  )
}

// ─── governed scope: the SQL decomposed so it can be audited without reading SQL ─

function ScopeRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[84px_minmax(0,1fr)] gap-3 px-5 py-2.5">
      <span className="pt-px text-[11px] font-semibold uppercase tracking-wider text-muted-2">
        {label}
      </span>
      <div className="min-w-0 text-[12px] text-text">{children}</div>
    </div>
  )
}

function ScopeChips({ items }: { items: string[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((it, i) => (
        <code
          key={i}
          className="break-all rounded border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-text"
        >
          {it}
        </code>
      ))}
    </div>
  )
}

/** The governed decomposition — table/fields/filter from the SQL, plus the
 *  definition, caller, and result from the trace. The reviewer's audit surface. */
function ScopeSection({ trace }: { trace: RequestTrace }) {
  const scope = trace.scope
  return (
    <div className="border-t border-border">
      <div className="border-b border-border bg-surface-2 px-5 py-2">
        <SectionLabel>Governed scope</SectionLabel>
      </div>
      <div className="divide-y divide-border/60">
        {scope?.tables.length ? (
          <ScopeRow label="Table">
            <ScopeChips items={scope.tables} />
          </ScopeRow>
        ) : null}
        {scope?.fields.length ? (
          <ScopeRow label="Fields">
            <ScopeChips items={scope.fields} />
          </ScopeRow>
        ) : null}
        <ScopeRow label="Filter">
          {scope?.filters.length ? (
            <ScopeChips items={scope.filters} />
          ) : (
            <span className="text-muted-2">no filter</span>
          )}
        </ScopeRow>
        {scope?.order_by.length ? (
          <ScopeRow label="Ranked by">
            <ScopeChips items={scope.order_by} />
          </ScopeRow>
        ) : null}
        <ScopeRow label="Definition">
          {trace.definition_used ? (
            <code className="rounded border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-text">
              {trace.definition_used}
            </code>
          ) : (
            <span className="text-muted-2">none applied</span>
          )}
        </ScopeRow>
        <ScopeRow label="Asked by">
          <span className="font-mono text-[11px] text-text">{trace.caller}</span>
        </ScopeRow>
        <ScopeRow label="Result">
          <span className="tabular-nums">
            {trace.row_count ?? '—'} {trace.row_count === 1 ? 'row' : 'rows'}
          </span>
        </ScopeRow>
      </div>
    </div>
  )
}

// ─── the story card: question → answer → governed scope → SQL → checks → judge ─

function StoryCard({
  ticket,
  trace,
  traceLoading,
}: {
  ticket: ReviewTicket
  trace: RequestTrace | undefined
  traceLoading: boolean
}) {
  const [sqlCopied, setSqlCopied] = useState(false)
  const copySql = () => {
    if (!trace?.sql) return
    navigator.clipboard.writeText(trace.sql).then(() => {
      setSqlCopied(true)
      setTimeout(() => setSqlCopied(false), 1800)
    })
  }

  const aiPass = ['pass', 'approve', 'approved'].includes(ticket.ai_verdict ?? '')
  const aiFail = ['fail', 'reject', 'rejected'].includes(ticket.ai_verdict ?? '')

  return (
    <div
      className="overflow-hidden rounded-lg border border-border bg-surface"
      style={{ boxShadow: 'var(--shadow-card)' }}
    >
      {/* Header strip */}
      <div className="flex items-center justify-between gap-3 border-b border-border bg-surface-2 px-5 py-2.5">
        <SectionLabel>Answer under review</SectionLabel>
        <code className="truncate font-mono text-[11px] text-muted">{ticket.request_id}</code>
      </div>

      {/* Question */}
      <div className="px-5 py-4">
        {traceLoading ? (
          <SkeletonText lines={2} />
        ) : (
          <p className="text-[16px] font-medium leading-snug text-text">
            {trace?.question ?? (
              <span className="text-[13px] font-normal text-muted">
                The underlying request trace is no longer available.
              </span>
            )}
          </p>
        )}
      </div>

      {/* Answer — the result */}
      {trace?.answer && (
        <div className="border-t border-border px-5 py-4">
          <SectionLabel>Answer</SectionLabel>
          <Answer text={trace.answer} className="mt-1.5 text-[13px] leading-relaxed text-text" />
        </div>
      )}

      {/* Governed scope — what produced this answer, without reading SQL */}
      {trace && <ScopeSection trace={trace} />}

      {/* SQL — the evidence underneath the scope */}
      {trace?.sql && (
        <div className="border-t border-border">
          <div className="flex items-center justify-between border-b border-border bg-surface-2 px-5 py-2">
            <SectionLabel>SQL</SectionLabel>
            <button
              onClick={copySql}
              className={[
                'flex items-center gap-1.5 rounded px-2 py-1 text-[11px] font-medium transition-colors',
                'outline-none focus-visible:ring-2 focus-visible:ring-accent',
                sqlCopied ? 'text-green' : 'text-muted hover:bg-surface-3 hover:text-text',
              ].join(' ')}
              style={{ transitionDuration: 'var(--duration-fast)' }}
            >
              {sqlCopied ? (
                <>
                  <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true">
                    <path
                      d="M1.5 5.5l3 3 5-5"
                      stroke="currentColor"
                      strokeWidth="1.25"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
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
          <pre className="overflow-x-auto bg-surface px-5 py-3 font-mono text-[12px] leading-relaxed text-text">
            {formatSql(trace.sql)}
          </pre>
        </div>
      )}

      {/* Verification / confidence */}
      {trace && (
        <div className="border-t border-border px-5 py-4">
          <SectionLabel>Verification</SectionLabel>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {trace.confidence && <ConfidenceBadge confidence={trace.confidence} dot />}
            {trace.verification?.checks?.map((c) => (
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
            {!trace.confidence && !trace.verification?.checks?.length && (
              <span className="text-[12px] text-muted">No verification recorded.</span>
            )}
          </div>
        </div>
      )}

      {/* AI judge */}
      {ticket.ai_verdict && (
        <div className="border-t border-border px-5 py-4">
          <div className="flex items-center gap-2">
            <SectionLabel>AI judge</SectionLabel>
            <span
              className={[
                'font-mono text-[12px] font-semibold',
                aiPass ? 'text-green' : aiFail ? 'text-red' : 'text-text',
              ].join(' ')}
            >
              {ticket.ai_verdict}
            </span>
            {ticket.judged_by && (
              <span className="font-mono text-[11px] text-muted">by {ticket.judged_by}</span>
            )}
          </div>
          {ticket.ai_reasoning && (
            <p className="mt-1.5 whitespace-pre-wrap break-words text-[12px] leading-relaxed text-muted">
              {ticket.ai_reasoning}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

// ─── the correction card: the caller's wrong-vs-right delta ──────────────────

function CorrectionCard({
  correction,
  originalSql,
}: {
  correction: CorrectionDelta
  originalSql: string | null
}) {
  return (
    <div
      className="overflow-hidden rounded-lg border border-accent/40 bg-surface"
      style={{ boxShadow: 'var(--shadow-card)' }}
    >
      <div className="flex items-center gap-2.5 border-b border-accent/20 bg-accent-fg px-5 py-2.5">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-accent-dark">
          Correction
        </span>
        <Badge variant="warning">{correction.kind}</Badge>
      </div>
      <div className="px-5 py-4">
        <p className="text-[13px] leading-relaxed text-text">{correction.note}</p>
        {correction.corrected_answer && (
          <div className="mt-3">
            <SectionLabel>Corrected answer</SectionLabel>
            <p className="mt-1 text-[12px] leading-relaxed text-text">
              {correction.corrected_answer}
            </p>
          </div>
        )}
        {correction.corrected_sql && (
          <div className="mt-3">
            <SectionLabel>SQL — recorded vs corrected</SectionLabel>
            <div className="mt-1.5">
              <DiffBlock before={originalSql} after={correction.corrected_sql} />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ─── the action rail: Correction → Patch → Applied → Publish ─────────────────

type StepStatus = 'done' | 'active' | 'todo'

function RailStep({
  status,
  title,
  last = false,
  children,
}: {
  status: StepStatus
  title: string
  last?: boolean
  children?: ReactNode
}) {
  return (
    <div className="relative flex gap-3">
      {!last && (
        <span aria-hidden="true" className="absolute bottom-1 left-[9px] top-[22px] w-px bg-border" />
      )}
      <span
        className={[
          'mt-px flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-full border',
          status === 'done'
            ? 'border-green/40 bg-green-bg text-green'
            : status === 'active'
              ? 'border-accent bg-accent-fg text-accent'
              : 'border-border bg-surface-2 text-muted-2',
        ].join(' ')}
      >
        {status === 'done' ? (
          <svg width="9" height="9" viewBox="0 0 10 10" fill="none" aria-hidden="true">
            <path
              d="M1.5 5l2.5 2.5 5-5"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        ) : (
          <span className="h-[5px] w-[5px] rounded-full bg-current" aria-hidden="true" />
        )}
      </span>
      <div className={['min-w-0 flex-1', last ? '' : 'pb-5'].join(' ')}>
        <p
          className={[
            'text-[12px] font-semibold leading-[19px]',
            status === 'todo' ? 'text-muted-2' : 'text-text',
          ].join(' ')}
        >
          {title}
        </p>
        {children && <div className="mt-1.5">{children}</div>}
      </div>
    </div>
  )
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <p className="flex gap-2 font-mono text-[11px] leading-relaxed">
      <span className="w-[76px] shrink-0 text-muted-2">{k}</span>
      <span className="min-w-0 break-words text-text">{v}</span>
    </p>
  )
}

function ActionRail({
  ticket,
  ticketPatches,
  patchesLoading,
}: {
  ticket: ReviewTicket
  ticketPatches: PatchCandidate[]
  patchesLoading: boolean
}) {
  const qc = useQueryClient()
  const draft = useDraftPatch()
  const approve = useApprovePatch()
  const dismiss = useRejectPatch()
  // The fresh approval result — what landed, the eval case, and the proposed file
  // to commit — lives only in the approve response, so keep it for the last steps.
  const [approval, setApproval] = useState<PatchApproval | null>(null)

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['patches', ticket.lens] })
    qc.invalidateQueries({ queryKey: ['reviews'] })
  }

  const hasCorrection = !!ticket.correction
  const candidate = ticketPatches.find((p) => p.status === 'candidate')
  const approvedPatch = ticketPatches.find((p) => p.status === 'approved')
  const shown = candidate ?? approvedPatch ?? ticketPatches[ticketPatches.length - 1]
  const isApplied = !!approvedPatch || !!approval

  const onDraft = () => draft.mutate(ticket.ticket_id, { onSuccess: refresh })
  const onApprove = (id: string) =>
    approve.mutate(id, {
      onSuccess: (r) => {
        setApproval(r)
        refresh()
      },
    })
  const onDismiss = (id: string) => dismiss.mutate(id, { onSuccess: refresh })

  return (
    <div
      className="rounded-lg border border-border bg-surface"
      style={{ boxShadow: 'var(--shadow-card)' }}
    >
      <div className="flex items-center justify-between border-b border-border bg-surface-2 px-4 py-2.5">
        <SectionLabel>The loop</SectionLabel>
        <span className="font-mono text-[10px] text-muted-2">correction → patch → ruling</span>
      </div>

      <div className="px-4 py-4">
        {/* 1 · Correction */}
        <RailStep status={hasCorrection ? 'done' : 'active'} title="Correction">
          {ticket.correction ? (
            <p className="text-[11px] leading-relaxed text-muted">
              <span className="font-mono text-accent-dark">Δ {ticket.correction.kind}</span>
              {' — '}
              {ticket.correction.note}
            </p>
          ) : ticket.state === 'needs_human' ? (
            <p className="text-[11px] leading-relaxed text-muted">
              Attach what&apos;s wrong in your ruling above — that delta is what the patch is
              drafted from.
            </p>
          ) : (
            <p className="text-[11px] leading-relaxed text-muted-2">
              No correction recorded — nothing to draft.
            </p>
          )}
        </RailStep>

        {/* 2 · Patch */}
        <RailStep
          status={shown ? (candidate ? 'active' : 'done') : hasCorrection ? 'active' : 'todo'}
          title="Patch"
        >
          {patchesLoading ? (
            <Skeleton className="h-8 w-full" />
          ) : shown ? (
            <div className="space-y-2">
              <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                <Badge variant="warning">{shown.kind}</Badge>
                <code
                  className="min-w-0 truncate font-mono text-[11px] text-muted"
                  title={shown.target}
                >
                  {shown.target}
                </code>
                <span className="font-mono text-[10px] text-muted-2">→ {shown.owner}</span>
              </div>
              <DiffBlock before={shown.diff_before} after={shown.diff_after} wrap />
              {shown.status === 'candidate' && (
                <div className="flex items-center gap-2 pt-0.5">
                  <Button
                    variant="primary"
                    size="sm"
                    disabled={approve.isPending || dismiss.isPending}
                    onClick={() => onApprove(shown.id)}
                  >
                    {approve.isPending ? <Busy label="Applying" /> : 'Apply'}
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={approve.isPending || dismiss.isPending}
                    onClick={() => onDismiss(shown.id)}
                    className="text-muted hover:bg-red-bg hover:text-red focus-visible:ring-red/50"
                  >
                    {dismiss.isPending ? 'Dismissing…' : 'Dismiss'}
                  </Button>
                </div>
              )}
              {shown.status === 'rejected' && (
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[11px] text-muted">dismissed</span>
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={draft.isPending}
                    onClick={onDraft}
                  >
                    {draft.isPending ? <Busy label="Drafting" /> : 'Draft again'}
                  </Button>
                </div>
              )}
              {approve.isError && (
                <p className="text-[11px] text-red">{(approve.error as Error).message}</p>
              )}
              {dismiss.isError && (
                <p className="text-[11px] text-red">{(dismiss.error as Error).message}</p>
              )}
              {draft.isError && (
                <p className="text-[11px] text-red">{(draft.error as Error).message}</p>
              )}
            </div>
          ) : hasCorrection ? (
            <div>
              <Button variant="primary" size="sm" disabled={draft.isPending} onClick={onDraft}>
                {draft.isPending ? <Busy label="Drafting" /> : 'Draft patch'}
              </Button>
              <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
                dst turns the correction into the smallest concrete fix, routed to where it
                belongs — a definition, a lens instruction, or a certified answer.
              </p>
              {draft.isError && (
                <p className="mt-1 text-[11px] text-red">{(draft.error as Error).message}</p>
              )}
            </div>
          ) : (
            <p className="text-[11px] leading-relaxed text-muted-2">Waits on a correction.</p>
          )}
        </RailStep>

        {/* 3 · Ruling */}
        <RailStep status={isApplied ? 'done' : candidate ? 'active' : 'todo'} title="Ruling">
          {isApplied ? (
            <div className="space-y-0.5">
              {approvedPatch && !approval && (
                <>
                  <KV k="kind" v={approvedPatch.kind} />
                  <KV k="target" v={approvedPatch.target} />
                </>
              )}
              {approval &&
                Object.entries(approval.applied).map(([k, v]) => (
                  <KV key={k} k={k.replace(/_/g, ' ')} v={String(v)} />
                ))}
              {approval?.eval_case_id && <KV k="eval case" v={approval.eval_case_id} />}
              {approval && <KV k="live" v={approval.live ? 'yes' : 'not yet'} />}
            </div>
          ) : (
            <p className="text-[11px] leading-relaxed text-muted-2">
              Approving records the ruling and files an eval case. Server-side state (a certified
              answer) lands here; anything your files author comes back as a file to commit.
            </p>
          )}
        </RailStep>

        {/* 4 · Land it — files author, so the ruling lands through the repo */}
        <RailStep status={isApplied ? 'active' : 'todo'} title="Land it" last>
          {approval?.proposed_file ? (
            <div className="space-y-1.5">
              <p className="text-[11px] leading-relaxed text-muted">
                Not live yet. Write this file, commit it, then run{' '}
                <code className="font-mono text-accent-dark">dst apply</code>.
              </p>
              <p className="font-mono text-[11px] text-muted-2">{approval.proposed_file.path}</p>
              <UnifiedDiff diff={approval.proposed_file.diff} />
            </div>
          ) : isApplied ? (
            <div>
              <p className="text-[11px] leading-relaxed text-muted">
                {approval?.next_step ?? 'The ruling is recorded on this lens.'}
              </p>
              <Link
                to={`/lenses/${encodeURIComponent(ticket.lens)}`}
                className={[
                  'mt-1.5 inline-flex items-center gap-1 rounded text-[12px] font-medium text-accent-dark',
                  'hover:underline outline-none focus-visible:ring-2 focus-visible:ring-accent',
                ].join(' ')}
              >
                Open <span className="font-mono">{ticket.lens}</span>
                <svg width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                  <path
                    d="M2.5 6h7M6.5 3l3 3-3 3"
                    stroke="currentColor"
                    strokeWidth="1.25"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </Link>
            </div>
          ) : (
            <p className="text-[11px] leading-relaxed text-muted-2">
              A fix your files author lands the file way: commit the proposed file, then{' '}
              <code className="font-mono">dst apply</code>.
            </p>
          )}
        </RailStep>
      </div>
    </div>
  )
}

// ─── the verdict: the human ruling + its correction, the primary act ─────────

const CORRECTION_KINDS: CorrectionDelta['kind'][] = [
  'definition',
  'scope',
  'number',
  'freshness',
  'other',
]

function VerdictCard({ ticket }: { ticket: ReviewTicket }) {
  const qc = useQueryClient()
  const rule = useRuleReview()
  const certify = useCertifyRequest(ticket.lens)
  const [certifiedId, setCertifiedId] = useState<string | null>(null)
  const [pendingVerdict, setPendingVerdict] = useState<string | null>(null)
  const [reasoning, setReasoning] = useState('')
  // The correction form — only when the ticket doesn't already carry one.
  const [kind, setKind] = useState<CorrectionDelta['kind']>('definition')
  const [note, setNote] = useState('')
  const [correctedSql, setCorrectedSql] = useState('')

  // The ruling stays open until the answer is resolved (approved/rejected). A
  // 'changes' ruling is NOT a dead end — changes_requested keeps the form live
  // so the reviewer can attach a correction or re-rule.
  const open = ticket.state !== 'approved' && ticket.state !== 'rejected'
  const correction: CorrectionDelta | undefined =
    !ticket.correction && note.trim()
      ? { kind, note: note.trim(), corrected_sql: correctedSql.trim() || null }
      : undefined

  const act = (verdict: string) => {
    setPendingVerdict(verdict)
    rule.mutate(
      { ticketId: ticket.ticket_id, verdict, reasoning: reasoning.trim(), correction },
      {
        onSuccess: () => qc.invalidateQueries({ queryKey: ['reviews'] }),
        onSettled: () => setPendingVerdict(null),
      },
    )
  }

  const fieldClass = [
    'rounded-md border border-border bg-surface px-2.5 py-1.5 text-[12px] text-text',
    'placeholder:text-muted-2 transition-colors outline-none',
    'focus:border-accent focus:ring-2 focus:ring-accent/20',
  ].join(' ')

  return (
    <div
      className="overflow-hidden rounded-lg border border-accent/40 bg-surface"
      style={{ boxShadow: 'var(--shadow-card)' }}
    >
      <div className="flex items-center justify-between gap-2 border-b border-accent/20 bg-accent-fg px-4 py-2.5">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-accent-dark">
          Human ruling
        </span>
        {open && <span className="font-mono text-[10px] text-accent-dark/70">your call</span>}
      </div>

      <div className="space-y-3 px-4 py-4">
        {/* Already ruled */}
        {ticket.human_verdict && (
          <div className="flex items-start gap-2 text-[12px] text-muted">
            <svg
              width="12"
              height="12"
              viewBox="0 0 12 12"
              fill="none"
              aria-hidden="true"
              className="mt-0.5 shrink-0"
            >
              <circle cx="6" cy="4" r="2" stroke="currentColor" strokeWidth="1.1" />
              <path
                d="M2 10c0-2.21 1.79-4 4-4s4 1.79 4 4"
                stroke="currentColor"
                strokeWidth="1.1"
                strokeLinecap="round"
              />
            </svg>
            <span>
              Ruled <span className="font-semibold text-text">{ticket.human_verdict}</span>
              {ticket.ruled_by && (
                <span>
                  {' '}by <span className="font-mono text-text">{actorLabel(ticket.ruled_by)}</span>
                </span>
              )}
              {ticket.human_reasoning && <span> — {ticket.human_reasoning}</span>}
            </span>
          </div>
        )}

        {/* Approved → certify */}
        {ticket.state === 'approved' &&
          (certifiedId ? (
            <p className="flex items-center gap-1.5 text-[12px] font-medium text-green">
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                <circle cx="6" cy="6" r="5" stroke="currentColor" strokeWidth="1.1" />
                <path
                  d="M3.8 6.1l1.5 1.5 3-3.2"
                  stroke="currentColor"
                  strokeWidth="1.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
              Certified — matching questions now serve this approved SQL.
            </p>
          ) : (
            <div>
              <Button
                variant="secondary"
                size="sm"
                disabled={certify.isPending}
                onClick={() =>
                  certify.mutate(ticket.request_id, { onSuccess: (r) => setCertifiedId(r.id) })
                }
                className="border-green/40 bg-green-bg text-green hover:bg-green/15 focus-visible:ring-green/50"
              >
                {certify.isPending ? <Busy label="Certifying" /> : 'Certify this answer'}
              </Button>
              <p className="mt-1.5 text-[11px] leading-relaxed text-muted-2">
                Promote this question→SQL so future matches are served from it.
              </p>
              {certify.isError && (
                <p className="mt-1 text-[11px] text-red">
                  {(certify.error as Error).message || 'Could not certify this answer.'}
                </p>
              )}
            </div>
          ))}

        {/* Open: correction form + verdict */}
        {open && (
          <>
            {!ticket.correction && (
              <div className="rounded-md border border-border bg-surface-2 px-3 py-2.5">
                <p className="text-[11px] font-medium uppercase tracking-wider text-muted">
                  Attach a correction
                </p>
                <div className="mt-2 flex items-center gap-2">
                  <select
                    value={kind}
                    onChange={(e) => setKind(e.target.value as CorrectionDelta['kind'])}
                    aria-label="What kind of wrong"
                    className="rounded-md border border-border bg-surface px-2 py-1.5 font-mono text-[12px] text-text"
                  >
                    {CORRECTION_KINDS.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder="what's wrong with this answer?"
                    aria-label="Correction note"
                    className={['min-w-0 flex-1', fieldClass].join(' ')}
                  />
                </div>
                <textarea
                  value={correctedSql}
                  onChange={(e) => setCorrectedSql(e.target.value)}
                  placeholder="corrected SQL (optional)"
                  aria-label="Corrected SQL"
                  rows={2}
                  className={['mt-2 w-full resize-y font-mono text-[11px]', fieldClass].join(' ')}
                />
                <p className="mt-1.5 text-[11px] leading-relaxed text-muted-2">
                  Saved with your ruling — the delta the patch is drafted from.
                </p>
              </div>
            )}

            <input
              value={reasoning}
              onChange={(e) => setReasoning(e.target.value)}
              placeholder="reasoning (optional)"
              aria-label="Ruling reasoning"
              className={['w-full', fieldClass].join(' ')}
            />

            <div className="flex flex-wrap items-center gap-2">
              <Button
                variant="secondary"
                size="sm"
                disabled={rule.isPending}
                onClick={() => act('approve')}
                className="border-green/40 bg-green-bg text-green hover:bg-green/15 focus-visible:ring-green/50"
              >
                {pendingVerdict === 'approve' ? <Busy label="Approving" /> : 'Approve'}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={rule.isPending}
                onClick={() => act('changes')}
              >
                {pendingVerdict === 'changes' ? <Busy label="Saving" /> : 'Request changes'}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={rule.isPending}
                onClick={() => act('reject')}
                className="text-muted hover:bg-red-bg hover:text-red focus-visible:ring-red/50"
              >
                {pendingVerdict === 'reject' ? <Busy label="Rejecting" /> : 'Reject'}
              </Button>
            </div>
            {rule.isError && (
              <p className="text-[11px] text-red">{(rule.error as Error).message}</p>
            )}
          </>
        )}
      </div>
    </div>
  )
}
