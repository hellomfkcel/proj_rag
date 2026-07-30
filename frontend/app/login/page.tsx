"use client";
// Login Page — dev mode (dynamic tenants) + production SSO placeholder

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import axios from "axios";
import { useAuthStore } from "@/stores/useAuthStore";
import { listTenants } from "@/lib/auth";
import type { User, Tenant } from "@/lib/auth";

const ROLES = [
  { value: "system_admin", label: "系统管理员" },
  { value: "user", label: "普通用户" },
];

export default function LoginPage() {
  const router = useRouter();
  const token = useAuthStore((s) => s.token);
  const login = useAuthStore((s) => s.login);
  const setAvailableTenants = useAuthStore((s) => s.setAvailableTenants);

  const [username, setUsername] = useState("admin");
  const [tenant, setTenant] = useState("tenant-dev");
  const [role, setRole] = useState("system_admin");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantsLoading, setTenantsLoading] = useState(true);

  // Fetch available tenants on mount
  useEffect(() => {
    listTenants()
      .then((data) => {
        setTenants(data);
        setAvailableTenants(data);
        if (data.length > 0) setTenant(data[0].id);
      })
      .catch(() => {
        // Fallback: keep hardcoded default
        setTenants([{ id: "tenant-dev", name: "tenant-dev", kb_count: 0, doc_count: 0 }]);
      })
      .finally(() => setTenantsLoading(false));
  }, [setAvailableTenants]);

  // If already authenticated, redirect to KB page
  useEffect(() => {
    if (token) router.push("/kb");
  }, [token, router]);

  const handleDevLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");

    try {
      const resp = await axios.post("/api/v1/auth/dev-login", {
        username, tenant, role,
      });
      const { access_token, expires_at, user } = resp.data;

      const userObj: User = {
        id: user.id,
        tenant_id: user.tenant_id,
        roles: user.roles,
        name: user.name || user.id,
      };

      login(access_token, userObj, new Date(expires_at).getTime());
      router.push("/kb");
    } catch (err: any) {
      setError(err.response?.data?.detail || err.response?.data?.message || "登录失败，请检查后端服务");
    } finally {
      setLoading(false);
    }
  };

  const handleSSOLogin = () => {
    // Production: redirect to IdP
    // For now, show a notice — actual IdP URL comes from env
    const idpUrl = process.env.NEXT_PUBLIC_IDP_URL;
    if (idpUrl) {
      window.location.href = idpUrl;
    } else {
      setError("SSO 未配置（设置 NEXT_PUBLIC_IDP_URL 环境变量）");
    }
  };

  return (
    <div className="flex items-center justify-center min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100">
      <div className="w-full max-w-md p-8 bg-white rounded-2xl shadow-lg">
        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="text-2xl font-bold text-gray-900">🔐 RAG v14</h1>
          <p className="text-sm text-gray-500 mt-1">企业知识库平台 · 开发模式</p>
        </div>

        {/* Dev Mode Login */}
        <form onSubmit={handleDevLogin} className="space-y-5">
          {/* Username */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">用户名</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full px-4 py-2.5 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none transition"
              placeholder="admin"
            />
          </div>

          {/* Tenant — dynamic from API */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              租户
              {tenantsLoading && <span className="ml-1 text-gray-400 text-xs">加载中...</span>}
            </label>
            <select
              value={tenant}
              onChange={(e) => setTenant(e.target.value)}
              className="w-full px-4 py-2.5 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none transition bg-white"
              disabled={tenantsLoading}
            >
              {tenants.length === 0 && !tenantsLoading && (
                <option value="tenant-dev">tenant-dev</option>
              )}
              {tenants.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name} {t.kb_count > 0 ? `(${t.kb_count} 个知识库)` : ""}
                </option>
              ))}
            </select>
          </div>

          {/* Role */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">角色</label>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="w-full px-4 py-2.5 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 outline-none transition bg-white"
            >
              {ROLES.map((r) => (
                <option key={r.value} value={r.value}>{r.label}</option>
              ))}
            </select>
          </div>

          {/* Error */}
          {error && (
            <div className="p-3 text-sm text-red-700 bg-red-50 rounded-lg border border-red-200">
              {error}
            </div>
          )}

          {/* Submit */}
          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed transition"
          >
            {loading ? "登录中..." : "登录"}
          </button>
        </form>

        {/* Divider */}
        <div className="relative my-6">
          <div className="absolute inset-0 flex items-center">
            <div className="w-full border-t border-gray-200" />
          </div>
          <div className="relative flex justify-center text-xs text-gray-400">
            <span className="bg-white px-3">或</span>
          </div>
        </div>

        {/* Production SSO */}
        <button
          onClick={handleSSOLogin}
          className="w-full py-2.5 border border-gray-300 text-gray-700 rounded-lg font-medium hover:bg-gray-50 transition text-sm"
        >
          通过 Keycloak 登录
        </button>

        <p className="text-xs text-gray-400 text-center mt-6">
          生产模式请通过企业 SSO (Keycloak) 登录
        </p>
      </div>
    </div>
  );
}
