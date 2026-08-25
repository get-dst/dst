import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

/**
 * The single page frame for the whole app.
 *
 * Two widths only — `prose` for focused single-column flows (Create, Settings)
 * and `data` for dense, table-heavy surfaces (Lenses, Observe, Reviews…).
 * One horizontal padding, one vertical rhythm. If you ever find yourself
 * reaching for `max-w-[…]` or `px-[…]` on a page, add it here instead.
 */
const widthClass = {
  prose: 'max-w-[720px]',
  data: 'max-w-[1040px]',
} as const

export function Page({
  width = 'data',
  className = '',
  children,
}: {
  width?: keyof typeof widthClass
  className?: string
  children: ReactNode
}) {
  return (
    <div className={['mx-auto px-8 py-8', widthClass[width], className].join(' ')}>
      {children}
    </div>
  )
}

/**
 * The one page heading, rendered as an instrument fascia: the
 * title block seated on a full-width hairline baseline, live mono readout
 * right-aligned on the same line. Semibold, 20px, defined once so no screen
 * drifts to `font-bold` or a stray size. Optional back-link, status accessory
 * beside the title, right-aligned actions, and the `readout` slot for the
 * page's live facts.
 */
export function PageHeader({
  title,
  description,
  actions,
  accessory,
  readout,
  back,
  mono = false,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  accessory?: ReactNode
  /** Live facts on the fascia baseline — render with <Readout/>. */
  readout?: ReactNode
  back?: { to: string; label: string }
  mono?: boolean
}) {
  return (
    <div className="mb-6 border-b border-border-strong pb-4">
      {back && (
        <Link
          to={back.to}
          className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text focus-ring rounded transition-colors"
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
          {back.label}
        </Link>
      )}
      <div
        className={['flex items-start justify-between gap-4', back ? 'mt-3' : ''].join(' ')}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <h1
              className={[
                'text-[20px] font-semibold tracking-tight text-text leading-none',
                mono ? 'font-mono' : '',
              ].join(' ')}
            >
              {title}
            </h1>
            {accessory}
          </div>
          {description && (
            <p className="mt-1.5 max-w-[64ch] text-[13px] text-muted leading-relaxed">
              {description}
            </p>
          )}
        </div>
        <div className="flex items-center gap-4 shrink-0 pt-1">
          {readout}
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      </div>
    </div>
  )
}
