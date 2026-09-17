/**
 * The Statement — Observe's landing tab. The screen a data-platform owner
 * takes to their boss: what was asked, what came back, how much of it carried
 * receipts, what it cost, per lens. Reads in five seconds, degrades
 * gracefully into detail; every figure traces to request_log rows.
 *
 * Vocabulary is inherited from observe or the page lies: a decline is not an
 * error, deltas exist only when both windows carried traffic, and admin SQL
 * never counts as governed usage. No comparisons to numbers we never
 * measured.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Bar,
  BarChart,
  LabelList,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from 'recharts'
import { getToken } from '../api/auth'
import { useAuditStatement, type AuditLensRow, type AuditStatement } from '../api/observe'
import { InfoHint } from '../components/ui/InfoHint'
import { Skeleton } from '../components/ui/Skeleton'
import { formatCost } from '../lib/format'

const WINDOWS = [7, 30, 90] as const
const EXPANDED_ROWS = 8

function Delta({ pp }: { pp: number | null }) {
  if (pp === null) return null // no prior traffic — a delta would be invented
  const up = pp >= 0
  return (
    <div
      className={['font-mono text-[11px] tabular-nums', up ? 'text-green' : 'text-red'].join(' ')}
    >
      {up ? '▲' : '▼'} {up ? '+' : ''}
      {pp.toFixed(1)} pp vs prior window
    </div>
  )
}

const fmtDay = (day: string, long = false) =>
  new Date(`${day}T00:00:00`).toLocaleDateString(undefined, {
    ...(long ? { weekday: 'short' } : {}),
    month: 'short',
    day: 'numeric',
  })

/** The largest round step ({1,2,5}·10^k) at or below the peak — one labeled
 * guide line gives the eye a scale without spending a full axis on it. */
const guideStep = (peak: number) => {
  const pow = 10 ** Math.floor(Math.log10(Math.max(peak, 1)))
  for (const m of [5, 2, 1]) if (m * pow <= peak) return m * pow
  return pow
}

function VolumeTip({ active, payload }: TooltipContentProps) {
  if (!active || !payload?.length) return null
  const p = payload[0].payload as AuditStatement['series'][number]
  return (
    <div
      className="rounded-md bg-accent px-2.5 py-1.5 font-mono text-[11px] text-accent-fg tabular-nums"
      style={{ boxShadow: 'var(--shadow-popover)' }}
    >
      {fmtDay(p.day, true)} · {p.asked} asked
    </div>
  )
}

/** The daily-volume chart — recharts owns the scale, ticks, and hover; the
 * statement styling stays ours. Marks wear the accent token, so the pending
 * ledger-green sweep recolors this chart in the token swap, not a rename. */
function VolumeChart({ series }: { series: AuditStatement['series'] }) {
  if (series.length < 2) {
    return (
      <p className="text-[12px] text-muted mt-2">
        Not enough days in the window to draw a trend yet.
      </p>
    )
  }
  const last = series[series.length - 1]
  const peak = series.reduce((a, b) => (b.asked > a.asked ? b : a))
  const peakIdx = series.findIndex((p) => p.day === peak.day)
  const guide = guideStep(peak.asked)
  // Weekly ticks on the short windows; monthly once Mondays would crowd.
  const ticks =
    series.length > 45
      ? series.filter((p) => p.day.endsWith('-01')).map((p) => p.day)
      : series.filter((p) => new Date(`${p.day}T00:00:00`).getDay() === 1).map((p) => p.day)
  const animate =
    typeof window.matchMedia === 'function'
      ? !window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false
  return (
    <div>
      <div
        className="h-[112px] w-full font-mono"
        role="img"
        aria-label={`Questions per day over the window, peaking at ${peak.asked}`}
      >
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={series} margin={{ top: 14, right: 4, bottom: 0, left: 4 }}>
            <XAxis
              dataKey="day"
              ticks={ticks}
              interval={0}
              tickLine={false}
              axisLine={{ stroke: 'var(--color-text)' }}
              tick={{ fontSize: 10, fill: 'var(--color-muted-2)' }}
              tickFormatter={(d: string) => fmtDay(d)}
            />
            <YAxis hide domain={[0, Math.ceil(peak.asked * 1.15)]} />
            <ReferenceLine
              y={guide}
              stroke="var(--color-border-strong)"
              label={{
                value: `${guide}/day`,
                position: 'insideTopRight',
                fontSize: 9,
                fill: 'var(--color-muted-2)',
              }}
            />
            <Tooltip content={VolumeTip} cursor={{ fill: 'var(--color-surface-3)' }} />
            <Bar
              dataKey="asked"
              fill="var(--color-accent)"
              radius={[2, 2, 0, 0]}
              maxBarSize={9}
              isAnimationActive={animate}
            >
              <LabelList
                dataKey="asked"
                content={({ x, y, width, index }) =>
                  index === peakIdx ? (
                    <text
                      x={Number(x) + Number(width) / 2}
                      y={Number(y) - 5}
                      textAnchor="middle"
                      fontSize={9.5}
                      fontWeight={600}
                      fill="var(--color-text)"
                    >
                      {peak.asked}
                    </text>
                  ) : null
                }
              />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1.5 font-mono text-[11px] text-muted-2 tabular-nums">
        peak {peak.asked}/day ({peak.day.slice(5)}) · latest {last.asked}
      </p>
    </div>
  )
}

/** The three-band split of everything served: verified / caveated / flagged.
 * The honest claim is the shape of the band, not a renamed number — "caveated"
 * is NOT "known not wrong" (a skipped check knows nothing), and the flagged
 * band going out loud is what the system actually earns. */
function ConfidenceBands({ histogram }: { histogram: Record<string, number> }) {
  const verified = histogram['verified'] ?? 0
  const caveated = histogram['partial'] ?? 0
  const flagged = histogram['unverified'] ?? 0
  const total = verified + caveated + flagged
  if (total === 0) return null
  const pctOf = (n: number) => Math.round((100 * n) / total)
  return (
    <div className="mt-3 max-w-[38ch]">
      <div className="flex h-1.5 w-full gap-0.5 overflow-hidden rounded-full" aria-hidden="true">
        {verified > 0 && (
          <span className="bg-green" style={{ width: `${(100 * verified) / total}%` }} />
        )}
        {caveated > 0 && (
          <span className="bg-amber" style={{ width: `${(100 * caveated) / total}%` }} />
        )}
        {flagged > 0 && (
          <span className="bg-red" style={{ width: `${(100 * flagged) / total}%` }} />
        )}
      </div>
      <p className="mt-1.5 font-mono text-[11px] text-muted tabular-nums">
        {pctOf(verified)}% verified · {pctOf(caveated)}% caveated ·{' '}
        <span className={flagged > 0 ? 'text-red' : ''}>
          {flagged.toLocaleString()} flagged
        </span>
      </p>
    </div>
  )
}

/** The ledger's split of everything served: where the figure's meaning came
 * from. Widths cover all five tags so the pre-ledger slice is visible; the
 * legend percentages are over the GRADED four, so they add up with the
 * headline. Nothing here is red — an inferred figure is not an error, and
 * an unknown one is unmeasured, not wrong. */
function ResolutionBands({ histogram }: { histogram: Record<string, number> }) {
  const n = (k: string) => histogram[k] ?? 0
  const certified = n('certified')
  const declared = n('declared')
  const mixed = n('mixed')
  const inferred = n('inferred')
  const unknown = n('unknown')
  const total = certified + declared + mixed + inferred + unknown
  const graded = total - unknown
  if (total === 0) return null
  const pctOf = (v: number) => (graded ? Math.round((100 * v) / graded) : 0)
  const bands: [number, string][] = [
    [certified, 'bg-green'],
    [declared, 'bg-accent'],
    [mixed, 'bg-muted-2'],
    [inferred, 'bg-border-strong'],
    [unknown, 'bg-surface-3'],
  ]
  return (
    <div className="mt-3 max-w-[38ch]">
      <div className="flex h-1.5 w-full gap-0.5 overflow-hidden rounded-full" aria-hidden="true">
        {bands.map(
          ([v, cls]) =>
            v > 0 && <span key={cls} className={cls} style={{ width: `${(100 * v) / total}%` }} />,
        )}
      </div>
      <p className="mt-1.5 font-mono text-[11px] text-muted tabular-nums">
        {pctOf(certified)}% certified · {pctOf(declared)}% declared · {pctOf(mixed)}% mixed ·{' '}
        {pctOf(inferred)}% inferred
        {unknown > 0 && <span className="text-muted-2"> · {unknown.toLocaleString()} unknown</span>}
      </p>
    </div>
  )
}

/** The slots that keep clarifying for want of a value the question could not
 * type. A clarification here is not a fault: the dictionary the resolver
 * chooses from is incomplete, and profiling the column is the lever. */
function DictionaryGaps({ slots }: { slots: AuditStatement['unresolved_slots'] }) {
  if (!slots?.length) return null
  return (
    <div className="mt-4 border-t border-border pt-3">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <span className="panel-label">Dictionary gaps</span>
        <span className="text-[11px] text-muted-2">
          columns that clarified for want of a complete value dictionary — profile them
        </span>
      </div>
      <ul className="mt-2 flex flex-wrap gap-x-5 gap-y-1 font-mono text-[12px] tabular-nums">
        {slots.map((s) => (
          <li key={s.slot} className="text-text">
            {s.slot}
            <span className="ml-1.5 text-muted">×{s.count.toLocaleString()}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function StateChip({ row }: { row: AuditLensRow }) {
  if (row.degraded) {
    return (
      <span
        className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-mono font-semibold uppercase tracking-wide bg-red-bg text-red border border-red/25"
        title={row.degraded}
      >
        degraded
      </span>
    )
  }
  if (row.gate_score !== null && row.gate_score < 1) {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-mono font-semibold uppercase tracking-wide bg-accent-light text-accent-dark border border-accent/30">
        gate {row.gate_score}
      </span>
    )
  }
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded text-[10px] font-mono font-semibold uppercase tracking-wide bg-green-bg text-green border border-green/20">
      healthy
    </span>
  )
}

function LensLedger({ rows }: { rows: AuditLensRow[] }) {
  const [expanded, setExpanded] = useState(false)
  const shown = expanded ? rows : rows.slice(0, EXPANDED_ROWS)
  const rest = rows.slice(EXPANDED_ROWS)
  const restAsked = rest.reduce((n, r) => n + r.asked, 0)
  const restCost = rest.reduce((n, r) => n + r.cost_usd, 0)
  return (
    <table className="w-full tabular-nums">
      <thead>
        <tr className="border-b border-border-strong">
          <th className="panel-label text-left pb-2 font-normal">Lens · owner</th>
          <th className="panel-label text-right pb-2 font-normal">Questions</th>
          <th className="panel-label text-right pb-2 font-normal">Verified</th>
          <th className="panel-label text-right pb-2 font-normal">Declared</th>
          <th className="panel-label text-right pb-2 font-normal">Typed</th>
          <th className="panel-label text-right pb-2 font-normal">Cost</th>
          <th className="panel-label text-right pb-2 font-normal">State</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-border">
        {shown.map((r) => (
          <tr key={r.lens} className="row-hover">
            <td className="py-2 pr-4">
              <Link
                to={`/lenses/${r.lens}`}
                className="font-mono text-[12.5px] font-semibold text-text hover:text-accent focus-ring rounded"
              >
                {r.lens}
              </Link>
              {r.owner && <span className="ml-2 text-[12px] text-muted-2">· {r.owner}</span>}
            </td>
            <td className="py-2 text-right font-mono text-[12.5px]">{r.asked.toLocaleString()}</td>
            <td className="py-2 text-right font-mono text-[12.5px]">
              {r.verified_pct !== null ? `${r.verified_pct}%` : '—'}
            </td>
            <td className="py-2 text-right font-mono text-[12.5px]">
              {r.declared_pct !== null ? `${r.declared_pct}%` : '—'}
            </td>
            <td className="py-2 text-right font-mono text-[12.5px]">
              {r.typed_pct !== null ? `${r.typed_pct}%` : '—'}
            </td>
            <td className="py-2 text-right font-mono text-[12.5px]">{formatCost(r.cost_usd)}</td>
            <td className="py-2 text-right">
              <StateChip row={r} />
            </td>
          </tr>
        ))}
        {!expanded && rest.length > 0 && (
          <tr>
            <td className="py-2 pr-4">
              <button
                onClick={() => setExpanded(true)}
                className="font-mono text-[12px] text-muted hover:text-text focus-ring rounded cursor-pointer"
              >
                {rest.length} more {rest.length === 1 ? 'lens' : 'lenses'} — expand
              </button>
            </td>
            <td className="py-2 text-right font-mono text-[12.5px] text-muted-2">
              {restAsked.toLocaleString()}
            </td>
            <td className="py-2 text-right font-mono text-[12.5px] text-muted-2">—</td>
            <td className="py-2 text-right font-mono text-[12.5px] text-muted-2">—</td>
            <td className="py-2 text-right font-mono text-[12.5px] text-muted-2">—</td>
            <td className="py-2 text-right font-mono text-[12.5px] text-muted-2">
              {formatCost(restCost)}
            </td>
            <td></td>
          </tr>
        )}
      </tbody>
    </table>
  )
}

export function AuditStatementPanel() {
  const hasToken = !!getToken()
  const [days, setDays] = useState<number>(30)
  const q = useAuditStatement(days)
  const d = q.data

  const served = d ? d.confidence_histogram : {}
  const basis = d ? (d.resolution_histogram ?? {}) : {}

  return (
    <div>
      <div className="flex flex-wrap items-center justify-end gap-3">
        <div className="flex items-center gap-2">
          <select
            aria-label="Window"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="rounded-md border border-border bg-surface px-2 py-1 font-mono text-[12px] text-text focus-ring"
          >
            {WINDOWS.map((w) => (
              <option key={w} value={w}>
                last {w} days
              </option>
            ))}
          </select>
          <button
            onClick={() => window.print()}
            className="rounded-md border border-border bg-surface px-2.5 py-1 font-mono text-[12px] text-text hover:bg-surface-2 focus-ring cursor-pointer"
          >
            Print
          </button>
        </div>
      </div>

      {!hasToken && (
        <p className="mt-4 text-[13px] text-muted">
          Set your admin token (top-right) to load the audit.
        </p>
      )}

      {hasToken && q.isLoading && (
        <div className="mt-5 space-y-3">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      )}
      {hasToken && q.isError && (
        <p className="mt-4 text-[13px] text-red">{String(q.error)}</p>
      )}

      {d && (
        <>
          {/* ── The four headline figures + the trend. Two-up at sm (the
              chart takes its own row), five across at xl. ── */}
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_1fr_1fr_1.35fr] border-b border-border-strong">
            <div className="py-5 pr-6">
              <div className="panel-label flex items-center gap-1.5">
                Answer yield
                <InfoHint>
                  Declines are governance working, not failing: refusals and clarifications
                  lower yield by design. Every figure traces to a request id.
                </InfoHint>
              </div>
              <div className="mt-2 font-mono text-[42px] font-semibold leading-none tracking-tight tabular-nums">
                {d.yield_pct !== null ? d.yield_pct : '—'}
                <span className="text-[20px] text-muted-2 font-medium">%</span>
              </div>
              <Delta pp={d.yield_delta_pp} />
              <p className="mt-2 text-[12px] text-muted max-w-[34ch]">
                {d.answered.toLocaleString()} of {d.asked.toLocaleString()} questions served a
                governed answer.
              </p>
            </div>
            <div className="py-5 pr-6 sm:pl-6 sm:border-l border-border">
              <div className="panel-label flex items-center gap-1.5">
                Verified coverage
                <InfoHint>
                  Verified means every check ran and passed — coverage grows as declarations
                  complete. Caveated answers say on their face what couldn&apos;t be confirmed;
                  flagged ones failed a core check and were ticketed. Nothing is wrong silently.
                </InfoHint>
              </div>
              <div className="mt-2 font-mono text-[42px] font-semibold leading-none tracking-tight tabular-nums">
                {d.verified_pct !== null ? d.verified_pct : '—'}
                <span className="text-[20px] text-muted-2 font-medium">%</span>
              </div>
              <Delta pp={d.verified_delta_pp} />
              <ConfidenceBands histogram={served} />
            </div>
            <div className="py-5 pr-6 sm:border-t xl:border-t-0 xl:pl-6 xl:border-l border-border">
              <div className="panel-label flex items-center gap-1.5">
                Governed basis
                <InfoHint>
                  Declared means the figure&apos;s meaning is a metric or definition in the
                  semantic model; inferred means the model computed over raw columns it chose;
                  unknown means the answer was recorded before the ledger existed.
                </InfoHint>
              </div>
              <div className="mt-2 font-mono text-[42px] font-semibold leading-none tracking-tight tabular-nums">
                {d.declared_pct !== null ? d.declared_pct : '—'}
                <span className="text-[20px] text-muted-2 font-medium">%</span>
              </div>
              <Delta pp={d.declared_delta_pp} />
              <p className="mt-2 text-[12px] text-muted max-w-[34ch]">
                {((basis['certified'] ?? 0) + (basis['declared'] ?? 0)).toLocaleString()} of{' '}
                {(d.answered - d.resolution_unknown).toLocaleString()} graded answers computed
                from a certified or declared definition.
              </p>
              {d.resolution_unknown > 0 && (
                <p className="mt-1 text-[12px] text-muted-2 max-w-[34ch]">
                  {d.resolution_unknown.toLocaleString()} answers predate the ledger — not graded.
                </p>
              )}
              <ResolutionBands histogram={basis} />
            </div>
            <div className="py-5 pr-6 sm:pl-6 sm:border-l sm:border-t xl:border-t-0 border-border">
              <div className="panel-label flex items-center gap-1.5">
                Typed
                <InfoHint>
                  Typed means every slot of the answer — metric, grain, filters, window — was a
                  closed-set decision the policy acted on, a deterministic parse, or a value the
                  caller supplied; no raw-SQL generation ran. Predating rows are counted apart,
                  never as untyped.
                </InfoHint>
              </div>
              <div className="mt-2 font-mono text-[42px] font-semibold leading-none tracking-tight tabular-nums">
                {d.typed_pct !== null ? d.typed_pct : '—'}
                <span className="text-[20px] text-muted-2 font-medium">%</span>
              </div>
              <Delta pp={d.typed_delta_pp} />
              {/* The payload carries the share, not the count — the share is
                  printed over its denominator rather than a count rounded
                  back out of it. */}
              <p className="mt-2 text-[12px] text-muted max-w-[34ch]">
                {d.typed_pct !== null
                  ? `${d.typed_pct}% of ${(d.answered - d.typed_unknown).toLocaleString()} answers typed end to end.`
                  : 'No answers served since typed serving existed.'}
              </p>
              {d.typed_unknown > 0 && (
                <p className="mt-1 text-[12px] text-muted-2 max-w-[34ch]">
                  {d.typed_unknown.toLocaleString()} answers predate typed serving.
                </p>
              )}
            </div>
            <div className="py-5 sm:col-span-2 sm:border-t xl:col-span-1 xl:border-t-0 xl:pl-6 xl:border-l border-border">
              <div className="panel-label">Questions per day</div>
              <div className="mt-2">
                <VolumeChart series={d.series} />
              </div>
            </div>
          </div>

          {/* ── The honest outcome split + cost. A grid with hairline gaps:
              seven cells fit or wrap at declared breakpoints, and the gap-px
              trick draws clean separators in both directions on every layout —
              no orphan mid-row borders (flex-wrap), no content silently cut
              off-screen (nowrap + scroll). Both were tried; both misled. ── */}
          <div className="grid grid-cols-2 gap-px border-y border-border-strong bg-border sm:grid-cols-4 xl:grid-cols-7">
            {(
              [
                ['Answered', d.answered, 'served with receipts'],
                ['Clarified', d.clarified, 'asked which meaning — not errors'],
                ['Refused', d.refused, 'governed boundary held'],
                [
                  'Flagged',
                  served['unverified'] ?? 0,
                  'served, but a figure could not be traced to the result rows',
                ],
                ['Faults', d.faults, 'dst defects — each one ticketed'],
                [
                  'Spend',
                  // Unpriced calls make this a floor, not a total — a
                  // confident "$0.00" over uncounted spend is the failure
                  // this trailing + exists to prevent.
                  (d.unpriced ?? 0) > 0
                    ? `${formatCost(d.ai_cost_usd + d.wh_cost_usd)}+`
                    : `${formatCost(d.ai_cost_usd + d.wh_cost_usd)}`,
                  (d.unpriced ?? 0) > 0
                    ? `${d.unpriced} call(s) unpriced — spend uncounted, not $0`
                    : `${formatCost(d.ai_cost_usd)} AI · ${formatCost(d.wh_cost_usd)} warehouse`,
                ],
                [
                  'Per answer',
                  d.cost_per_answer_usd !== null ? formatCost(d.cost_per_answer_usd) : '—',
                  'blended AI + warehouse',
                ],
              ] as const
            ).map(([label, value, sub]) => (
              <div key={label} className="bg-bg px-5 py-3.5 min-w-0">
                <div className="panel-label">{label}</div>
                <div
                  className={[
                    'mt-1 font-mono text-[18px] font-semibold tabular-nums',
                    (label === 'Faults' || label === 'Flagged') && Number(value) > 0
                      ? 'text-red'
                      : '',
                  ].join(' ')}
                >
                  {typeof value === 'number' ? value.toLocaleString() : value}
                </div>
                <div className="mt-0.5 text-[11px] text-muted-2">{sub}</div>
              </div>
            ))}
          </div>

          {/* ── Per-lens ledger ── */}
          <div className="mt-4">
            <LensLedger rows={d.lenses} />
          </div>

          {/* ── The profiling backlog: slots that clarified for want of a dictionary ── */}
          <DictionaryGaps slots={d.unresolved_slots} />

          {/* ── The trail line ── */}
          <div className="mt-4 flex flex-wrap items-baseline justify-between gap-3 border-t border-border pt-3">
            <span className="font-mono text-[11px] text-muted-2 tabular-nums">
              certified answers <span className="text-text">{d.certified_active}</span> · open
              incident tickets{' '}
              <span className={d.open_incident_tickets > 0 ? 'text-accent-dark' : 'text-text'}>
                {d.open_incident_tickets}
              </span>{' '}
              · admin SQL audited separately, never counted here
            </span>
            <span className="panel-label">
              every figure traces to request ids — see Cost &amp; requests
            </span>
          </div>
        </>
      )}
    </div>
  )
}
