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
    if (typeof window !== "undefined") {
      // 结束 Keycloak SSO 会话：否则"退出"后 Keycloak 会话仍在，再点 SSO 直接免密进入，无法切账号。
      // 经 permission-nginx :18081 调 end_session，post_logout_redirect_uri 跳回本系统 /login。
      const kcUrl = process.env.NEXT_PUBLIC_KEYCLOAK_URL || window.location.origin;
      const realm = process.env.NEXT_PUBLIC_KEYCLOAK_REALM || "rag-v14";
      const clientId = process.env.NEXT_PUBLIC_KEYCLOAK_CLIENT_ID || "rag-frontend";
      const postLogout = encodeURIComponent(`${window.location.origin}/login`);
      window.location.href =
        `${kcUrl}/realms/${realm}/protocol/openid-connect/logout` +
        `?client_id=${clientId}&post_logout_redirect_uri=${postLogout}`;
    }
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

    // 不允许切换到当前已登录的租户
    if (state.user.tenant_id === tenantId) return;

    try {
      const axios = (await import("axios")).default;
      const resp = await axios.post(
        "/api/v1/auth/switch-tenant",
        { target_tenant: tenantId },
        { headers: { Authorization: `Bearer ${state.token}` } },
      );
      const { access_token, expires_at, user } = resp.data;
      const userObj: User = {
        id: user.id,
        tenant_id: user.tenant_id,
        roles: user.roles,
        name: user.name || user.id,
      };
      get().login(access_token, userObj, new Date(expires_at).getTime());

      // 清除所有租户相关的 localStorage 缓存，防止跨租户数据泄露
      localStorage.removeItem("rag_selected_kbs");
      try { localStorage.removeItem("rag-chat-active-conv"); } catch (_) {}

      // 跳转到 /kb 确保页面状态完全重置
      window.location.href = "/kb";
    } catch (e) {
      console.error("Tenant switch failed", e);
      const msg = e instanceof Error ? e.message : "";
      alert(`切换租户失败: ${msg}`);
    }
  },

  setAvailableTenants: (tenants: Tenant[]) => {
    set({ availableTenants: tenants });
  },
}));
