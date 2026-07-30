// Dashboard API — Phase 6
import api from "./api";

export async function usageStats(days?:number){const {data}=await api.get(`/stats/usage?days=${days||30}`);return data;}
export async function topKBs(days?:number){const {data}=await api.get(`/stats/top-kbs?days=${days||30}`);return data;}
export async function docStats(){const {data}=await api.get("/stats/documents");return data;}
export async function qualityStats(){const {data}=await api.get("/stats/quality");return data;}
