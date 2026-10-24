import { createContext, useContext } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { Scope } from '../shared/contracts.ts';
import type { PanelApi } from '../shared/api.ts';
import type { NativeAgent } from '../shared/validation.ts';
import { samePartition } from '../shared/scope.ts';

// PUI-03: once this host serves the native OpenCode window, ordinary conversation and new work
// go there; the panel keeps old conversations for diagnostic reading only. A host without the
// window keeps the old forms (until the personal launch is switched).
export type Native = { agents: NativeAgent[] } | null;
export const NativeEntry = createContext<Native>(null);
export const useNativeEntry = () => useContext(NativeEntry);

export function useNative(api: PanelApi): Native {
  const query = useQuery({
    queryKey: ['native-entry'], staleTime: 60000, retry: false,
    queryFn: async ({ signal }) => (await api.nativeWindow(signal)) ? { agents: await api.nativeAgents(signal) } : null,
  });
  // A failed refetch must not keep a formerly successful probe's cached read-only mode.
  return query.isError ? null : query.data ?? null;
}

// The native address of a person (and one of its conversations): base64url of the directory.
export function nativeHref(directory: string, session?: string): string {
  const slug = btoa(directory).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return '/opencode/' + slug + '/session' + (session ? '/' + encodeURIComponent(session) : '');
}
export function nativeAgent(native: Native, scope: Scope): NativeAgent | undefined {
  return native?.agents.find((agent) => samePartition(agent.scope, scope));
}
