import { useQuery } from '@tanstack/react-query'
import { apiGet } from './client'

/** One file in the materialized lens tree. */
export interface RepoFile {
  path: string
  size: number
}

export interface RepoTree {
  files: RepoFile[]
}

export interface RepoFileContent {
  path: string
  content: string
}

/** One entry in a lens's published history. */
export interface LensVersion {
  version: number
  summary: string
  created_at: string
  /** Who published: `human:<email>` | `token:<label>` | `process:<id>`; '' = unknown. */
  created_by: string
}

export interface DiffEntry {
  path: string
  diff: string
}

export interface RepoDiff {
  from: number
  to: number
  files: DiffEntry[]
}

const enc = encodeURIComponent

/** The lens materialized as a file tree (paths + sizes). */
export function useLensRepo(name: string) {
  return useQuery({
    queryKey: ['repo', name],
    queryFn: () => apiGet<RepoTree>(`/mgmt/lenses/${enc(name)}/repo`),
    enabled: !!name,
  })
}

/** One file's contents from the live tree. */
export function useLensFile(name: string, path: string | null) {
  return useQuery({
    queryKey: ['repo-file', name, path],
    queryFn: () => apiGet<RepoFileContent>(`/mgmt/lenses/${enc(name)}/repo/file?path=${enc(path!)}`),
    enabled: !!name && !!path,
  })
}

/** A lens's published history, newest first. */
export function useLensVersions(name: string) {
  return useQuery({
    queryKey: ['versions', name],
    queryFn: () => apiGet<LensVersion[]>(`/mgmt/lenses/${enc(name)}/versions`),
    enabled: !!name,
  })
}

/** Unified diff between two published versions. */
export function useLensDiff(name: string, from: number | null, to: number | null) {
  return useQuery({
    queryKey: ['diff', name, from, to],
    queryFn: () => apiGet<RepoDiff>(`/mgmt/lenses/${enc(name)}/diff?from=${from}&to=${to}`),
    enabled: !!name && from != null && to != null && from !== to,
  })
}
