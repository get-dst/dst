import { useEffect } from 'react'
import { useAuth } from '@clerk/clerk-react'
import { setClerkTokenGetter } from '../api/clerkToken'

// Registers Clerk's getToken so the API client can attach the session JWT.
export function ClerkTokenBridge() {
  const { getToken } = useAuth()
  useEffect(() => {
    setClerkTokenGetter(() => getToken())
    return () => setClerkTokenGetter(null)
  }, [getToken])
  return null
}
