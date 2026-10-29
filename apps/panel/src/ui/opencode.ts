import { createContext, useContext } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { Scope } from '../shared/contracts.ts';
import type { PanelApi } from '../shared/api.ts';

// doc33: the persona's own OpenCode server; when the host has one, every link goes there.
export type Harness = import('../shared/contracts.ts').HarnessStatus | null;
export const HarnessEntry = createContext<Harness>(null);
export const useHarnessEntry = () => useContext(HarnessEntry);

export function useHarness(api: PanelApi, scope: Scope): Harness {
  const query = useQuery({
    queryKey: ['harness-status', scope.persona_id, scope.memory_space_id, scope.memory_mode],
    refetchInterval: 10000, retry: false,
    queryFn: ({ signal }) => api.harnessStatus(scope, signal),
  });
  return query.isError ? null : query.data ?? null;
}
