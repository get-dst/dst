import type { ButtonHTMLAttributes } from 'react'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'sm' | 'md' | 'lg'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
}

const variantClasses: Record<Variant, string> = {
  primary:
    'bg-accent text-accent-fg border border-transparent hover:bg-accent-dark active:bg-[#3d3c3a] active:scale-[0.98] disabled:opacity-40',
  secondary:
    'bg-surface text-text border border-border hover:bg-surface-2 hover:border-border-strong active:bg-surface-3 active:scale-[0.98] disabled:opacity-50',
  ghost:
    'bg-transparent text-muted border border-transparent hover:bg-surface-2 hover:text-text active:bg-surface-3 active:scale-[0.98] disabled:opacity-50',
  danger:
    'bg-red-bg text-red border border-red/20 hover:bg-red/10 active:bg-red/15 active:scale-[0.98] disabled:opacity-50',
}

const sizeClasses: Record<Size, string> = {
  sm: 'px-2.5 py-1 text-[12px] rounded-md gap-1',
  md: 'px-3.5 py-1.5 text-[13px] rounded-md gap-1.5',
  lg: 'px-4 py-2 text-[14px] rounded-lg gap-2',
}

export function Button({
  variant = 'primary',
  size = 'md',
  className = '',
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      type="button"
      className={[
        'inline-flex items-center justify-center font-medium select-none',
        'transition-all cursor-pointer',
        'outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1',
        'disabled:cursor-not-allowed',
        variantClasses[variant],
        sizeClasses[size],
        className,
      ].join(' ')}
      style={{ transitionDuration: 'var(--duration-fast)' }}
      {...props}
    >
      {children}
    </button>
  )
}
