import { QueryClient } from '@tanstack/react-query';
export function createProjectionCache(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 5000, gcTime: 60000, refetchOnWindowFocus: false, refetchOnReconnect: false },
      mutations: { retry: false },
    },
  });
}
