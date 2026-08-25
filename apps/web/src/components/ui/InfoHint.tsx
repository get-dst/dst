import type { ReactNode } from 'react'

/** An (i) glyph that reveals its explanation on hover or keyboard focus.
 * For prose that earns its place but not its space: the figure stays clean,
 * the reasoning is one hover away. The text still renders into the DOM
 * (visibility, not conditional mount), so vocabulary tests keep their grip. */
export function InfoHint({ children }: { children: ReactNode }) {
  return (
    <span className="group relative inline-flex" tabIndex={0} aria-label="What this figure means">
      <svg
        width="13"
        height="13"
        viewBox="0 0 14 14"
        fill="none"
        aria-hidden="true"
        className="text-muted-2 transition-colors group-hover:text-text group-focus-visible:text-text"
      >
        <circle cx="7" cy="7" r="6" stroke="currentColor" strokeWidth="1.25" />
        <path
          d="M7 6.2v3.3M7 4.2v.5"
          stroke="currentColor"
          strokeWidth="1.25"
          strokeLinecap="round"
        />
      </svg>
      <span
        role="tooltip"
        className="pointer-events-none invisible absolute left-0 top-full z-20 mt-1.5 w-max max-w-[38ch] rounded-md border border-border bg-surface px-3 py-2 text-left text-[12px] font-normal normal-case leading-relaxed tracking-normal text-muted group-hover:visible group-focus-visible:visible"
        style={{ boxShadow: 'var(--shadow-card)' }}
      >
        {children}
      </span>
    </span>
  )
}
