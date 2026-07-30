"use client";
// Header — global KB multi-selector + tenant switcher + user info

import { useState, useRef, useEffect, useCallback } from "react";
import Link from "next/link";
import { useAuthStore } from "@/stores/useAuthStore";
import { useKBStore } from "@/stores/useKBStore";
import KBList from "@/app/components/KBList";
import { listKBs, renameKB, deleteKB } from "@/lib/kb";

interface KB {
  id: string;
  name: string;
  description?: string;
  status?: string;
  doc_count?: number;
}

export default function Header() {
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);
  const token = useAuthStore((s) => s.token);
  const tenants = useAuthStore((s) => s.availableTenants);
  const switchTenant = useAuthStore((s) => s.switchTenant);
  const recordActivity = useAuthStore((s) => s.recordActivity);
  const { selectedKBs, toggleKB } = useKBStore();

  const [tenantOpen, setTenantOpen] = useState(false);
  const tenantRef = useRef<HTMLDivElement>(null);
  const [kbs, setKBs] = useState<KB[]>([]);
  const [showCreateKB, setShowCreateKB] = useState(false);
  const [newKBName, setNewKBName] = useState("");
  const [newKBStrategy, setNewKBStrategy] = useState("sentence");

  // Close tenant dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (tenantRef.current && !tenantRef.current.contains(e.target as Node)) {
        setTenantOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // Record activity on any interaction
  useEffect(() => {
    const handler = () => recordActivity();
    document.addEventListener("mousedown", handler);
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("keydown", handler);
    };
  }, [recordActivity]);

  // Fetch KBs for the selector
  const fetchKBs = useCallback(async () => {
    try {
      const data = await listKBs();
      setKBs(data);
    } catch {}
  }, []);

  useEffect(() => {
    if (token) fetchKBs();
  }, [token, fetchKBs]);

  // KB CRUD handlers
  const handleRename = async (kbId: string, name: string) => {
    await renameKB(kbId, name);
    await fetchKBs();
  };

  const handleDelete = async (kbId: string) => {
    if (!confirm("确认删除此知识库？此操作不可撤销。")) return;
    try {
      await deleteKB(kbId);
      await fetchKBs();
    } catch (e: any) {
      const status = e?.response?.status;
      const detail = e?.response?.data?.detail || "";
      if (status === 409) {
        alert(detail || "该知识库下仍有文档挂载，请先移除所有文档后再删除");
      } else {
        alert("删除失败: " + (e.message || "未知错误"));
      }
    }
  };

  const handleCreateKB = async () => {
    if (!newKBName.trim()) return;
    try {
      const { createKB } = await import("@/lib/kb");
      await createKB(newKBName.trim(), "", newKBStrategy);
      setShowCreateKB(false);
      setNewKBName("");
      setNewKBStrategy("sentence");
      await fetchKBs();
    } catch (e: any) {
      alert("创建失败: " + (e.message || "未知错误"));
    }
  };

  if (!token || !user) return null;

  const hasMultipleTenants = tenants.length > 1;

  return (
    <>
      <header className="bg-white border-b border-gray-200 shadow-sm sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-5">
            <Link href="/kb" className="text-lg font-bold text-gray-900 hover:text-blue-600 transition shrink-0">
              🏠 RAG v14
            </Link>

            {/* ── Global KB Selector ── */}
            <KBList
              kbs={kbs}
              selectedKBs={selectedKBs}
              onToggle={toggleKB}
              onRename={handleRename}
              onDelete={handleDelete}
              onCreateKB={() => setShowCreateKB(true)}
            />

            <nav className="flex items-center gap-2 text-sm">
              <Link href="/kb" className="px-2.5 py-1.5 rounded-lg text-gray-600 hover:bg-gray-100 transition font-medium">📂 知识库</Link>
              <Link href="/chat" className="px-2.5 py-1.5 rounded-lg text-gray-600 hover:bg-gray-100 transition font-medium">💬 对话</Link>
              <Link href="/settings" className="px-2.5 py-1.5 rounded-lg text-gray-600 hover:bg-gray-100 transition font-medium">⚙️ 设置</Link>
              <Link href="/dashboard" className="px-2.5 py-1.5 rounded-lg text-gray-600 hover:bg-gray-100 transition font-medium">📊 Dashboard</Link>
            </nav>
          </div>

          <div className="flex items-center gap-3 text-sm text-gray-600 shrink-0">
            {/* Tenant badge / switcher */}
            <div ref={tenantRef} className="relative">
              <button
                onClick={() => hasMultipleTenants && setTenantOpen(!tenantOpen)}
                className={`flex items-center gap-1 px-2.5 py-1 rounded-full font-medium transition ${
                  hasMultipleTenants
                    ? "bg-blue-50 text-blue-700 hover:bg-blue-100 cursor-pointer"
                    : "bg-blue-50 text-blue-700 cursor-default"
                }`}
                title={hasMultipleTenants ? "切换租户" : undefined}
              >
                <span>🏢 {user.tenant_id}</span>
                {hasMultipleTenants && (
                  <svg className={`w-3 h-3 transition ${tenantOpen ? "rotate-180" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                )}
              </button>

              {tenantOpen && hasMultipleTenants && (
                <div className="absolute top-full mt-1 right-0 w-64 bg-white border border-gray-200 rounded-xl shadow-lg z-50 overflow-hidden">
                  <div className="px-3 py-2 border-b border-gray-100 text-xs text-gray-400 font-medium">切换租户</div>
                  <div className="max-h-48 overflow-y-auto py-1">
                    {tenants.map((t) => (
                      <button
                        key={t.id}
                        onClick={() => {
                          if (t.id !== user.tenant_id) switchTenant(t.id);
                          setTenantOpen(false);
                        }}
                        className={`w-full text-left px-4 py-2.5 hover:bg-blue-50 transition flex items-center justify-between ${
                          t.id === user.tenant_id ? "bg-blue-50 border-l-2 border-blue-500" : ""
                        }`}
                      >
                        <div>
                          <div className="text-sm font-medium text-gray-700">{t.name}</div>
                          <div className="text-xs text-gray-400">{t.kb_count} 个知识库 · {t.doc_count} 个文档</div>
                        </div>
                        {t.id === user.tenant_id && <span className="text-xs text-blue-600">✓ 当前</span>}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <span className="text-gray-400">|</span>
            <span>👤 <span className="font-medium">{user.name}</span></span>
            <button onClick={logout} className="ml-2 px-3 py-1 text-red-600 hover:bg-red-50 rounded-lg transition text-xs font-medium">🚪 退出</button>
          </div>
        </div>
      </header>

      {/* Create KB Dialog — global, accessible from Header on any page */}
      {showCreateKB && (
        <div className="fixed inset-0 bg-black/30 z-[60] flex items-center justify-center">
          <div className="bg-white rounded-xl shadow-xl p-6 w-full max-w-md">
            <h3 className="text-lg font-semibold mb-4">新建知识库</h3>
            <input
              type="text"
              value={newKBName}
              onChange={(e) => setNewKBName(e.target.value)}
              placeholder="知识库名称"
              className="w-full px-4 py-2.5 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-blue-500 mb-3"
              autoFocus
              onKeyDown={(e) => e.key === "Enter" && handleCreateKB()}
            />
            {/* Chunking strategy */}
            <div className="mb-4">
              <label className="block text-sm font-medium text-gray-600 mb-1">切分策略</label>
              <select
                value={newKBStrategy}
                onChange={(e) => setNewKBStrategy(e.target.value)}
                className="w-full px-4 py-2.5 border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              >
                <option value="sentence">按句子 (sentence)</option>
                <option value="word">按词 (word)</option>
                <option value="passage">按段落 (passage)</option>
              </select>
              <p className="text-xs text-gray-400 mt-1">创建后可在设置页更新切分配置</p>
            </div>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => { setShowCreateKB(false); setNewKBName(""); setNewKBStrategy("sentence"); }}
                className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg"
              >
                取消
              </button>
              <button
                onClick={handleCreateKB}
                className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
                disabled={!newKBName.trim()}
              >
                创建
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
