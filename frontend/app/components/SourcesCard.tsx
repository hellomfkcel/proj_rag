"use client";
// SourceCard — 来源卡片：序号徽标 + 文档名 + 内容摘要（2 行），点击弹出原文详情。

import { useState } from "react";

interface Source {
  chunk_id: string;
  doc_name?: string;
  content?: string;
}

// 文档名美化：去掉常见扩展名，便于展示为标题
function friendlyDocName(name: string): string {
  return name.replace(/\.(txt|md|markdown|docx?|pdf)$/i, "");
}

export default function SourcesCard({ index, source }: { index: number; source: Source }) {
  const [open, setOpen] = useState(false);
  const docName = source.doc_name ? friendlyDocName(source.doc_name) : `来源 ${index + 1}`;

  return (
    <>
      {/* 卡片：序号 + 文档名 + 摘要，点击查看原文 */}
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen(true)}
        onKeyDown={(e) => { if (e.key === "Enter") setOpen(true); }}
        title="点击查看原文"
        className="rounded-lg border border-gray-200 bg-white p-2.5 transition cursor-pointer hover:border-blue-300 hover:bg-blue-50/60"
      >
        <div className="flex items-center gap-2 mb-1.5">
          <span className="inline-flex items-center justify-center w-5 h-5 rounded-md bg-blue-100 text-blue-700 text-xs font-semibold shrink-0">
            {index + 1}
          </span>
          <span className="text-xs font-medium text-gray-700 truncate">{docName}</span>
        </div>
        {source.content ? (
          <p className="text-xs text-gray-500 leading-relaxed line-clamp-2">{source.content}</p>
        ) : (
          <p className="text-xs text-gray-400 italic">原文未加载</p>
        )}
      </div>

      {/* 详情弹窗：原文 + 来源序号 + chunk_id */}
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={() => setOpen(false)}
        >
          <div
            className="bg-white rounded-xl shadow-2xl w-full max-w-lg max-h-[80vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-center justify-between gap-2 px-4 py-3 border-b border-gray-100">
              <div className="flex items-center gap-2 min-w-0">
                <span className="inline-flex items-center justify-center w-5 h-5 rounded-md bg-blue-100 text-blue-700 text-xs font-semibold shrink-0">
                  {index + 1}
                </span>
                <span className="text-sm font-semibold text-gray-800 truncate">{docName}</span>
              </div>
              <button
                onClick={() => setOpen(false)}
                className="text-gray-400 hover:text-gray-600 text-lg leading-none shrink-0"
              >
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
            {/* Footer：来源序号 + chunk_id */}
            <div className="px-4 py-2 border-t border-gray-100 flex items-center justify-between gap-2">
              <span className="text-xs text-gray-400">来源 {index + 1}</span>
              {source.chunk_id && (
                <span className="text-xs font-mono text-gray-400 truncate">{source.chunk_id.slice(0, 16)}…</span>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
