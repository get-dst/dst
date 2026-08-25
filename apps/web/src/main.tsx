import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import { ClerkProvider, SignedIn, SignedOut, SignIn } from '@clerk/clerk-react'
import './index.css'
import App from './App'
import { ClerkTokenBridge } from './components/ClerkTokenBridge'
import { hasCredential } from './api/auth'
import { Login } from './pages/Login'

const queryClient = new QueryClient()
const clerkKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined

const inner = clerkKey ? (
  <ClerkProvider publishableKey={clerkKey}>
    <ClerkTokenBridge />
    <SignedIn>
      <App clerkEnabled />
    </SignedIn>
    <SignedOut>
      <div className="flex h-full items-center justify-center">
        <SignIn routing="hash" />
      </div>
    </SignedOut>
  </ClerkProvider>
) : hasCredential() ? (
  // No Clerk (self-host): either credential opens the dashboard — a browser
  // sign-in (cookie, flagged in localStorage) or a pasted dstadm_ token. The
  // server re-checks on every request; this only picks which screen to render.
  <App />
) : (
  <Login />
)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>{inner}</BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
