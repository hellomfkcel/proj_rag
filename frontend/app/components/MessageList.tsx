"use client";
// Message List — user + assistant messages with copy buttons, source chips, error states

import { useEffect, useRef, useState } from "react";
import SourcesCard from "./SourcesCard";

interface Source {
  chunk_id: string;
  doc_name?: string;
  content?: string;
}
interface Message {
  role: "user" | "assistant";
  content: string;
  thinking?: string;
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
  showThinking?: boolean;
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

export default function MessageList({ messages, msgEndRef, streamError, showThinking = true }: Props) {
  const [copiedId, setCopiedId] = useState<number | null>(null);

  // ── 流式滚动：主容器 ref + 贴底跟踪 ──
  const scrollRef = useRef<HTMLDivElement>(null);
  const thinkingRef = useRef<HTMLParagraphElement>(null);
  const [stickToBottom, setStickToBottom] = useState(true);

  // 用户滚动时检测是否贴近底部：贴底则跟随流式内容，向上阅读则让用户自由滚动不抢夺
  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    setStickToBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
  };

  // 思考过程内部自动滚动到最新（max-h-40 溢出时跟随最新内容）
  useEffect(() => {
    const el = thinkingRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages[messages.length - 1]?.thinking]);

  // 流式时跟随最新内容：rAF 每帧合并一次 + 瞬时 scrollTop，
  // 避免原 scrollIntoView({behavior:"smooth"}) 高频动画队列堆积卡顿。
  // rAF 保证最后一次更新也会执行（无节流漏尾）。
  useEffect(() => {
    if (!stickToBottom) return;
    const el = scrollRef.current;
    if (!el) return;
    const raf = requestAnimationFrame(() => {
      el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(raf);
  }, [messages, stickToBottom]);

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
    <div ref={scrollRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
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

              {/* 思考过程（推理流，可折叠；showThinking=false 时不展示） */}
              {showThinking && msg.thinking && (
                <details open={!!msg.streaming} className="mb-2 text-xs text-gray-500 border border-gray-200 rounded-md px-2.5 py-1.5 bg-white/50">
                  <summary className="cursor-pointer select-none">
                    <span className="inline-flex items-center gap-1">🧠 思考过程</span>
                  </summary>
                  <p
                    ref={i === messages.length - 1 ? thinkingRef : undefined}
                    className="whitespace-pre-wrap leading-relaxed mt-1 max-h-40 overflow-y-auto"
                  >{msg.thinking}</p>
                </details>
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

              {/* Sources — 紧凑来源标签（wrap 两行内），点击看原文详情 */}
              {msg.sources && msg.sources.length > 0 && (
                <div className="mt-2 pt-2 border-t border-gray-200">
                  <p className="text-xs text-gray-400 mb-1.5">
                    📎 引用来源（{msg.sources.length}）
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
