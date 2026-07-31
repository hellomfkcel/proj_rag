// Permissions — 前端细粒度按钮权限控制
//
// 设计依据：docs/权限管理系统架构设计.md §6.4 阶段三
//          + docs/RAG系统设计v14.md §6.3 check
//
// 所有授权决策经后端 P-AUTHC → 权限服务（Cerbos PDP），前端不执行任何本地判定。
// canPerform() 调 POST /api/v1/auth/check-permission 获取真实判定结果。

import api from "./api";
import { useAuthStore } from "@/stores/useAuthStore";

// ── Check single permission (real backend call) ────────────────────

export interface CheckResult {
  decision: "allow" | "deny" | "indeterminate";
  decision_id: string;
  reasons: string[];
}

/** 内存缓存：暂存最近的判定结果（TTL 30s），避免重复请求。 */
const _checkCache = new Map<string, { result: CheckResult; expiresAt: number }>();
const CACHE_TTL_MS = 30_000;

function _cacheKey(action: string, resourceType: string, resourceId: string): string {
  return `${action}:${resourceType}:${resourceId}`;
}

/**
 * 检查当前用户是否可以对指定资源执行操作。
 *
 * 调用后端 POST /api/v1/auth/check-permission → P-AUTHC → 权限服务，
 * 获取真实权限判定结果。结果缓存 30 秒以减少重复调用。
 *
 * @param action      动词（如 "kb:write", "doc:download"）
 * @param resourceType 资源类型（"kb" | "document"）
 * @param resourceId   资源 ID
 * @param channelKb    通道类动词必带（如 "doc:unmount"）
 */
export async function checkPermission(
  action: string,
  resourceType: string,
  resourceId: string,
  channelKb?: string,
): Promise<CheckResult> {
  const key = _cacheKey(action, resourceType, resourceId);
  const cached = _checkCache.get(key);
  if (cached && cached.expiresAt > Date.now()) {
    return cached.result;
  }

  try {
    const { data } = await api.post("/auth/check-permission", {
      action,
      resource_type: resourceType,
      resource_id: resourceId,
      channel_kb: channelKb || undefined,
    });
    const result: CheckResult = {
      decision: data.decision,
      decision_id: data.decision_id,
      reasons: data.reasons || [],
    };
    _checkCache.set(key, { result, expiresAt: Date.now() + CACHE_TTL_MS });
    return result;
  } catch (err: any) {
    // 网络错误或后端不可达 → 保守拒绝（fail-closed）
    if (err?.response?.status === 401) {
      return { decision: "deny", decision_id: "", reasons: ["unauthenticated"] };
    }
    // 后端返回 403 表示权限不足（正常 deny）
    if (err?.response?.status === 403) {
      return { decision: "deny", decision_id: "", reasons: ["forbidden"] };
    }
    // 其他错误 → 保守拒绝
    console.warn("[permissions] checkPermission failed:", err?.message || err);
    return { decision: "deny", decision_id: "", reasons: ["check_error"] };
  }
}

/**
 * 检查当前用户是否有指定角色（从 JWT claims 本地读取）。
 *
 * 仅用于乐观 UI 渲染（快速显示/隐藏非敏感元素）。
 * 最终权限判定由后端 P-AUTHC 强制执行。
 */
export function hasRole(role: string): boolean {
  const user = useAuthStore.getState().user;
  return user?.roles?.includes(role) ?? false;
}

/**
 * 检查当前用户是否为系统管理员（从 JWT claims 本地读取）。
 */
export function isAdmin(): boolean {
  return hasRole("system_admin");
}

/**
 * 判断操作按钮是否应该显示。
 *
 * 优先调用后端 /v1/check 获取真实判定，回退到角色映射（乐观渲染）。
 * 后端在 API 层强制执行最终权限判定——前端按钮可见不等于操作可通过。
 *
 * 异步版本 — 用于需要真实权限判定的关键操作按钮。
 *
 * @param action      动词
 * @param resourceType 资源类型
 * @param resourceId   资源 ID
 */
export async function canPerformAsync(
  action: string,
  resourceType: string,
  resourceId: string,
): Promise<boolean> {
  const result = await checkPermission(action, resourceType, resourceId);
  return result.decision === "allow";
}

/**
 * 同步版本 — 基于本地角色的乐观渲染。
 *
 * 用于非关键 UI 元素（如快速显示/隐藏按钮骨架）。
 * 对于关键操作（删除、下载、授权），请使用 canPerformAsync() 获取真实判定。
 *
 * @deprecated 关键操作请使用 canPerformAsync() 获取后端真实判定。
 */
export function canPerform(action: string): boolean {
  const userRoles = useAuthStore.getState().user?.roles || [];
  // 管理员无条件显示所有操作按钮（乐观渲染，后端最终判定）
  if (userRoles.includes("system_admin")) return true;
  // 普通用户显示读取类操作，写入类操作需后端判定
  const readActions = ["kb:read", "doc:view"];
  return readActions.includes(action);
}
