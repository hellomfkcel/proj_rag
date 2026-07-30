"use client";
// ClientShell — ErrorBoundary ▶ AuthGuard ▶ PageChildren + ToastContainer

import { useEffect } from "react";
import AuthGuard from "@/app/components/AuthGuard";
import ErrorBoundary from "@/app/components/ErrorBoundary";
import ToastContainer from "@/app/components/Toast";
import { QueryProvider } from "@/lib/query-provider";
import { setAdminConsoleUrl } from "@/lib/api";
import { getAppConfig } from "@/lib/settings";

export default function ClientShell({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    getAppConfig().then((cfg) => {
      if (cfg.admin_console_url) setAdminConsoleUrl(cfg.admin_console_url);
    }).catch(() => {});
  }, []);

  return (
    <ErrorBoundary>
      <QueryProvider>
        <AuthGuard>
          <PageTransition>{children}</PageTransition>
          <ToastContainer />
        </AuthGuard>
      </QueryProvider>
    </ErrorBoundary>
  );
}

/** Smooth page transition — subtle fade-in to prevent jarring content swaps. */
function PageTransition({ children }: { children: React.ReactNode }) {
  return (
    <div className="animate-fade-in">
      {children}
    </div>
  );
}
