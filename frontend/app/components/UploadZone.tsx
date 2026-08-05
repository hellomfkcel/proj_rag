"use client";
// Document Upload Zone — multi-file upload with progress bars + retry

import { useState, useRef, useCallback } from "react";
import { useAuthStore } from "@/stores/useAuthStore";

interface Props { kbId: string; onUploaded: () => void; }

interface FileStatus {
  name: string;
  file: File;
  status: "pending" | "uploading" | "success" | "duplicate" | "error" | "skipped";
  msg: string;
  progress: number;
}

const ALLOWED = /\.(txt|md|pdf|docx|html|csv|json)$/i;
const MAX_SIZE = 50 * 1024 * 1024;

export default function UploadZone({ kbId, onUploaded }: Props) {
  const user = useAuthStore((s) => s.user);
  const [dragging, setDragging] = useState(false);
  const [files, setFiles] = useState<FileStatus[]>([]);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // 未选定 KB 时禁止上传
  if (!kbId) {
    return (
      <div className="bg-yellow-50 border border-yellow-200 rounded-xl p-8 text-center">
        <p className="text-yellow-700 text-sm font-medium">请先在顶部选择一个知识库</p>
        <p className="text-yellow-500 text-xs mt-1">选择知识库后才能上传文档</p>
      </div>
    );
  }

  const tenantId = user?.tenant_id || "";
  const userId = user?.id || "";

  const uploadOne = useCallback(async (item: FileStatus, idx: number, statuses: FileStatus[]) => {
    statuses[idx] = { ...item, status: "uploading", msg: "上传中...", progress: 0 };
    setFiles([...statuses]);

    try {
      const { uploadDocument } = await import("@/lib/kb");
      const result = await uploadDocument(
        kbId, item.file, tenantId, userId,
        (pct) => {
          statuses[idx] = { ...statuses[idx], progress: pct, msg: `${pct}%` };
          setFiles([...statuses]);
        }
      );
      statuses[idx] = result.duplicate
        ? { ...item, status: "duplicate", msg: "已存在 (指纹去重)", progress: 100 }
        : { ...item, status: "success", msg: "上传成功", progress: 100 };
    } catch (e: any) {
      statuses[idx] = { ...item, status: "error", msg: e.response?.data?.detail || e.message || "上传失败", progress: 0 };
    }
    setFiles([...statuses]);
  }, [kbId, tenantId, userId]);

  const handleFileInput = async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return;
    setUploading(true);

    const newItems: FileStatus[] = [];
    for (let i = 0; i < fileList.length; i++) {
      const f = fileList[i];
      if (!f.name.match(ALLOWED)) {
        newItems.push({ name: f.name, file: f, status: "skipped", msg: "不支持格式", progress: 0 });
        continue;
      }
      if (f.size > MAX_SIZE) {
        newItems.push({ name: f.name, file: f, status: "skipped", msg: "超过50MB限制", progress: 0 });
        continue;
      }
      newItems.push({ name: f.name, file: f, status: "pending", msg: "等待上传", progress: 0 });
    }

    const statuses = [...files, ...newItems];
    setFiles(statuses);

    for (let i = 0; i < newItems.length; i++) {
      await uploadOne(newItems[i], files.length + i, statuses);
    }

    setUploading(false);
    onUploaded();
  };

  const retryFailed = async () => {
    const statuses = [...files];
    setUploading(true);
    for (let i = 0; i < statuses.length; i++) {
      if (statuses[i].status === "error" || statuses[i].status === "skipped") {
        // Reset skipped to pending (re-validate)
        if (statuses[i].status === "skipped") continue; // Can't retry invalid files
        await uploadOne(statuses[i], i, statuses);
      }
    }
    setUploading(false);
    onUploaded();
  };

  const dropHandler = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    handleFileInput(e.dataTransfer.files);
  };

  const statColor = (s: string) =>
    s === "success" ? "text-green-600" :
    s === "duplicate" ? "text-yellow-600" :
    s === "error" || s === "skipped" ? "text-red-500" :
    s === "uploading" ? "text-blue-600" : "text-gray-400";

  const statIcon = (s: string) =>
    s === "success" ? "✅" : s === "duplicate" ? "⚠️" :
    s === "error" ? "❌" : s === "skipped" ? "⏭️" :
    s === "uploading" ? "⏳" : "📄";

  const successCount = files.filter(f => f.status === "success" || f.status === "duplicate").length;
  const failCount = files.filter(f => f.status === "error" || f.status === "skipped").length;

  return (
    <div>
      {/* Drop zone */}
      <div
        className={`border-2 border-dashed rounded-xl p-6 text-center transition cursor-pointer ${
          dragging ? "border-blue-500 bg-blue-50" : "border-gray-300 hover:border-gray-400 bg-white"
        }`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={dropHandler}
        onClick={() => inputRef.current?.click()}
      >
        <input
          ref={inputRef}
          type="file"
          className="hidden"
          multiple
          accept=".txt,.md,.pdf,.docx,.html,.csv,.json"
          onChange={(e) => handleFileInput(e.target.files)}
        />
        <div className="text-3xl mb-2">📁</div>
        <p className="text-gray-600 font-medium text-sm">
          {uploading ? "上传中..." : "拖拽文件到此处，或点击选择文件"}
        </p>
        <p className="text-xs text-gray-400 mt-1">支持批量 · .txt .md .pdf .docx .html .csv · 最大 50MB</p>
      </div>

      {/* File status list with progress bars */}
      {files.length > 0 && (
        <div className="mt-3 bg-white border border-gray-200 rounded-lg overflow-hidden">
          <div className="px-3 py-2 bg-gray-50 border-b border-gray-200 flex items-center justify-between text-xs text-gray-500">
            <span>{files.length} 个文件</span>
            <span>{successCount} 成功 · {failCount} 失败</span>
          </div>
          <div className="max-h-64 overflow-y-auto">
            {files.map((f, i) => (
              <div key={i} className="px-3 py-2 border-b border-gray-50 last:border-0">
                <div className="flex items-center gap-2 text-sm">
                  <span>{statIcon(f.status)}</span>
                  <span className="truncate flex-1 text-gray-700">{f.name}</span>
                  <span className={`text-xs shrink-0 ${statColor(f.status)}`}>{f.msg}</span>
                </div>
                {/* Progress bar (shown during upload) */}
                {f.status === "uploading" && (
                  <div className="mt-1.5 w-full bg-gray-100 rounded-full h-1.5 overflow-hidden">
                    <div
                      className="h-full bg-blue-500 rounded-full transition-all duration-300"
                      style={{ width: `${f.progress}%` }}
                    />
                  </div>
                )}
              </div>
            ))}
          </div>
          <div className="px-3 py-2 border-t border-gray-100 flex items-center justify-between">
            <button onClick={() => setFiles([])} className="text-xs text-gray-400 hover:text-gray-600">清除记录</button>
            {failCount > 0 && (
              <button
                onClick={retryFailed}
                disabled={uploading}
                className="text-xs text-blue-600 hover:text-blue-700 font-medium disabled:opacity-50"
              >
                🔄 重试失败 ({failCount})
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
