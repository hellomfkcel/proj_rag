"use client";
// Conversation Sidebar List — Phase 3: select, delete, inline rename

import { useState } from "react";
import type { Conversation } from "@/lib/chat";

interface Props {
  convs: Conversation[];
  activeId: string | null;
  onSelect: (c: Conversation) => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
}

export default function ConvList({ convs, activeId, onSelect, onDelete, onRename }: Props) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editVal, setEditVal] = useState("");

  const startRename = (c: Conversation) => {
    setEditingId(c.id);
    setEditVal(c.title || "");
  };

  const commitRename = (id: string) => {
    const v = editVal.trim();
    if (v) onRename(id, v);
    setEditingId(null);
  };

  return (
    <div className="flex-1 overflow-y-auto py-1">
      {convs.length === 0 && (
        <div className="px-4 py-8 text-center text-gray-400 text-sm">
          📭 暂无会话
        </div>
      )}
      {convs.map((c) => (
        <div
          key={c.id}
          onClick={() => editingId !== c.id && onSelect(c)}
          className={`group flex items-center justify-between px-3 py-2.5 mx-1 rounded-lg cursor-pointer transition ${
            c.id === activeId ? "bg-blue-100 text-blue-800" : "hover:bg-gray-100 text-gray-700"
          }`}
        >
          {editingId === c.id ? (
            <input
              autoFocus
              className="flex-1 text-sm px-1.5 py-0.5 border border-blue-300 rounded outline-none"
              value={editVal}
              onChange={(e) => setEditVal(e.target.value)}
              onBlur={() => commitRename(c.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitRename(c.id);
                if (e.key === "Escape") setEditingId(null);
              }}
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <>
              <div className="min-w-0 flex-1">
                <div className="text-sm truncate">{c.title || "新会话"}</div>
                <div className="text-xs text-gray-400">{c.turn_count || 0} 轮</div>
              </div>
              <div className="flex items-center gap-0.5 ml-1">
                <button
                  onClick={(e) => { e.stopPropagation(); startRename(c); }}
                  className="opacity-0 group-hover:opacity-100 px-1 text-gray-400 hover:text-blue-500 rounded transition text-xs"
                  title="重命名"
                >
                  ✏️
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); onDelete(c.id); }}
                  className="opacity-0 group-hover:opacity-100 px-1 text-gray-400 hover:text-red-500 rounded transition text-xs"
                  title="删除"
                >
                  🗑
                </button>
              </div>
            </>
          )}
        </div>
      ))}
    </div>
  );
}
