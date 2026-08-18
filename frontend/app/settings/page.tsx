"use client";
// Settings Page — models, retrieval config, chunking config, prompts with preview

import { Fragment, useEffect, useState, useMemo } from "react";
import { useAuthStore } from "@/stores/useAuthStore";
import { useKBStore } from "@/stores/useKBStore";
import Header from "@/app/components/Header";
import { listModels, setDefaultModel, createModel, updateModel, deleteModel, setModelApiKey, testModelConnection,
         getRetrievalConfig, updateRetrievalConfig,
         listPrompts, activatePrompt, updatePrompt, getAppConfig } from "@/lib/settings";
import { getChunkingConfig, updateChunkingConfig } from "@/lib/kb";
import { isAdmin } from "@/lib/permissions";

interface Model { model_id:string;model_type:string;provider:string;model_name:string;base_url:string;is_default:boolean;
                  has_key:boolean;key_source:string;effective_model_name:string;effective_base_url:string;is_local:boolean; }
interface Prompt { id:string;prompt_id:string;version:string;template_text:string;description:string;is_active:boolean; }
interface RConfig { top_k:number;retrieval_mode:string;fusion_method:string;synthesis_mode:string;rerank_model_id:string;strict:boolean;oversample_factor:number;min_results:number;refetch_max_rounds:number;haystack_pipeline_name:string;dense_weight?:number;sparse_weight?:number;min_score?:number;refine_batch_size?:number;tree_summarize_batch_size?:number;max_answer_length?:number;compress_target_length?:number;doc_preview_max_chars?:number; }
interface ChunkCfg { kb_id:string;version:string;haystack_strategy:string;split_length:number;split_overlap:number;advanced_params?:Record<string,any>; }

// 与 DB model_registry.model_type 值一致（llm/embedding/reranker；rerank 为历史遗留值）
const MODEL_TYPE_LABELS: Record<string, string> = { llm: "🤖 生成模型", embedding: "📊 嵌入模型", reranker: "🔍 重排模型", rerank: "🔍 重排模型" };
const KEY_SOURCE_LABELS: Record<string, string> = { db: "DB key", env: "env 兜底", none: "无 key" };
const PROVIDER_OPTIONS = ["ollama", "vllm", "deepseek", "openai", "sentence_transformers", "local", "infinity"];

const LOCAL_MODEL_HINT = "本地进程内直载权重（FlagEmbedding）。加载器固定、模型名锁死，此条目仅登记用途，切换模型请走 vllm/ollama/infinity 或外部 API。";
const PROMPT_PREVIEW_QUESTION = "如何在 Kubernetes 中配置资源限制？";

export default function SettingsPage() {
  const token = useAuthStore(s=>s.token);
  const {selectedKBs} = useKBStore();
  const selectedKB = selectedKBs.length > 0 ? selectedKBs[0] : null;

  const [models,setModels]=useState<Model[]>([]);
  const [config,setConfig]=useState<RConfig|null>(null);
  const [prompts,setPrompts]=useState<Prompt[]>([]);
  const [saved,setSaved]=useState(false);
  const [chunkSaved,setChunkSaved]=useState(false);
  const [chunkCfg,setChunkCfg]=useState<ChunkCfg|null>(null);
  const [appCfg,setAppCfg]=useState<{grafana_url:string;langfuse_url:string;cerbos_url:string;admin_console_url:string}|null>(null);

  // Prompt selector state
  const [selectedPromptId, setSelectedPromptId] = useState<string | null>(null);
  const [expandedPrompt, setExpandedPrompt] = useState<string | null>(null);
  // Prompt edit state
  const [editingPromptId, setEditingPromptId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [savingPrompt, setSavingPrompt] = useState(false);
  const [promptError, setPromptError] = useState<string | null>(null);

  useEffect(()=>{if(!token)return;fetchModels();fetchPrompts();getAppConfig().then(setAppCfg).catch(()=>{});},[token]);
  useEffect(()=>{if(selectedKB){fetchConfig();fetchChunkConfig();}},[selectedKB]);

  const fetchModels=async()=>{try{setModels(await listModels());}catch{}};
  const fetchPrompts=async()=>{try{setPrompts(await listPrompts());}catch{}};
  const fetchConfig=async()=>{try{setConfig(await getRetrievalConfig(selectedKB!));}catch{}};
  const fetchChunkConfig=async()=>{try{setChunkCfg(await getChunkingConfig(selectedKB!));}catch{}};

  const saveConfig=async(fields:Record<string,any>)=>{
    if(!selectedKB)return;
    await updateRetrievalConfig(selectedKB,fields);
    setConfig(prev=>({...prev!,...fields}));
    setSaved(true); setTimeout(()=>setSaved(false),2000);
  };
  const saveChunkConfig=async(fields:Record<string,any>)=>{
    if(!selectedKB)return;
    await updateChunkingConfig(selectedKB,fields);
    setChunkCfg(prev=>prev?{...prev,...fields}:null);
    setChunkSaved(true); setTimeout(()=>setChunkSaved(false),2000);
  };

  const handleSetDefaultModel = async (modelId: string) => {
    await setDefaultModel(modelId);
    fetchModels();
  };

  // ── 模型编辑/创建 ──
  const [modelDialog, setModelDialog] = useState<null | { mode: "create" } | { mode: "edit"; model: Model }>(null);
  const [modelForm, setModelForm] = useState({ model_id:"", model_type:"llm", provider:"ollama", model_name:"", base_url:"", api_key:"", is_default:false });
  const [modelSaving, setModelSaving] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, {ok:boolean;detail:string}>>({});

  const openCreateModel = () => {
    setModelForm({ model_id:"", model_type:"llm", provider:"ollama", model_name:"", base_url:"", api_key:"", is_default:false });
    setModelError(null);
    setModelDialog({ mode:"create" });
  };
  const openEditModel = (m: Model) => {
    setModelForm({ model_id:m.model_id, model_type:m.model_type, provider:m.provider, model_name:m.model_name, base_url:m.base_url, api_key:"", is_default:m.is_default });
    setModelError(null);
    setModelDialog({ mode:"edit", model:m });
  };

  const handleModelSubmit = async () => {
    setModelSaving(true); setModelError(null);
    try {
      if (modelDialog?.mode === "create") {
        await createModel({ model_id:modelForm.model_id, model_type:modelForm.model_type, provider:modelForm.provider,
          model_name:modelForm.model_name, base_url:modelForm.base_url, api_key:modelForm.api_key, is_default:modelForm.is_default });
      } else if (modelDialog?.mode === "edit") {
        await updateModel(modelDialog.model.model_id, { provider:modelForm.provider, model_name:modelForm.model_name,
          base_url:modelForm.base_url, is_default:modelForm.is_default });
        if (modelForm.api_key) {
          await setModelApiKey(modelDialog.model.model_id, modelForm.api_key);
        }
      }
      setModelDialog(null); fetchModels();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      setModelError(err.response?.data?.detail || "保存失败");
    } finally { setModelSaving(false); }
  };

  const handleDeleteModel = async (m: Model) => {
    if (!confirm(`确认删除模型「${m.model_id}」？`)) return;
    try {
      await deleteModel(m.model_id);
      fetchModels();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      alert(err.response?.data?.detail || "删除失败");
    }
  };

  const handleTestModel = async (m: Model) => {
    setTestResults(prev => ({ ...prev, [m.model_id]: { ok: false, detail: "测试中…" } }));
    try {
      const r = await testModelConnection(m.model_id);
      setTestResults(prev => ({ ...prev, [m.model_id]: { ok: r.ok, detail: r.detail } }));
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      setTestResults(prev => ({ ...prev, [m.model_id]: { ok: false, detail: err.response?.data?.detail || "测试失败" } }));
    }
  };

  const handleActivatePrompt = async (promptId: string) => {
    await activatePrompt(promptId);
    fetchPrompts();
  };

  const startEditPrompt = (p: Prompt) => {
    setEditingPromptId(p.id);
    setEditText(p.template_text);
    setEditDesc(p.description || "");
    setPromptError(null);
  };

  const cancelEditPrompt = () => {
    setEditingPromptId(null);
    setPromptError(null);
  };

  const handleUpdatePrompt = async () => {
    if (!editingPromptId) return;
    setSavingPrompt(true);
    setPromptError(null);
    try {
      await updatePrompt(editingPromptId, { template_text: editText, description: editDesc || undefined });
      setEditingPromptId(null);
      await fetchPrompts();
    } catch (e: any) {
      setPromptError(e?.response?.data?.detail || "保存失败，请重试");
    } finally {
      setSavingPrompt(false);
    }
  };

  // Group prompts by prompt_id
  const promptGroups = useMemo(() => {
    const map = new Map<string, Prompt[]>();
    for (const p of prompts) {
      const arr = map.get(p.prompt_id) || [];
      arr.push(p);
      map.set(p.prompt_id, arr);
    }
    return [...map.entries()];
  }, [prompts]);

  // Currently selected prompt object
  const selectedPrompt = useMemo(() => {
    return prompts.find(p => p.is_active) || (promptGroups[0]?.[1]?.[0] || null);
  }, [prompts, promptGroups]);

  // Preview renderer — simple Jinja2-style {{ variable }} replacement
  const renderPreview = (template: string): string => {
    return template
      .replace(/\{\{\s*query\s*\}\}/gi, PROMPT_PREVIEW_QUESTION)
      .replace(/\{\{\s*documents\s*\}\}/gi, "[文档1] Kubernetes资源限制配置指南...\n[文档2] Pod QoS Classes 说明...\n[文档3] LimitRange 对象参考...")
      .replace(/\{\{\s*doc\s*\}\}/gi, "[文档1] Kubernetes资源限制配置指南...")
      .replace(/\{\{\s*context\s*\}\}/gi, "用户是一线运维工程师，需要了解资源限制配置")
      .replace(/\{\{\s*\w+\s*\}\}/g, (m) => `[${m.replace(/[{}]/g,"").trim()}]`);
  };

  // Group models by type
  const modelsByType = useMemo(() => {
    const map = new Map<string, Model[]>();
    for (const m of models) {
      const arr = map.get(m.model_type) || [];
      arr.push(m);
      map.set(m.model_type, arr);
    }
    return [...map.entries()];
  }, [models]);

  if (!token) return null;

  // 非管理员用户：显示权限不足页面
  if (!isAdmin()) {
    return <div className="min-h-screen bg-gray-50">
      <Header />
      <main className="max-w-4xl mx-auto px-6 py-8">
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-12 text-center">
          <div className="text-6xl mb-4">🔒</div>
          <h1 className="text-2xl font-bold text-gray-900 mb-2">需要管理员权限</h1>
          <p className="text-gray-500 mb-6">
            系统设置（模型管理、检索配置、切分配置、Prompt 模板）仅对系统管理员开放。
          </p>
          <a
            href="/kb"
            className="inline-block px-6 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition font-medium"
          >
            返回知识库 →
          </a>
        </div>
      </main>
    </div>;
  }

  return <div className="min-h-screen bg-gray-50">
    <Header />
    <main className="max-w-4xl mx-auto px-6 py-8 space-y-8">
      <h1 className="text-2xl font-bold text-gray-900">⚙️ 设置</h1>

      {/* ── Models ── */}
      <section className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">🔧 模型管理</h2>
          <button
            onClick={openCreateModel}
            className="text-xs bg-blue-600 text-white px-3 py-1.5 rounded-lg hover:bg-blue-700 transition font-medium">
            ＋ 新增模型
          </button>
        </div>
        <p className="text-xs text-gray-400 mb-4 leading-relaxed">
          HTTP 服务类模型（外部 API / vllm / ollama / infinity）可完整管理（模型名/base_url/key/默认切换生效）；
          <span className="text-amber-600">local / sentence_transformers 为本地进程内直载（FlagEmbedding 加载器固定），仅登记用途</span>，
          切换请走 HTTP 服务类。
        </p>
        {modelsByType.map(([type, typeModels]) => (
          <div key={type} className="mb-4 last:mb-0">
            <h3 className="text-sm font-medium text-gray-500 mb-2">{MODEL_TYPE_LABELS[type] || type}</h3>
            <div className="grid gap-2">
              {typeModels.map(m => (
                <div key={m.model_id}
                  className={`px-4 py-3 rounded-lg border transition ${m.is_default ? "border-blue-300 bg-blue-50" : "border-gray-200 hover:border-gray-300"}`}>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div>
                        <span className="font-medium text-sm text-gray-800">{m.model_name}</span>
                        <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                          <span className="text-xs text-gray-400">{m.provider}</span>
                          {m.base_url && <span className="text-[10px] text-gray-300 truncate max-w-[180px]">{m.base_url}</span>}
                          {/* 生效秘钥来源徽标 */}
                          <span className={`text-[10px] px-1.5 py-0.5 rounded-full border ${
                            m.key_source === "db" ? "bg-emerald-50 text-emerald-600 border-emerald-200"
                            : m.key_source === "env" ? "bg-amber-50 text-amber-600 border-amber-200"
                            : "bg-gray-50 text-gray-400 border-gray-200"}`}>
                            {KEY_SOURCE_LABELS[m.key_source] || m.key_source}
                            {m.has_key ? " 🔑" : ""}
                          </span>
                          {/* 本地直载·登记徽标 */}
                          {m.is_local && (
                            <span title={LOCAL_MODEL_HINT}
                              className="text-[10px] px-1.5 py-0.5 rounded-full border bg-purple-50 text-purple-600 border-purple-200 cursor-help">
                              🖥️ 本地直载·登记
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 flex-wrap justify-end">
                      {m.is_default ? (
                        <span className="text-xs bg-blue-600 text-white px-2 py-0.5 rounded-full font-medium">✓ 默认</span>
                      ) : (
                        <button onClick={() => handleSetDefaultModel(m.model_id)}
                          className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full hover:bg-blue-100 hover:text-blue-700 transition font-medium">
                          设为默认
                        </button>
                      )}
                      <button onClick={() => handleTestModel(m)}
                        className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full hover:bg-blue-100 hover:text-blue-700 transition font-medium">
                        测试
                      </button>
                      {m.is_local ? (
                        <span title={LOCAL_MODEL_HINT}
                          className="text-xs bg-gray-50 text-gray-400 px-2 py-0.5 rounded-full cursor-not-allowed select-none">
                          🔒 登记
                        </span>
                      ) : (
                        <>
                          <button onClick={() => openEditModel(m)}
                            className="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full hover:bg-blue-100 hover:text-blue-700 transition font-medium">
                            编辑
                          </button>
                          <button onClick={() => handleDeleteModel(m)}
                            className="text-xs bg-gray-100 text-gray-400 px-2 py-0.5 rounded-full hover:bg-red-50 hover:text-red-600 transition font-medium">
                            删除
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                  {testResults[m.model_id] && (
                    <div className={`mt-2 text-xs ${testResults[m.model_id].ok ? "text-emerald-600" : "text-red-600"}`}>
                      {testResults[m.model_id].ok ? "✅ 连接正常" : "❌ 连接失败"}: {testResults[m.model_id].detail}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
      </section>

      {/* ── 模型新增/编辑对话框 ── */}
      {modelDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => setModelDialog(null)}>
          <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6" onClick={(e) => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-gray-900 mb-4">
              {modelDialog.mode === "create" ? "新增模型" : `编辑模型 ${modelDialog.model.model_id}`}
            </h2>
            {modelError && <div className="mb-3 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">{modelError}</div>}
            <div className="space-y-3">
              {modelDialog.mode === "create" && (
                <div>
                  <label className="block text-xs font-medium text-gray-600 mb-1">model_id *</label>
                  <input value={modelForm.model_id} onChange={(e) => setModelForm({ ...modelForm, model_id: e.target.value })}
                    placeholder="deepseek-chat" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono" />
                </div>
              )}
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">模型类型 *</label>
                <select value={modelForm.model_type} disabled={modelDialog.mode === "edit"}
                  onChange={(e) => setModelForm({ ...modelForm, model_type: e.target.value })}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm">
                  <option value="llm">生成模型 (llm)</option>
                  <option value="embedding">嵌入模型 (embedding)</option>
                  <option value="reranker">重排模型 (reranker)</option>
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">provider</label>
                <select value={modelForm.provider} onChange={(e) => setModelForm({ ...modelForm, provider: e.target.value })}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm">
                  {PROVIDER_OPTIONS.map(p => <option key={p} value={p}>{p}</option>)}
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">模型名 *</label>
                <input value={modelForm.model_name} onChange={(e) => setModelForm({ ...modelForm, model_name: e.target.value })}
                  placeholder="BAAI/bge-m3" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">base_url</label>
                <input value={modelForm.base_url} onChange={(e) => setModelForm({ ...modelForm, base_url: e.target.value })}
                  placeholder="https://api.deepseek.com/v1（空=类型默认）" className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">api_key（留空 = 回落 env 秘钥）</label>
                <input type="password" value={modelForm.api_key} onChange={(e) => setModelForm({ ...modelForm, api_key: e.target.value })}
                  placeholder={modelDialog.mode === "edit" ? "留空则不修改" : "留空则用 .env 兜底"}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm" />
                <p className="text-xs text-gray-400 mt-1">秘钥不回显。DB 有 key 时优先；为空时回落对应类型 env 秘钥。</p>
              </div>
              <label className="flex items-center gap-2 text-sm text-gray-600">
                <input type="checkbox" checked={modelForm.is_default}
                  onChange={(e) => setModelForm({ ...modelForm, is_default: e.target.checked })} />
                设为该类型默认模型
              </label>
            </div>
            <div className="flex justify-end gap-3 mt-6">
              <button onClick={() => setModelDialog(null)} className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg">取消</button>
              <button onClick={handleModelSubmit} disabled={modelSaving}
                className="px-4 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50">
                {modelSaving ? "保存中…" : "保存"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Retrieval Config ── */}
      {selectedKB&&config&&<section className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
        <h2 className="text-lg font-semibold mb-4">📊 检索参数 (KB: {selectedKB.slice(0,8)}...)</h2>
        <div className="space-y-4">
          {[["top_k","Top-K (返回文档数)",3,50,1],["oversample_factor","过采样系数",1.0,3.0,0.1],["min_results","最小结果数",1,20,1],["refetch_max_rounds","最大补检索轮数",0,5,1]].map(([key,label,min,max,step])=>(
            <div key={key as string}>
              <label className="text-sm text-gray-600 mb-1 block">{label as string}: <span className="font-bold text-gray-800">{(config as any)[key]}</span></label>
              <input type="range" min={min as number} max={max as number} step={step as number} value={(config as any)[key]} onChange={e=>saveConfig({[key]:parseFloat(e.target.value)})}
                className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-blue-600"/>
            </div>
          ))}
          {/* ── P1-4: 检索策略选择 ── */}
          <div className="pt-2 border-t border-gray-100">
            <label className="text-sm text-gray-600 mb-1 block">检索模式</label>
            <select value={config.retrieval_mode || "hybrid"}
              onChange={(e) => saveConfig({ retrieval_mode: e.target.value })}
              className="px-3 py-1.5 border border-gray-300 rounded-lg text-sm bg-white outline-none focus:ring-2 focus:ring-blue-500 w-full">
              <option value="hybrid">🔀 混合检索 (Hybrid)</option>
              <option value="vector_only">🧬 仅稠密向量 (Vector Only)</option>
              <option value="keyword_only">🔤 仅关键词 (Keyword Only)</option>
            </select>
          </div>
          <div>
            <label className="text-sm text-gray-600 mb-1 block">融合方式</label>
            <select value={config.fusion_method || "rrf"}
              onChange={(e) => saveConfig({ fusion_method: e.target.value })}
              className="px-3 py-1.5 border border-gray-300 rounded-lg text-sm bg-white outline-none focus:ring-2 focus:ring-blue-500 w-full">
              <option value="rrf">📊 RRF (倒数排名融合)</option>
              <option value="weighted_sum">⚖️ Weighted Sum (加权求和)</option>
            </select>
          </div>
          <div>
            <label className="text-sm text-gray-600 mb-1 block">生成合成模式</label>
            <select value={config.synthesis_mode || "auto"}
              onChange={(e) => saveConfig({ synthesis_mode: e.target.value })}
              className="px-3 py-1.5 border border-gray-300 rounded-lg text-sm bg-white outline-none focus:ring-2 focus:ring-blue-500 w-full">
              <option value="auto">🤖 自动选择（按文档数量）</option>
              <option value="compact">📝 Compact (单次生成)</option>
              <option value="refine">🔄 Refine (逐文档迭代)</option>
              <option value="tree_summarize">🌳 Tree Summarize (分组摘要)</option>
              <option value="no_synthesis">📋 No Synthesis (仅返回文档)</option>
            </select>
          </div>
          {/* Weight slider — only meaningful for weighted_sum */}
          {config.fusion_method === "weighted_sum" && (
            <div>
              <label className="text-sm text-gray-600 mb-1 block">
                稀疏权重 (Weighted Sum): <span className="font-bold text-gray-800">{((config.sparse_weight ?? 0.5) * 100).toFixed(0)}%</span>
                &nbsp;|&nbsp; 稠密: {(((1 - (config.sparse_weight ?? 0.5))) * 100).toFixed(0)}%
              </label>
              <div className="flex items-center gap-2 text-xs text-gray-400">
                <span>纯稠密</span>
                <input type="range" min={0} max={1} step={0.05} value={config.sparse_weight ?? 0.5}
                  onChange={e => saveConfig({ sparse_weight: parseFloat(e.target.value), dense_weight: Math.round((1 - parseFloat(e.target.value)) * 100) / 100 })}
                  className="flex-1 h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-purple-500"/>
                <span>纯稀疏</span>
              </div>
            </div>
          )}
          <div>
            <label className="text-sm text-gray-600 mb-1 block">重排序模型</label>
            <select value={config.rerank_model_id || ""}
              onChange={(e) => saveConfig({ rerank_model_id: e.target.value })}
              className="px-3 py-1.5 border border-gray-300 rounded-lg text-sm bg-white outline-none focus:ring-2 focus:ring-blue-500 w-full">
              <option value="">🚀 默认 (BGE-Reranker-v2-m3)</option>
              {models.filter(m => m.model_type === "reranker").map(m => (
                <option key={m.model_id} value={m.model_id}>{m.model_name} ({m.provider})</option>
              ))}
            </select>
          </div>
          <div className="flex items-center justify-between pt-2 border-t border-gray-100">
            <span className="text-sm text-gray-600">Strict 模式 (实时权限复核)</span>
            <button onClick={()=>saveConfig({strict:!config.strict})} className={`px-3 py-1 rounded-full text-xs font-medium transition ${config.strict?"bg-green-100 text-green-700":"bg-gray-200 text-gray-500"}`}>
              {config.strict?"🟢 开启":"⚪ 关闭"}
            </button>
          </div>
          <div>
            <label className="text-sm text-gray-600 mb-1 block">Min Score (rerank 得分阈值): <span className="font-bold text-gray-800">{config.min_score ?? 0.0}</span></label>
            <input type="range" min={0} max={1} step={0.05} value={config.min_score ?? 0.0}
              onChange={e=>saveConfig({min_score:parseFloat(e.target.value)})}
              className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-rose-500"/>
            <p className="text-[10px] text-gray-400 mt-0.5">低于该 rerank 得分的检索片段会被丢弃（0 = 不过滤）</p>
          </div>
          {/* ── 合成参数 ── */}
          <div className="pt-3 border-t border-gray-100">
            <p className="text-sm font-medium text-gray-700 mb-2">🎯 合成参数（Refine / Tree Summarize / Compact 共用）</p>
            {[["refine_batch_size","Refine 每批 Chunk 数",1,5,1],["tree_summarize_batch_size","Tree Summarize 每批数",2,10,1],["doc_preview_max_chars","Chunk 截断长度(通用)",500,3000,100],["max_answer_length","Refine 答案压缩阈值",1000,5000,500],["compress_target_length","Refine 压缩目标长度",300,2000,100]].map(([key,label,min,max,step])=>(
              <div key={key as string} className="flex items-center justify-between py-1">
                <span className="text-xs text-gray-500">{label as string}: <b>{(config as any)[key] ?? {refine_batch_size:2,doc_preview_max_chars:1000,max_answer_length:3000,compress_target_length:1000}[key as string]}</b></span>
                <input type="range" min={min as number} max={max as number} step={step as number}
                  value={(config as any)[key] ?? {refine_batch_size:2,doc_preview_max_chars:1000,max_answer_length:3000,compress_target_length:1000}[key as string]}
                  onChange={e => saveConfig({[key]:parseFloat(e.target.value)})}
                  className="w-32" />
              </div>
            ))}
          </div>
        </div>
        {saved&&<p className="text-xs text-green-600 mt-2">✅ 已保存</p>}
      </section>}

      {/* ── Chunking Config ── */}
      {selectedKB&&chunkCfg&&<section className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
        <h2 className="text-lg font-semibold mb-4">✂️ 切分配置 (KB: {selectedKB.slice(0,8)}...)</h2>
        <div className="space-y-4">
          <div>
            <label className="text-sm text-gray-600 mb-1 block">切分策略</label>
            <select value={chunkCfg.haystack_strategy}
              onChange={(e) => saveChunkConfig({ haystack_strategy: e.target.value })}
              className="px-3 py-1.5 border border-gray-300 rounded-lg text-sm bg-white outline-none focus:ring-2 focus:ring-blue-500">
              <option value="sentence">按句子 (sentence)</option>
              <option value="word">按词 (word)</option>
              <option value="passage">按段落 (passage)</option>
              <option value="semantic">语义切分 (semantic)</option>
              <option value="hierarchical">层级切分 (hierarchical)</option>
            </select>
            <span className="ml-2 text-xs text-gray-400">当前版本: {chunkCfg.version}</span>
          </div>
          {[["split_length","切分长度 (字符数)",128,1024,64],["split_overlap","重叠长度 (字符数)",0,256,16]].map(([key,label,min,max,step])=>(
            <div key={key as string}>
              <label className="text-sm text-gray-600 mb-1 block">{label as string}: <span className="font-bold text-gray-800">{(chunkCfg as any)[key]}</span></label>
              <input type="range" min={min as number} max={max as number} step={step as number} value={(chunkCfg as any)[key]}
                onChange={e=>saveChunkConfig({[key]:parseInt(e.target.value)})}
                className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-orange-500"/>
            </div>
          ))}

          {/* P1-7: Advanced params for semantic strategy */}
          {chunkCfg.haystack_strategy === "semantic" && (
            <div className="pt-2 border-t border-gray-100 space-y-3">
              <p className="text-xs text-gray-400">📐 语义切分高级参数</p>
              <div>
                <label className="text-sm text-gray-600 mb-1 block">相似度断点阈值百分位: <span className="font-bold text-gray-800">{chunkCfg.advanced_params?.breakpoint_threshold_percentile ?? 50}</span></label>
                <input type="range" min={10} max={90} step={5}
                  value={chunkCfg.advanced_params?.breakpoint_threshold_percentile ?? 50}
                  onChange={e=>saveChunkConfig({advanced_params:{...chunkCfg.advanced_params,breakpoint_threshold_percentile:parseInt(e.target.value)}})}
                  className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-purple-500"/>
              </div>
              <div>
                <label className="text-sm text-gray-600 mb-1 block">缓冲句子数: <span className="font-bold text-gray-800">{chunkCfg.advanced_params?.buffer_size ?? 1}</span></label>
                <input type="range" min={1} max={10} step={1}
                  value={chunkCfg.advanced_params?.buffer_size ?? 1}
                  onChange={e=>saveChunkConfig({advanced_params:{...chunkCfg.advanced_params,buffer_size:parseInt(e.target.value)}})}
                  className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-purple-500"/>
              </div>
            </div>
          )}

          {/* P1-7: Advanced params for hierarchical strategy */}
          {chunkCfg.haystack_strategy === "hierarchical" && (
            <div className="pt-2 border-t border-gray-100 space-y-3">
              <p className="text-xs text-gray-400">🏗️ 层级切分高级参数</p>
              <div>
                <label className="text-sm text-gray-600 mb-1 block">父块长度: <span className="font-bold text-gray-800">{chunkCfg.advanced_params?.parent_split_length ?? 1024}</span></label>
                <input type="range" min={512} max={4096} step={128}
                  value={chunkCfg.advanced_params?.parent_split_length ?? 1024}
                  onChange={e=>saveChunkConfig({advanced_params:{...chunkCfg.advanced_params,parent_split_length:parseInt(e.target.value)}})}
                  className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-teal-500"/>
              </div>
              <div>
                <label className="text-sm text-gray-600 mb-1 block">子块长度: <span className="font-bold text-gray-800">{chunkCfg.advanced_params?.child_split_length ?? 256}</span></label>
                <input type="range" min={64} max={1024} step={64}
                  value={chunkCfg.advanced_params?.child_split_length ?? 256}
                  onChange={e=>saveChunkConfig({advanced_params:{...chunkCfg.advanced_params,child_split_length:parseInt(e.target.value)}})}
                  className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-teal-500"/>
              </div>
            </div>
          )}
        </div>
        {chunkSaved&&<p className="text-xs text-green-600 mt-2">✅ 已保存</p>}
        <p className="text-xs text-gray-400 mt-3">切分配置变更后需重新解析已有文档才会生效</p>
      </section>}

      {/* ── Prompts ── */}
      <section className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
        <h2 className="text-lg font-semibold mb-4">📝 Prompt 模板</h2>

        {/* Prompt group selector + active indicator */}
        {promptGroups.length === 0 ? (
          <p className="text-sm text-gray-400">暂无 Prompt 模板</p>
        ) : (
          <div className="space-y-4">
            {promptGroups.map(([groupId, versions]) => {
              const activeVersion = versions.find(v => v.is_active);
              const isExpanded = expandedPrompt === groupId;

              return (
                <div key={groupId} className={`rounded-lg border transition ${activeVersion ? "border-green-300 bg-green-50/50" : "border-gray-200"}`}>
                  {/* Header: group name + active badge + expand toggle */}
                  <div
                    className="flex items-center justify-between px-4 py-3 cursor-pointer hover:bg-gray-50/50 transition"
                    onClick={() => setExpandedPrompt(isExpanded ? null : groupId)}
                  >
                    <div className="flex items-center gap-3">
                      <svg className={`w-4 h-4 transition ${isExpanded ? "rotate-90" : ""} text-gray-400`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                      </svg>
                      <div>
                        <span className="font-medium text-sm text-gray-800">{groupId}</span>
                        {versions.length > 1 && (
                          <span className="ml-1.5 text-xs text-gray-400">{versions.length} 个版本</span>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {activeVersion ? (
                        <span className="text-xs text-green-600 font-medium flex items-center gap-1">
                          ✅ v{activeVersion.version}
                        </span>
                      ) : (
                        <span className="text-xs text-gray-400">未激活</span>
                      )}
                    </div>
                  </div>

                  {/* Expanded: version list */}
                  {isExpanded && (
                    <div className="border-t border-gray-100 px-4 py-3 bg-white rounded-b-lg">
                      <div className="space-y-2">
                        {versions.sort((a,b) => b.version.localeCompare(a.version)).map(v => (
                          <Fragment key={v.id}>
                            <div className={`flex items-center justify-between px-3 py-2 rounded-lg text-sm transition ${
                              v.is_active ? "bg-green-50 border border-green-200" : "bg-gray-50 border border-gray-100 hover:bg-gray-100"
                            }`}>
                              <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2">
                                  <span className="font-medium text-gray-700">v{v.version}</span>
                                  {v.description && <span className="text-xs text-gray-400 truncate">— {v.description.slice(0, 60)}</span>}
                                </div>
                                <p className="text-xs text-gray-500 mt-0.5 line-clamp-1 font-mono">{v.template_text.slice(0, 100)}</p>
                              </div>
                              <div className="shrink-0 ml-3 flex items-center gap-2">
                                <button
                                  onClick={(e) => { e.stopPropagation(); startEditPrompt(v); }}
                                  className="text-xs bg-gray-200 text-gray-700 px-2.5 py-1 rounded-full hover:bg-gray-300 font-medium transition">
                                  ✏️ 编辑
                                </button>
                                {v.is_active ? (
                                  <span className="text-xs text-green-600 font-medium bg-green-100 px-2 py-0.5 rounded-full">✓ 激活</span>
                                ) : (
                                  <button
                                    onClick={(e) => { e.stopPropagation(); handleActivatePrompt(v.id); }}
                                    className="text-xs bg-blue-600 text-white px-2.5 py-1 rounded-full hover:bg-blue-700 font-medium transition">
                                    激活此版本
                                  </button>
                                )}
                              </div>
                            </div>
                            {/* 内联编辑面板 */}
                            {editingPromptId === v.id && (
                              <div className="mt-2 p-3 bg-white border border-blue-200 rounded-lg space-y-2">
                                <div>
                                  <span className="text-xs text-gray-400 font-medium">模板内容（Jinja2）</span>
                                  <textarea
                                    value={editText}
                                    onChange={(e) => setEditText(e.target.value)}
                                    rows={6}
                                    className="mt-1 w-full p-2 border border-gray-200 rounded-lg text-xs font-mono leading-relaxed focus:outline-none focus:ring-1 focus:ring-blue-400"
                                  />
                                </div>
                                <div>
                                  <span className="text-xs text-gray-400 font-medium">描述（可选）</span>
                                  <input
                                    value={editDesc}
                                    onChange={(e) => setEditDesc(e.target.value)}
                                    className="mt-1 w-full p-2 border border-gray-200 rounded-lg text-xs focus:outline-none focus:ring-1 focus:ring-blue-400"
                                  />
                                </div>
                                {promptError && <p className="text-xs text-red-600">{promptError}</p>}
                                <div className="flex justify-end gap-2">
                                  <button
                                    onClick={cancelEditPrompt}
                                    disabled={savingPrompt}
                                    className="text-xs bg-gray-100 text-gray-600 px-3 py-1.5 rounded-lg hover:bg-gray-200 font-medium">
                                    取消
                                  </button>
                                  <button
                                    onClick={handleUpdatePrompt}
                                    disabled={savingPrompt || !editText.trim()}
                                    className="text-xs bg-blue-600 text-white px-3 py-1.5 rounded-lg hover:bg-blue-700 font-medium disabled:opacity-50">
                                    {savingPrompt ? "保存中…" : "保存修改"}
                                  </button>
                                </div>
                              </div>
                            )}
                          </Fragment>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* ── Preview Panel (#28) ── */}
        {selectedPrompt && (
          <div className="mt-5 pt-4 border-t border-gray-100">
            <h3 className="text-sm font-semibold text-gray-600 mb-3 flex items-center gap-2">
              👁 预览
              <span className="text-xs text-gray-400 font-normal">
                (当前激活: {selectedPrompt.prompt_id} v{selectedPrompt.version})
              </span>
            </h3>
            <div className="space-y-3">
              {/* Raw template */}
              <div>
                <span className="text-xs text-gray-400 font-medium">原始模板</span>
                <pre className="mt-1 p-3 bg-gray-50 border border-gray-200 rounded-lg text-xs text-gray-600 font-mono max-h-32 overflow-y-auto whitespace-pre-wrap leading-relaxed">
                  {selectedPrompt.template_text}
                </pre>
              </div>
              {/* Rendered preview */}
              <div>
                <span className="text-xs text-gray-400 font-medium">渲染预览</span>
                <pre className="mt-1 p-3 bg-blue-50 border border-blue-200 rounded-lg text-xs text-gray-800 font-mono max-h-48 overflow-y-auto whitespace-pre-wrap leading-relaxed">
                  {renderPreview(selectedPrompt.template_text)}
                </pre>
              </div>
            </div>
            <p className="text-[10px] text-gray-400 mt-2">
              ⚡ 预览仅作参考 — 实际渲染时变量值由检索结果和会话上下文决定。{'\n'}
              <code className="bg-gray-100 px-1 rounded">{"{{ query }}"}</code> <code className="bg-gray-100 px-1 rounded">{"{{ documents }}"}</code> <code className="bg-gray-100 px-1 rounded">{"{{ context }}"}</code> 等变量已在后端模板引擎中定义。
            </p>
          </div>
        )}
      </section>

      {/* ── External tools ── */}
      <section className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
        <h2 className="text-lg font-semibold mb-4">🔗 外部工具</h2>

        {/* Permissions management — dedicated card, always visible */}
        <div className="mb-4 p-4 bg-purple-50 border border-purple-200 rounded-lg">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h3 className="font-semibold text-gray-800 flex items-center gap-2">
                🔐 权限管理
              </h3>
              <p className="text-sm text-gray-600 mt-1">
                文档授权、角色绑定、访问控制请在管理台操作。
              </p>
              <p className="text-xs text-gray-400 mt-1">
                本系统不内置权限判定 — 全部授权决策由外部权限服务完成。
              </p>
            </div>
            {appCfg?.admin_console_url ? (
              <a
                href={appCfg.admin_console_url}
                target="_blank"
                rel="noreferrer"
                className="shrink-0 px-4 py-2 bg-purple-600 text-white text-sm rounded-lg hover:bg-purple-700 transition font-medium"
              >
                前往管理台 →
              </a>
            ) : (
              <span
                className="shrink-0 px-4 py-2 bg-gray-300 text-gray-500 text-sm rounded-lg cursor-not-allowed font-medium"
                title="未配置 ADMIN_CONSOLE_URL 环境变量"
              >
                管理台未配置
              </span>
            )}
          </div>
        </div>

        <div className="grid grid-cols-3 gap-4 text-sm">
          {appCfg?.grafana_url ? (
            <a href={appCfg.grafana_url} target="_blank" rel="noreferrer" className="p-3 border border-gray-200 rounded-lg hover:border-orange-300 hover:bg-orange-50 transition">
              <div className="font-medium text-gray-700">📊 Grafana</div>
              <div className="text-xs text-gray-400 mt-1">Trace / Log / Metric</div>
            </a>
          ) : (
            <div className="p-3 border border-gray-100 rounded-lg bg-gray-50 cursor-not-allowed">
              <div className="font-medium text-gray-400">📊 Grafana</div>
              <div className="text-xs text-gray-300 mt-1">未配置</div>
            </div>
          )}
          {appCfg?.langfuse_url ? (
            <a href={appCfg.langfuse_url} target="_blank" rel="noreferrer" className="p-3 border border-gray-200 rounded-lg hover:border-green-300 hover:bg-green-50 transition">
              <div className="font-medium text-gray-700">🔍 Langfuse</div>
              <div className="text-xs text-gray-400 mt-1">LLM 调用链路与成本</div>
            </a>
          ) : (
            <div className="p-3 border border-gray-100 rounded-lg bg-gray-50 cursor-not-allowed">
              <div className="font-medium text-gray-400">🔍 Langfuse</div>
              <div className="text-xs text-gray-300 mt-1">未配置</div>
            </div>
          )}
          {appCfg?.admin_console_url ? (
            <a href={appCfg.admin_console_url} target="_blank" rel="noreferrer" className="p-3 border border-gray-200 rounded-lg hover:border-purple-300 hover:bg-purple-50 transition">
              <div className="font-medium text-gray-700">🔗 管理台</div>
              <div className="text-xs text-gray-400 mt-1">权限与用户管理</div>
            </a>
          ) : (
            <div className="p-3 border border-gray-100 rounded-lg bg-gray-50 cursor-not-allowed">
              <div className="font-medium text-gray-400">🔗 管理台</div>
              <div className="text-xs text-gray-300 mt-1">未配置</div>
            </div>
          )}
        </div>
      </section>
    </main>
  </div>;
}
