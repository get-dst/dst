/**
 * ConnectionLogo — official brand marks per connection type.
 * Each mark is a real vendor SVG (see ../logos) wrapped in a soft tinted tile.
 */
import bigqueryLogo from '../logos/bigquery.svg'
import snowflakeLogo from '../logos/snowflake.svg'
import postgresLogo from '../logos/postgres.svg'
import mysqlLogo from '../logos/mysql.svg'
import duckdbLogo from '../logos/duckdb.svg'
import dbtLogo from '../logos/dbt.svg'

interface ConnectionLogoProps {
  type: string
  size?: 'sm' | 'md'
}

interface BrandMeta {
  bg: string
  src: string
}

/** Fallback: generic database/connection icon */
function FallbackMark() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      {/* Cylinder — generic DB */}
      <ellipse cx="12" cy="7" rx="6" ry="2.2" stroke="currentColor" strokeWidth="1.4" className="text-muted" />
      <path
        d="M6 7 L6 17 C6 18.2 8.7 19.2 12 19.2 C15.3 19.2 18 18.2 18 17 L18 7"
        stroke="currentColor"
        strokeWidth="1.4"
        fill="none"
        className="text-muted"
      />
      <path
        d="M6 12 C6 13.2 8.7 14.2 12 14.2 C15.3 14.2 18 13.2 18 12"
        stroke="currentColor"
        strokeWidth="1.4"
        fill="none"
        className="text-muted"
        opacity="0.6"
      />
    </svg>
  )
}

const BRAND_META: Record<string, BrandMeta> = {
  bigquery:  { bg: '#E8F0FE', src: bigqueryLogo },
  duckdb:    { bg: '#FFF4CC', src: duckdbLogo },
  postgres:  { bg: '#E8EFF8', src: postgresLogo },
  mysql:     { bg: '#E0F3F6', src: mysqlLogo },
  snowflake: { bg: '#E3F4FC', src: snowflakeLogo },
  dbt:       { bg: '#FEEDE9', src: dbtLogo },
}

export function ConnectionLogo({ type, size = 'md' }: ConnectionLogoProps) {
  const key = type?.toLowerCase() ?? ''
  const meta = BRAND_META[key]

  const tileSize = size === 'sm' ? 'h-[24px] w-[24px]' : 'h-[32px] w-[32px]'
  const iconSize = size === 'sm' ? 'h-[15px] w-[15px]' : 'h-[20px] w-[20px]'
  const radius = size === 'sm' ? 'rounded-[5px]' : 'rounded-[7px]'

  if (!meta) {
    return (
      <span
        className={[
          tileSize,
          radius,
          size === 'sm' ? '[&>svg]:h-[15px] [&>svg]:w-[15px]' : '[&>svg]:h-[20px] [&>svg]:w-[20px]',
          'inline-flex items-center justify-center shrink-0',
          'bg-surface-2 border border-border-strong',
        ].join(' ')}
        aria-label={type}
      >
        <FallbackMark />
      </span>
    )
  }

  return (
    <span
      className={[
        tileSize,
        radius,
        'inline-flex items-center justify-center shrink-0 overflow-hidden',
        'border border-black/[0.06]',
      ].join(' ')}
      style={{ background: meta.bg }}
      aria-label={type}
    >
      <img src={meta.src} alt="" className={`${iconSize} object-contain`} />
    </span>
  )
}
