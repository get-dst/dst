/**
 * Router — the lens-router's surface area, live.
 *
 * Requests to the lens-less /v1/query land here: surface area (routed / asked), the
 * uncovered-metric gap candidates, and the recent route/decline decisions. This is
 * where you watch surface area move as agents ask.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useSurface } from '../api/router'
import { EmptyState } from '../components/ui/EmptyState'
import { Page, PageHeader } from '../components/ui/Page'
import { Readout } from '../components/ui/Readout'

const WINDOWS = [7, 14, 30, 60, 90] as const
const pct = (n: number | null) => (n === null ? '—' : `${Math.round(n * 100)}%`)
const select =
  'rounded-md border border-border bg-surface px-2 py-1 font-mono text-[12px] text-text'

function rel(iso: string): string {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

export function Router() {
  const [days, setDays] = useState(30)
  const q = useSurface(days)
  const d = q.data

  return (
    <Page width="data">
      <PageHeader
        title="Router"
        description="Questions asked without a lens land here. Surface area is the share the router could place onto a governed lens: the live other half of answer yield."
        readout={
          d ? (
            <Readout
              items={[
                { label: 'surface', value: pct(d.surface_area) },
                { label: 'asked', value: d.asked },
                { label: 'routed', value: d.routed },
              ]}
            />
          ) : undefined
        }
      />

      <div className="flex flex-wrap items-center gap-3 border-y border-border-strong py-3">
        <span className="panel-label">
          Window
        </span>
        <select
          aria-label="Window"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className={select}
        >
          {WINDOWS.map((w) => (
            <option key={w} value={w}>
              last {w} days
            </option>
          ))}
        </select>
        {d && (
          <span className="ml-auto font-mono text-[11px] text-muted">
            {d.asked} asked · {d.routed} routed · {d.declined} declined
          </span>
        )}
      </div>

      {d && d.asked === 0 && (
        <div className="mt-6">
          <EmptyState
            title="No router traffic yet"
            description="Only lens-less asks land here: POST /v1/query, or the MCP route_query tool. Queries that name a lens — the MCP query tool, the CLI, chat completions — go straight to that lens and never touch the router. Empty just means every caller so far knew its lens."
          />
        </div>
      )}

      {d && d.asked > 0 && (
        <>
          {/* Surface area — the headline */}
          <div className="mt-6 flex items-baseline gap-4">
            <span className="font-mono text-[44px] font-semibold tabular-nums text-text">
              {pct(d.surface_area)}
            </span>
            <span className="text-[13px] text-muted">
              surface area · routed / asked
              {d.trend !== null && (
                <span className={d.trend >= 0 ? 'text-green ml-2' : 'text-red ml-2'}>
                  {d.trend >= 0 ? '▲' : '▼'} {pct(Math.abs(d.trend))}
                </span>
              )}
            </span>
          </div>

          {/* Uncovered gaps — what no lens governs */}
          {d.uncovered_clusters.length > 0 && (
            <section className="mt-7">
              <h2 className="font-mono text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-2">
                Uncovered — no lens governs these
              </h2>
              <div className="mt-3 flex flex-wrap gap-2">
                {d.uncovered_clusters.map((c) => (
                  <span
                    key={c.label}
                    title={c.examples.join('\n')}
                    className="rounded-md border border-amber-strong/40 bg-amber-bg px-2.5 py-1 font-mono text-[12px] text-text"
                  >
                    {c.label} <span className="text-muted-2">×{c.count}</span>
                  </span>
                ))}
              </div>
            </section>
          )}

          {/* Recent decisions */}
          <section className="mt-7">
            <h2 className="font-mono text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-2">
              Recent decisions
            </h2>
            <ul className="mt-2 divide-y divide-border">
              {d.recent.map((r) => (
                <li key={r.id} className="flex items-baseline gap-3 py-2 text-[13px]">
                  <span className="w-14 shrink-0 font-mono text-[10.5px] text-muted-2">
                    {rel(r.created_at)}
                  </span>
                  <span className="flex-1 truncate text-text">{r.question}</span>
                  {r.covered ? (
                    <span className="shrink-0 font-mono text-[12px] text-accent-dark">
                      → {r.routed_lens}{' '}
                      <span className="text-muted-2">{r.score.toFixed(2)}</span>
                    </span>
                  ) : (
                    <span className="shrink-0 font-mono text-[12px] text-muted">
                      declined <span className="text-muted-2">{r.score.toFixed(2)}</span>
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>

          <footer className="mt-8 border-t border-border-strong pt-3 font-mono text-[10.5px] text-muted-2">
            <Link to="/audit" className="hover:underline">
              the same gaps, mined from query history → Drift audit
            </Link>
          </footer>
        </>
      )}
    </Page>
  )
}
