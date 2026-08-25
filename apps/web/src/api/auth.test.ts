import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { stubLocalStorage } from '../test/storage'
import {
  MAX_ORGS,
  clearCredentials,
  credentialKind,
  getOrgs,
  getToken,
  hasCredential,
  markSignedIn,
  registerOrg,
  setToken,
} from './auth'

let cells: Map<string, string>
beforeEach(() => {
  cells = stubLocalStorage()
})
afterEach(() => vi.unstubAllGlobals())

test('a browser sign-in stores no credential, only the fact of one', () => {
  markSignedIn()

  // The dstsess_ token is in the httpOnly cookie and must never be written here:
  // whatever is in localStorage is readable by any script that reaches this origin.
  expect(getToken()).toBe('')
  expect([...cells.values()].join()).not.toContain('dstsess_')

  // …and the app still knows it is signed in.
  expect(hasCredential()).toBe(true)
  expect(credentialKind()).toBe('session')
})

test('a pasted admin token outranks a session, and says so', () => {
  markSignedIn()
  setToken('dstadm_pasted')
  // The bearer header beats the session cookie server-side, so the label has to
  // follow the token — a UI claiming "session" while acting as another org lies.
  expect(credentialKind()).toBe('token')
})

test('the org registry is bounded', () => {
  // Long-lived dstadm_ tokens: unbounded accumulation meant every org this browser
  // ever touched kept a live admin credential in localStorage forever.
  for (let i = 0; i < MAX_ORGS + 5; i++) registerOrg(`org-${i}`, `dstadm_${i}`)

  const orgs = getOrgs()
  expect(orgs).toHaveLength(MAX_ORGS)
  expect(orgs.map((o) => o.name)).not.toContain('org-0') // oldest dropped
  expect(orgs.at(-1)?.name).toBe(`org-${MAX_ORGS + 4}`) // newest kept
})

test('re-registering an org updates it in place rather than growing the list', () => {
  registerOrg('Acme', 'dstadm_a')
  registerOrg('Acme renamed', 'dstadm_a')
  expect(getOrgs()).toEqual([{ name: 'Acme renamed', token: 'dstadm_a' }])
})

test('sign-out clears every credential, not just the active one', () => {
  markSignedIn()
  setToken('dstadm_current')
  registerOrg('Current', 'dstadm_current')
  registerOrg('Another org', 'dstadm_other')

  clearCredentials()

  // The other org's admin token used to survive sign-out, sitting in localStorage
  // on a machine whose user had just said they were done.
  expect(getToken()).toBe('')
  expect(getOrgs()).toEqual([])
  expect(hasCredential()).toBe(false)
  expect(credentialKind()).toBe('none')
  expect([...cells.values()].join()).not.toContain('dstadm_')
})
