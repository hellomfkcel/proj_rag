// Auth API — JWT utilities + tenant + refresh + SSO

import api from "./api";

export interface User {
  id: string;
  tenant_id: string;
  roles: string[];
  name: string;
}

export interface Tenant {
  id: string;
  name: string;
  kb_count: number;
  doc_count: number;
}

// ── JWT Utilities ──────────────────────────────────────────────

export function decodeToken(token: string): { sub: string; tenant: string; roles: string[]; exp: number } | null {
  try {
    const payload = token.split(".")[1];
    const decoded = JSON.parse(atob(payload));
    return {
      sub: decoded.sub || "unknown",
      tenant: decoded.tenant || "tenant-dev",
      roles: decoded.roles || ["user"],
      exp: decoded.exp || 0,
    };
  } catch {
    return null;
  }
}

export function isTokenExpired(token: string): boolean {
  const decoded = decodeToken(token);
  if (!decoded) return true;
  return decoded.exp * 1000 < Date.now();
}

export function loadUser(): User | null {
  try {
    const raw = localStorage.getItem("user");
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

// ── Tenant API ─────────────────────────────────────────────────

export async function listTenants(): Promise<Tenant[]> {
  const { data } = await api.get("/tenants");
  return data;
}

// ── Token Refresh ──────────────────────────────────────────────

export async function refreshToken(): Promise<{ access_token: string; expires_at: string }> {
  const { data } = await api.post("/auth/refresh");
  return data;
}

// ── Production SSO ─────────────────────────────────────────────

export async function exchangeCode(code: string): Promise<{ access_token: string; refresh_token?: string; expires_at: string; user: User }> {
  const { data } = await api.post("/auth/token", { code });
  return data;
}
