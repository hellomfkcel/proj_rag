// Settings API — Phase 5
import api from "./api";

export async function listModels() { const {data}=await api.get("/models"); return data; }
export async function setDefaultModel(modelId:string) { const {data}=await api.patch(`/models/${modelId}/set-default`); return data; }
export async function createModel(body: Record<string, any>) { const {data}=await api.post("/models", body); return data; }
export async function updateModel(modelId:string, fields: Record<string, any>) { const {data}=await api.patch(`/models/${modelId}`, fields); return data; }
export async function deleteModel(modelId:string) { const {data}=await api.delete(`/models/${modelId}`); return data; }
export async function setModelApiKey(modelId:string, apiKey:string) { const {data}=await api.put(`/models/${modelId}/api-key`, {api_key: apiKey}); return data; }
export async function testModelConnection(modelId:string) { const {data}=await api.post(`/models/${modelId}/test`); return data; }
export async function getRetrievalConfig(kbId:string){const {data}=await api.get(`/configs/retrieval?kb_id=${kbId}`);return data;}
export async function updateRetrievalConfig(kbId:string,fields:Record<string,any>){const {data}=await api.patch(`/configs/retrieval?kb_id=${kbId}`,fields);return data;}
export async function listPrompts(){const {data}=await api.get("/prompts");return data;}
export async function activatePrompt(id:string){const {data}=await api.patch(`/prompts/${id}/activate`);return data;}
export async function updatePrompt(id:string, fields:{template_text:string; description?:string}){const {data}=await api.patch(`/prompts/${id}`, fields);return data;}

// Dynamic config — Phase 6: external URLs from backend
export interface AppConfig { grafana_url:string;langfuse_url:string;cerbos_url:string;admin_console_url:string;otel_collector_url:string; }
let _appConfig: AppConfig|null = null;
export async function getAppConfig(): Promise<AppConfig> {
  if (_appConfig) return _appConfig;
  const {data} = await api.get("/config");
  _appConfig = data;
  return data;
}
