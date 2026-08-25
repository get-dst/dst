import { useState } from 'react'
import { markSignedIn, setToken } from '../api/auth'
import { login } from '../api/session'
import { DstMark } from '../components/Header'

// Local sign-in (self-host, no Clerk): email+password against /auth/login.
//
// The response carries the dstsess_ token in its body AND as an httpOnly cookie.
// We keep the cookie and drop the body token on the floor: same-origin requests
// send the cookie by themselves, and a token no script can read is a token an
// injection cannot exfiltrate. localStorage only records THAT a session exists.
// Token paste stays as the escape hatch for automation-only installs.

const inputCls = [
  'w-full rounded-md border bg-surface px-2.5 py-1.5 text-[13px] text-text',
  'placeholder:text-muted-2 transition-colors outline-none',
  'border-border hover:border-border-strong',
  'focus:border-accent focus:ring-2 focus:ring-accent/20',
].join(' ')

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <span className="mb-1 block text-[10px] font-semibold uppercase tracking-wide text-muted-2">
      {children}
    </span>
  )
}

export function Login() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [tokenMode, setTokenMode] = useState(false)
  const [tokenVal, setTokenVal] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Hard-reload after either path, so the whole app queries under the fresh credential.
  const enterWithToken = (token: string) => {
    setToken(token.trim())
    window.location.assign('/')
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    if (tokenMode) {
      if (tokenVal.trim()) enterWithToken(tokenVal)
      return
    }
    setPending(true)
    try {
      await login(email, password)
      markSignedIn() // the session itself is in the httpOnly cookie
      window.location.assign('/')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setPending(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center px-4">
      <div className="w-full max-w-[360px]">
        <div className="mb-6 flex items-center justify-center">
          <DstMark size={40} />
        </div>

        <form
          onSubmit={submit}
          className="rounded-lg border border-border bg-surface p-5"
          style={{ boxShadow: 'var(--shadow-card)' }}
        >
          {tokenMode ? (
            <label className="block">
              <FieldLabel>Admin token</FieldLabel>
              <input
                type="password"
                value={tokenVal}
                onChange={(e) => setTokenVal(e.target.value)}
                placeholder="dstadm_…"
                autoFocus
                className={`${inputCls} font-mono text-[12px]`}
                style={{ transitionDuration: 'var(--duration-fast)' }}
              />
            </label>
          ) : (
            <>
              <label className="block">
                <FieldLabel>Email</FieldLabel>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoComplete="email"
                  autoFocus
                  required
                  className={inputCls}
                  style={{ transitionDuration: 'var(--duration-fast)' }}
                />
              </label>
              <label className="mt-3 block">
                <FieldLabel>Password</FieldLabel>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  required
                  className={inputCls}
                  style={{ transitionDuration: 'var(--duration-fast)' }}
                />
              </label>
            </>
          )}

          {error && (
            <div className="mt-3 rounded-md border border-red/25 bg-red-bg px-2.5 py-1.5 text-[12px] text-red">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={pending}
            className={[
              'mt-4 w-full rounded-md border border-transparent bg-text px-3 py-2',
              'text-[13px] font-medium text-bg transition-all cursor-pointer select-none',
              'outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1',
              'hover:bg-[#2d2c2a] active:bg-[#3d3c3a] active:scale-[0.99]',
              'disabled:cursor-default disabled:opacity-60',
            ].join(' ')}
            style={{ transitionDuration: 'var(--duration-fast)' }}
          >
            {pending ? 'Signing in…' : tokenMode ? 'Continue with token' : 'Sign in'}
          </button>

          <button
            type="button"
            onClick={() => {
              setTokenMode((v) => !v)
              setError(null)
            }}
            className={[
              'mt-3 w-full text-center text-[12px] text-muted-2 transition-colors',
              'cursor-pointer hover:text-text outline-none rounded-sm',
              'focus-visible:ring-2 focus-visible:ring-accent',
            ].join(' ')}
            style={{ transitionDuration: 'var(--duration-fast)' }}
          >
            {tokenMode ? 'Sign in with email instead' : 'Use an admin token instead'}
          </button>
        </form>

        <p className="mt-4 text-center text-[12px] leading-relaxed text-muted-2">
          First run? Create the admin account with{' '}
          <code className="rounded border border-border bg-surface-2 px-1 py-0.5 font-mono text-[11px] text-muted">
            dst bootstrap --email you@…
          </code>
        </p>
      </div>
    </div>
  )
}
