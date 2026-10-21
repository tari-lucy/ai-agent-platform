import { useQuery } from '@tanstack/react-query';
import type { Scope } from '../../shared/contracts.ts';
import type { PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';

// One shared query for the work conversations: the task cards and the conversation block read it.
export function useWorkSessions(api: PanelApi, scope: Scope) {
  return useQuery({ queryKey: projectionKey('work-sessions', scope), queryFn: ({ signal }) => api.workSessions(scope, signal) });
}
