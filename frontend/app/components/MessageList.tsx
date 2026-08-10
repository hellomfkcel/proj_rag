"use client";
// Message List — user + assistant messages with source cards, error states

import SourcesCard from "./SourcesCard";

interface Source {
  chunk_id: string;
  content: string;
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

export default function MessageList({ messages, msgEndRef, streamError }: Props) {
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
      {messages.map((msg, i) => (
        <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
          <div
            className={`max-w-[80%] rounded-2xl px-4 py-3 ${
              msg.role === "user"
                ? "bg-blue-600 text-white"
                : msg.errorCode
                  ? "bg-red-50 text-red-800 border border-red-200"
                  : "bg-gray-100 text-gray-800"
            }`}
          >
            {msg.role === "user" ? (
              <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
            ) : (
              <div>
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

                {/* Sources */}
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
                      onClick={() => navigator.clipboard?.writeText(msg.trace_id!)}
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
              </div>
            )}
          </div>
        </div>
      ))}
      <div ref={msgEndRef} />
    </div>
  );
}
