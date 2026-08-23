"use client";
// ClientShell — ErrorBoundary ▶ AuthGuard ▶ PageChildren + ToastContainer

import { useEffect } from "react";
import AuthGuard from "@/app/components/AuthGuard";
import ErrorBoundary from "@/app/components/ErrorBoundary";
import ToastContainer from "@/app/components/Toast";
import { QueryProvider } from "@/lib/query-provider";
import { setAdminConsoleUrl } from "@/lib/api";
import { getAppConfig } from "@/lib/settings";
import { useAuthStore } from "@/stores/useAuthStore";

export default function ClientShell({ children }: { children: React.ReactNode }) {
  const token = useAuthStore((s) => s.token);
  useEffect(() => {
    // 未登录（含 SSO 回调 /auth/callback 换码前）不请求 /config：否则 401 会被拦截器弹回登录页，中断换码
    if (!token) return;
    getAppConfig().then((cfg) => {
      if (cfg.admin_console_url) setAdminConsoleUrl(cfg.admin_console_url);
    }).catch(() => {});
  }, [token]);

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
