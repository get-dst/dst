import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  useConnections,
  useConnectionDependents,
  useDeleteConnection,
  useTestConnection,
} from '../api/connections'
import { ConnectionDeclareGuide } from '../components/ConnectionDeclareGuide'
import { WAREHOUSES } from '../warehouses'
import type { WarehouseSpec } from '../warehouses/types'
import { ConnectionLogo } from '../components/ConnectionLogo'
import { Button } from '../components/ui/Button'
import { Badge } from '../components/ui/Badge'
import { Card } from '../components/ui/Card'
import { Skeleton } from '../components/ui/Skeleton'
import { EmptyState } from '../components/ui/EmptyState'
import { Page, PageHeader } from '../components/ui/Page'
import { ConfirmDialog } from '../components/ui/ConfirmDialog'

// ─── connection card ─────────────────────────────────────────────────────────
function ConnectionCard({
  conn,
  testMsg,
  onTest,
  onDelete,
  testPending,
}: {
  conn: { name: string; type: string; has_secret?: boolean; config?: Record<string, unknown> }
  testMsg: string | undefined
  onTest: () => void
  onDelete: () => void
  testPending: boolean
}) {
  const isOk = testMsg?.startsWith('OK')
  const isErr = testMsg && !isOk
  const access = Array.isArray(conn.config?.access)
    ? (conn.config!.access as string[])
    : ['read']
  const canWrite = access.includes('write')

  return (
    <Card
      elevated
      className="transition-shadow hover:shadow-[var(--shadow-card-hover,var(--shadow-card))]"
      style={{ transitionDuration: 'var(--duration-fast)' }}
    >
      <div className="flex items-center justify-between px-4 py-3.5">
        {/* Left: logo + name + meta */}
        <div className="min-w-0 flex items-center gap-3">
          <div className="shrink-0">
            <ConnectionLogo type={conn.type} size="md" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-[14px] font-bold text-text leading-none">
                {conn.name}
              </span>
              <Badge variant="info">{conn.type}</Badge>
              <Badge variant={canWrite ? 'warning' : 'default'}>
                {canWrite ? 'read + write' : 'read-only'}
              </Badge>
              {conn.has_secret && (
                <Badge variant="success" dot>
                  credential stored
                </Badge>
              )}
            </div>
            {testMsg && (
              <p
                className={[
                  'mt-1.5 text-[12px] font-medium',
                  isOk ? 'text-green' : 'text-red',
                ].join(' ')}
              >
                {testMsg}
              </p>
            )}
          </div>
        </div>

        {/* Right: actions */}
        <div className="flex shrink-0 items-center gap-2 pl-4">
          <Button
            variant="secondary"
            size="sm"
            disabled={testPending}
            onClick={onTest}
          >
            {testPending ? (
              <>
                <span className="h-2.5 w-2.5 rounded-full border-2 border-border border-t-muted animate-spin" />
                Testing…
              </>
            ) : isOk ? (
              <>
                <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true" className="text-green">
                  <circle cx="5.5" cy="5.5" r="4.5" stroke="currentColor" strokeWidth="1.1" />
                  <path d="M3.5 5.5l1.5 1.5 2.5-2.5" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                <span className="text-green">Test</span>
              </>
            ) : isErr ? (
              <>
                <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true" className="text-red">
                  <circle cx="5.5" cy="5.5" r="4.5" stroke="currentColor" strokeWidth="1.1" />
                  <path d="M4 4l3 3M7 4l-3 3" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
                </svg>
                <span className="text-red">Test</span>
              </>
            ) : (
              'Test'
            )}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onDelete}
            className="text-muted hover:text-red hover:bg-red-bg hover:border-red/20"
          >
            Delete
          </Button>
        </div>
      </div>
    </Card>
  )
}

// ─── delete-connection confirmation ────────────────────────────────────────────
// Deleting a warehouse connection pulls the credential out from under every lens
// that queries it, so we name those lenses before letting the operator confirm.
function DeleteConnectionDialog({
  name,
  pending,
  onCancel,
  onConfirm,
}: {
  name: string
  pending: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  const deps = useConnectionDependents(name)
  const lenses = deps.data?.lenses ?? []

  return (
    <ConfirmDialog
      title={
        <>
          Delete connection <span className="font-mono">{name}</span>?
        </>
      }
      confirmLabel="Delete connection"
      destructive
      pending={pending}
      onConfirm={onConfirm}
      onClose={onCancel}
    >
      <p>
        This permanently removes the connection and its stored credential. It can't be undone —
        you'd have to re-add and re-verify it.
      </p>

      {deps.isLoading && (
        <p className="mt-2.5 text-muted-2">Checking which lenses use this connection…</p>
      )}

      {lenses.length > 0 && (
        <div className="mt-3 rounded-md border border-red/20 bg-red-bg px-3 py-2.5">
          <p className="font-semibold text-red">
            {lenses.length} {lenses.length === 1 ? 'lens depends' : 'lenses depend'} on this
            connection
          </p>
          <p className="mt-1 text-[12px] text-muted">
            They'll stop answering anything that hits this warehouse until you repoint them at
            another connection.
          </p>
          <ul className="mt-2 space-y-1.5">
            {lenses.map((l) => (
              <li key={l.name} className="flex items-center gap-2 text-[12px]">
                <span className="font-mono font-bold text-text">{l.name}</span>
                <Badge variant={l.status === 'live' ? 'success' : 'default'}>{l.status}</Badge>
              </li>
            ))}
          </ul>
        </div>
      )}

      {deps.data && lenses.length === 0 && (
        <p className="mt-2.5 text-green">No lenses currently reference this connection.</p>
      )}
    </ConfirmDialog>
  )
}

// ─── main page ───────────────────────────────────────────────────────────────
// Warehouses only — the read/write destinations dst queries. Context sources
// (docs, code, tickets…) live under Settings.
export function DataSources() {
  const conns = useConnections()
  const test = useTestConnection()
  const del = useDeleteConnection()
  const qc = useQueryClient()
  const refresh = () => qc.invalidateQueries({ queryKey: ['connections'] })

  const [activeWarehouse, setActiveWarehouse] = useState<WarehouseSpec | null>(null)
  const [testMsg, setTestMsg] = useState<Record<string, string>>({})
  const [testingConn, setTestingConn] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)

  return (
    <Page width="data">
      <PageHeader
        title="Data sources"
        description="The warehouses dst reads. Read-only: dst writes nothing into them. Every connection, its access level, and its credential, encrypted at rest."
      />

      {/* ── Data connections ─────────────────────────────────────────────── */}
      <section className="mt-6">
        <p className="mt-1 text-[13px] text-muted">
          The warehouses dst queries. Connections are declared in{' '}
          <code className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-text">
            dst.yaml
          </code>{' '}
          and landed with{' '}
          <code className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-text">
            dst apply
          </code>{' '}
          — pick a warehouse for the snippet to copy. Apply probes every credential (connect +
          read) before accepting it.
        </p>

        {/* Declare a connection — warehouse tiles open the per-type declare guide */}
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {WAREHOUSES.map((w) => {
            const active = activeWarehouse?.type === w.type
            return (
              <button
                key={w.type}
                type="button"
                onClick={() => setActiveWarehouse(active ? null : w)}
                aria-pressed={active}
                className={[
                  'group flex flex-col gap-2 rounded-lg border cursor-pointer',
                  'px-3.5 py-3 text-left transition-all',
                  active
                    ? 'border-accent bg-accent-fg'
                    : 'border-border bg-surface hover:border-border-strong hover:bg-surface-2 hover:-translate-y-px hover:shadow-[var(--shadow-card-hover,var(--shadow-card))]',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1',
                ].join(' ')}
                style={{ transitionDuration: 'var(--duration-fast)', boxShadow: 'var(--shadow-card)' }}
              >
                <div className="flex w-full items-center justify-between">
                  <ConnectionLogo type={w.type} size="md" />
                  <span
                    className={[
                      'transition-colors',
                      active ? 'text-accent-dark' : 'text-muted group-hover:text-accent',
                    ].join(' ')}
                    style={{ transitionDuration: 'var(--duration-fast)' }}
                  >
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      {active ? (
                        <path d="M3.5 8h9" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
                      ) : (
                        <path d="M8 3.5v9M3.5 8h9" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
                      )}
                    </svg>
                  </span>
                </div>
                <span
                  className={[
                    'block text-[13px] font-bold transition-colors',
                    active ? 'text-accent-dark' : 'text-text group-hover:text-accent',
                  ].join(' ')}
                  style={{ transitionDuration: 'var(--duration-fast)' }}
                >
                  {w.label}
                </span>
              </button>
            )
          })}
        </div>

        {/* The per-type "declare this connection" help panel */}
        {activeWarehouse && <ConnectionDeclareGuide spec={activeWarehouse} />}

        {/* Connected warehouses */}
        <h3 className="mt-6 text-[11px] font-bold uppercase tracking-wider text-muted-2 border-b border-border pb-1.5">Connected</h3>
        <div className="mt-3 space-y-3">
          {conns.isLoading && (
            <>
              {[...Array(2)].map((_, i) => (
                <div key={i} className="rounded-lg border border-border bg-surface px-4 py-3">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <Skeleton className="h-4 w-4 rounded" />
                      <Skeleton className="h-4 w-36" />
                      <Skeleton className="h-4 w-16" />
                    </div>
                    <div className="flex gap-2">
                      <Skeleton className="h-7 w-14 rounded-md" />
                      <Skeleton className="h-7 w-16 rounded-md" />
                    </div>
                  </div>
                </div>
              ))}
            </>
          )}

          {conns.data?.map((c) => (
            <ConnectionCard
              key={c.name}
              conn={c}
              testMsg={testMsg[c.name]}
              testPending={testingConn === c.name && test.isPending}
              onTest={() => {
                setTestingConn(c.name)
                test.mutate(c.name, {
                  onSuccess: (r) => {
                    setTestMsg((m) => ({ ...m, [c.name]: `OK · ${r.tables} tables` }))
                    setTestingConn(null)
                  },
                  onError: (e) => {
                    setTestMsg((m) => ({ ...m, [c.name]: String(e) }))
                    setTestingConn(null)
                  },
                })
              }}
              onDelete={() => setPendingDelete(c.name)}
            />
          ))}

          {conns.data && conns.data.length === 0 && (
            <EmptyState
              icon={
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              }
              title="No warehouse connections yet"
              description="Declare one in dst.yaml (pick a warehouse above for the snippet) and land it with dst apply — your project's AGENTS.md and the dst-semantic skill walk an agent through the loop."
            />
          )}
        </div>
      </section>

      {pendingDelete && (
        <DeleteConnectionDialog
          name={pendingDelete}
          pending={del.isPending}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() =>
            del.mutate(pendingDelete, {
              onSuccess: () => {
                setPendingDelete(null)
                refresh()
              },
            })
          }
        />
      )}
    </Page>
  )
}
