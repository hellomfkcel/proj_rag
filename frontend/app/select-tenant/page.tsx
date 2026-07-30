"use client";
// Tenant Selection Page — shown post-login if user has multiple tenants

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/useAuthStore";
import { listTenants, type Tenant } from "@/lib/auth";

export default function SelectTenantPage() {
  const router = useRouter();
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);
  const hydrated = useAuthStore((s) => s.hydrated);
  const hydrate = useAuthStore((s) => s.hydrate);
  const logout = useAuthStore((s) => s.logout);
  const switchTenant = useAuthStore((s) => s.switchTenant);
  const setAvailableTenants = useAuthStore((s) => s.setAvailableTenants);

  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState<string | null>(null);

  useEffect(() => { hydrate(); }, []);

  useEffect(() => {
    if (!hydrated) return;
    if (!token) { router.push("/login"); return; }

    listTenants()
      .then((data) => {
        setTenants(data);
        setAvailableTenants(data);
        // Auto-redirect if only 1 tenant and it matches current
        if (data.length === 1 && user && data[0].id === user.tenant_id) {
          router.push("/kb");
        }
      })
      .catch(() => setTenants([]))
      .finally(() => setLoading(false));
  }, [hydrated, token]);

  const handleSelectTenant = async (tenantId: string) => {
    if (user?.tenant_id === tenantId) {
      // Already on this tenant, just go to KB page
      router.push("/kb");
      return;
    }
    setSwitching(tenantId);
    await switchTenant(tenantId);
    // switchTenant does a full reload, so no further navigation needed
  };

  if (!hydrated || loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-spin w-8 h-8 border-4 border-blue-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  if (!token || !user) return null;

  return (
    <div className="flex items-center justify-center min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100">
      <div className="w-full max-w-lg p-8">
        <div className="text-center mb-8">
          <h1 className="text-2xl font-bold text-gray-900">选择租户</h1>
          <p className="text-sm text-gray-500 mt-1">
            你的账户关联了多个租户，请选择要进入的租户
          </p>
        </div>

        <div className="space-y-3">
          {tenants.length === 0 && (
            <div className="text-center py-8 bg-white rounded-xl border border-gray-200">
              <p className="text-gray-400">未找到可用租户</p>
            </div>
          )}

          {tenants.map((t) => (
            <div
              key={t.id}
              className={`bg-white rounded-xl border-2 p-5 transition cursor-pointer ${
                t.id === user.tenant_id
                  ? "border-blue-300 bg-blue-50"
                  : "border-gray-200 hover:border-blue-300 hover:shadow-md"
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xl">🏢</span>
                    <span className="font-semibold text-gray-800">{t.name}</span>
                    {t.id === user.tenant_id && (
                      <span className="text-xs bg-blue-100 text-blue-600 px-2 py-0.5 rounded-full">当前</span>
                    )}
                  </div>
                  <div className="flex gap-4 text-sm text-gray-500 ml-8">
                    <span>📚 {t.kb_count} 个知识库</span>
                    <span>📄 {t.doc_count} 个文档</span>
                  </div>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); handleSelectTenant(t.id); }}
                  disabled={switching === t.id}
                  className={`px-4 py-2 rounded-lg text-sm font-medium transition ${
                    t.id === user.tenant_id
                      ? "bg-blue-600 text-white hover:bg-blue-700"
                      : "bg-gray-100 text-gray-700 hover:bg-blue-600 hover:text-white"
                  } disabled:opacity-50`}
                >
                  {switching === t.id ? "切换中..." : t.id === user.tenant_id ? "进入" : "进入"}
                </button>
              </div>
            </div>
          ))}
        </div>

        <div className="mt-6 text-center">
          <button
            onClick={logout}
            className="text-sm text-gray-400 hover:text-red-500 transition"
          >
            🚪 退出登录
          </button>
        </div>
      </div>
    </div>
  );
}
