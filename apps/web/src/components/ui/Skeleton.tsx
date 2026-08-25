import type { HTMLAttributes } from 'react'

interface SkeletonProps extends HTMLAttributes<HTMLDivElement> {
  /** Explicit width (Tailwind class or inline) */
  width?: string
  /** Explicit height */
  height?: string
}

export function Skeleton({ width, height, className = '', style, ...props }: SkeletonProps) {
  return (
    <div
      className={['skeleton rounded-md', className].join(' ')}
      style={{ width, height, ...style }}
      aria-hidden="true"
      {...props}
    />
  )
}

/** A multi-line text skeleton block */
export function SkeletonText({ lines = 3, className = '' }: { lines?: number; className?: string }) {
  return (
    <div className={['space-y-2', className].join(' ')} aria-hidden="true">
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton
          key={i}
          className="h-3 rounded-md"
          style={{ width: i === lines - 1 ? '60%' : '100%' }}
        />
      ))}
    </div>
  )
}
