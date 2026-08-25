import { useQuery } from '@tanstack/react-query'
import { apiGet } from './client'

export interface ContextSource {
  source: string
  chunks: number
  last_indexed: string | null
}

export function useContext(name: string) {
  return useQuery({
    queryKey: ['context', name],
    queryFn: () => apiGet<{ lens: string; sources: ContextSource[] }>(`/mgmt/lenses/${name}/context`),
    enabled: !!name,
  })
}

// Context is synced, not authored: the ingest POST carries the source + scopes
// directly (nothing in lens.yaml). The UI reads the indexed result only.

export interface ContextChunk {
  id: string
  source: string
  text: string
}

export function useContextChunks(name: string, source: string | null) {
  return useQuery({
    queryKey: ['context-chunks', name, source],
    queryFn: () =>
      apiGet<{ chunks: ContextChunk[] }>(
        `/mgmt/lenses/${name}/context/chunks?source=${encodeURIComponent(source ?? '')}`,
      ),
    enabled: !!name && !!source,
  })
}
