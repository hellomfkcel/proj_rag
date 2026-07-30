// Axios instance — Phase 1 auth interceptor + Phase 3 error handling

import axios from "axios";

const api = axios.create({
  baseURL: "/api/v1",
  timeout: 120000,
  headers: { "Content-Type": "application/json" },
});

// ── Error code → user-friendly messages ──────────────────────────

const ERROR_MESSAGES: Record<string, string> = {
  "auth:unauthenticated": "请重新登录",
  "auth:forbidden": "权限不足，无法执行此操作",
  "auth:authz_unavailable": "权限服务暂时不可用，请稍后重试",
  "doc:not_found": "文档不存在",
  "doc:not_parsed": "文档尚未解析",
  "kb:not_found": "知识库不存在",
  "retrieve:insufficient_evidence": "未找到足够的相关信息",
  "retrieve:vector_store_unavailable": "检索服务暂时不可用",
  "chat:stream_timeout": "生成超时，请重试",
};

function friendlyMessage(err: any): string {
  const code = err?.response?.data?.error_code || err?.response?.data?.detail || "";
  if (ERROR_MESSAGES[code]) return ERROR_MESSAGES[code];
  if (err?.response?.data?.message) return err.response.data.message;
  if (err?.message === "Network Error") return "网络连接失败，请检查网络";
  if (err?.code === "ECONNABORTED") return "请求超时，请重试";
  return "服务器内部错误，请稍后重试";
}

// ── Request interceptor ──────────────────────────────────────────

api.interceptors.request.use((config) => {
  const token = localStorage.getItem("access_token");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// ── Response interceptor ──────────────────────────────────────────

api.interceptors.response.use(
  (res) => res,
  (err) => {
    const status = err.response?.status;
    const msg = friendlyMessage(err);

    // Lazy import to avoid circular deps
    if (typeof window !== "undefined") {
      import("@/app/components/Toast").then(({ toastError, toastWarning }) => {
        if (status === 401) {
          // Already handled below — don't double-toast
        } else if (status === 403) {
          const adminUrl = (window as any).__ADMIN_CONSOLE_URL__;
          toastError(msg, adminUrl ? { label: "前往管理台", onClick: () => window.open(adminUrl, "_blank") } : undefined);
        } else if (status === 503) {
          toastWarning(msg, { label: "重试", onClick: () => window.location.reload() });
        } else if (status >= 500) {
          toastError(msg);
        }
        // 4xx (except 401/403) are handled by page-level catch blocks
      }).catch(() => {});
    }

    // 401 — clear token and redirect
    if (status === 401) {
      localStorage.removeItem("access_token");
      localStorage.removeItem("user");
      if (typeof window !== "undefined" && window.location.pathname !== "/login") {
        window.location.href = "/login";
      }
    }

    return Promise.reject(err);
  }
);

export default api;

// ── Store admin console URL for toast interceptor ─────────────────
export function setAdminConsoleUrl(url: string) {
  if (typeof window !== "undefined") {
    (window as any).__ADMIN_CONSOLE_URL__ = url;
  }
}
