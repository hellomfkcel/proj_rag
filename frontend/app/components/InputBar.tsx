"use client";
// Chat Input Bar — textarea with Shift+Enter for newline, Enter to send
// P1-5: Advanced Options panel for per-query retrieval parameter overrides
// P2: 检索参数持久化到 localStorage（按 KB 隔离），刷新页面后保持用户最后设置的值

import { useState, useEffect, useCallback } from "react";
import { getRetrievalConfig } from "@/lib/settings";

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
}

interface Props {
  input: string;
  onInputChange: (v: string) => void;
  onSend: (overrides?: QueryOverrides) => void;
  disabled: boolean;
  kbName: string;
  kbId: string;
}

// localStorage key prefix for per-KB retrieval param persistence
const PARAM_STORAGE_PREFIX = "rag_retrieval_params_";

// 默认值（最低优先级）
const DEFAULTS = {
  retrievalMode: "hybrid",
  fusionMethod: "rrf",
  strict: false,
  topK: 10,
  sparseWeight: 0.5,
  synthesisMode: "auto",
  oversampleFactor: 1.5,
  minResults: 3,
  refetchMaxRounds: 2,
};

/** 从 localStorage 读取指定 KB 的最后使用参数 */
function loadParams(kbId: string): Partial<typeof DEFAULTS> | null {
  if (typeof window === "undefined" || !kbId) return null;
  try {
    const raw = localStorage.getItem(PARAM_STORAGE_PREFIX + kbId);
    if (raw) return JSON.parse(raw);
  } catch {}
  return null;
}

/** 保存参数到 localStorage（按 KB 隔离） */
function saveParams(kbId: string, params: Record<string, unknown>) {
  if (typeof window === "undefined" || !kbId) return;
  try {
    localStorage.setItem(PARAM_STORAGE_PREFIX + kbId, JSON.stringify(params));
  } catch {}
}

export default function InputBar({ input, onInputChange, onSend, disabled, kbName, kbId }: Props) {
  const canSend = !disabled && input.trim().length > 0 && kbName !== "请选择知识库";
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [retrievalMode, setRetrievalMode] = useState(DEFAULTS.retrievalMode);
  const [fusionMethod, setFusionMethod] = useState(DEFAULTS.fusionMethod);
  const [strict, setStrict] = useState(DEFAULTS.strict);
  const [topK, setTopK] = useState(DEFAULTS.topK);
  const [sparseWeight, setSparseWeight] = useState(DEFAULTS.sparseWeight);
  const [synthesisMode, setSynthesisMode] = useState(DEFAULTS.synthesisMode);
  const [oversampleFactor, setOversampleFactor] = useState(DEFAULTS.oversampleFactor);
  const [minResults, setMinResults] = useState(DEFAULTS.minResults);
  const [refetchMaxRounds, setRefetchMaxRounds] = useState(DEFAULTS.refetchMaxRounds);

  // 加载参数：优先级 localStorage > DB 设置页配置 > 硬编码默认值
  useEffect(() => {
    if (!kbId) return;

    // Step 1: 检查 localStorage（用户上次手动设置的值，最高优先级）
    const saved = loadParams(kbId);

    // Step 2: 从 DB 获取设置页配置（管理员配置的默认值）
    getRetrievalConfig(kbId).then(cfg => {
      // localStorage 中有的值优先使用（用户覆盖优先于 DB 配置）
      if (saved?.retrievalMode !== undefined) setRetrievalMode(saved.retrievalMode);
      else if (cfg.retrieval_mode) setRetrievalMode(cfg.retrieval_mode);

      if (saved?.fusionMethod !== undefined) setFusionMethod(saved.fusionMethod);
      else if (cfg.fusion_method) setFusionMethod(cfg.fusion_method);

      if (saved?.synthesisMode !== undefined) setSynthesisMode(saved.synthesisMode);
      else if (cfg.synthesis_mode) setSynthesisMode(cfg.synthesis_mode);

      if (saved?.topK !== undefined) setTopK(saved.topK);
      else if (cfg.top_k) setTopK(cfg.top_k);

      if (saved?.strict !== undefined) setStrict(saved.strict);
      else if (cfg.strict !== undefined) setStrict(cfg.strict);

      if (saved?.sparseWeight !== undefined) setSparseWeight(saved.sparseWeight);
      else if (cfg.sparse_weight !== undefined) setSparseWeight(cfg.sparse_weight);

      if (saved?.oversampleFactor !== undefined) setOversampleFactor(saved.oversampleFactor);
      else if (cfg.oversample_factor) setOversampleFactor(cfg.oversample_factor);

      if (saved?.minResults !== undefined) setMinResults(saved.minResults);
      else if (cfg.min_results !== undefined) setMinResults(cfg.min_results);

      if (saved?.refetchMaxRounds !== undefined) setRefetchMaxRounds(saved.refetchMaxRounds);
      else if (cfg.refetch_max_rounds !== undefined) setRefetchMaxRounds(cfg.refetch_max_rounds);
    }).catch(() => {
      // DB 不可达时使用 localStorage 值或默认值
      if (saved) {
        if (saved.retrievalMode !== undefined) setRetrievalMode(saved.retrievalMode);
        if (saved.fusionMethod !== undefined) setFusionMethod(saved.fusionMethod);
        if (saved.synthesisMode !== undefined) setSynthesisMode(saved.synthesisMode);
        if (saved.topK !== undefined) setTopK(saved.topK);
        if (saved.strict !== undefined) setStrict(saved.strict);
        if (saved.sparseWeight !== undefined) setSparseWeight(saved.sparseWeight);
        if (saved.oversampleFactor !== undefined) setOversampleFactor(saved.oversampleFactor);
        if (saved.minResults !== undefined) setMinResults(saved.minResults);
        if (saved.refetchMaxRounds !== undefined) setRefetchMaxRounds(saved.refetchMaxRounds);
      }
    });
  }, [kbId]);

  // 每次参数变更时持久化到 localStorage
  const persistAndSet = useCallback(<T,>(setter: (v: T) => void, key: string, value: T) => {
    setter(value);
    if (!kbId) return;
    // 读取当前 localStorage 数据，合并更新后写回
    const current = loadParams(kbId) || {};
    (current as Record<string, unknown>)[key] = value;
    saveParams(kbId, current as Record<string, unknown>);
  }, [kbId]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (canSend) handleSend();
    }
  };

  const handleSend = () => {
    if (!canSend) return;
    if (showAdvanced) {
      onSend({
        retrieval_mode: retrievalMode,
        fusion_method: fusionMethod,
        top_k: topK,
        strict,
        synthesis_mode: synthesisMode,
        dense_weight: Math.round((1 - sparseWeight) * 100) / 100,
        sparse_weight: Math.round(sparseWeight * 100) / 100,
        oversample_factor: oversampleFactor,
        min_results: minResults,
        refetch_max_rounds: refetchMaxRounds,
      });
    } else {
      onSend(undefined);
    }
  };

  return (
    <div className="border-t border-gray-200 px-6 py-3 bg-white">
      {/* Advanced Options */}
      {showAdvanced && (
        <div className="max-w-4xl mx-auto mb-3 p-3 bg-gray-50 rounded-lg border border-gray-200 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <label className="text-xs text-gray-500 block mb-1">检索模式</label>
            <select value={retrievalMode} onChange={(e) => persistAndSet(setRetrievalMode, "retrievalMode", e.target.value)}
              className="w-full px-2 py-1 border border-gray-300 rounded text-xs bg-white outline-none focus:ring-1 focus:ring-blue-500">
              <option value="hybrid">🔀 混合检索</option>
              <option value="vector_only">🧬 仅稠密向量</option>
              <option value="keyword_only">🔤 仅关键词</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">融合方式</label>
            <select value={fusionMethod} onChange={(e) => persistAndSet(setFusionMethod, "fusionMethod", e.target.value)}
              className="w-full px-2 py-1 border border-gray-300 rounded text-xs bg-white outline-none focus:ring-1 focus:ring-blue-500">
              <option value="rrf">📊 RRF</option>
              <option value="weighted_sum">⚖️ Weighted Sum</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">合成模式</label>
            <select value={synthesisMode} onChange={(e) => persistAndSet(setSynthesisMode, "synthesisMode", e.target.value)}
              className="w-full px-2 py-1 border border-gray-300 rounded text-xs bg-white outline-none focus:ring-1 focus:ring-blue-500">
              <option value="auto">🤖 自动选择</option>
              <option value="compact">📝 Compact</option>
              <option value="refine">🔄 Refine</option>
              <option value="tree_summarize">🌳 Tree Summarize</option>
              <option value="no_synthesis">📋 仅返回文档</option>
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">Top-K: <span className="font-bold">{topK}</span></label>
            <input type="range" min={1} max={50} value={topK}
              onChange={(e) => persistAndSet(setTopK, "topK", parseInt(e.target.value))}
              className="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-blue-600" />
          </div>

          <div>
            <label className="text-xs text-gray-500 block mb-1">过采样: <span className="font-bold">{oversampleFactor.toFixed(1)}x</span></label>
            <input type="range" min={1.0} max={3.0} step={0.1} value={oversampleFactor}
              onChange={(e) => persistAndSet(setOversampleFactor, "oversampleFactor", parseFloat(e.target.value))}
              className="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-cyan-500" />
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">最少结果: <span className="font-bold">{minResults}</span></label>
            <input type="range" min={1} max={20} step={1} value={minResults}
              onChange={(e) => persistAndSet(setMinResults, "minResults", parseInt(e.target.value))}
              className="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-cyan-500" />
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">补检索轮数: <span className="font-bold">{refetchMaxRounds}</span></label>
            <input type="range" min={0} max={5} step={1} value={refetchMaxRounds}
              onChange={(e) => persistAndSet(setRefetchMaxRounds, "refetchMaxRounds", parseInt(e.target.value))}
              className="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-cyan-500" />
          </div>

          {/* Weight slider — only when weighted_sum is selected */}
          {fusionMethod === "weighted_sum" && (
            <div className="col-span-2">
              <label className="text-xs text-gray-500 block mb-1">
                稀疏权重: <span className="font-bold">{(sparseWeight * 100).toFixed(0)}%</span>
                &nbsp;&nbsp;|&nbsp;&nbsp;
                稠密权重: <span className="font-bold">{((1 - sparseWeight) * 100).toFixed(0)}%</span>
              </label>
              <div className="flex items-center gap-2 text-[10px] text-gray-400">
                <span>纯稠密</span>
                <input type="range" min={0} max={1} step={0.05} value={sparseWeight}
                  onChange={(e) => persistAndSet(setSparseWeight, "sparseWeight", parseFloat(e.target.value))}
                  className="flex-1 h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-purple-500" />
                <span>纯稀疏</span>
              </div>
            </div>
          )}

          <div className="flex items-end">
            <button onClick={() => persistAndSet(setStrict, "strict", !strict)}
              className={`px-2 py-1 rounded text-xs font-medium transition w-full ${
                strict ? "bg-green-100 text-green-700 border border-green-300" : "bg-gray-100 text-gray-500 border border-gray-200"
              }`}>
              {strict ? "🟢 Strict 复核" : "⚪ Strict 关闭"}
            </button>
          </div>
        </div>
      )}

      {/* Input Row */}
      <div className="flex items-end gap-3 max-w-4xl mx-auto">
        {/* Advanced Toggle */}
        <button
          onClick={() => setShowAdvanced(!showAdvanced)}
          title="高级检索选项"
          className={`px-2 py-2.5 rounded-xl text-sm font-medium transition shrink-0 ${
            showAdvanced ? "bg-blue-100 text-blue-700 border border-blue-300" : "bg-gray-100 text-gray-500 border border-gray-200 hover:bg-gray-200"
          }`}
        >
          ⚙️
        </button>

        <textarea
          value={input}
          onChange={(e) => onInputChange(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={kbName && kbName !== "请选择知识库" ? `向 ${kbName} 提问... (Shift+Enter 换行)` : "请先选择知识库"}
          disabled={disabled || kbName === "请选择知识库"}
          rows={1}
          className="flex-1 px-4 py-2.5 border border-gray-300 rounded-xl outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:bg-gray-100 text-sm transition resize-none"
        />
        <button
          onClick={handleSend}
          disabled={!canSend}
          className="px-5 py-2.5 bg-blue-600 text-white rounded-xl font-medium hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition text-sm shrink-0"
        >
          {disabled ? "..." : "发送"}
        </button>
      </div>
    </div>
  );
}
