import type { InputHTMLAttributes } from 'react'

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  mono?: boolean
}

export function Input({ mono = false, className = '', ...props }: InputProps) {
  return (
    <input
      className={[
        'w-full rounded-md border border-border bg-surface',
        'px-3 py-1.5 text-[13px] text-text placeholder:text-muted-2',
        'transition-colors outline-none',
        'hover:border-border-strong',
        'focus:border-accent focus:ring-2 focus:ring-accent/20 focus:hover:border-accent',
        'disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:border-border',
        mono ? 'font-mono text-[12px]' : '',
        className,
      ].join(' ')}
      style={{ transitionDuration: 'var(--duration-fast)' }}
      {...props}
    />
  )
}
