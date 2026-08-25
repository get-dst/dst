import { NavLink } from 'react-router-dom'
import { OrganizationSwitcher, UserButton } from '@clerk/clerk-react'
import { ProfileMenu } from './ProfileMenu'

/** THE dst logo — the ink tile with the mono wordmark, identical to the one
 * on the docs site, the favicon and the social card. One mark everywhere;
 * the app must not wear a private variant. */
export function DstMark({ size = 26 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <rect width="64" height="64" rx="12" fill="#1a1917" />
      <text
        x="32"
        y="42"
        textAnchor="middle"
        fontFamily="IBM Plex Mono, ui-monospace, Menlo, monospace"
        fontSize="24"
        fontWeight="600"
        fill="#faf9f6"
      >
        dst
      </text>
    </svg>
  )
}

// Primary work surfaces, left-aligned. Data sources/Router/Settings are the
// configuration cluster — pushed to the right, away from the day-to-day tabs.
const PRIMARY_NAV = [
  { to: '/', label: 'Lenses', end: true },
  { to: '/observe', label: 'Observe', end: false },
  { to: '/certify', label: 'Certify', end: false },
] as const

const SECONDARY_NAV = [
  { to: '/data-sources', label: 'Data sources', end: false },
  { to: '/router', label: 'Router', end: false },
  { to: '/settings', label: 'Settings', end: false },
] as const

function NavTab({ to, label, end }: { to: string; label: string; end: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        [
          'relative flex items-center h-full px-3.5 text-[13px] font-medium transition-colors',
          'outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset',
          isActive ? 'text-text' : 'text-muted hover:text-text',
        ].join(' ')
      }
      style={{ transitionDuration: 'var(--duration-fast)' }}
    >
      {({ isActive }) => (
        <>
          {label}
          {/* Active accent underline — flush with header bottom border */}
          {isActive && (
            <span
              className="absolute bottom-0 left-2 right-2 h-[2px] rounded-t-full bg-accent"
              aria-hidden="true"
            />
          )}
        </>
      )}
    </NavLink>
  )
}

export function Header({ clerkEnabled = false }: { clerkEnabled?: boolean }) {
  return (
    <header
      className="h-[52px] bg-surface border-b border-border flex items-center px-6 gap-0 shrink-0"
      style={{ boxShadow: '0 1px 0 0 var(--color-border), 0 1px 3px 0 rgb(26 25 23 / 0.04)' }}
    >
      {/* ── Brand ── */}
      <NavLink
        to="/"
        end
        aria-label="dst home"
        className="flex items-center gap-2 mr-8 shrink-0 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-inset"
      >
        {/* The tile carries the wordmark — a second "dst" beside it read twice. */}
        <DstMark />
      </NavLink>

      {/* ── Nav ── */}
      <nav className="flex items-center gap-0.5 h-full" aria-label="Main navigation">
        {PRIMARY_NAV.map((item) => (
          <NavTab key={item.to} {...item} />
        ))}
      </nav>

      {/* ── Right controls ── */}
      <div className="ml-auto flex items-center h-full gap-3">
        <nav className="flex items-center gap-0.5 h-full" aria-label="Settings navigation">
          {SECONDARY_NAV.map((item) => (
            <NavTab key={item.to} {...item} />
          ))}
        </nav>
        <div className="h-5 w-px bg-border mx-1 self-center" aria-hidden="true" />
        {clerkEnabled ? (
          <>
            <OrganizationSwitcher
              appearance={{
                elements: {
                  rootBox: 'h-8',
                  organizationSwitcherTrigger:
                    'h-8 px-2 text-[13px] rounded-md border border-border bg-surface hover:bg-surface-2 text-text',
                },
              }}
            />
            <ProfileMenu clerkEnabled />
            <UserButton
              appearance={{
                elements: {
                  avatarBox: 'h-7 w-7',
                },
              }}
            />
          </>
        ) : (
          <ProfileMenu />
        )}
      </div>
    </header>
  )
}
