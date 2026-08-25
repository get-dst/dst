/**
 * The readout — dst's one grammar for machine facts.
 *
 * `label value` pairs in 11px mono, tabular figures, `·` separators; labels
 * muted, values ink. The fascia, table footers, and provenance lines all
 * render through this so "what the system knows" looks the same everywhere —
 * the chrome carries receipts the way the answers do.
 */
export type ReadoutItem = { label?: string; value: string | number }

export function Readout({ items, className = '' }: { items: ReadoutItem[]; className?: string }) {
  const shown = items.filter((i) => i.value !== '' && i.value !== undefined && i.value !== null)
  if (shown.length === 0) return null
  return (
    <span
      className={['font-mono text-[11px] leading-none text-muted-2 tabular-nums', className].join(
        ' ',
      )}
    >
      {shown.map((item, i) => (
        <span key={i} className="whitespace-nowrap">
          {i > 0 && <span aria-hidden="true">{' · '}</span>}
          {item.label && <>{item.label} </>}
          <span className="text-text">{item.value}</span>
        </span>
      ))}
    </span>
  )
}
