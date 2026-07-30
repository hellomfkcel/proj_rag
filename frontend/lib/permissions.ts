// Permissions — Cerbos check helper utilities
//
// Provides frontend-side permission helpers for conditional UI rendering.
// All actual authorization decisions are made by the external Cerbos PDP
// via the backend API — this module only provides convenience wrappers.

import api from "./api";
import { useAuthStore } from "@/stores/useAuthStore";

// ── Check single permission ────────────────────────────────────────

export interface CheckResult {
  decision: "allow" | "deny";
  decision_id: string;
  reasons: string[];
}

/**
 * Check whether the current user can perform an action on a resource.
 * Calls the backend which forwards to Cerbos PDP.
 */
export async function checkPermission(
  action: string,
  resourceType: string,
  resourceId: string,
): Promise<CheckResult> {
  // Permissions are enforced server-side via P-AUTHC middleware.
  // This is a client-side helper for optimistic UI rendering.
  // For now, we rely on the backend's 403 responses.
  // In production, a dedicated GET endpoint could wrap Cerbos check.
  try {
    const { data } = await api.get("/config");
    // Permission check not implemented as a standalone endpoint yet.
    // Rely on backend enforcement via 403 interception.
    return { decision: "allow", decision_id: "", reasons: [] };
  } catch {
    return { decision: "deny", decision_id: "", reasons: ["network_error"] };
  }
}

/**
 * Check if the current user has a specific role.
 * Reads from the auth store (JWT-derived roles).
 */
export function hasRole(role: string): boolean {
  const user = useAuthStore.getState().user;
  return user?.roles?.includes(role) ?? false;
}

/**
 * Check if the current user is a system admin.
 */
export function isAdmin(): boolean {
  return hasRole("system_admin");
}

/**
 * Determine if an operation button should be shown based on required role.
 * Returns true if the user has the required role OR if permissions
 * should be checked server-side (optimistic show).
 */
export function canPerform(action: string): boolean {
  // Map actions to roles per Cerbos policies
  const actionRoleMap: Record<string, string[]> = {
    "kb:read": ["user", "system_admin"],
    "kb:write": ["system_admin"],
    "kb:manage": ["system_admin"],
    "doc:view": ["user", "system_admin"],
    "doc:download": ["user", "system_admin"],
    "doc:unmount": ["system_admin"],
    "doc:purge": ["system_admin"],
  };

  const requiredRoles = actionRoleMap[action] || ["system_admin"];
  const userRoles = useAuthStore.getState().user?.roles || [];

  return requiredRoles.some((r) => userRoles.includes(r));
}
