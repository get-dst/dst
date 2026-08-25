import type { ComponentPropsWithoutRef, ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * The answer composer emits light markdown — `**bold**` for the figures it wants
 * to surface, and the occasional GFM pipe table when the answer is a small grid
 * of numbers. We render exactly those two constructs; everything else stays
 * literal text. Shared so every place an answer renders (AnswerCard, the review
 * story, the request-trace panel) agrees on the look.
 */
function renderBold(text: string): ReactNode[] {
  return text.split('**').map((part, i) => (i % 2 === 1 ? <strong key={i}>{part}</strong> : part))
}

/** A GFM table's separator row — only dashes, colons, pipes, spaces (and ≥1 dash). */
const isSeparator = (line: string): boolean => /^[\s:|-]*-[\s:|-]*$/.test(line.trim())

/** Split a `| a | b |` row into trimmed cells, dropping the outer pipes. */
function cells(line: string): string[] {
  let s = line.trim()
  if (s.startsWith('|')) s = s.slice(1)
  if (s.endsWith('|')) s = s.slice(0, -1)
  return s.split('|').map((c) => c.trim())
}

function MdTable({ rows }: { rows: string[] }) {
  const header = cells(rows[0])
  const body = rows.slice(2).map(cells) // rows[1] is the --- separator
  return (
    <div className="overflow-x-auto rounded border border-border">
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr className="bg-surface-2">
            {header.map((c, i) => (
              <th
                key={i}
                className="border-b border-border px-3 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-muted"
              >
                {renderBold(c)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((r, ri) => (
            <tr key={ri} className={ri % 2 === 1 ? 'bg-surface-2/50' : 'bg-surface'}>
              {r.map((c, ci) => (
                <td key={ci} className="border-b border-border px-3 py-1.5 text-text">
                  {renderBold(c)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ─── full markdown document (react-markdown + GFM) ───────────────────────────
// The lens repo materializes prose as real markdown files — headings, lists,
// tables, fenced code. Styled to the ink+paper+mono identity (mono for code,
// hairline rules + surface-2 for tables) rather than a generic prose theme.
const mdComponents: NonNullable<ComponentPropsWithoutRef<typeof ReactMarkdown>['components']> = {
  h1: (p: ComponentPropsWithoutRef<'h1'>) => <h1 className="mt-4 mb-2 text-[18px] font-bold text-text first:mt-0" {...p} />,
  h2: (p: ComponentPropsWithoutRef<'h2'>) => <h2 className="mt-4 mb-2 text-[15px] font-bold text-text first:mt-0" {...p} />,
  h3: (p: ComponentPropsWithoutRef<'h3'>) => <h3 className="mt-3 mb-1.5 text-[13px] font-bold uppercase tracking-wider text-muted first:mt-0" {...p} />,
  h4: (p: ComponentPropsWithoutRef<'h4'>) => <h4 className="mt-3 mb-1 text-[13px] font-semibold text-text first:mt-0" {...p} />,
  p: (p: ComponentPropsWithoutRef<'p'>) => <p className="my-2 text-[13px] leading-relaxed text-text" {...p} />,
  ul: (p: ComponentPropsWithoutRef<'ul'>) => <ul className="my-2 ml-5 list-disc space-y-1 text-[13px] text-text marker:text-muted-2" {...p} />,
  ol: (p: ComponentPropsWithoutRef<'ol'>) => <ol className="my-2 ml-5 list-decimal space-y-1 text-[13px] text-text marker:text-muted-2" {...p} />,
  li: (p: ComponentPropsWithoutRef<'li'>) => <li className="leading-relaxed" {...p} />,
  a: (p: ComponentPropsWithoutRef<'a'>) => <a className="text-accent-dark underline underline-offset-2 hover:text-accent" {...p} />,
  strong: (p: ComponentPropsWithoutRef<'strong'>) => <strong className="font-semibold text-text" {...p} />,
  em: (p: ComponentPropsWithoutRef<'em'>) => <em className="italic" {...p} />,
  hr: () => <hr className="my-4 border-border" />,
  blockquote: (p: ComponentPropsWithoutRef<'blockquote'>) => (
    <blockquote className="my-2 border-l-2 border-accent/40 bg-accent-fg/30 px-3 py-1.5 text-[13px] text-text" {...p} />
  ),
  code: ({ className, children, ...rest }: ComponentPropsWithoutRef<'code'>) => {
    // Inline code (no language class) vs. a fenced block child.
    const fenced = /language-/.test(className ?? '')
    return fenced ? (
      <code className={`font-mono text-[12px] text-text ${className ?? ''}`} {...rest}>
        {children}
      </code>
    ) : (
      <code className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono text-[12px] text-text" {...rest}>
        {children}
      </code>
    )
  },
  pre: (p: ComponentPropsWithoutRef<'pre'>) => (
    <pre
      className="my-2 overflow-x-auto rounded-md border border-border bg-surface px-3 py-2 font-mono text-[11px] leading-relaxed text-text"
      {...p}
    />
  ),
  table: (p: ComponentPropsWithoutRef<'table'>) => (
    <div className="my-2 overflow-x-auto rounded border border-border">
      <table className="w-full border-collapse text-[12px]" {...p} />
    </div>
  ),
  thead: (p: ComponentPropsWithoutRef<'thead'>) => <thead className="bg-surface-2" {...p} />,
  th: (p: ComponentPropsWithoutRef<'th'>) => (
    <th className="border-b border-border px-3 py-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-muted" {...p} />
  ),
  td: (p: ComponentPropsWithoutRef<'td'>) => <td className="border-b border-border px-3 py-1.5 text-text" {...p} />,
}

/** Render a markdown document — headings, lists, tables, fenced code — to the
 * ink+paper+mono identity. Use for repo prose files; `Answer` stays the lighter
 * renderer for composed answers. */
export function Markdown({ text, className = '' }: { text: string; className?: string }) {
  return (
    <div className={className}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
        {text}
      </ReactMarkdown>
    </div>
  )
}

/**
 * Render an answer body: blank-line-separated blocks, each a GFM pipe table
 * (header + `---` separator + rows) or a paragraph with `**bold**` applied. A
 * plain-prose answer renders as a single <p>.
 */
export function Answer({ text, className = '' }: { text: string; className?: string }) {
  const blocks = text.split(/\n\s*\n/)
  return (
    <div className={`space-y-2 ${className}`}>
      {blocks.map((block, i) => {
        const lines = block.split('\n').filter((l) => l.trim())
        const isTable =
          lines.length >= 2 && lines[0].includes('|') && isSeparator(lines[1])
        return isTable ? (
          <MdTable key={i} rows={lines} />
        ) : (
          <p key={i}>{renderBold(lines.join(' '))}</p>
        )
      })}
    </div>
  )
}
