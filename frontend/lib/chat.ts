// Chat API — Phase 3

import api from "./api";

export interface Conversation {
  id: string; title: string; turn_count: number; bound_kb_ids: string[]; created_at: string;
}
export interface Turn {
  id: string; turn_index: number; user_question: string; resolved_query: string; answer: string; chunk_ids: string[];
}

export interface QueryOverrides {
  retrieval_mode?: string;
  fusion_method?: string;
  strict?: boolean;
  top_k?: number;
  dense_weight?: number;
  sparse_weight?: number;
  synthesis_mode?: string;
}

export interface QueryResult {
  answer: string; chunk_ids: string[]; conversation_id: string; turn_index: number;
  error_code?: string;
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
  });
  return data;
}
