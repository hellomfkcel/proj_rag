// Chat API — Phase 3

import api from "./api";

export interface Conversation {
  id: string; title: string; turn_count: number; bound_kb_ids: string[]; created_at: string;
}
export interface Turn {
  id: string; turn_index: number; user_question: string; resolved_query: string; answer: string; chunk_ids: string[];
  trace_id?: string;
  retrieved_chunks?: { chunk_id: string; doc_name?: string; content?: string }[];
}

export interface QueryOverrides {
  retrieval_mode?: string;
  fusion_method?: string;
  strict?: boolean;
  top_k?: number;
  dense_weight?: number;
  sparse_weight?: number;
  synthesis_mode?: string;
  oversample_factor?: number;
  min_results?: number;
  refetch_max_rounds?: number;
  refine_batch_size?: number;
  doc_preview_max_chars?: number;
  tree_summarize_batch_size?: number;
  max_answer_length?: number;
  compress_target_length?: number;
}

export interface QueryResult {
  answer: string; chunk_ids: string[]; conversation_id: string; turn_index: number;
  error_code?: string;
  trace_id?: string;       // 本次查询的 OTel trace_id（跳 Tempo 查看链路）
  trace_ui_url?: string;   // Grafana Tempo 查看链路深链
}

export async function listConversations() {
  const { data } = await api.get("/conversations");
  return data as Conversation[];
}

export async function createConversation(kbIds: string[]) {
  const { data } = await api.post("/conversations", { kb_ids: kbIds });
  return data as Conversation;
}

export async function deleteConversation(id: string) {
  const { data } = await api.delete(`/conversations/${id}`);
  return data;
}

export async function renameConversation(id: string, title: string) {
  const { data } = await api.patch(`/conversations/${id}`, { title });
  return data;
}

export async function getTurns(convId: string) {
  const { data } = await api.get(`/conversations/${convId}/turns`);
  return data as Turn[];
}

export async function query(question: string, kbIds: string[], convId?: string, overrides?: QueryOverrides): Promise<QueryResult> {
  const { data } = await api.post("/conversations/query", {
    question, kb_ids: kbIds, conversation_id: convId || undefined,
    retrieval_mode: overrides?.retrieval_mode,
    fusion_method: overrides?.fusion_method,
    strict: overrides?.strict,
    top_k: overrides?.top_k,
    dense_weight: overrides?.dense_weight,
    sparse_weight: overrides?.sparse_weight,
    synthesis_mode: overrides?.synthesis_mode,
    oversample_factor: overrides?.oversample_factor,
    min_results: overrides?.min_results,
    refetch_max_rounds: overrides?.refetch_max_rounds,
    refine_batch_size: overrides?.refine_batch_size,
    doc_preview_max_chars: overrides?.doc_preview_max_chars,
    tree_summarize_batch_size: overrides?.tree_summarize_batch_size,
    max_answer_length: overrides?.max_answer_length,
    compress_target_length: overrides?.compress_target_length,
  });
  return data;
}
