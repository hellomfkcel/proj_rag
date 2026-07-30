"use client";
// KB Page — doc list + upload + directory tree + preview Sheet
// KB selector is in global Header (multi-select)
// Search/filter/sort state lives here — passed down to DocTable

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/useAuthStore";
import { useKBStore } from "@/stores/useKBStore";
import { useKBSync } from "@/lib/useKBSync";
import DocTable from "@/app/components/DocTable";
import Header from "@/app/components/Header";
import UploadZone from "@/app/components/UploadZone";
import DirTree from "@/app/components/DirTree";
import DocPreviewSheet from "@/app/components/DocPreviewSheet";
import { listDocuments } from "@/lib/kb";
import { listDirs, createDir, renameDir, deleteDir } from "@/lib/dirs";
import { getAppConfig } from "@/lib/settings";
import type { DirItem } from "@/lib/dirs";

interface Doc {
  document_id: string; mount_id: string; filename: string;
  file_size: number; mime_type?: string; parse_status: string; is_enabled: boolean;
}

export default function KBPage() {
  const token = useAuthStore((s) => s.token);
  const router = useRouter();
  const { selectedKBs } = useKBStore();
  useKBSync();

  const activeKB = selectedKBs.length > 0 ? selectedKBs[0] : null;

  const [docs, setDocs] = useState<Doc[]>([]);
  const [dirs, setDirs] = useState<DirItem[]>([]);
  const [selectedDirId, setSelectedDirId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Search / filter / sort state
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [sortBy, setSortBy] = useState("created_at");
  const [sortOrder, setSortOrder] = useState("desc");

  // Preview Sheet
  const [previewDocId, setPreviewDocId] = useState<string | null>(null);

  // Admin console URL for 👥 jump
  const [adminConsoleUrl, setAdminConsoleUrl] = useState("");

  const fetchDocs = useCallback(async (s?: string, st?: string, sb?: string, od?: string) => {
    if (!activeKB) { setDocs([]); return; }
    const sv = s ?? search;
    const stv = st ?? statusFilter;
    const sbv = sb ?? sortBy;
    const odv = od ?? sortOrder;

    setLoading(true); setError(null);
    try {
      const data = await listDocuments(activeKB, sv || undefined, stv !== "all" ? stv : undefined, sbv, odv);
      setDocs(data);
    } catch (e: any) {
      setError(e?.message || "加载文档列表失败");
      setDocs([]);
    } finally { setLoading(false); }
  }, [activeKB, search, statusFilter, sortBy, sortOrder]);

  const fetchDirs = useCallback(async () => {
    if (!activeKB) { setDirs([]); return; }
    try { setDirs(await listDirs(activeKB)); } catch {}
  }, [activeKB]);

  useEffect(() => {
    if (!token) { router.push("/login"); return; }
    getAppConfig().then(cfg => { if (cfg.admin_console_url) setAdminConsoleUrl(cfg.admin_console_url); }).catch(() => {});
  }, [token, router]);

  useEffect(() => {
    fetchDocs(); fetchDirs();
    const interval = setInterval(() => { fetchDocs(); fetchDirs(); }, 5000);
    return () => clearInterval(interval);
  }, [fetchDocs, fetchDirs]);

  // Listen for doc-moved events (from drag-drop)
  useEffect(() => {
    const handler = () => { fetchDocs(); fetchDirs(); };
    window.addEventListener("doc-moved", handler);
    return () => window.removeEventListener("doc-moved", handler);
  }, [fetchDocs, fetchDirs]);

  // Called by DocTable when search/filter/sort changes
  const handleSearchChange = (v: string) => {
    setSearch(v); setLoading(true); setError(null);
    const stv = statusFilter;
    const sbv = sortBy; const odv = sortOrder;
    listDocuments(activeKB!, v || undefined, stv !== "all" ? stv : undefined, sbv, odv)
      .then(setDocs).catch((e) => { setError(e?.message || "搜索失败"); setDocs([]); })
      .finally(() => setLoading(false));
  };

  const handleFilterChange = (v: string) => {
    setStatusFilter(v); setLoading(true); setError(null);
    const sv = search; const sbv = sortBy; const odv = sortOrder;
    listDocuments(activeKB!, sv || undefined, v !== "all" ? v : undefined, sbv, odv)
      .then(setDocs).catch((e) => { setError(e?.message || "筛选失败"); setDocs([]); })
      .finally(() => setLoading(false));
  };

  const handleSortToggle = (key: string) => {
    let newOrder: "asc" | "desc";
    let newKey: string;
    if (sortBy === key) {
      newOrder = sortOrder === "asc" ? "desc" : "asc";
      newKey = key;
    } else {
      newKey = key; newOrder = "asc";
    }
    setSortBy(newKey); setSortOrder(newOrder);
    setLoading(true); setError(null);
    const sv = search; const stv = statusFilter;
    listDocuments(activeKB!, sv || undefined, stv !== "all" ? stv : undefined, newKey, newOrder === "asc" ? "asc" : "desc")
      .then(setDocs).catch((e) => { setError(e?.message || "排序失败"); setDocs([]); })
      .finally(() => setLoading(false));
  };

  if (!token) return null;

  return (
    <div className="min-h-screen bg-gray-50">
      <Header />
      <main className="max-w-7xl mx-auto px-6 py-8">
        {activeKB && (
          <div className="flex items-center justify-between mb-6">
            <span className="text-sm text-gray-500">
              {docs.length} 个文档
              {selectedKBs.length > 1 && (
                <span className="ml-2 text-blue-500">（多选了 {selectedKBs.length} 个知识库，对话页可交叉检索）</span>
              )}
            </span>
            {adminConsoleUrl ? (
              <a
                href={adminConsoleUrl}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1 text-xs text-gray-400 hover:text-purple-600 transition px-2 py-1 rounded hover:bg-purple-50"
                title="前往管理台管理此知识库的授权策略"
              >
                👥 管理授权
              </a>
            ) : (
              <span
                className="flex items-center gap-1 text-xs text-gray-300 px-2 py-1 cursor-not-allowed"
                title="未配置 ADMIN_CONSOLE_URL 环境变量，授权管理请前往外部管理台"
              >
                👥 管理授权
              </span>
            )}
          </div>
        )}
        {!activeKB ? (
          <div className="text-center py-20">
            <p className="text-gray-400 text-lg">在顶部选择或创建一个知识库开始</p>
          </div>
        ) : (
          <div className="flex gap-6">
            <div className="w-56 shrink-0 bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden h-fit">
              <DirTree
                dirs={dirs} selectedDirId={selectedDirId} onSelectDir={setSelectedDirId}
                onCreateDir={async (name) => { await createDir(name, activeKB); fetchDirs(); }}
                onRenameDir={async (id, name) => { await renameDir(id, name); fetchDirs(); }}
                onDeleteDir={async (id) => { if (confirm("删除空文件夹？")) { await deleteDir(id); fetchDirs(); }}}
              />
            </div>
            <div className="flex-1 min-w-0 space-y-4">
              <UploadZone kbId={activeKB} onUploaded={() => { fetchDocs(); fetchDirs(); }} />
              <DocTable
                docs={docs} loading={loading} error={error} kbId={activeKB} dirs={dirs}
                search={search} statusFilter={statusFilter} sortBy={sortBy} sortOrder={sortOrder}
                onSearchChange={handleSearchChange} onFilterChange={handleFilterChange} onSortToggle={handleSortToggle}
                onRefresh={() => { fetchDocs(); fetchDirs(); }}
                onPreviewDoc={setPreviewDocId}
              />
            </div>
          </div>
        )}
      </main>
      {previewDocId && activeKB && (
        <DocPreviewSheet docId={previewDocId} kbId={activeKB} onClose={() => setPreviewDocId(null)} />
      )}
    </div>
  );
}
