/**
 * Minimal YAML tinting for the file viewer — keys, list dashes, comments.
 * Line-regex only, no parser and no highlighter dependency: lens files are
 * small and hand-authored, and a missed tint is cosmetic. Colors come from
 * the app's own tokens so files read in the ink/paper identity, not a
 * stock highlighter theme.
 */
import type { ReactNode } from 'react'

export function yamlLines(text: string): ReactNode[] {
  return text.split('\n').map((line, i) => {
    const trimmed = line.trimStart()
    if (trimmed.startsWith('#') || trimmed === '---' || trimmed === '...') {
      return (
        <span key={i} className="text-muted">
          {line}
          {'\n'}
        </span>
      )
    }
    const kv = line.match(/^(\s*)(- )?([\w.$/-]+)(:)(\s.*|$)/)
    if (kv) {
      const [, indent, dash, key, colon, rest] = kv
      return (
        <span key={i}>
          {indent}
          {dash && <span className="text-muted-2">{dash}</span>}
          <span className="text-accent-dark">{key}</span>
          <span className="text-muted">{colon}</span>
          {rest}
          {'\n'}
        </span>
      )
    }
    const item = line.match(/^(\s*)(- )(.*)$/)
    if (item) {
      return (
        <span key={i}>
          {item[1]}
          <span className="text-muted-2">{item[2]}</span>
          {item[3]}
          {'\n'}
        </span>
      )
    }
    return (
      <span key={i}>
        {line}
        {'\n'}
      </span>
    )
  })
}
