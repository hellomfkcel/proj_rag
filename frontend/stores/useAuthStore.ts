// Zustand Auth Store — persistent across refresh, session timeout, multi-tenant
"use client";
import { create } from "zustand";

interface User { id: string; tenant_id: string; roles: string[]; name: string; }

interface Tenant {
  id: string;
  name: string;
  kb_count: number;
  doc_count: number;
}

interface AuthState {
  // Core auth
  token: string | null;
  user: User | null;
  expiresAt: number | null;
  hydrated: boolean;

  // Multi-tenant
  availableTenants: Tenant[];

  // Session tracking
  lastActivity: number;

  // Actions
  login: (t: string, u: User, e: number) => void;
  logout: () => void;
  hydrate: () => void;
  checkSession: () => boolean; // returns true if session expired
  recordActivity: () => void;
  switchTenant: (tenantId: string) => Promise<void>;
  setAvailableTenants: (tenants: Tenant[]) => void;
}

const SESSION_TIMEOUT_MS = 60 * 60 * 1000; // 1 hour

export const useAuthStore = create<AuthState>((set, get) => ({
  token: null,
  user: null,
  expiresAt: null,
  hydrated: false,
  availableTenants: [],
  lastActivity: Date.now(),

  login: (token, user, expiresAt) => {
    if (typeof window !== "undefined") {
      localStorage.setItem("access_token", token);
      localStorage.setItem("user", JSON.stringify(user));
      localStorage.setItem("expires_at", String(expiresAt));
    }
    set({ token, user, expiresAt, hydrated: true, lastActivity: Date.now() });
  },

  logout: () => {
    if (typeof window !== "undefined") {
      localStorage.removeItem("access_token");
      localStorage.removeItem("user");
      localStorage.removeItem("expires_at");
    }
    set({ token: null, user: null, expiresAt: null, hydrated: true, availableTenants: [] });
    if (typeof window !== "undefined") window.location.href = "/login";
  },

  hydrate: () => {
    if (typeof window === "undefined") return;
    try {
      const t = localStorage.getItem("access_token");
      const u = localStorage.getItem("user");
      const expStr = localStorage.getItem("expires_at");
      if (t && u) {
        const user = JSON.parse(u);
        const exp = expStr ? Number(expStr) : 0;
        // Also try JWT exp claim as fallback
        let jwtExp = 0;
        try {
          const p = JSON.parse(atob(t.split(".")[1]));
          jwtExp = (p.exp || 0) * 1000;
        } catch {}
        const expiresAt = exp || jwtExp;

        if (expiresAt > Date.now()) {
          set({ token: t, user, expiresAt, hydrated: true, lastActivity: Date.now() });
          return;
        }
      }
    } catch {}
    set({ hydrated: true });
  },

  checkSession: () => {
    const state = get();
    if (!state.token) return true; // not logged in, treat as expired

    const now = Date.now();

    // Check JWT expiry
    if (state.expiresAt && now > state.expiresAt) return true;

    // Check inactivity timeout
    if (now - state.lastActivity > SESSION_TIMEOUT_MS) return true;

    return false;
  },

  recordActivity: () => {
    set({ lastActivity: Date.now() });
  },

  switchTenant: async (tenantId: string) => {
    const state = get();
    if (!state.user) return;

    // Dev mode: re-issue dev-login with new tenant
    const role = state.user.roles[0] || "user";
    const username = state.user.id;

    try {
      const axios = (await import("axios")).default;
      const resp = await axios.post("/api/v1/auth/dev-login", {
        username,
        tenant: tenantId,
        role,
      });
      const { access_token, expires_at, user } = resp.data;
      const userObj: User = {
        id: user.id,
        tenant_id: user.tenant_id,
        roles: user.roles,
        name: user.name || user.id,
      };
      get().login(access_token, userObj, new Date(expires_at).getTime());
      window.location.reload(); // Full reload to refresh all data under new tenant
    } catch (e) {
      console.error("Tenant switch failed", e);
    }
  },

  setAvailableTenants: (tenants: Tenant[]) => {
    set({ availableTenants: tenants });
  },
}));
