// Directory API — Phase 4
import api from "./api";

export interface DirItem { id: string; name: string; directory_type: string; bound_kb_id?: string; parent_id?: string; doc_count: number; }

export async function listDirs(kbId: string) { const { data } = await api.get(`/knowledge-bases/${kbId}/directories`); return data as DirItem[]; }
export async function createDir(name: string, kbId: string, parentId?: string) { const { data } = await api.post("/directories", { name, parent_id: parentId, kb_id: kbId }); return data; }
export async function renameDir(id: string, name: string) { const { data } = await api.patch(`/directories/${id}`, { name }); return data; }
export async function deleteDir(id: string) { const { data } = await api.delete(`/directories/${id}`); return data; }
export async function moveDocToDir(dirId: string, docId: string) { await api.post(`/directories/${dirId}/documents`, { document_id: docId }); }
export async function removeDocFromDir(dirId: string, docId: string) { await api.delete(`/directories/${dirId}/documents/${docId}`); }
export async function renameDocument(docId: string, filename: string) { const { data } = await api.patch(`/documents/${docId}`, { filename }); return data; }
export async function batchDelete(items: { document_id: string; kb_id: string }[]) { const { data } = await api.post("/documents/batch/delete", { items }); return data; }
export async function batchParse(mountIds: string[]) { const { data } = await api.post("/documents/batch/parse", { mount_ids: mountIds }); return data; }
