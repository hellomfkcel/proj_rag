"use client";
// Document Preview Sheet — right-side slideout showing chunks + metadata

import { useEffect, useState } from "react";
import { getDocumentDetail, getDocumentChunks, getDocumentContent } from "@/lib/kb";

interface Props {
  docId: string | null;
  kbId: string;
  onClose: () => void;
}

export default function DocPreviewSheet({ docId, kbId, onClose }: Props) {
  const [tab, setTab] = useState<"chunks" | "content">("chunks");
  const [detail, setDetail] = useState<any>(null);
  const [chunks, setChunks] = useState<{ chunk_id: string; content: string }[]>([]);
  const [content, setContent] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!docId) return;
    setLoading(true);
    setError("");
    setChunks([]);
    setContent("");

    Promise.all([
      getDocumentDetail(docId).catch(() => null),
      tab === "chunks"
        ? getDocumentChunks(docId).catch(() => [] as { chunk_id: string; content: string }[])
        : getDocumentContent(docId).then(r => r.content).catch(() => ""),
    ]).then(([det, chunkOrContent]) => {
      setDetail(det);
      if (tab === "chunks") setChunks(chunkOrContent as any[]);
      else setContent(chunkOrContent as string);
      setLoading(false);
    }).catch(() => {
      setError("加载失败");
      setLoading(false);
    });
  }, [docId, tab]);

  if (!docId) return null;

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black/20 z-40" onClick={onClose} />

      {/* Sheet */}
      <div className="fixed top-0 right-0 h-full w-[420px] bg-white shadow-2xl z-50 flex flex-col animate-slide-in-right">
        {/* Header */}
        <div className="px-5 py-4 border-b border-gray-200 flex items-center justify-between shrink-0">
          <div className="min-w-0">
            <h3 className="font-semibold text-gray-900 truncate">
              {detail?.filename || "文档预览"}
            </h3>
            <p className="text-xs text-gray-400 mt-0.5">
              {detail ? `${detail.mime_type || "未知类型"} · ${detail.file_size ? (detail.file_size / 1024).toFixed(1) + " KB" : ""}` : ""}
            </p>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-gray-100 rounded-lg text-gray-400 hover:text-gray-600 transition shrink-0">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-gray-200 shrink-0">
          <button
            onClick={() => setTab("chunks")}
            className={`flex-1 py-2.5 text-sm font-medium transition ${tab === "chunks" ? "text-blue-600 border-b-2 border-blue-600" : "text-gray-500 hover:text-gray-700"}`}
          >
            🧩 Chunks
          </button>
          <button
            onClick={() => setTab("content")}
            className={`flex-1 py-2.5 text-sm font-medium transition ${tab === "content" ? "text-blue-600 border-b-2 border-blue-600" : "text-gray-500 hover:text-gray-700"}`}
          >
            📄 原文
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="flex items-center justify-center py-12">
              <div className="animate-spin w-6 h-6 border-2 border-blue-500 border-t-transparent rounded-full" />
            </div>
          )}

          {error && (
            <div className="p-6 text-center">
              <p className="text-red-500 text-sm">{error}</p>
              <button onClick={() => { setTab(tab); }} className="mt-2 text-sm text-blue-600 hover:underline">重试</button>
            </div>
          )}

          {!loading && !error && tab === "chunks" && (
            <div className="divide-y divide-gray-100">
              {chunks.length === 0 && (
                <div className="p-8 text-center text-gray-400 text-sm">
                  <p>📭 暂无 Chunk 数据</p>
                  <p className="text-xs mt-1">文档可能尚未解析，或向量库中无匹配记录</p>
                </div>
              )}
              {chunks.map((c, i) => (
                <div key={c.chunk_id || i} className="px-5 py-3">
                  <div className="flex items-center gap-2 mb-1.5">
                    <span className="text-xs font-mono text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">#{i + 1}</span>
                    <span className="text-[10px] text-gray-400 font-mono truncate">{c.chunk_id?.slice(0, 12)}</span>
                  </div>
                  <p className="text-sm text-gray-700 leading-relaxed line-clamp-6">{c.content}</p>
                </div>
              ))}
            </div>
          )}

          {!loading && !error && tab === "content" && (
            <div className="px-5 py-4">
              {!content ? (
                <p className="text-gray-400 text-sm text-center py-8">暂无原文内容</p>
              ) : (
                <pre className="text-sm text-gray-700 leading-relaxed whitespace-pre-wrap font-sans break-words">{content}</pre>
              )}
            </div>
          )}
        </div>
      </div>

      <style jsx>{`
        @keyframes slide-in-right {
          from { transform: translateX(100%); }
          to { transform: translateX(0); }
        }
        .animate-slide-in-right {
          animation: slide-in-right 0.2s ease-out;
        }
      `}</style>
    </>
  );
}
