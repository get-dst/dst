import { vi } from 'vitest'

/** A working in-memory `localStorage` for tests that exercise credential storage.
 *
 * The test environment provides none at all, and the auth module deliberately
 * tolerates that (`typeof localStorage !== 'undefined'`), so a test asserting what
 * is or is not stored needs a real one — a stub whose setItem is a spy would pass
 * whether or not the code stored a secret. */
export function stubLocalStorage(initial: Record<string, string> = {}): Map<string, string> {
  const cells = new Map(Object.entries(initial))
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => cells.get(k) ?? null,
    setItem: (k: string, v: string) => void cells.set(k, String(v)),
    removeItem: (k: string) => void cells.delete(k),
    clear: () => cells.clear(),
    key: (i: number) => [...cells.keys()][i] ?? null,
    get length() {
      return cells.size
    },
  })
  return cells
}
