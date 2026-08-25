/**
 * Money / cost formatting — the single source of truth for how dollar amounts
 * render across the app. One unit everywhere: dollars. Sub-cent amounts keep
 * their precision with more decimals rather than switching to ¢, so a per-row
 * figure and the total above it never read as two different currencies.
 */

import { format as formatSqlDialect } from 'sql-formatter'

/**
 * Pretty-print SQL for display — keywords upper-cased, clauses on their own
 * lines, so a model's one-liner actually reads like SQL. Best-effort: malformed
 * or non-SQL text falls back to the original string untouched.
 */
export function formatSql(sql: string): string {
  try {
    return formatSqlDialect(sql, { language: 'postgresql', keywordCase: 'upper' })
  } catch {
    return sql
  }
}

/**
 * USD for every cost, aggregate or per-operation.
 *   $1,234.56 · $0.42 · $0.0023 · <$0.0001 · $0.00
 */
export function formatCost(n: number | null | undefined): string {
  const v = n ?? 0
  if (v === 0) return '$0.00'
  if (v >= 0.01) {
    return `$${v.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`
  }
  if (v >= 0.0001) return `$${v.toFixed(4)}`
  return '<$0.0001'
}

/** Human-readable byte counts: 480 B · 12.3 KB · 340 MB · 1.2 GB. */
export function formatBytes(n: number | null | undefined): string {
  const v = n ?? 0
  if (v < 1024) return `${v} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let x = v
  let u = -1
  do {
    x /= 1024
    u++
  } while (x >= 1024 && u < units.length - 1)
  return `${x >= 100 ? Math.round(x) : x.toFixed(1)} ${units[u]}`
}
