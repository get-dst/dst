import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { getToken, setToken } from '../api/auth'

// Admin-token entry: paste the dstadm_… token `dst bootstrap` mints. Saved to localStorage.
export function TokenBar() {
  const qc = useQueryClient()
  const [val, setVal] = useState(getToken())
  const [saved, setSaved] = useState(false)

  const save = () => {
    setToken(val.trim())
    qc.invalidateQueries()
    setSaved(true)
    setTimeout(() => setSaved(false), 1600)
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') save()
  }

  const hasValue = val.trim().length > 0

  return (
    <div className="flex items-center gap-1.5">
      <div className="relative">
        <input
          type="password"
          value={val}
          onChange={(e) => { setVal(e.target.value); setSaved(false) }}
          onKeyDown={handleKeyDown}
          placeholder="dstadm_…"
          aria-label="Admin token"
          className={[
            'w-40 rounded-md border bg-surface px-2.5 py-1.5',
            'font-mono text-[12px] text-text placeholder:text-muted-2',
            'transition-colors outline-none',
            'hover:border-border-strong',
            'focus:border-accent focus:ring-2 focus:ring-accent/20',
            hasValue ? 'border-border-strong' : 'border-border',
          ].join(' ')}
          style={{ transitionDuration: 'var(--duration-fast)' }}
        />
      </div>
      <button
        type="button"
        onClick={save}
        className={[
          'rounded-md px-2.5 py-1.5 text-[12px] font-medium transition-all cursor-pointer select-none',
          'outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1',
          saved
            ? 'bg-green-bg text-green border border-green/25'
            : 'bg-text text-bg border border-transparent hover:bg-[#2d2c2a] active:bg-[#3d3c3a] active:scale-[0.98]',
        ].join(' ')}
        style={{ transitionDuration: 'var(--duration-fast)', minWidth: '46px' }}
      >
        {saved ? (
          <span className="flex items-center gap-1">
            <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true">
              <path d="M1.5 5l2.5 2.5 4.5-4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            OK
          </span>
        ) : (
          'Save'
        )}
      </button>
    </div>
  )
}
