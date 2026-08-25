import { confidenceVariant, statusVariant } from '../../lib/outcomes'
import { Badge } from './Badge'

/** Status wins when it isn't 'ok' (a decline or fault has no meaningful grade). */
export function ConfidenceBadge({
  confidence,
  status,
  dot,
}: {
  confidence: string | null
  status?: string
  dot?: boolean
}) {
  if (status && status !== 'ok') {
    return (
      <Badge variant={statusVariant(status)} dot={dot}>
        {status}
      </Badge>
    )
  }
  return (
    <Badge variant={confidenceVariant(confidence)} dot={dot}>
      {confidence ?? '—'}
    </Badge>
  )
}
