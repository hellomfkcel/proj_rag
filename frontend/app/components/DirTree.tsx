"use client";
// Directory Sidebar — kb_bound (auto) + manual folders, drag-to-move support

import { useState } from "react";
import { moveDocToDir } from "@/lib/dirs";
import type { DirItem } from "@/lib/dirs";

interface Props {
  dirs: DirItem[];
  selectedDirId: string | null;
  onSelectDir: (id: string | null) => void;
  onCreateDir: (name: string) => void;
  onRenameDir: (id: string, name: string) => void;
  onDeleteDir: (id: string) => void;
}

export default function DirTree({ dirs, selectedDirId, onSelectDir, onCreateDir, onRenameDir, onDeleteDir }: Props) {
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");

  // Drag state
  const [dragOverId, setDragOverId] = useState<string | null>(null);

  const kbBound = dirs.filter(d => d.directory_type === "kb_bound");
  const manual = dirs.filter(d => d.directory_type !== "kb_bound");

  const handleCreate = () => {
    if (!newName.trim()) return;
    onCreateDir(newName.trim());
    setNewName(""); setShowCreate(false);
  };

  const handleDrop = async (dirId: string, e: React.DragEvent) => {
    e.preventDefault();
    setDragOverId(null);
    try {
      const raw = e.dataTransfer.getData("text/doc-id");
      if (raw) {
        await moveDocToDir(dirId, raw);
        // Refresh parent
        window.dispatchEvent(new CustomEvent("doc-moved"));
      }
    } catch {}
  };

  const handleDragOver = (dirId: string, e: React.DragEvent) => {
    e.preventDefault();
    setDragOverId(dirId);
  };

  const handleDragLeave = () => {
    setDragOverId(null);
  };

  return (
    <div className="h-full flex flex-col">
      <div className="px-3 py-2 border-b border-gray-200 flex items-center justify-between">
        <span className="text-xs font-semibold text-gray-500 uppercase tracking-wider">目录</span>
        <button onClick={() => setShowCreate(true)} className="text-gray-400 hover:text-blue-600 text-lg leading-none" title="新建文件夹">+</button>
      </div>

      {showCreate && (
        <div className="px-3 py-2 border-b border-gray-100">
          <input autoFocus className="w-full px-2 py-1 text-xs border border-blue-300 rounded outline-none" placeholder="文件夹名"
            value={newName} onChange={e => setNewName(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") handleCreate(); if (e.key === "Escape") { setShowCreate(false); setNewName(""); } }} />
        </div>
      )}

      <div className="flex-1 overflow-y-auto py-1">
        {/* "全部文档" pseudo-root */}
        <div onClick={() => onSelectDir(null)}
          className={`flex items-center px-3 py-2 cursor-pointer text-sm mx-1 rounded transition ${!selectedDirId ? "bg-blue-100 text-blue-700 font-medium" : "text-gray-600 hover:bg-gray-100"}`}>
          <span className="mr-2">📂</span><span>全部文档</span>
          <span className="ml-auto text-xs text-gray-400">{dirs.reduce((s, d) => s + (d.doc_count || 0), 0)}</span>
        </div>

        {/* kb_bound directories — read only */}
        {kbBound.map(d => (
          <div key={d.id} onClick={() => onSelectDir(d.id)}
            className={`flex items-center px-3 py-2 cursor-pointer text-sm mx-1 rounded transition ${d.id === selectedDirId ? "bg-blue-100 text-blue-700 font-medium" : "text-gray-600 hover:bg-gray-100"}`}>
            <span className="mr-2">📄</span><span className="truncate">{d.name}</span>
            <span className="ml-auto text-xs text-gray-400">{d.doc_count}</span>
          </div>
        ))}

        {/* Manual directories — draggable targets */}
        {manual.map(d => (
          <div
            key={d.id}
            className={`group flex items-center px-3 py-2 mx-1 rounded transition cursor-pointer ${
              d.id === selectedDirId ? "bg-blue-100 text-blue-700 font-medium" : "text-gray-600 hover:bg-gray-100"
            } ${dragOverId === d.id ? "bg-blue-100 border-2 border-blue-400 border-dashed" : ""}`}
            onDragOver={(e) => handleDragOver(d.id, e)}
            onDragLeave={handleDragLeave}
            onDrop={(e) => handleDrop(d.id, e)}
          >
            {editingId === d.id ? (
              <input autoFocus className="flex-1 px-1 py-0 text-xs border border-blue-300 rounded outline-none"
                value={editName} onChange={e => setEditName(e.target.value)}
                onBlur={() => { if (editName.trim() && editName !== d.name) onRenameDir(d.id, editName.trim()); setEditingId(null); }}
                onKeyDown={e => { if (e.key === "Enter") { if (editName.trim() && editName !== d.name) onRenameDir(d.id, editName.trim()); setEditingId(null); } if (e.key === "Escape") setEditingId(null); }}
                onClick={e => e.stopPropagation()} />
            ) : (
              <>
                <span className="mr-2 cursor-pointer" onClick={() => onSelectDir(d.id)}>📁</span>
                <span className="truncate text-sm cursor-pointer flex-1" onClick={() => onSelectDir(d.id)}>{d.name}</span>
                <span className="text-xs text-gray-400 mr-1">{d.doc_count}</span>
                <button className="opacity-0 group-hover:opacity-100 px-1 text-gray-400 hover:text-blue-500 text-xs" onClick={e => { e.stopPropagation(); setEditingId(d.id); setEditName(d.name); }}>✏️</button>
                <button className="opacity-0 group-hover:opacity-100 px-1 text-gray-400 hover:text-red-500 text-xs" onClick={e => { e.stopPropagation(); onDeleteDir(d.id); }}>🗑</button>
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
