/**
 * Drift audit — the wrongness probe as STANDING RESULTS. Lives as a tab of the
 * Certify page (the finder that feeds certification); /audit deep-links to it.
 *
 * Audits happen regularly; their findings sit here to be examined. The page
 * default-loads the latest PERSISTED run for the selected connection (GET
 * …/audit/latest) and presents it as standing findings under a "Last refreshed
 * <when> · <N>-day window · <M> statements scanned" header. Re-running is a
 * SECONDARY gesture: a "Refresh" button (re-run + persist, ignoring any schedule)
 * and a window selector (re-run over a different timeframe) sit to the side, not
 * as the hero. Empty state when a connection has never been audited.
 *
 * dst mines the warehouse's OWN query history for definition drift — the same
 * business metric computed N different ways — and lays the findings out as
 * numbered exhibits (the Definition Drift Report's visual language, in app
 * components): exhibit kickers, the diverging numbers large and tabular-mono,
 * the SQL small, severity chips, principal/blast-radius attribution.
 *
 * Each conflict exhibit closes with the bridge: "Draft definition → <lens>",
 * which records a ticket-less definition PatchCandidate on the chosen lens —
 * the SAME approval rail as corrections and distillation (Reviews).
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useConnections } from '../api/connections'
import { useLenses } from '../api/lenses'
import {
  useAuditSummary,
  useDraftDefinition,
  useLatestAudit,
  useRunAudit,
  type AuditRun,
  type AuditSummary,
  type DriftFinding,
  type DriftVariant,
  type Tier,
} from '../api/audit'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { EmptyState } from '../components/ui/EmptyState'

const WINDOWS = [7, 14, 30, 60, 90] as const
const SCOPE_LINE = 'read-only · statements and metadata only'

/** "just now" / "12 minutes ago" / "3 days ago" — the standing-result freshness. */
function relativeTime(iso: string): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return 'unknown time'
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000))
  if (secs < 45) return 'just now'
  let n = secs / 60
  let unit = 'minute'
  for (const [step, name] of [
    [60, 'hour'],
    [24, 'day'],
    [7, 'week'],
  ] as [number, string][]) {
    if (n < step) break
    n /= step
    unit = name
  }
  n = Math.round(n)
  return `${n} ${unit}${n === 1 ? '' : 's'} ago`
}

// ─── observed-value helpers (mirroring services/probe/report.py) ──────────────

function fmtNumber(value: number): string {
  return Number.isInteger(value)
    ? value.toLocaleString('en-US')
    : value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/** The variant's single comparable number, when its result is exactly that. */
function scalar(v: DriftVariant): number | null {
  const rows = v.observed_rows
  if (rows && rows.length === 1 && rows[0].length === 1 && typeof rows[0][0] === 'number') {
    return rows[0][0]
  }
  return null
}

/** (absolute spread, pct of the lowest reading) across comparable numbers. */
function spread(f: DriftFinding): { abs: string; pct: string | null } | null {
  const values = f.variants.map(scalar).filter((s): s is number => s !== null)
  if (new Set(values).size < 2) return null
  const low = Math.min(...values)
  const delta = Math.max(...values) - low
  return { abs: fmtNumber(delta), pct: low !== 0 ? `${((delta / Math.abs(low)) * 100).toFixed(1)}%` : null }
}

/** The variant the audit proposes as canon. Prefers the convention-aware choice the backend
 * stamped (medallion tier — not a vote); falls back to the agreement rule for
 * findings that carry no canon annotation. */
function canonIndex(f: DriftFinding): number {
  if (f.canon_index != null && f.canon_index >= 0 && f.canon_index < f.variants.length) {
    return f.canon_index
  }
  const groups = new Map<number, number[]>()
  f.variants.forEach((v, i) => {
    const s = scalar(v)
    if (s !== null) groups.set(s, [...(groups.get(s) ?? []), i])
  })
  if (groups.size === 0) return 0 // no comparable numbers — most-run variant (sorted first)
  let best: number[] = []
  let bestKey: [number, number, number] = [-1, -1, -1]
  for (const indices of groups.values()) {
    const runs = indices.reduce((acc, i) => acc + f.variants[i].run_count, 0)
    const key: [number, number, number] = [indices.length, runs, -Math.min(...indices)]
    if (key[0] > bestKey[0] || (key[0] === bestKey[0] && (key[1] > bestKey[1] || (key[1] === bestKey[1] && key[2] > bestKey[2])))) {
      bestKey = key
      best = indices
    }
  }
  return best.reduce((a, b) => (f.variants[b].run_count > f.variants[a].run_count ? b : a))
}

const variantLabel = (i: number) => (i < 26 ? String.fromCharCode(97 + i) : String(i + 1))

const n = (count: number, singular: string, plural?: string) =>
  `${count} ${count === 1 ? singular : (plural ?? singular + 's')}`

const pct = (v: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`)

// ─── the accuracy KPI band — the data leader's headline ───────────────────────

/** Color the accuracy number the way Observe does: green ≥90, amber ≥70, red below. */
function accuracyTone(score: number | null): string {
  if (score == null) return 'text-muted-2'
  if (score >= 0.9) return 'text-green'
  if (score >= 0.7) return 'text-amber'
  return 'text-red'
}

function Kpi({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string
  sub: string
  tone?: string
}) {
  return (
    <div className="border border-border bg-surface px-4 py-3.5">
      <div className="font-mono text-[9.5px] font-semibold uppercase tracking-[0.12em] text-muted-2">
        {label}
      </div>
      <div
        className={[
          'mt-2 font-mono text-[28px] font-semibold leading-none tracking-tight tabular-nums',
          tone ?? 'text-text',
        ].join(' ')}
      >
        {value}
      </div>
      <div className="mt-1.5 font-mono text-[10.5px] text-muted">{sub}</div>
    </div>
  )
}

function AccuracyBand({ summary }: { summary: AuditSummary }) {
  const governedTotal = summary.governed_metrics + summary.ungoverned_metrics
  return (
    <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-3">
      <Kpi
        label="Answer accuracy"
        value={pct(summary.answer_accuracy ?? null)}
        tone={accuracyTone(summary.answer_accuracy ?? null)}
        sub={
          summary.accuracy_status === 'unsupported'
            ? 'not scorable — this connector has no query history'
            : summary.answer_accuracy == null
              ? 'no scored eval runs yet'
              : `across ${n(summary.accuracy_lenses, 'governing lens', 'governing lenses')}`
        }
      />
      <Kpi
        label="Governed coverage"
        value={pct(summary.governed_share)}
        tone={
          summary.governed_share == null
            ? 'text-muted-2'
            : summary.governed_share >= 0.7
              ? 'text-green'
              : summary.governed_share >= 0.4
                ? 'text-amber'
                : 'text-red'
        }
        sub={
          governedTotal === 0
            ? 'no metrics observed yet'
            : `${summary.governed_metrics}/${governedTotal} metrics on a governed lens`
        }
      />
      <Kpi
        label="Open conflicts"
        value={String(summary.conflicts)}
        tone={summary.conflicts === 0 ? 'text-green' : 'text-red'}
        sub={
          summary.conflicts === 0
            ? 'no contradictory definitions'
            : 'one metric, several answers — drift erodes accuracy'
        }
      />
    </div>
  )
}

/** The verdict line, derived from the findings themselves: what they say. */
function verdictText(findings: DriftFinding[]): string {
  const conflicts = findings.filter((f) => f.severity === 'conflict')
  if (conflicts.length === 0) return ''
  const worst = conflicts.reduce((a, b) => (b.blast_radius > a.blast_radius ? b : a))
  const sp = spread(worst)
  const split = sp
    ? `splits ${sp.abs}${sp.pct ? ` — ${sp.pct} of the lowest reading` : ''}`
    : 'is computed several incomparable ways'
  const golden = conflicts.filter(
    (f) => f.canon_index != null && f.variants[f.canon_index]?.tier === 'gold',
  ).length
  const settled =
    golden === 0
      ? 'None yet have a business-ready gold reading to make canon.'
      : golden === conflicts.length
        ? golden === 1
          ? 'It already has a business-ready gold reading to make canon.'
          : `All ${golden} already have a business-ready gold reading to make canon.`
        : `${golden} already ${golden === 1 ? 'has' : 'have'} a gold reading to make canon; ${
            conflicts.length - golden
          } still need a governed definition.`
  const lead =
    conflicts.length === 1
      ? 'One metric is computed more than one way'
      : `${conflicts.length} metrics are computed more than one way`
  return `${lead}. The widest, “${worst.metric_intent}”, ${split}. ${settled}`
}

// ─── small pieces ─────────────────────────────────────────────────────────────

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

const selectClass = [
  'rounded-md border border-border bg-surface px-2.5 py-1.5 font-mono text-[12px] text-text',
  'outline-none transition-colors focus:border-accent focus:ring-2 focus:ring-accent/20',
].join(' ')

// ─── medallion tier coloring: canon reads solid ink, the raw one reads red ───────

const TIER: Record<Tier, { label: string; chip: string }> = {
  gold: { label: 'gold · business-ready', chip: 'border-accent bg-accent text-accent-fg' },
  silver: { label: 'silver · cleaned', chip: 'border-border-strong bg-surface-2 text-muted' },
  bronze: { label: 'bronze · raw', chip: 'border-red/40 bg-red-bg text-red' },
  unknown: { label: '', chip: '' },
}

function TierChip({ tier }: { tier: Tier | null }) {
  if (!tier || tier === 'unknown') return null
  return (
    <span
      className={[
        'inline-block border px-1.5 py-px font-mono text-[9px] font-semibold uppercase tracking-[0.08em]',
        TIER[tier].chip,
      ].join(' ')}
    >
      {TIER[tier].label}
    </span>
  )
}

// ─── the observed column: the number, large — or what stands in for it ────────

function Observed({ variant, isCanon }: { variant: DriftVariant; isCanon: boolean }) {
  const value = scalar(variant)
  const rows = variant.observed_rows
  return (
    <div>
      {value !== null ? (
        <div className="break-words font-mono text-[24px] font-semibold leading-tight tracking-tight text-text tabular-nums">
          {fmtNumber(value)}
        </div>
      ) : variant.observed_error ? (
        <div className="break-words font-mono text-[11px] leading-relaxed text-red">
          failed: {variant.observed_error}
        </div>
      ) : rows && rows.length > 0 ? (
        <div className="font-mono text-[11px] leading-relaxed text-text tabular-nums">
          {rows[0].map((c) => (typeof c === 'number' ? fmtNumber(c) : String(c ?? 'NULL'))).join(' · ')}
          {rows.length > 1 && <span className="text-muted-2"> +{rows.length - 1} more rows</span>}
        </div>
      ) : (
        <div className="pt-1 font-mono text-[11px] text-muted-2">not executed</div>
      )}
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        {isCanon && (
          <span className="inline-block border border-accent/30 bg-accent-fg px-1.5 py-px font-mono text-[9px] font-bold uppercase tracking-[0.1em] text-accent-dark">
            Proposed canon
          </span>
        )}
        <TierChip tier={variant.tier} />
      </div>
    </div>
  )
}

function VariantRow({
  variant,
  label,
  isCanon,
}: {
  variant: DriftVariant
  label: string
  isCanon: boolean
}) {
  const where = [n(variant.run_count, 'run')]
  if (variant.principals.length) where.push('by ' + variant.principals.join(', '))
  if (variant.source_tools.length) where.push('via ' + variant.source_tools.join(', '))
  // The right reading is highlighted; a raw bronze reading is tinted as suspect.
  const accent = isCanon
    ? 'border-l-2 border-l-accent bg-accent-fg/40'
    : variant.tier === 'bronze'
      ? 'border-l-2 border-l-red/40 bg-red-bg/30'
      : 'border-l-2 border-l-transparent'
  return (
    <li className={['border-t border-border py-3.5 pl-3', accent].join(' ')}>
      <div className="font-mono text-[10px] text-muted-2">({label})</div>
      <div className="mt-1 grid gap-x-6 gap-y-2 sm:grid-cols-[200px_minmax(0,1fr)]">
        <Observed variant={variant} isCanon={isCanon} />
        <div className="min-w-0">
          <div className="break-words font-mono text-[12px] leading-relaxed text-text">
            {variant.distinguishing}
          </div>
          <div className="mt-1 font-mono text-[10.5px] text-muted-2">{where.join(' · ')}</div>
          <details className="group mt-2">
            <summary className="cursor-pointer list-none font-mono text-[10.5px] text-accent-dark outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent">
              <span className="group-open:hidden">show SQL</span>
              <span className="hidden group-open:inline">hide SQL</span>
            </summary>
            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words border-l-2 border-border-strong bg-surface-2 px-3 py-2 font-mono text-[10.5px] leading-relaxed text-muted">
              {variant.statement}
            </pre>
          </details>
        </div>
      </div>
    </li>
  )
}

// ─── the bridge: conflict finding → definition patch on a lens ────────────────

function DraftBridge({ connection, finding }: { connection: string; finding: DriftFinding }) {
  const lenses = useLenses()
  const draft = useDraftDefinition()
  const [lens, setLens] = useState('')
  const names = (lenses.data ?? []).map((l) => l.name)
  const selected = lens || names[0] || ''

  if (draft.isSuccess) {
    return (
      <aside className="mt-4 border border-accent/30 bg-accent-fg px-4 py-3">
        <p className="text-[12.5px] text-text">
          Drafted <span className="font-mono font-semibold">“{draft.data.target}”</span> on{' '}
          <span className="font-mono font-semibold">{draft.data.lens}</span> — a candidate on the
          lens&apos;s patch queue, awaiting human approval.
        </p>
        <Link
          to="/reviews"
          className="mt-1.5 inline-flex items-center gap-1 rounded text-[12px] font-medium text-accent-dark outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
        >
          Review in the patch queue
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
      </aside>
    )
  }

  return (
    <aside className="mt-4 border border-accent/30 bg-accent-fg px-4 py-3.5">
      <div className="font-mono text-[10px] font-bold uppercase tracking-[0.14em] text-accent-dark">
        Drafted lens definition
      </div>
      <p className="mt-1.5 max-w-[78ch] text-[12.5px] leading-relaxed text-muted">
        Make one reading canon: dst pre-drafts the definition — the proposed canon plus the
        rejected readings — as a patch candidate on a lens. Approval stays human.
      </p>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <span className="font-mono text-[12px] text-muted">Draft definition →</span>
        <select
          aria-label="Target lens"
          value={selected}
          onChange={(e) => setLens(e.target.value)}
          className={selectClass}
          disabled={names.length === 0}
        >
          {names.length === 0 ? (
            <option value="">no lenses yet</option>
          ) : (
            names.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))
          )}
        </select>
        <Button
          variant="primary"
          size="sm"
          disabled={!selected || draft.isPending}
          onClick={() => draft.mutate({ connection, finding, lens: selected })}
        >
          {draft.isPending ? <Busy label="Drafting" /> : 'Draft definition'}
        </Button>
      </div>
      {draft.isError && (
        <p className="mt-2 text-[11px] text-red">{(draft.error as Error).message}</p>
      )}
    </aside>
  )
}

// ─── audit → lens binding ─────────────────────────────────────────────────────

/** How the finding squares against a declared standard (dbt metric, etc.).
 * Agrees reads green; contradicts reads red (the canon disagrees with your standard);
 * undetermined reads amber ("align by hand"). Nothing when there's no declared canon. */
function CanonMatchChip({ match }: { match: DriftFinding['canon_match'] }) {
  if (!match) return null
  const { term, state } = match
  const cls =
    state === 'agrees'
      ? 'border-green/30 bg-green-bg text-green'
      : state === 'contradicts'
        ? 'border-red/40 bg-red-bg text-red'
        : 'border-amber-strong bg-amber-bg text-amber'
  const text =
    state === 'agrees'
      ? <>matches your declared standard <span className="font-semibold">“{term}”</span></>
      : state === 'contradicts'
        ? <>contradicts your declared standard <span className="font-semibold">“{term}”</span></>
        : <>declared <span className="font-semibold">“{term}”</span> — align by hand</>
  return (
    <span
      className={[
        'inline-flex items-center border px-1.5 py-px font-mono text-[10px] font-medium tracking-[0.02em]',
        cls,
      ].join(' ')}
    >
      {text}
    </span>
  )
}

/** "covered by X" (governed) or "ungoverned" — the finding's lens-binding state. */
function GovernanceChip({ governedBy }: { governedBy: string | null | undefined }) {
  if (governedBy === undefined) return null
  if (governedBy === null) {
    return (
      <span className="border border-border-strong bg-surface-2 px-1.5 py-px font-mono text-[10px] font-medium uppercase tracking-[0.06em] text-muted-2">
        ungoverned
      </span>
    )
  }
  return (
    <span className="border border-green/30 bg-green-bg px-1.5 py-px font-mono text-[10px] font-medium tracking-[0.02em] text-green">
      covered by {governedBy}
    </span>
  )
}

// ─── one finding: a colored summary you drill into ────────────────────────────

function FindingCard({
  finding,
  number,
  connection,
  governedBy,
  expanded,
  onToggle,
}: {
  finding: DriftFinding
  number: number
  connection: string
  governedBy: string | null | undefined
  expanded: boolean
  onToggle: () => void
}) {
  const isConflict = finding.severity === 'conflict'
  const canon = isConflict ? canonIndex(finding) : -1
  const canonVariant = canon >= 0 ? finding.variants[canon] : null
  const sp = spread(finding)
  const principals = [...new Set(finding.variants.flatMap((v) => v.principals))]
  const tools = [...new Set(finding.variants.flatMap((v) => v.source_tools))]
  const meta = [n(finding.variants.length, 'definition'), `blast radius ${finding.blast_radius} runs`]
  if (principals.length) meta.push('run by ' + principals.slice(0, 4).join(', '))
  if (tools.length) meta.push('via ' + tools.slice(0, 3).join(', '))

  const accent = isConflict ? 'border-l-[3px] border-l-red' : 'border-l-[3px] border-l-border-strong'

  return (
    <article className={['mt-3 border border-border bg-surface', accent].join(' ')}>
      {/* The high-level summary — always visible, severity- and tier-colored. */}
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-start gap-3 px-4 py-3.5 text-left outline-none transition-colors hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
      >
        <span className="mt-1 font-mono text-[11px] font-semibold tabular-nums text-muted-2">
          {String(number).padStart(2, '0')}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-[17px] font-semibold leading-tight tracking-tight text-text">
              {finding.metric_intent}
            </h2>
            <Badge variant={isConflict ? 'error' : 'default'}>{finding.severity}</Badge>
            {sp && (
              <span className="border border-amber-strong bg-amber-bg px-1.5 py-px font-mono text-[10px] font-semibold tabular-nums text-amber">
                {sp.abs}
                {sp.pct ? ` · ${sp.pct}` : ''} apart
              </span>
            )}
            <GovernanceChip governedBy={governedBy} />
            <CanonMatchChip match={finding.canon_match} />
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[10.5px] text-muted">
            {canonVariant ? (
              <>
                <span className="text-muted-2">canon →</span>
                <span className="text-text">
                  {canonVariant.source_tables[0] ?? canonVariant.distinguishing}
                </span>
                <TierChip tier={canonVariant.tier} />
              </>
            ) : (
              <span>
                {meta[0]} · {meta[1]}
              </span>
            )}
          </div>
        </div>
        <svg
          className={['mt-1 shrink-0 text-muted transition-transform', expanded ? 'rotate-90' : ''].join(
            ' ',
          )}
          width="12"
          height="12"
          viewBox="0 0 12 12"
          fill="none"
          aria-hidden="true"
        >
          <path
            d="M4 2.5l4 3.5-4 3.5"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {/* The drill-down — variants, the canon rationale, the bridge. */}
      {expanded && (
        <div className="border-t border-border px-4 pb-4 pt-2">
          <div className="font-mono text-[11px] text-muted">{meta.join(' · ')}</div>

          {sp && (
            <div className="mt-3 border border-amber-strong bg-amber-bg px-3.5 py-2 text-[12.5px] text-amber">
              Comparable answers are{' '}
              <b className="font-mono font-semibold tabular-nums">{sp.abs}</b> apart
              {sp.pct && (
                <>
                  {' — '}
                  <b className="font-mono font-semibold tabular-nums">{sp.pct}</b> of the lowest
                  reading
                </>
              )}
              .
            </div>
          )}

          {isConflict && finding.canon_rationale && (
            <p className="mt-3 border-l-2 border-accent bg-accent-fg/50 px-3 py-2 text-[12.5px] leading-relaxed text-text">
              {finding.canon_rationale}
            </p>
          )}

          <ol className="mt-2 list-none">
            {finding.variants.map((v, i) => (
              <VariantRow key={i} variant={v} label={variantLabel(i)} isCanon={i === canon} />
            ))}
          </ol>

          {isConflict ? (
            <DraftBridge connection={connection} finding={finding} />
          ) : (
            <aside className="mt-4 border border-border bg-surface-2 px-4 py-3 text-[12.5px] leading-relaxed text-muted">
              Equivalent definitions in different SQL — consolidate into one certified statement so
              the duplication stops accruing cost and risk.
            </aside>
          )}
        </div>
      )}
    </article>
  )
}

// ─── the panel ────────────────────────────────────────────────────────────────

export function DriftAuditPanel() {
  const connections = useConnections()
  const audit = useRunAudit()
  const [connection, setConnection] = useState('')
  const [days, setDays] = useState<number>(30)
  // Which findings are drilled into — the report opens as a high-level overview.
  const [openFindings, setOpenFindings] = useState<Set<number>>(new Set())
  const toggleFinding = (i: number) =>
    setOpenFindings((prev) => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })

  const names = (connections.data ?? []).map((c) => c.name)
  const selected = connection || names[0] || ''

  // The headline KPIs (accuracy + coverage + conflicts) load independently of a run.
  const summary = useAuditSummary(selected)
  // Default-load the latest PERSISTED run — the audit stands here to be examined.
  const latest = useLatestAudit(selected)
  const standing: AuditRun | null = latest.data?.found ? latest.data : null
  // Prefer a just-run refresh, but only for the connection it was run against.
  const fresh = audit.data && audit.data.connection === selected ? audit.data : null
  const result: AuditRun | null = fresh ?? standing

  const conflicts = result?.findings.filter((f) => f.severity === 'conflict').length ?? 0
  const duplications = (result?.findings.length ?? 0) - conflicts

  const refresh = () => audit.mutate({ connection: selected, days })

  return (
    <div>
      <p className="max-w-prose text-[13px] leading-relaxed text-muted">
        Your warehouse&apos;s own query history, mined for definition drift — the same metric,
        computed different ways. The findings stand here to be examined.
      </p>

      {/* ── Standing-result header: what you're looking at, refresh to the side ── */}
      <div className="mt-4 flex flex-wrap items-center gap-3 border-y border-border-strong py-3">
        <label className="flex items-center gap-2">
          <span className="panel-label">
            Warehouse
          </span>
          <select
            aria-label="Connection"
            value={selected}
            onChange={(e) => setConnection(e.target.value)}
            className={selectClass}
            disabled={names.length === 0}
          >
            {names.length === 0 ? (
              <option value="">no connections</option>
            ) : (
              names.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))
            )}
          </select>
        </label>

        {/* Freshness — the standing-result hero, not a button. */}
        <span className="font-mono text-[11px] text-muted">
          {result
            ? `last refreshed ${relativeTime(result.created_at)} · last ${result.days} days · ${result.records_scanned} statements`
            : latest.isLoading
              ? 'loading…'
              : 'never audited'}
        </span>

        {/* Secondary controls: change the window, or refresh now (ignoring schedule). */}
        <div className="ml-auto flex items-center gap-2">
          <select
            aria-label="History window"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className={selectClass}
          >
            {WINDOWS.map((d) => (
              <option key={d} value={d}>
                last {d} days
              </option>
            ))}
          </select>
          <Button
            variant="secondary"
            size="md"
            disabled={!selected || audit.isPending}
            onClick={refresh}
          >
            {audit.isPending ? <Busy label="Refreshing" /> : 'Refresh'}
          </Button>
        </div>
      </div>

      <p className="mt-2 font-mono text-[10.5px] text-muted-2">{SCOPE_LINE}</p>

      {/* ── The headline: accuracy + governed coverage + open drift ── */}
      {summary.data && (summary.data.has_run || summary.data.answer_accuracy != null) && (
        <AccuracyBand summary={summary.data} />
      )}

      {audit.isPending && (
        <p className="mt-3 font-mono text-[11px] text-muted">
          Reading {days} days of query history and executing the variants read-only — typically
          10–30 s on a live warehouse.
        </p>
      )}

      {audit.isError && (
        <div className="mt-4 rounded-md border border-red/20 bg-red-bg px-4 py-3 text-[13px] text-red">
          {(audit.error as Error).message}
        </div>
      )}

      {connections.data && names.length === 0 && (
        <div className="mt-6">
          <EmptyState
            title="No warehouse connections"
            description="Declare a warehouse in dst.yaml and land it with dst apply first — the audit reads its query-history catalog. The Data sources page shows the snippet to copy."
            action={
              <Link to="/data-sources" className="text-[13px] font-medium text-accent-dark hover:underline">
                Go to Data sources →
              </Link>
            }
          />
        </div>
      )}

      {/* ── Never-audited empty state ── */}
      {!result && !audit.isPending && !latest.isLoading && names.length > 0 && (
        <div className="mt-6">
          <EmptyState
            title="No audit yet for this connection"
            description="Audits stand here once they have run. Refresh to run the first one — findings arrive as numbered exhibits, each conflicting metric with its diverging numbers and who runs which variant."
            action={
              <Button variant="primary" size="md" disabled={!selected} onClick={refresh}>
                Run the first audit
              </Button>
            }
          />
        </div>
      )}

      {/* ── The report ── */}
      {result && (
        <>
          <dl className="mt-5 grid grid-cols-2 gap-x-10 gap-y-3 border-b border-border-strong pb-4 sm:flex sm:flex-wrap">
            {[
              ['Warehouse', result.connection],
              ['Window', `last ${result.days} days`],
              ['Statements scanned', String(result.records_scanned)],
              ['Findings', `${n(conflicts, 'conflict')} · ${n(duplications, 'duplication')}`],
            ].map(([k, v]) => (
              <div key={k}>
                <dt className="font-mono text-[9.5px] font-semibold uppercase tracking-[0.12em] text-muted-2">
                  {k}
                </dt>
                <dd className="mt-0.5 font-mono text-[13px] text-text tabular-nums">{v}</dd>
              </div>
            ))}
          </dl>

          {result.findings.length === 0 ? (
            <div className="mt-5 border-l-[3px] border-accent py-1 pl-5">
              <p className="max-w-[62ch] text-[15px] leading-relaxed text-text">
                No definition drift found in the audited window. Either the definitions agree — or
                the window was too quiet to tell.
              </p>
            </div>
          ) : (
            <>
              {verdictText(result.findings) && (
                <div className="mt-5 border-l-[3px] border-accent py-1 pl-5">
                  <p className="max-w-[68ch] text-[15px] leading-relaxed text-text">
                    {verdictText(result.findings)}
                  </p>
                </div>
              )}
              {result.findings.map((f, i) => (
                <FindingCard
                  key={i}
                  finding={f}
                  number={i + 1}
                  connection={result.connection}
                  governedBy={summary.data?.metric_governance?.[f.metric_intent]}
                  expanded={openFindings.has(i)}
                  onToggle={() => toggleFinding(i)}
                />
              ))}
              <footer className="mt-10 flex flex-wrap justify-between gap-3 border-t border-border-strong pt-3 font-mono text-[10.5px] text-muted-2">
                <span>generated by the dst audit</span>
                <span>{SCOPE_LINE}</span>
              </footer>
            </>
          )}
        </>
      )}
    </div>
  )
}
