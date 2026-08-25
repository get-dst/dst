import { useEffect, useRef, useState } from 'react'
import {
  clearCredentials,
  credentialKind,
  getActiveOrgName,
  getOrgs,
  getToken,
  registerOrg,
  setToken,
  type CredentialKind,
} from '../api/auth'
import { logout, useMe } from '../api/session'
import { useWhoami } from '../api/whoami'
import { TokenBar } from './TokenBar'

/** Small gear/settings glyph — instrument-panel feel, ink-on-hover. */
function GearIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="8" cy="8" r="2.25" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M8 1.5v1.6M8 12.9v1.6M14.5 8h-1.6M3.1 8H1.5M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1M12.6 12.6l-1.1-1.1M4.5 4.5L3.4 3.4"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  )
}

function CheckIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true">
      <path d="M2.5 7l3 3 5-7" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** The credential in play, as a label + how it's rendered. `sso` is Clerk: its
 * JWT is minted per request and outranks anything in localStorage. */
type Credential = CredentialKind | 'sso'

const CREDENTIAL_LABEL: Record<Credential, string> = {
  session: 'session',
  token: 'token',
  sso: 'sso',
  none: 'none',
}

/** Chip on the header trigger: which credential the UI is acting with. A pasted
 * token wears the accent because it overrides whoever is signed in. */
function CredentialChip({ credential }: { credential: Credential }) {
  return (
    <span
      title={`credential in play: ${CREDENTIAL_LABEL[credential]}`}
      className={[
        'rounded-sm border px-1 py-px font-mono text-[9px] font-semibold uppercase tracking-[0.1em]',
        credential === 'token'
          ? 'border-accent/40 bg-accent-fg text-accent-dark'
          : 'border-border bg-surface-2 text-muted-2',
      ].join(' ')}
    >
      {CREDENTIAL_LABEL[credential]}
    </span>
  )
}

/** Who the UI is acting as, and on which credential.
 *
 * One localStorage slot carries either a `dstsess_` session or a pasted `dstadm_`
 * admin token, and the bearer header beats the session cookie server-side — so a
 * pasted token silently governs ITS org while the sign-in still looks current.
 * This card names the identity, names the credential, and offers the way out.
 */
function IdentityCard({ credential }: { credential: Credential }) {
  const me = useMe()
  const whoami = useWhoami()
  const orgName = getActiveOrgName() ?? whoami.data?.org_name ?? undefined
  const orgId = whoami.data?.org_id

  const clear = () => {
    setToken('')
    window.location.assign('/')
  }

  return (
    <div className="px-3 py-3">
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-2">
        Acting as
      </div>

      <div className="flex items-baseline justify-between gap-2">
        {/* A token authenticates as the org's admin and names no user — say so
            rather than implying someone is signed in. */}
        <span className="min-w-0 truncate text-[12px] font-medium text-text">
          {me.data?.email ?? (credential === 'token' ? 'Org admin (no user)' : 'Signed in')}
        </span>
        {me.data?.role && (
          <span className="shrink-0 font-mono text-[10px] uppercase tracking-wider text-muted-2">
            {me.data.role}
          </span>
        )}
      </div>

      <div className="mt-0.5 flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate text-[11px] text-muted">
          {orgName ?? (whoami.isLoading ? 'resolving org…' : 'org unknown')}
        </span>
        {orgId && (
          <span className="shrink-0 font-mono text-[10px] text-muted-2">
            {orgId.slice(0, 8)}
          </span>
        )}
      </div>

      <div className="mt-2 flex items-center gap-1.5 border-t border-border pt-2">
        <span className="text-[11px] text-muted-2">via</span>
        <CredentialChip credential={credential} />
        <span className="text-[11px] text-muted">
          {credential === 'token'
            ? 'pasted admin token'
            : credential === 'session'
              ? 'browser sign-in'
              : credential === 'sso'
                ? 'identity provider'
                : 'no credential'}
        </span>
      </div>

      {credential === 'token' && (
        <>
          <p className="mt-1.5 text-[11px] leading-snug text-muted">
            This token outranks any sign-in — every request runs as its org.
          </p>
          <button
            type="button"
            onClick={clear}
            className={[
              'mt-1.5 w-full rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted',
              'cursor-pointer transition-colors hover:border-border-strong hover:bg-surface-2 hover:text-text',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            ].join(' ')}
            style={{ transitionDuration: 'var(--duration-fast)' }}
          >
            Clear token & sign in
          </button>
        </>
      )}
    </div>
  )
}

/** Org switcher: lists the orgs this browser has held ADMIN TOKENS for.
 *
 * The registry is keyed by token, so it can only ever hold pasted admin tokens —
 * a browser sign-in has no token to key on (its session is in an httpOnly cookie)
 * and so is shown as the single current org, from whoami, rather than persisted.
 */
function OrgSwitcher() {
  const whoami = useWhoami()
  const activeToken = getToken()
  const signedIn = credentialKind() !== 'none'

  // Persist the org a freshly-pasted token belongs to, so the switcher remembers it.
  useEffect(() => {
    const t = getToken()
    if (whoami.data?.org_name && t && !getOrgs().some((o) => o.token === t)) {
      registerOrg(whoami.data.org_name, t)
    }
  }, [whoami.data])

  // Include the current org (from whoami) even before it's persisted, plus known orgs.
  const known = getOrgs()
  const orgs =
    whoami.data?.org_name && activeToken && !known.some((o) => o.token === activeToken)
      ? [...known, { name: whoami.data.org_name, token: activeToken }]
      : known

  const switchTo = (token: string) => {
    setToken(token)
    // Hard-reload so the whole app re-queries under the newly active org token.
    window.location.assign('/')
  }

  return (
    <div className="px-3 py-3">
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-2">
        Organization
      </div>

      {!signedIn && (
        <div className="text-[11px] leading-snug text-muted">Set an admin token below to begin.</div>
      )}

      {/* Known orgs (active first) */}
      <div className="space-y-0.5">
        {orgs.map((o) => {
          const active = o.token === activeToken
          return (
            <button
              key={o.token}
              type="button"
              onClick={() => !active && switchTo(o.token)}
              className={[
                'flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left',
                'transition-colors',
                active ? 'bg-surface-2 text-text' : 'text-muted hover:bg-surface-2 hover:text-text',
              ].join(' ')}
              style={{ transitionDuration: 'var(--duration-fast)' }}
            >
              <span className="truncate text-[12px] font-medium">{o.name}</span>
              {active && <span className="text-accent shrink-0"><CheckIcon /></span>}
            </button>
          )
        })}
        {signedIn && orgs.length === 0 && (
          <div className="rounded-md bg-surface-2 px-2 py-1.5 text-[12px] text-text">
            {whoami.data?.org_name ?? 'Current org'}
          </div>
        )}
      </div>
    </div>
  )
}

export function ProfileMenu({ clerkEnabled = false }: { clerkEnabled?: boolean }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const whoami = useWhoami()
  const activeName = getActiveOrgName() ?? whoami.data?.org_name ?? undefined
  // Clerk mints its own JWT per request, which wins over anything stored locally.
  const credential: Credential = clerkEnabled ? 'sso' : credentialKind()

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Settings & organization"
        className={[
          'flex h-8 items-center gap-1.5 rounded-md border px-2',
          'transition-colors cursor-pointer outline-none',
          'focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1',
          open
            ? 'border-border-strong bg-surface-2 text-text'
            : 'border-border bg-surface text-muted hover:text-text hover:bg-surface-2 hover:border-border-strong',
        ].join(' ')}
        style={{ transitionDuration: 'var(--duration-fast)' }}
      >
        {activeName && (
          <span className="max-w-[120px] truncate text-[11px] font-medium">{activeName}</span>
        )}
        <CredentialChip credential={credential} />
        <GearIcon />
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-[calc(100%+8px)] z-50 w-[300px] rounded-lg border border-border bg-surface"
          style={{ boxShadow: 'var(--shadow-popover)' }}
        >
          <IdentityCard credential={credential} />
          <div className="border-t border-border" />
          <OrgSwitcher />
          <div className="border-t border-border" />
          <div className="px-3 py-3">
            <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-2">
              Admin token
            </div>
            <TokenBar />
          </div>
          {credential === 'session' && (
            <>
              <div className="border-t border-border" />
              <div className="px-3 py-2">
                <button
                  type="button"
                  onClick={() => {
                    // Revoke the session server-side (which also clears the cookie),
                    // then drop every credential this browser holds — including the
                    // other orgs' admin tokens — and return to the login form.
                    void logout().finally(() => {
                      clearCredentials()
                      window.location.assign('/')
                    })
                  }}
                  className={[
                    'w-full rounded-md px-2 py-1.5 text-left text-[12px] font-medium text-muted',
                    'transition-colors cursor-pointer hover:bg-surface-2 hover:text-text',
                  ].join(' ')}
                  style={{ transitionDuration: 'var(--duration-fast)' }}
                >
                  Sign out
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
