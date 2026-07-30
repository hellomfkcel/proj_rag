"use client";
// Document Table — controlled search/filter/sort, download, batch ops, skeleton, error, preview

import { useState, useRef, useEffect } from "react";
import { deleteDocument, toggleDocument, triggerParse, getDownloadUrl } from "@/lib/kb";
import { renameDocument, batchDelete, batchParse, moveDocToDir } from "@/lib/dirs";
import type { DirItem } from "@/lib/dirs";

interface Doc {
  document_id: string; mount_id: string; filename: string;
  file_size: number; mime_type?: string; parse_status: string; is_enabled: boolean;
}

interface Props {
  docs: Doc[];
  loading: boolean;
  error: string | null;
  kbId: string;
  dirs: DirItem[];
  // Controlled filter state from parent
  search: string;
  statusFilter: string;
  sortBy: string;
  sortOrder: string;
  // Callbacks
  onSearchChange: (v: string) => void;
  onFilterChange: (v: string) => void;
  onSortToggle: (key: string) => void;
  onRefresh: () => void;
  onPreviewDoc: (docId: string) => void;
}

const STATUS_OPTIONS = [
  { value: "all", label: "全部状态" },
  { value: "completed", label: "✅ 已完成" },
  { value: "processing", label: "🔄 处理中" },
  { value: "queued", label: "⏳ 队列中" },
  { value: "failed", label: "❌ 失败" },
  { value: "not_parsed", label: "📄 未解析" },
];

function bdg(s: string) {
  const m: Record<string, { l: string; c: string }> = {
    completed: { l: "✅", c: "bg-green-100 text-green-700" },
    processing: { l: "🔄", c: "bg-yellow-100 text-yellow-700 animate-pulse" },
    queued: { l: "⏳", c: "bg-blue-100 text-blue-700" },
    failed: { l: "❌", c: "bg-red-100 text-red-700" },
    not_parsed: { l: "📄", c: "bg-gray-100 text-gray-600" },
  };
  const x = m[s] || m.not_parsed;
  return <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${x.c}`}>{x.l} {s.replace("_", " ")}</span>;
}

function fm(n: number) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}

export default function DocTable({
  docs, loading, error, kbId, dirs,
  search, statusFilter, sortBy, sortOrder,
  onSearchChange, onFilterChange, onSortToggle, onRefresh, onPreviewDoc,
}: Props) {
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [renId, setRenId] = useState<string | null>(null);
  const [renVal, setRenVal] = useState("");
  const [busy, setBusy] = useState(false);

  // Debounced search
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [localSearch, setLocalSearch] = useState(search);

  useEffect(() => { setLocalSearch(search); }, [search]);

  const handleSearchInput = (v: string) => {
    setLocalSearch(v);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => onSearchChange(v), 300);
  };

  // Batch move
  const [moveMenuOpen, setMoveMenuOpen] = useState(false);
  const moveRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const h = (e: MouseEvent) => { if (moveRef.current && !moveRef.current.contains(e.target as Node)) setMoveMenuOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, []);

  const sortIcon = (key: string) => {
    if (sortBy !== key) return <span className="text-gray-300 ml-0.5">↕</span>;
    return <span className="text-blue-500 ml-0.5">{sortOrder === "asc" ? "↑" : "↓"}</span>;
  };

  const tg = (id: string) => { setSel(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; }); };
  const tgAll = () => { if (sel.size === docs.length && docs.length > 0) setSel(new Set()); else setSel(new Set(docs.map(d => d.document_id))); };

  const doBatchDelete = async () => {
    if (!confirm(`确认删除 ${sel.size} 个文档？此操作不可撤销。`)) return;
    setBusy(true);
    const items = docs.filter(d => sel.has(d.document_id)).map(d => ({ document_id: d.document_id, kb_id: kbId }));
    await batchDelete(items); setSel(new Set()); setBusy(false); onRefresh();
  };

  const doBatchParse = async () => {
    setBusy(true);
    const mids = docs.filter(d => sel.has(d.document_id) && (d.parse_status === "not_parsed" || d.parse_status === "failed")).map(d => d.mount_id);
    if (mids.length === 0) { alert("选中的文档都已解析完成或不需要解析"); setBusy(false); return; }
    try { await batchParse(mids); setSel(new Set()); onRefresh(); } catch (e: any) { alert("批量解析失败: " + (e.message || e)); }
    finally { setBusy(false); }
  };

  const doBatchMove = async (dirId: string) => {
    setBusy(true);
    const docIds = docs.filter(d => sel.has(d.document_id)).map(d => d.document_id);
    try { for (const docId of docIds) await moveDocToDir(dirId, docId); setSel(new Set()); setMoveMenuOpen(false); onRefresh(); }
    catch (e: any) { alert("移动失败: " + (e.message || e)); }
    finally { setBusy(false); }
  };

  const manualDirs = dirs.filter(d => d.directory_type !== "kb_bound");

  // ── Loading skeleton ──
  if (loading && docs.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-4 py-3 border-b border-gray-100 flex items-center gap-3">
          <div className="h-8 w-48 bg-gray-100 rounded-lg animate-pulse" />
          <div className="h-8 w-32 bg-gray-100 rounded-lg animate-pulse" />
        </div>
        <div className="divide-y divide-gray-100">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="flex items-center px-4 py-3 gap-4">
              <div className="w-4 h-4 bg-gray-100 rounded animate-pulse" />
              <div className="h-4 flex-1 bg-gray-100 rounded animate-pulse" />
              <div className="h-4 w-16 bg-gray-100 rounded animate-pulse" />
              <div className="h-4 w-20 bg-gray-100 rounded animate-pulse" />
              <div className="h-4 w-24 bg-gray-100 rounded animate-pulse" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  // ── Error state ──
  if (error && docs.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-red-200 shadow-sm p-8 text-center">
        <div className="text-3xl mb-3">⚠️</div>
        <p className="text-red-600 font-medium mb-1">加载失败</p>
        <p className="text-sm text-gray-500 mb-4">{error}</p>
        <button onClick={onRefresh} className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">重试</button>
      </div>
    );
  }

  // ── Main table ──
  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      {/* Toolbar: search + filter */}
      <div className="px-4 py-3 border-b border-gray-100 flex items-center gap-3 flex-wrap">
        {/* Search */}
        <div className="relative">
          <input type="text" value={localSearch}
            onChange={e => handleSearchInput(e.target.value)}
            placeholder="🔍 搜索文件名..." autoComplete="off"
            className="pl-3 pr-8 py-1.5 text-sm border border-gray-300 rounded-lg outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 w-56" />
          {localSearch && (
            <button onClick={() => { setLocalSearch(""); onSearchChange(""); }} className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-xs">✕</button>
          )}
        </div>

        {/* Status filter */}
        <select value={statusFilter} onChange={e => onFilterChange(e.target.value)}
          className="px-3 py-1.5 text-sm border border-gray-300 rounded-lg bg-white outline-none focus:ring-2 focus:ring-blue-500">
          {STATUS_OPTIONS.map(o => (<option key={o.value} value={o.value}>{o.label}</option>))}
        </select>

        {/* Batch ops when selected */}
        {sel.size > 0 && (
          <div className="flex items-center gap-2 ml-auto">
            <span className="text-sm font-medium text-blue-700">已选 {sel.size} 个</span>
            <button onClick={doBatchParse} disabled={busy}
              className="px-3 py-1 bg-blue-600 text-white rounded text-xs hover:bg-blue-700 disabled:opacity-50">🔄 批量解析</button>
            {manualDirs.length > 0 && (
              <div ref={moveRef} className="relative">
                <button onClick={() => setMoveMenuOpen(!moveMenuOpen)} disabled={busy}
                  className="px-3 py-1 bg-purple-600 text-white rounded text-xs hover:bg-purple-700 disabled:opacity-50">📁 移动到</button>
                {moveMenuOpen && (
                  <div className="absolute top-full right-0 mt-1 w-48 bg-white border border-gray-200 rounded-lg shadow-lg z-50 overflow-hidden">
                    <div className="px-3 py-1.5 text-xs text-gray-400 border-b border-gray-100">选择目标文件夹</div>
                    <div className="max-h-40 overflow-y-auto">
                      {manualDirs.map(d => (
                        <button key={d.id} onClick={() => doBatchMove(d.id)}
                          className="w-full text-left px-3 py-2 text-sm hover:bg-blue-50 transition flex items-center gap-2">
                          <span>📁</span><span className="truncate">{d.name}</span>
                          <span className="ml-auto text-xs text-gray-400">{d.doc_count}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
            <button onClick={doBatchDelete} disabled={busy}
              className="px-3 py-1 bg-red-600 text-white rounded text-xs hover:bg-red-700 disabled:opacity-50">🗑 批量删除</button>
            <button onClick={() => setSel(new Set())} className="px-3 py-1 text-gray-500 hover:bg-gray-100 rounded text-xs">取消</button>
          </div>
        )}
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b border-gray-200">
            <tr>
              <th className="w-10 px-3 py-3"><input type="checkbox" checked={sel.size === docs.length && docs.length > 0} onChange={tgAll} /></th>
              <th className="text-left px-4 py-3 font-medium text-gray-600 w-[35%] cursor-pointer select-none hover:bg-gray-100 transition" onClick={() => onSortToggle("filename")}>文件名 {sortIcon("filename")}</th>
              <th className="text-left px-4 py-3 font-medium text-gray-600 cursor-pointer select-none hover:bg-gray-100 transition" onClick={() => onSortToggle("file_size")}>大小 {sortIcon("file_size")}</th>
              <th className="text-left px-4 py-3 font-medium text-gray-600">状态</th>
              <th className="text-left px-4 py-3 font-medium text-gray-600 w-[190px]">操作</th>
            </tr>
          </thead>
          <tbody>
            {!loading && docs.length === 0 && (
              <tr><td colSpan={5} className="px-4 py-12 text-center text-gray-400">
                <div className="text-3xl mb-2">📭</div>
                <p>{search || statusFilter !== "all" ? "没有匹配的文档" : "暂无文档，上传第一个文件开始吧"}</p>
              </td></tr>
            )}
            {docs.map(d => (
              <tr key={d.document_id} className={`border-b border-gray-100 hover:bg-gray-50 transition ${!d.is_enabled ? "opacity-50" : ""}`}>
                <td className="px-3 py-3"><input type="checkbox" checked={sel.has(d.document_id)} onChange={() => tg(d.document_id)} /></td>
                <td className="px-4 py-3">
                  {renId === d.document_id ? (
                    <input autoFocus className="w-full px-2 py-0.5 text-sm border border-blue-300 rounded outline-none" value={renVal} onChange={e => setRenVal(e.target.value)}
                      onBlur={async () => { if (renVal.trim()) { await renameDocument(d.document_id, renVal.trim()); onRefresh(); } setRenId(null); }}
                      onKeyDown={e => { if (e.key === "Enter") { if (renVal.trim()) { renameDocument(d.document_id, renVal.trim()); onRefresh(); } setRenId(null); } if (e.key === "Escape") setRenId(null); }} />
                  ) : (
                    <span className="font-medium text-gray-800 truncate max-w-[220px] inline-block cursor-pointer hover:text-blue-600" title={d.filename} onClick={() => onPreviewDoc(d.document_id)}>📄 {d.filename}</span>
                  )}
                  {!d.is_enabled && <span className="ml-1 text-xs bg-gray-200 text-gray-500 px-1 py-0.5 rounded">停用</span>}
                </td>
                <td className="px-4 py-3 text-gray-500">{fm(d.file_size)}</td>
                <td className="px-4 py-3">{bdg(d.parse_status)}</td>
                <td className="px-4 py-3">
                  <div className="flex items-center gap-1">
                    <button onClick={() => onPreviewDoc(d.document_id)} className="px-2 py-0.5 text-xs text-gray-500 hover:bg-gray-100 rounded" title="预览">👁</button>
                    <a href={getDownloadUrl(d.document_id)} className="px-2 py-0.5 text-xs text-gray-500 hover:bg-gray-100 rounded" title="下载" target="_blank" rel="noreferrer">📥</a>
                    <button onClick={() => { setRenId(d.document_id); setRenVal(d.filename); }} className="px-2 py-0.5 text-xs text-gray-500 hover:bg-gray-100 rounded" title="重命名">✏️</button>
                    <button onClick={async () => { await toggleDocument(d.document_id, kbId, !d.is_enabled); onRefresh(); }} className="px-2 py-0.5 text-xs text-gray-500 hover:bg-gray-100 rounded" title={d.is_enabled ? "停用" : "启用"}>{d.is_enabled ? "⏸" : "▶️"}</button>
                    {(d.parse_status === "not_parsed" || d.parse_status === "failed") && (
                      <button onClick={async () => { await triggerParse(d.document_id); onRefresh(); }} className="px-2 py-0.5 text-xs text-blue-600 hover:bg-blue-50 rounded font-medium">🔄 解析</button>
                    )}
                    <button onClick={async () => { if (confirm("确认移除？")) { await deleteDocument(d.document_id, kbId); onRefresh(); } }} className="px-2 py-0.5 text-xs text-red-500 hover:bg-red-50 rounded">🗑</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
