import type { HTMLAttributes } from 'react'

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Add a subtle shadow in addition to the border */
  elevated?: boolean
  /**
   * Mark this card as a navigable/clickable surface.
   * Adds hover border-strong + bg lift + cursor-pointer + subtle active press.
   * Page agents can use this for any card that has an onClick or is wrapped in a Link.
   */
  interactive?: boolean
}

export function Card({
  elevated = false,
  interactive = false,
  className = '',
  children,
  ...props
}: CardProps) {
  return (
    <div
      className={[
        'bg-surface border border-border rounded-lg overflow-hidden',
        elevated ? 'shadow-[var(--shadow-card)]' : '',
        interactive
          ? [
              'cursor-pointer select-none',
              'transition-all',
              'hover:border-border-strong hover:shadow-[var(--shadow-card)] hover:-translate-y-px',
              'active:translate-y-0 active:shadow-none active:bg-surface-2',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1',
            ].join(' ')
          : '',
        className,
      ].join(' ')}
      style={interactive ? { transitionDuration: 'var(--duration-fast)' } : undefined}
      {...props}
    >
      {children}
    </div>
  )
}

export function CardHeader({ className = '', children, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={[
        'flex items-center justify-between gap-4',
        'px-4 py-3 bg-surface-2 border-b border-border',
        className,
      ].join(' ')}
      {...props}
    >
      {children}
    </div>
  )
}

export function CardBody({ className = '', children, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={['px-4 py-4', className].join(' ')} {...props}>
      {children}
    </div>
  )
}

export function CardFooter({ className = '', children, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={[
        'px-4 py-3 bg-surface-2 border-t border-border',
        className,
      ].join(' ')}
      {...props}
    >
      {children}
    </div>
  )
}
