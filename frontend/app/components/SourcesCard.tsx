"use client";
// SourceCard — 来源标注 chip：显示文档名/缩写，点击弹出 modal 展示详细内容

import { useState } from "react";

interface Source {
  chunk_id: string;
  doc_name?: string;
  content?: string;
}

export default function SourcesCard({ index, source }: { index: number; source: Source }) {
  const [open, setOpen] = useState(false);
  const fallback = source.content ? source.content.slice(0, 12) + "…" : `来源 ${index + 1}`;
  const label = source.doc_name || fallback;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title={label}
        className="max-w-[220px] truncate px-2.5 py-1 text-xs border rounded-full transition font-medium border-gray-300 bg-white text-gray-600 hover:border-blue-400 hover:bg-blue-50"
      >
        📄 {label}
      </button>

      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setOpen(false)}>
          <div
            className="bg-white rounded-xl shadow-2xl w-full max-w-lg max-h-[80vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-center justify-between gap-2 px-4 py-3 border-b border-gray-100">
              <div className="flex items-center gap-2 min-w-0">
                <span className="text-sm font-semibold text-gray-800 truncate">📄 {label}</span>
                <span className="text-xs font-mono text-gray-400 truncate">{source.chunk_id?.slice(0, 16)}...</span>
              </div>
              <button onClick={() => setOpen(false)} className="text-gray-400 hover:text-gray-600 text-lg leading-none shrink-0">
                ✕
              </button>
            </div>
            {/* Body */}
            <div className="px-4 py-3 overflow-y-auto">
              {source.content ? (
                <p className="text-sm text-gray-700 leading-relaxed whitespace-pre-wrap">{source.content}</p>
              ) : (
                <p className="text-sm text-gray-400 italic">原文未加载</p>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
