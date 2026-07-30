"use client";
// Route Guardian — waits for localStorage hydration before checking auth
// Public paths (/login) bypass the guard.

import { useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { useAuthStore } from "@/stores/useAuthStore";

const PUBLIC_PATHS = ["/login"];

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const token = useAuthStore((s) => s.token);
  const hydrated = useAuthStore((s) => s.hydrated);
  const hydrate = useAuthStore((s) => s.hydrate);
  const logout = useAuthStore((s) => s.logout);
  const checkSession = useAuthStore((s) => s.checkSession);
  const router = useRouter();
  const pathname = usePathname();

  const isPublic = PUBLIC_PATHS.some((p) => pathname?.startsWith(p));

  useEffect(() => { hydrate(); }, []);

  // Session timeout checker — runs every 30s when authenticated
  useEffect(() => {
    if (!hydrated || !token) return;
    const interval = setInterval(() => {
      const expired = checkSession();
      if (expired) logout();
    }, 30_000);
    return () => clearInterval(interval);
  }, [hydrated, token, checkSession, logout]);

  // Redirect unauthenticated users
  useEffect(() => {
    if (hydrated && !token && !isPublic) {
      router.push("/login");
    }
  }, [hydrated, token, isPublic, router]);

  // Loading state
  if (!hydrated) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  // On public pages, always render (even if authenticated — login page will redirect on success)
  if (isPublic) return <>{children}</>;

  // On protected pages, require token
  if (!token) return null;

  return <>{children}</>;
}
