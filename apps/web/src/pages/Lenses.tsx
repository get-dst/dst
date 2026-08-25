import { Link } from 'react-router-dom'
import { getToken } from '../api/auth'
import { useActivation, type Activation } from '../api/activation'
import { useLenses, type LensSummary } from '../api/lenses'
import { Page, PageHeader } from '../components/ui/Page'
import { Readout } from '../components/ui/Readout'

// ─── Status badge ──────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const isLive = status === 'live'
  const isDraft = status === 'draft'

  if (isLive) {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-medium uppercase tracking-wide bg-green-bg text-green border border-green/20">
        <span className="h-1.5 w-1.5 rounded-full bg-green inline-block" aria-hidden="true" />
        Live
      </span>
    )
  }

  if (isDraft) {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-[11px] font-medium uppercase tracking-wide bg-surface-2 text-muted border border-border">
        Draft
      </span>
    )
  }

  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium uppercase tracking-wide bg-surface-2 text-muted border border-border">
      {status}
    </span>
  )
}

/** Quiet provenance tag: this lens was created through the API, never applied
    from files — `dst plan` lists it server-only until it is adopted with
    `dst export --lens <name>`. */
function NotInFilesTag() {
  return (
    <span
      className="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium uppercase tracking-wide bg-surface-2 text-muted-2 border border-dashed border-border"
      title="Never applied from files — adopt with dst export --lens <name>"
    >
      not in files
    </span>
  )
}

// ─── Lens row card ──────────────────────────────────────────────────────────────

function relativeTime(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(ms / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  return new Date(iso).toLocaleDateString()
}

/** The structural stats of a lens, e.g. "6 tables · 4 definitions · 2 questions". */
function shapeStats(lens: LensSummary): string[] {
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`
  const parts: string[] = []
  if (lens.entity_count > 0) parts.push(plural(lens.entity_count, 'table'))
  if (lens.definition_count > 0) parts.push(plural(lens.definition_count, 'definition'))
  if (lens.question_count > 0) parts.push(plural(lens.question_count, 'accepted question'))
  return parts
}

/**
 * One ledger row: weight follows evidence. A lens that answers queries gets its
 * description and usage at full height; a quiet or empty one compresses to a
 * single dense line. Unequal treatment IS the information — a uniform grid
 * would render a heavily-used lens identically to an empty one.
 */
function LensRow({ lens }: { lens: LensSummary }) {
  const stats = shapeStats(lens)
  const active = lens.query_count > 0
  return (
    <Link
      to={`/lenses/${lens.name}`}
      className={[
        'group block row-hover px-4 outline-none',
        active ? 'py-3' : 'py-2',
        'focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset',
      ].join(' ')}
    >
      {/* Baseline: name + status + usage on one shared line */}
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0 flex items-center gap-3">
          <span
            className="font-mono text-[13px] font-semibold text-text group-hover:text-accent"
            style={{ transition: 'color var(--duration-fast)' }}
          >
            {lens.name}
          </span>
          {lens.display_name && lens.display_name !== lens.name && (
            <span className="text-[13px] text-muted truncate">{lens.display_name}</span>
          )}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <span className="font-mono text-[11px] text-muted-2 tabular-nums">
            {active ? (
              <>
                <span className="text-text">{lens.query_count.toLocaleString()}</span>{' '}
                {lens.query_count === 1 ? 'query' : 'queries'}
                {lens.last_queried_at && <> · {relativeTime(lens.last_queried_at)}</>}
              </>
            ) : (
              'no queries'
            )}
          </span>
          {lens.from_files === false && <NotInFilesTag />}
          <StatusBadge status={lens.status} />
        </div>
      </div>

      {/* Earned rows: description + shape only where there is activity to explain */}
      {active && lens.description && (
        <p className="mt-1 max-w-[72ch] text-[12.5px] text-muted leading-relaxed line-clamp-2">
          {lens.description}
        </p>
      )}
      {active && stats.length > 0 && (
        <p className="mt-1 font-mono text-[11px] text-muted-2 truncate">{stats.join(' · ')}</p>
      )}
    </Link>
  )
}

// ─── Skeleton loading ───────────────────────────────────────────────────────────

function LensSkeleton() {
  return (
    <div className="bg-surface border border-border rounded-lg px-4 py-3 flex items-center justify-between gap-4">
      <div className="flex-1 space-y-2">
        <div className="skeleton h-3.5 w-36 rounded" />
        <div className="skeleton h-3 w-56 rounded" />
      </div>
      <div className="skeleton h-5 w-12 rounded" />
    </div>
  )
}

// ─── Empty state ────────────────────────────────────────────────────────────────

function EmptyState() {
  return (
    <div className="mt-6 rounded-lg border border-dashed border-border-strong bg-surface px-6 py-10 text-center">
      {/* Lens glyph */}
      <svg
        className="mx-auto mb-3 text-muted-2"
        width="28"
        height="28"
        viewBox="0 0 28 28"
        fill="none"
        aria-hidden="true"
      >
        <circle cx="14" cy="14" r="9" stroke="currentColor" strokeWidth="1.5" />
        <line x1="14" y1="6"  x2="14" y2="9"  stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <line x1="14" y1="19" x2="14" y2="22" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <line x1="6"  y1="14" x2="9"  y2="14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <line x1="19" y1="14" x2="22" y2="14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <circle cx="14" cy="14" r="2.5" stroke="currentColor" strokeWidth="1.5" />
      </svg>
      <p className="text-[14px] font-medium text-text">No lenses yet</p>
      <p className="mx-auto mt-1 max-w-[52ch] text-[13px] leading-relaxed text-muted">
        Lenses are authored in files and landed with{' '}
        <code className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-text">
          dst apply
        </code>
        {' '}— never here. Start from your project&apos;s{' '}
        <code className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-text">
          AGENTS.md
        </code>
        , explore the warehouse with{' '}
        <code className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-text">
          dst introspect
        </code>
        , or let the scaffolded{' '}
        <span className="font-mono text-[12px] text-text">dst-semantic</span> skill drive the
        loop.
      </p>
    </div>
  )
}

// ─── No-token notice ────────────────────────────────────────────────────────────

function NoTokenNotice() {
  return (
    <div className="mt-4 flex items-start gap-3 rounded-lg border border-border bg-surface px-4 py-3">
      <svg
        className="mt-0.5 h-4 w-4 shrink-0 text-accent"
        viewBox="0 0 16 16"
        fill="none"
        aria-hidden="true"
      >
        <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.25" />
        <line x1="8" y1="5" x2="8" y2="9" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
        <circle cx="8" cy="11.5" r="0.75" fill="currentColor" />
      </svg>
      <p className="text-[13px] text-muted">
        Paste your admin token (minted by{' '}
        <code className="font-mono text-[12px] bg-surface-2 px-1 py-0.5 rounded border border-border">
          dst bootstrap
        </code>
        ) in the top-right to load lenses.
      </p>
    </div>
  )
}

// ─── Activation banner ────────────────────────────────────────────────────────
// The guided "next step" surface — names the single next action with its real
// payload, derived from /mgmt/activation. An activated org sees nothing.

/** Each non-activated step → its headline + the one CTA that advances the funnel. */
function activationCta(a: Activation): { headline: React.ReactNode; cta: { to: string; label: string } | null } | null {
  const conn = a.focus_connection
  switch (a.step) {
    case 'empty':
      return {
        headline: <>Declare a warehouse connection to get started.</>,
        cta: { to: '/data-sources', label: 'Declare a connection' },
      }
    case 'auditing':
      return {
        headline: (
          <>
            Scanning {conn ? <span className="font-mono text-text">{conn}</span> : 'your warehouse'}
            &apos;s query history for contradictions…
          </>
        ),
        cta: { to: '/audit', label: 'View the audit' },
      }
    case 'findings_ready':
      return {
        headline: (
          <>
            <span className="font-semibold text-text">{a.findings}</span>{' '}
            {a.findings === 1 ? 'contradiction' : 'contradictions'} found
            {conn && <> in <span className="font-mono text-text">{conn}</span></>}.
          </>
        ),
        cta: { to: '/audit', label: 'Review them' },
      }
    case 'connected':
      return {
        headline: (
          <>
            No contradictions found — author your first lens in files and land it with{' '}
            <span className="font-mono text-text">dst apply</span>.
          </>
        ),
        cta: null,
      }
    case 'drafting':
      return {
        headline: <>You have a draft lens — open it below to review and publish.</>,
        cta: null,
      }
    default:
      return null
  }
}

function ActivationBanner() {
  const { data } = useActivation()
  if (!data || data.step === 'activated') return null
  const surfaced = activationCta(data)
  if (!surfaced) return null
  const working = data.step === 'auditing'

  return (
    <div className="mt-4 flex items-center gap-3 rounded-lg border border-accent/30 bg-accent-fg px-4 py-3">
      {working ? (
        <span
          className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-accent/30 border-t-accent"
          aria-hidden="true"
        />
      ) : (
        <svg
          className="h-4 w-4 shrink-0 text-accent-dark"
          viewBox="0 0 16 16"
          fill="none"
          aria-hidden="true"
        >
          <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.25" />
          <path d="M5.5 8l1.75 1.75L10.5 6.25" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      )}
      <p className="min-w-0 flex-1 text-[13px] leading-relaxed text-text">{surfaced.headline}</p>
      {surfaced.cta && (
        <Link
          to={surfaced.cta.to}
          className={[
            'shrink-0 inline-flex items-center gap-1.5 rounded-md',
            'bg-accent text-accent-fg px-3.5 py-1.5 text-[12.5px] font-medium',
            'hover:bg-accent-dark transition-colors',
            'outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2',
          ].join(' ')}
          style={{ transitionDuration: 'var(--duration-fast)' }}
        >
          {surfaced.cta.label}
          <svg width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <path d="M2.5 6h7M6.5 3l3 3-3 3" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </Link>
      )}
    </div>
  )
}

// ─── Main page ──────────────────────────────────────────────────────────────────

export function Lenses() {
  const hasToken = !!getToken()
  const { data, isLoading, isError, error } = useLenses()

  const showSkeletons = hasToken && isLoading
  const showError = hasToken && isError
  const showEmpty = hasToken && !isLoading && !isError && data && data.length === 0
  const showList = hasToken && !isLoading && !isError && data && data.length > 0

  const live = data?.filter((l) => l.status === 'live').length ?? 0
  const queries = data?.reduce((n, l) => n + l.query_count, 0) ?? 0

  return (
    <Page width="data">
      <PageHeader
        title="Lenses"
        description="One governed door per consumer. Every answer through it is grounded, verified, and priced."
        readout={
          showList ? (
            <Readout
              items={[
                { value: `${data.length} ${data.length === 1 ? 'lens' : 'lenses'}` },
                { value: `${live} live` },
                { label: 'queries', value: queries.toLocaleString() },
              ]}
            />
          ) : undefined
        }
      />

      {/* ── Activation: the guided next step ── */}
      {hasToken && <ActivationBanner />}

      {/* ── States ── */}
      {!hasToken && <NoTokenNotice />}

      {showError && (
        <p className="text-[13px] text-red-600 mt-2">{String(error)}</p>
      )}

      {/* Skeleton list */}
      {showSkeletons && (
        <div className="space-y-2" aria-label="Loading lenses…">
          {[1, 2, 3].map((i) => (
            <LensSkeleton key={i} />
          ))}
        </div>
      )}

      {/* The ledger: one container, hairline dividers, full-bleed rules —
          shared column baselines rather than a tray of cards */}
      {showList && (
        <>
          <div className="flex items-center justify-between px-4 mb-1.5">
            <span className="panel-label">Lens</span>
            <span className="panel-label">Usage · Status</span>
          </div>
          <div className="border-y border-border-strong divide-y divide-border">
            {data.map((l) => (
              <LensRow key={l.name} lens={l} />
            ))}
          </div>
        </>
      )}

      {/* Empty state */}
      {showEmpty && <EmptyState />}
    </Page>
  )
}
