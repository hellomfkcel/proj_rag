// KB + Document API — Phase 2

import api from "./api";

// ── KB ──

export async function listKBs() {
  const { data } = await api.get("/knowledge-bases");
  return data;
}

export async function createKB(name: string, description: string, chunkingStrategy: string = "sentence") {
  const { data } = await api.post("/knowledge-bases", { name, description, chunking_strategy: chunkingStrategy });
  return data;
}

export async function renameKB(kbId: string, name: string) {
  const { data } = await api.patch(`/knowledge-bases/${kbId}`, { name });
  return data;
}

export async function deleteKB(kbId: string) {
  const { data } = await api.delete(`/knowledge-bases/${kbId}`);
  return data;
}

// ── Documents ──

export async function listDocuments(kbId: string, search?: string, status?: string, sortBy?: string, order?: string) {
  const params = new URLSearchParams();
  if (search) params.set("search", search);
  if (status && status !== "all") params.set("status", status);
  if (sortBy) params.set("sort_by", sortBy);
  if (order) params.set("order", order);
  const qs = params.toString();
  const { data } = await api.get(`/knowledge-bases/${kbId}/documents${qs ? `?${qs}` : ""}`);
  return data;
}

export async function uploadDocument(
  kbId: string, file: File, tenantId: string, userId: string,
  onProgress?: (pct: number) => void,
) {
  const form = new FormData();
  form.append("file", file);
  form.append("kb_id", kbId);
  form.append("tenant_id", tenantId);
  form.append("user_id", userId);
  form.append("auto_parse", "false");
  const { data } = await api.post("/documents/upload", form, {
    headers: { "Content-Type": "multipart/form-data" },
    onUploadProgress: (e) => {
      if (e.total && onProgress) {
        onProgress(Math.round((e.loaded * 100) / e.total));
      }
    },
  });
  return data;
}

// ── Chunking Config ──

export async function getChunkingConfig(kbId: string) {
  const { data } = await api.get(`/knowledge-bases/${kbId}/chunking-config`);
  return data;
}

export async function updateChunkingConfig(kbId: string, fields: Record<string, any>) {
  const { data } = await api.patch(`/knowledge-bases/${kbId}/chunking-config`, fields);
  return data;
}

export async function toggleDocument(docId: string, kbId: string, enabled: boolean) {
  const { data } = await api.patch(`/documents/${docId}/kb/${kbId}`, { is_enabled: enabled });
  return data;
}

export async function triggerParse(docId: string) {
  const { data } = await api.post(`/documents/${docId}/trigger-parse`);
  return data;
}

export async function deleteDocument(docId: string, kbId: string) {
  const { data } = await api.delete(`/documents/${docId}/kb/${kbId}`);
  return data;
}

// ── Document Preview ──

export async function getDocumentDetail(docId: string) {
  const { data } = await api.get(`/documents/${docId}`);
  return data;
}

export async function getDocumentChunks(docId: string) {
  const { data } = await api.get(`/documents/${docId}/chunks`);
  return data as { chunk_id: string; content: string }[];
}

export async function getDocumentContent(docId: string) {
  const { data } = await api.get(`/documents/${docId}/content`);
  return data as { content: string };
}

export function getDownloadUrl(docId: string): string {
  return `/api/v1/documents/${docId}/download`;
}
