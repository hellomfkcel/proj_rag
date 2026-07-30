"use client";
// KB Multi-Select Dropdown — used in Header, supports multi-select with checkboxes

import { useState, useRef, useEffect } from "react";

interface KB {
  id: string;
  name: string;
  description?: string;
  status?: string;
  doc_count?: number;
}

interface Props {
  kbs: KB[];
  selectedKBs: string[];
  onToggle: (id: string) => void;
  onRename: (id: string, name: string) => void;
  onDelete: (id: string) => void;
  onCreateKB?: () => void;
}

export default function KBList({ kbs, selectedKBs, onToggle, onRename, onDelete, onCreateKB }: Props) {
  const [open, setOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const selectedCount = selectedKBs.length;
  const label =
    selectedCount === 0
      ? "选择知识库"
      : selectedCount === 1
        ? (kbs.find((k) => k.id === selectedKBs[0])?.name || "已选 1 个")
        : `已选 ${selectedCount} 个`;

  // Clear selection when clicking the trigger (convenience)
  const handleTriggerClick = () => setOpen(!open);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={handleTriggerClick}
        className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border transition shadow-sm text-sm ${
          selectedCount > 0
            ? "border-blue-300 bg-blue-50 text-blue-700"
            : "border-gray-300 bg-white text-gray-500 hover:border-gray-400"
        }`}
      >
        <span>{selectedCount > 0 ? "📚" : "📚"}</span>
        <span className="font-medium truncate max-w-[160px]">{label}</span>
        {selectedCount > 0 && (
          <span
            className="text-xs text-blue-400 hover:text-red-400 cursor-pointer"
            onClick={(e) => {
              e.stopPropagation();
              if (selectedKBs.length > 0) {
                // We can't clear directly here since this is inside the dropdown trigger.
                // Instead, use a dedicated clear button visible when open.
              }
            }}
            title="清除选择"
          >
            {/* Clear button shown in dropdown instead */}
          </span>
        )}
        <svg
          className={`w-3.5 h-3.5 text-gray-400 transition ${open ? "rotate-180" : ""}`}
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="absolute top-full mt-1 left-0 w-80 bg-white border border-gray-200 rounded-xl shadow-lg z-50 overflow-hidden">
          {/* Header bar */}
          <div className="px-3 py-2 border-b border-gray-100 flex items-center justify-between text-xs">
            <span className="text-gray-400 font-medium">
              {selectedCount > 0 ? `已选 ${selectedCount} 个` : "选择知识库"}
            </span>
            <div className="flex items-center gap-2">
              {selectedCount > 0 && (
                <button
                  onClick={() => {
                    // Clear by toggling each selected one off
                    selectedKBs.forEach((id) => onToggle(id));
                  }}
                  className="text-gray-400 hover:text-red-500"
                >
                  清除
                </button>
              )}
              {onCreateKB && (
                <button
                  onClick={() => { setOpen(false); onCreateKB(); }}
                  className="text-blue-600 hover:text-blue-800 font-medium"
                >
                  + 新建
                </button>
              )}
            </div>
          </div>

          <div className="max-h-64 overflow-y-auto py-1">
            {kbs.length === 0 && (
              <div className="px-4 py-6 text-center text-gray-400 text-sm">
                暂无知识库
                {onCreateKB && (
                  <button onClick={() => { setOpen(false); onCreateKB(); }} className="block mx-auto mt-2 text-blue-600 hover:underline">
                    + 创建第一个知识库
                  </button>
                )}
              </div>
            )}
            {kbs.map((kb) => {
              const isSelected = selectedKBs.includes(kb.id);
              return (
                <div
                  key={kb.id}
                  className={`flex items-center gap-2 px-3 py-2 cursor-pointer hover:bg-blue-50 transition ${
                    isSelected ? "bg-blue-50" : ""
                  }`}
                  onClick={() => onToggle(kb.id)}
                >
                  {/* Checkbox */}
                  <span
                    className={`w-4 h-4 rounded border-2 flex items-center justify-center text-[10px] shrink-0 transition ${
                      isSelected
                        ? "bg-blue-600 border-blue-600 text-white"
                        : "border-gray-300"
                    }`}
                  >
                    {isSelected && "✓"}
                  </span>

                  {/* Name (inline editable) */}
                  {editingId === kb.id ? (
                    <input
                      className="flex-1 text-sm px-1.5 py-0 border border-blue-300 rounded outline-none"
                      value={editName}
                      autoFocus
                      onChange={(e) => setEditName(e.target.value)}
                      onBlur={() => {
                        if (editName.trim() && editName !== kb.name) onRename(kb.id, editName.trim());
                        setEditingId(null);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          if (editName.trim() && editName !== kb.name) onRename(kb.id, editName.trim());
                          setEditingId(null);
                        }
                        if (e.key === "Escape") setEditingId(null);
                      }}
                      onClick={(e) => e.stopPropagation()}
                    />
                  ) : (
                    <div className="flex items-center gap-1.5 flex-1 min-w-0">
                      <span className="text-sm text-gray-700 truncate">{kb.name}</span>
                      {kb.status && (
                        <span className="text-[10px] text-gray-400 bg-gray-100 px-1 py-0.5 rounded">{kb.status}</span>
                      )}
                    </div>
                  )}

                  {/* Actions */}
                  <div className="flex items-center gap-0.5 ml-auto" onClick={(e) => e.stopPropagation()}>
                    <button
                      className="p-0.5 text-gray-400 hover:text-blue-600 rounded transition"
                      title="重命名"
                      onClick={() => { setEditingId(kb.id); setEditName(kb.name); }}
                    >
                      ✏️
                    </button>
                    <button
                      className="p-0.5 text-gray-400 hover:text-red-600 rounded transition"
                      title="删除"
                      onClick={() => onDelete(kb.id)}
                    >
                      🗑
                    </button>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Footer hint */}
          {selectedCount > 0 && (
            <div className="px-3 py-1.5 border-t border-gray-100 text-[10px] text-gray-400 bg-gray-50">
              对话页将使用选中的知识库进行交叉检索
            </div>
          )}
        </div>
      )}
    </div>
  );
}
