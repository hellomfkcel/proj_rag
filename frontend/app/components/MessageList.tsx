"use client";
// Message List — user + assistant messages with copy buttons, source chips, error states

import { useState } from "react";
import SourcesCard from "./SourcesCard";

interface Source {
  chunk_id: string;
  doc_name?: string;
  content?: string;
}
interface Message {
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  streaming?: boolean;
  errorCode?: string;
  trace_id?: string;       // 查询链路 OTel trace_id
  trace_ui_url?: string;   // Grafana Tempo 查看链路深链（仅本次 query 有）
}

interface Props {
  messages: Message[];
  msgEndRef: React.RefObject<HTMLDivElement>;
  streamError?: string | null;
}

function CopyIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

export default function MessageList({ messages, msgEndRef, streamError }: Props) {
  const [copiedId, setCopiedId] = useState<number | null>(null);

  // 兼容非安全上下文（http://ip，非 localhost）：navigator.clipboard 不可用时回退 execCommand
  const copyToClipboard = async (text: string): Promise<boolean> => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch {
      // 回退到 execCommand
    }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  };

  const copyContent = async (id: number, text: string) => {
    const ok = await copyToClipboard(text);
    if (ok) {
      setCopiedId(id);
      setTimeout(() => setCopiedId(null), 1500);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
      {messages.length === 0 && (
        <div className="flex items-center justify-center h-full text-gray-400">
          <div className="text-center">
            <div className="text-4xl mb-3">💬</div>
            <p className="text-lg">开始一个新会话</p>
            <p className="text-sm mt-1">选择知识库，输入你的问题</p>
          </div>
        </div>
      )}
      {messages.map((msg, i) =>
        msg.role === "user" ? (
          /* ── 用户消息：贴合内容的文字框（最长 60% 白板宽）+ 框下右侧复制图标（hover 显示） ── */
          <div key={i} className="flex justify-end">
            {/* max-w 放在外层，相对 conversation 白板宽度；短内容贴合、超 60% 才换行 */}
            <div className="group relative max-w-[60%]">
              <div className="rounded-md bg-blue-600 text-white px-3 py-2">
                <p className="text-sm whitespace-pre-wrap leading-relaxed">{msg.content}</p>
              </div>
              {msg.content && (
                <button
                  type="button"
                  onClick={() => copyContent(i, msg.content)}
                  title="复制内容"
                  className="absolute top-full mt-1.5 right-0 rounded-md bg-white p-1.5 text-gray-500 shadow-md ring-1 ring-gray-200 hover:text-gray-800 hover:bg-gray-50 active:scale-90 transition-all duration-150 opacity-0 group-hover:opacity-100"
                >
                  {copiedId === i ? <CheckIcon /> : <CopyIcon />}
                </button>
              )}
            </div>
          </div>
        ) : (
          /* ── 助手消息 ── */
          <div key={i} className="flex justify-start">
            <div className={`max-w-[80%] rounded-2xl px-4 py-3 ${
              msg.errorCode ? "bg-red-50 text-red-800 border border-red-200" : "bg-gray-100 text-gray-800"
            }`}>
              {/* Error badge */}
              {msg.errorCode && (
                <div className="flex items-center gap-1.5 mb-2 text-xs text-red-600">
                  <span>⚠️</span>
                  <span>{msg.errorCode === "retrieve:insufficient_evidence" ? "未找到足够信息" : "生成出错"}</span>
                </div>
              )}

              {/* Content */}
              {msg.content ? (
                <p className="text-sm whitespace-pre-wrap leading-relaxed">
                  {msg.content}
                  {msg.streaming && (
                    <span className="inline-block w-1.5 h-4 bg-blue-500 ml-1 animate-pulse rounded-sm align-middle" />
                  )}
                </p>
              ) : msg.streaming ? (
                <div className="flex items-center gap-1.5 py-1">
                  <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                  <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                  <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                </div>
              ) : null}

              {/* Sources — 标注化来源 chips（文档名，点击弹窗看详情） */}
              {msg.sources && msg.sources.length > 0 && (
                <div className="mt-3 pt-2 border-t border-gray-200">
                  <p className="text-xs text-gray-400 mb-1.5">
                    📎 {msg.sources.filter(s => s.content).length > 0 ? `${msg.sources.filter(s => s.content).length} 个引用来源` : `${msg.sources.length} 个来源`}
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {msg.sources.map((s, j) => (
                      <SourcesCard key={j} index={j} source={s} />
                    ))}
                  </div>
                </div>
              )}

              {/* Trace 链路（可复制 trace_id + 跳转 Grafana Tempo） */}
              {msg.trace_id && (
                <div className="mt-3 pt-2 border-t border-gray-200 flex items-center gap-2 text-xs text-gray-400">
                  <span title={msg.trace_id}>🛰️ {msg.trace_id.slice(0, 12)}</span>
                  <button
                    type="button"
                    onClick={() => copyToClipboard(msg.trace_id!)}
                    className="hover:text-gray-600 underline"
                  >
                    复制
                  </button>
                  {msg.trace_ui_url && (
                    <a href={msg.trace_ui_url} target="_blank" rel="noreferrer" className="hover:text-gray-600 underline">
                      查看链路
                    </a>
                  )}
                </div>
              )}

              {/* 复制按钮 — 放在内容下方 */}
              {msg.content && (
                <div className="mt-1.5 flex justify-end">
                  <button
                    type="button"
                    onClick={() => copyContent(i, msg.content)}
                    title="复制内容"
                    className="rounded-md p-1.5 text-gray-400 hover:text-gray-700 hover:bg-gray-200 active:scale-90 transition-all duration-150"
                  >
                    {copiedId === i ? <CheckIcon /> : <CopyIcon />}
                  </button>
                </div>
              )}
            </div>
          </div>
        )
      )}
      <div ref={msgEndRef} />
    </div>
  );
}
