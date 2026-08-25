/**
 * ConnectionDeclareGuide — the "declare this connection" help panel.
 *
 * Connections are authored in files, never in the UI: per warehouse type this panel
 * renders the dst.yaml snippet + .env lines to copy, alongside the credential
 * roles and the provider setup steps that earn them.
 * `dst apply` lands the declaration — the server probes the credential
 * (connect + read) before accepting it, so a dead key never replaces a working one.
 */
import { useState } from 'react'
import type { WarehouseField, WarehouseSpec } from '../warehouses/types'
import { ConnectionLogo } from './ConnectionLogo'

/** Env-var name the CLI convention derives from the connection name. */
const keyEnv = (name: string) => 'DST_API_KEY_' + name.toUpperCase().replace(/-/g, '_')

/** An example value for a field: its default, else the leading chunk of its placeholder. */
const exampleValue = (f: WarehouseField): string =>
  (f.defaultValue ?? f.placeholder ?? '').split(/\s{2,}/)[0].trim()

/** The connection's example config as a YAML flow map, mirroring what `dst init` writes. */
function yamlConfig(spec: WarehouseSpec): string {
  const pairs = spec.fields
    .map((f) => [f.key, exampleValue(f)] as const)
    .filter(([, v]) => v !== '')
    .map(([k, v]) => `${k}: ${v}`)
  return `{${pairs.join(', ')}}`
}

function yamlSnippet(spec: WarehouseSpec): string {
  const env = keyEnv(spec.type)
  const lines = [
    '# dst.yaml — name the connection what you like',
    'connections:',
    `  ${spec.type}:`,
    `    type: ${spec.type}`,
    `    config: ${yamlConfig(spec)}`,
  ]
  if (spec.secret) {
    lines.push(`    secret_env: ${env}  # convention: DST_API_KEY_<NAME>`)
  }
  return lines.join('\n')
}

function envSnippet(spec: WarehouseSpec): string {
  if (!spec.secret) return ''
  const env = keyEnv(spec.type)
  if (spec.secret.multiline) {
    // Service-account JSON etc. — the @path file-ref keeps the blob out of .env.
    return [
      '# .env — secrets only, never committed',
      `# @/path loads that file's contents server-side (the ${spec.secret.label.toLowerCase()} itself)`,
      `${env}=@/path/to/service-account.json`,
    ].join('\n')
  }
  const placeholder = 'the-' + spec.secret.label.toLowerCase().replace(/\s+/g, '-')
  return [
    '# .env — secrets only, never committed',
    `${env}=${placeholder}`,
    '# a value of @/path/to/file loads that file\'s contents instead',
  ].join('\n')
}

/** Copy-able code block — same idiom as Settings' CodeBlock. */
function CopyBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    })
  }
  return (
    <div className="relative">
      <pre className="overflow-x-auto rounded-md border border-border bg-bg px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-text">
        {code}
      </pre>
      <button
        type="button"
        onClick={copy}
        className={[
          'absolute right-2 top-2 rounded border px-2 py-0.5 text-[10px] font-medium transition-colors',
          copied
            ? 'border-green/30 bg-green-bg text-green'
            : 'border-border bg-surface text-muted hover:text-text hover:bg-surface-2',
        ].join(' ')}
        style={{ transitionDuration: 'var(--duration-fast)' }}
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <h4 className="panel-label">
      {children}
    </h4>
  )
}

export function ConnectionDeclareGuide({ spec }: { spec: WarehouseSpec }) {
  const env = keyEnv(spec.type)
  return (
    <div
      className="mt-4 overflow-hidden rounded-lg border border-border bg-surface"
      style={{ boxShadow: 'var(--shadow-card)' }}
    >
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface-2 px-4 py-3">
        <div className="flex items-center gap-3">
          <ConnectionLogo type={spec.type} size="md" />
          <div>
            <span className="text-[13px] font-bold text-text">
              Declare a {spec.label} connection
            </span>
            <p className="mt-0.5 text-[11px] leading-snug text-muted">{spec.blurb}</p>
          </div>
        </div>
        {spec.docsUrl && (
          <a
            href={spec.docsUrl}
            target="_blank"
            rel="noreferrer"
            className="text-[11px] font-medium text-accent-dark underline-offset-2 hover:underline"
          >
            {spec.docsLabel ?? 'Provider docs'} ↗
          </a>
        )}
      </div>

      <div className="grid grid-cols-1 gap-x-8 gap-y-5 px-4 py-4 md:grid-cols-2">
        {/* Left: the declaration to copy */}
        <div className="space-y-4">
          <div>
            <SectionLabel>1 · Declare it in dst.yaml</SectionLabel>
            <div className="mt-2">
              <CopyBlock code={yamlSnippet(spec)} />
            </div>
          </div>

          {spec.secret && (
            <div>
              <SectionLabel>2 · Put the {spec.secret.label.toLowerCase()} in .env</SectionLabel>
              <div className="mt-2">
                <CopyBlock code={envSnippet(spec)} />
              </div>
            </div>
          )}

          <div>
            <SectionLabel>{spec.secret ? '3' : '2'} · Land it</SectionLabel>
            <div className="mt-2">
              <CopyBlock code="dst apply --token <dstadm-token>" />
            </div>
            <p className="mt-2 text-[12px] leading-relaxed text-muted">
              Apply probes the credential (connect + read) before accepting the connection — a
              failing {spec.secret ? <span className="font-mono text-text">{env}</span> : 'setup'}{' '}
              is rejected with the env ref to fix, and any previously working credential is kept.
            </p>
          </div>
        </div>

        {/* Right: the credential roles + the provider setup that grants them */}
        <div className="space-y-4">
          <div>
            <SectionLabel>Credential roles</SectionLabel>
            <ul className="mt-2 space-y-1">
              {spec.permissions.read.map((p) => (
                <li key={p} className="flex items-start gap-2 text-[12px] leading-snug text-text">
                  <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-accent" aria-hidden="true" />
                  {p}
                </li>
              ))}
              {spec.permissions.write.map((p) => (
                <li key={p} className="flex items-start gap-2 text-[12px] leading-snug text-muted">
                  <span
                    className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-border-strong"
                    aria-hidden="true"
                  />
                  {p} <span className="text-muted-2">(only for write access)</span>
                </li>
              ))}
            </ul>
          </div>

          <div>
            <SectionLabel>Provider setup</SectionLabel>
            <ol className="mt-2 space-y-3">
              {spec.guide.map((step, i) => (
                <li key={step.title} className="flex gap-2.5">
                  <span className="mt-px font-mono text-[11px] font-semibold tabular-nums text-muted-2">
                    {i + 1}.
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-[12.5px] font-medium leading-snug text-text">{step.title}</p>
                    {step.detail && (
                      <p className="mt-0.5 text-[12px] leading-relaxed text-muted">{step.detail}</p>
                    )}
                    {step.code && (
                      <div className="mt-1.5">
                        <CopyBlock code={step.code} />
                      </div>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>
    </div>
  )
}
