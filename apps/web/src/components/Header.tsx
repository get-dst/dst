import { NavLink } from 'react-router-dom'
import { OrganizationSwitcher, UserButton } from '@clerk/clerk-react'
import { ProfileMenu } from './ProfileMenu'

/** dst brand glyph — precision aperture/lens mark, ink on paper */
export function DstMark() {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      {/* Outer ring — slightly heavier for presence */}
      <circle cx="12" cy="12" r="8.5" stroke="#1a1917" strokeWidth="1.5" />
      {/* Inner ring */}
      <circle cx="12" cy="12" r="4.5" stroke="#1a1917" strokeWidth="1" opacity="0.5" />
      {/* Cross-hair ticks — instrument panel feel */}
      <line x1="12" y1="3"   x2="12" y2="6"    stroke="#1a1917" strokeWidth="1.5" strokeLinecap="round" />
      <line x1="12" y1="18"  x2="12" y2="21"   stroke="#1a1917" strokeWidth="1.5" strokeLinecap="round" />
      <line x1="3"  y1="12"  x2="6"  y2="12"   stroke="#1a1917" strokeWidth="1.5" strokeLinecap="round" />
      <line x1="18" y1="12"  x2="21" y2="12"   stroke="#1a1917" strokeWidth="1.5" strokeLinecap="round" />
      {/* Centre dot — filled ink */}
      <circle cx="12" cy="12" r="2" fill="#1a1917" />
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
        <DstMark />
        <span
          className="font-semibold text-[15px] text-text select-none"
          style={{ letterSpacing: '-0.02em' }}
        >
          dst
        </span>
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
