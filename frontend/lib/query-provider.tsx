"use client";
// TanStack Query Provider — wraps the app for useQuery/useMutation support

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 3_000,       // 3s before data is considered stale
            refetchInterval: 3_000,  // Auto-refetch every 3s (for doc status polling)
            retry: 1,
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}
