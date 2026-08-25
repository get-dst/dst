import { useEffect } from 'react'
import type { ReactNode } from 'react'
import { Button } from './Button'
import { Card } from './Card'

/**
 * A deliberate, focused confirmation modal for consequential actions — most of all
 * destructive ones. It is not a styled `window.confirm`: the body slot is meant to
 * carry real context (e.g. which lenses depend on the thing being deleted) so the
 * operator decides with the consequences in front of them. Backdrop click and Escape
 * cancel; the confirm button reflects `pending` while the mutation runs.
 */
export function ConfirmDialog({
  title,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  destructive = false,
  pending = false,
  onConfirm,
  onClose,
  children,
}: {
  title: ReactNode
  confirmLabel?: string
  cancelLabel?: string
  destructive?: boolean
  pending?: boolean
  onConfirm: () => void
  onClose: () => void
  children?: ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !pending) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, pending])

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-auto bg-black/30 p-6 pt-[14vh]"
      onClick={() => !pending && onClose()}
      role="dialog"
      aria-modal="true"
    >
      <Card elevated className="w-full max-w-md" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start gap-3 px-5 pt-5">
          {destructive && (
            <span className="mt-px inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-red-bg text-red">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path
                  d="M8 1.6 1.2 13.4h13.6L8 1.6Z"
                  stroke="currentColor"
                  strokeWidth="1.25"
                  strokeLinejoin="round"
                />
                <path d="M8 6v3.2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
                <circle cx="8" cy="11.4" r="0.7" fill="currentColor" />
              </svg>
            </span>
          )}
          <h3 className="pt-1 text-[15px] font-bold leading-snug text-text">{title}</h3>
        </div>

        {children != null && (
          <div className="px-5 py-3.5 text-[13px] leading-relaxed text-muted [&_strong]:font-semibold [&_strong]:text-text">
            {children}
          </div>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={pending}>
            {cancelLabel}
          </Button>
          <Button
            variant={destructive ? 'danger' : 'primary'}
            size="sm"
            onClick={onConfirm}
            disabled={pending}
          >
            {pending ? 'Working…' : confirmLabel}
          </Button>
        </div>
      </Card>
    </div>
  )
}
