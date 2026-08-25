import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { getToken } from '../api/auth'
import { useCallers, useKeys, useRevokeKey, type Caller } from '../api/callers'
import { Badge } from '../components/ui/Badge'
import { Skeleton } from '../components/ui/Skeleton'
import { Page, PageHeader } from '../components/ui/Page'

// ─── keys table for a single caller ─────────────────────────────────────────
// Listing + revocation only: revoking a leaked key is incident response and stays
// in the UI. MINTING is CLI-only (`dst keys create <caller>`) — authoring a
// credential belongs to the file/CLI loop, and the key is shown once there.
function CallerKeys({ name }: { name: string }) {
  const qc = useQueryClient()
  const keys = useKeys(name)
  const revoke = useRevokeKey(name)
  const refresh = () => qc.invalidateQueries({ queryKey: ['keys', name] })

  return (
    <div className="mt-2 border-t border-border pt-3">
      {keys.isLoading && (
        <div className="space-y-1.5 pb-1">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/4" />
        </div>
      )}

      {keys.data && keys.data.length > 0 && (
        <div className="mb-2 divide-y divide-border overflow-hidden rounded-md border border-border">
          <div className="grid grid-cols-[1fr_auto_auto_auto] gap-3 border-b border-border bg-surface-2 px-3 py-1.5">
            {['Key prefix', 'Issued', 'Status', ''].map((h) => (
              <span key={h} className="text-[10px] font-medium uppercase tracking-wider text-muted">
                {h}
              </span>
            ))}
          </div>
          {keys.data.map((k) => (
            <div
              key={k.id}
              className="grid grid-cols-[1fr_auto_auto_auto] items-center gap-3 bg-surface px-3 py-2 hover:bg-surface-2 transition-colors"
              style={{ transitionDuration: 'var(--duration-fast)' }}
            >
              <span className="font-mono text-[12px] text-text tabular-nums">
                {k.prefix}<span className="text-muted-2">…</span>
              </span>
              <span className="text-[12px] text-muted tabular-nums">
                {new Date(k.created_at).toLocaleDateString()}
              </span>
              <span>
                {k.revoked ? (
                  <Badge variant="default">revoked</Badge>
                ) : (
                  <Badge variant="success" dot>active</Badge>
                )}
              </span>
              <span className="w-12 text-right">
                {!k.revoked && (
                  <button
                    type="button"
                    onClick={() => revoke.mutate(k.id, { onSuccess: refresh })}
                    className={[
                      'rounded px-1.5 py-0.5 text-[11px] text-muted transition-colors',
                      'hover:text-red hover:bg-red-bg',
                      'outline-none focus-visible:ring-2 focus-visible:ring-red/50',
                    ].join(' ')}
                    style={{ transitionDuration: 'var(--duration-fast)' }}
                  >
                    Revoke
                  </button>
                )}
              </span>
            </div>
          ))}
        </div>
      )}

      {keys.data && keys.data.length === 0 && (
        <p className="mb-1 text-[12px] text-muted">
          No keys yet — mint one with{' '}
          <code className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono text-[11px] text-text">
            dst keys create --caller {name}
          </code>
          .
        </p>
      )}
    </div>
  )
}

// ─── one collapsed caller row ───────────────────────────────────────────────
function CallerRow({ caller }: { caller: Caller }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="bg-surface">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className={[
          'flex w-full items-center gap-2.5 px-4 py-2.5 text-left cursor-pointer',
          'transition-colors hover:bg-surface-2',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent',
        ].join(' ')}
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
        <span className="font-mono text-[13px] font-semibold text-text">{caller.name}</span>
        <Badge variant="default">{caller.type}</Badge>
        {caller.groups?.length > 0 && (
          <span className="flex min-w-0 flex-wrap gap-1">
            {caller.groups.map((g) => (
              <span
                key={g}
                className="inline-flex items-center rounded border border-accent/30 bg-accent-fg px-2 py-0.5 font-mono text-[11px] text-accent-dark"
              >
                {g}
              </span>
            ))}
          </span>
        )}
      </button>
      {open && (
        <div className="px-4 pb-3">
          <CallerKeys name={caller.name} />
        </div>
      )}
    </div>
  )
}

// ─── main page ───────────────────────────────────────────────────────────────
export function Settings() {
  const hasToken = !!getToken()
  const callers = useCallers()

  return (
    <Page width="prose">
      <PageHeader
        title="Settings"
        description="Callers and their API keys. Keys are minted from the CLI — revoke a compromised one here."
      />

      {/* Auth notice */}
      {!hasToken && (
        <div className="mt-4 flex items-center gap-3 rounded-md border border-border bg-surface-2 px-4 py-3">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true" className="shrink-0 text-muted">
            <circle cx="7" cy="7" r="6" stroke="currentColor" strokeWidth="1.25" />
            <path d="M7 4.5v3M7 9.5v.5" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
          </svg>
          <p className="text-[13px] text-muted">Set your admin token (top-right) to manage callers.</p>
        </div>
      )}

      {/* Section header */}
      <h2 className="mt-7 text-[11px] font-bold uppercase tracking-wider text-muted">
        Callers
      </h2>
      <p className="mt-1.5 text-[13px] text-muted">
        Every caller and its keys, for audit and revocation. Create callers and mint keys from
        the CLI:{' '}
        <code className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-text">
          dst keys create --caller &lt;name&gt;
        </code>{' '}
        — the key is shown once there.
      </p>

      {/* Callers list — collapsed rows; a caller's keys load when its row opens,
          so fifty callers stay one screen and one query, not fifty cards. */}
      <div className="mt-5">
        {callers.isLoading && (
          <div className="space-y-1.5">
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-3/4" />
          </div>
        )}

        {callers.data && callers.data.length > 0 && (
          <div
            className="divide-y divide-border overflow-hidden rounded-md border border-border"
            style={{ boxShadow: 'var(--shadow-card)' }}
          >
            {callers.data.map((c) => (
              <CallerRow key={c.id} caller={c} />
            ))}
          </div>
        )}

        {callers.data && callers.data.length === 0 && (
          <p className="text-[13px] text-muted">
            No callers yet —{' '}
            <code className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-text">
              dst keys create --caller my_agent
            </code>{' '}
            creates one and mints its first key.
          </p>
        )}
      </div>
    </Page>
  )
}
