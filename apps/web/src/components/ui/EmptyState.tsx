import type { ReactNode } from 'react'

interface EmptyStateProps {
  icon?: ReactNode
  title: string
  description?: string
  action?: ReactNode
}

export function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center text-center px-6 py-14 rounded-lg border border-dashed border-border-strong bg-surface-2">
      {icon && (
        <div
          className="mb-4 flex h-10 w-10 items-center justify-center rounded-lg bg-surface border border-border text-muted-2"
          aria-hidden="true"
        >
          {icon}
        </div>
      )}
      <p className="text-[14px] font-semibold text-text">{title}</p>
      {description && (
        <p className="mt-1.5 text-[13px] text-muted leading-relaxed max-w-xs">{description}</p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </div>
  )
}
