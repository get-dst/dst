// Bridges Clerk's session token (from React context) to the plain API client.
let getter: (() => Promise<string | null>) | null = null

export function setClerkTokenGetter(fn: (() => Promise<string | null>) | null): void {
  getter = fn
}

export async function getClerkToken(): Promise<string | null> {
  return getter ? await getter() : null
}
