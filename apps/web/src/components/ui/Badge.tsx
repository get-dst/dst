import type { HTMLAttributes } from 'react'

type BadgeVariant = 'default' | 'success' | 'warning' | 'error' | 'info'

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant
  dot?: boolean
}

const variantClasses: Record<BadgeVariant, string> = {
  default: 'bg-surface-2 text-muted border-border-strong',
  success: 'bg-green-bg text-green border-green/25',
  warning: 'bg-amber-bg text-amber border-amber/25',
  error:   'bg-red-bg text-red border-red/25',
  info:    'bg-surface-3 text-muted border-border-strong',
}

const dotColors: Record<BadgeVariant, string> = {
  default: 'bg-muted-2',
  success: 'bg-green',
  warning: 'bg-amber',
  error:   'bg-red',
  info:    'bg-muted-2',
}

export function Badge({
  variant = 'default',
  dot = false,
  className = '',
  children,
  ...props
}: BadgeProps) {
  return (
    <span
      className={[
        'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-[4px]',
        'text-[11px] font-medium uppercase tracking-wide border',
        variantClasses[variant],
        className,
      ].join(' ')}
      {...props}
    >
      {dot && (
        <span
          className={['h-1.5 w-1.5 rounded-full inline-block shrink-0', dotColors[variant]].join(' ')}
          aria-hidden="true"
        />
      )}
      {children}
    </span>
  )
}
