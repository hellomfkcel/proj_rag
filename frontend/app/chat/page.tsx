"use client";
// Chat Page — multi-KB queries, conversation list, SSE stream, source cards
// Implements: #23 real chunk content, #24 SSE error handling, #25 conv rename, #26 textarea

import { useEffect, useState, useRef } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/useAuthStore";
import { useKBStore } from "@/stores/useKBStore";
import { useKBSync } from "@/lib/useKBSync";
import Header from "@/app/components/Header";
import ConvList from "@/app/components/ConvList";
import MessageList from "@/app/components/MessageList";
import InputBar from "@/app/components/InputBar";
import { listConversations, createConversation, deleteConversation, renameConversation, getTurns, query as queryAPI } from "@/lib/chat";
import type { Conversation, Turn } from "@/lib/chat";

interface Message {
  role: "user" | "assistant";
  content: string;
  sources?: { chunk_id: string; content: string }[];
  streaming?: boolean;
  errorCode?: string;
  trace_id?: string;
  trace_ui_url?: string;
}

export default function ChatPage() {
  const token = useAuthStore((s) => s.token);
  const router = useRouter();
  const { selectedKBs } = useKBStore();

  useKBSync();

  const [convs, setConvs] = useState<Conversation[]>([]);
  const [activeConvId, setActiveConvId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [retryCountdown, setRetryCountdown] = useState(0);
  const retryTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const latestMessagesRef = useRef<Message[]>([]);
  const msgEndRef = useRef<HTMLDivElement>(null!);

  // Keep ref in sync
  useEffect(() => { latestMessagesRef.current = messages; }, [messages]);

  // Auto-retry countdown for vector_store_unavailable
  useEffect(() => {
    if (retryCountdown > 0) {
      retryTimerRef.current = setInterval(() => {
        setRetryCountdown(prev => {
          if (prev <= 1) {
            // Auto-retry: restore last user question to input, clear error
            const msgs = latestMessagesRef.current;
            const lastUserMsg = [...msgs].reverse().find(m => m.role === "user");
            if (lastUserMsg) {
              setInput(lastUserMsg.content);
              setMessages(prevMsgs => prevMsgs.slice(0, -1));
            }
            setStreamError(null);
            return 0;
          }
          return prev - 1;
        });
      }, 1000);
    } else {
      if (retryTimerRef.current) {
        clearInterval(retryTimerRef.current);
        retryTimerRef.current = null;
      }
    }
    return () => {
      if (retryTimerRef.current) clearInterval(retryTimerRef.current);
    };
  }, [retryCountdown]);

  useEffect(() => {
    if (!token) { router.push("/login"); return; }
    fetchConvs();
  }, [token, router]);

  useEffect(() => { msgEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const fetchConvs = async () => {
    try { setConvs(await listConversations()); } catch {}
  };

  const kbName = selectedKBs.length === 0
    ? "请选择知识库"
    : selectedKBs.length === 1
      ? `已选 1 个知识库`
      : `已选 ${selectedKBs.length} 个知识库（交叉检索）`;

  const handleNewConv = async () => {
    if (selectedKBs.length === 0) { alert("请先在顶部选择知识库"); return; }
    try {
      const conv = await createConversation(selectedKBs);
      setConvs(prev => [conv, ...prev]);
      setActiveConvId(conv.id);
      setMessages([]);
    } catch {}
  };

  const handleDeleteConv = async (id: string) => {
    if (!confirm("删除此会话？")) return;
    await deleteConversation(id);
    if (activeConvId === id) { setActiveConvId(null); setMessages([]); }
    fetchConvs();
  };

  const handleRenameConv = async (id: string, title: string) => {
    await renameConversation(id, title);
    setConvs(prev => prev.map(c => c.id === id ? { ...c, title } : c));
  };

  const handleSelectConv = async (conv: Conversation) => {
    setActiveConvId(conv.id);
    setStreamError(null);
    try {
      const turns = await getTurns(conv.id);
      const msgs: Message[] = [];
      for (const t of turns) {
        msgs.push({ role: "user", content: t.user_question });
        msgs.push({
          role: "assistant",
          content: t.answer || (t.chunk_ids?.length ? "回答已生成（请刷新查看完整内容）" : "正在生成回答..."),
          streaming: !t.answer,
          sources: (t.chunk_ids || []).map((id: string) => ({ chunk_id: id, content: "" })),
          trace_id: t.trace_id || undefined,
        });
      }
      setMessages(msgs);

      // 后台静默轮询未完成的 turn：非阻塞 setInterval，不卡 UI
      const lastTurn = turns[turns.length - 1];
      if (lastTurn && !lastTurn.answer && lastTurn.turn_index) {
        const POLL_MS = 2000, MAX_MS = 60_000;
        const start = Date.now();
        const timer = setInterval(async () => {
          try {
            const fresh = await getTurns(conv.id);
            const t = fresh.find((x: any) => x.turn_index === lastTurn.turn_index);
            if (t?.answer) {
              clearInterval(timer);
              setMessages(prev => {
                const next = [...prev];
                const last = next[next.length - 1];
                if (last?.role === "assistant") {
                  last.content = t.answer;
                  last.streaming = false;
                  last.sources = (t.chunk_ids || []).map((id: string) => ({ chunk_id: id, content: "" }));
                }
                return [...next];
              });
            }
          } catch {}
          if (Date.now() - start > MAX_MS) clearInterval(timer);
        }, POLL_MS);
      }
    } catch {
      setMessages([]);
    }
  };

  const handleSend = async (overrides?: {
    retrieval_mode?: string; fusion_method?: string; strict?: boolean; top_k?: number;
    dense_weight?: number; sparse_weight?: number; synthesis_mode?: string;
    oversample_factor?: number; min_results?: number; refetch_max_rounds?: number;
  }) => {
    const q = input.trim();
    if (!q || selectedKBs.length === 0) return;
    setInput("");
    setStreamError(null);

    const userMsg: Message = { role: "user", content: q };
    setMessages(prev => [...prev, userMsg]);

    const loadingMsg: Message = { role: "assistant", content: "", streaming: true };
    setMessages(prev => [...prev, loadingMsg]);
    setLoading(true);

    let sseAnswer = "";
    let sseSources: { chunk_id: string; content: string }[] = [];

    try {
      // ── Step 1: Fire the query FIRST ──
      // The POST response gives us the canonical conversation_id and turn_index.
      // We need these to subscribe to the correct Redis Pub/Sub channel.
      // (Design doc §3.3: POST → SSE, not SSE → POST.)
      const result = await queryAPI(q, selectedKBs, activeConvId || undefined, overrides);
      const activeConvIdNew = result.conversation_id || activeConvId;
      if (!activeConvId) setActiveConvId(activeConvIdNew);
      const turnIndex = result.turn_index || 1;

      // 在助手消息上挂本次查询的链路 trace（跳 Grafana Tempo 查看）
      if (result.trace_id) {
        setMessages(prev => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.role === "assistant") {
            last.trace_id = result.trace_id;
            last.trace_ui_url = result.trace_ui_url || "";
          }
          return [...next];
        });
      }

      // Handle typed error codes from the query response (#32, #33, #34)
      if (result.error_code) {
        setStreamError(result.error_code);
        if (result.error_code === "retrieve:vector_store_unavailable") {
          setRetryCountdown(10);
        } else if (result.error_code === "retrieve:insufficient_evidence") {
          setStreamError(null);
        }
      }

      // ── Step 2: Open SSE with CORRECT conversation_id + turn_index ──
      // The Celery worker publishes results to Redis Pub/Sub when done.
      // We subscribe via SSE and wait for the "done" event (or 60s timeout).
      const streamUrl = `/api/v1/conversations/${activeConvIdNew}/stream?turn_index=${turnIndex}&token=${encodeURIComponent(token || "")}`;

      // Use a Promise to properly await SSE completion
      await new Promise<void>((resolve) => {
        let resolved = false;
        const finish = () => { if (!resolved) { resolved = true; resolve(); } };

        const evtSource = new EventSource(streamUrl);

        // Timeout fallback — close after 60s
        const streamTimeout = setTimeout(() => {
          evtSource.close();
          finish();
        }, 60_000);

        evtSource.addEventListener("retrieved", (e: MessageEvent) => {
          try {
            const d = JSON.parse(e.data);
            sseSources = (d.chunks || []).map((c: any) => ({
              chunk_id: c.chunk_id || c.id || "",
              content: c.content || "",
            }));
            setMessages(prev => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant") {
                last.sources = sseSources.length > 0
                  ? sseSources
                  : (d.chunk_ids || []).map((id: string) => ({ chunk_id: id, content: "" }));
              }
              return [...next];
            });
          } catch {}
        });

        evtSource.addEventListener("token", (e: MessageEvent) => {
          try {
            const d = JSON.parse(e.data);
            sseAnswer += d.content || "";
            setMessages(prev => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant") { last.content = sseAnswer; last.streaming = true; }
              return [...next];
            });
          } catch {}
        });

        evtSource.addEventListener("error", (e: MessageEvent) => {
          try {
            const d = JSON.parse((e as any).data || "{}");
            const code = d.error_code || "chat:stream_error";
            setStreamError(code);
            setMessages(prev => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant") {
                last.errorCode = code;
                if (!last.content) last.content = getErrorMessage(code);
                last.streaming = false;
              }
              return [...next];
            });
          } catch {}
          evtSource.close();
          clearTimeout(streamTimeout);
          finish();
        });

        evtSource.addEventListener("done", () => {
          evtSource.close();
          clearTimeout(streamTimeout);
          setMessages(prev => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant") last.streaming = false;
            return [...next];
          });
          finish();
        });

        evtSource.onerror = () => {
          if (!resolved) {
            evtSource.close();
            clearTimeout(streamTimeout);
            finish();
          }
        };
      });

      // SSE stream ended — if no content was delivered via SSE,
      // start auto-polling the turns API until the answer is persisted by the worker.
      if (!sseAnswer && !streamError) {
        const POLL_INTERVAL_MS = 2000;  // 每 2 秒查询一次
        const POLL_TIMEOUT_MS = 45_000; // 最多轮询 45 秒
        const pollStart = Date.now();
        let pollAnswer: string | null = null;
        let pollChunkIds: string[] = [];

        // Progress dots animation — cycles through "生成中.", "生成中..", "生成中..."
        const progressStates = ["正在生成回答.", "正在生成回答..", "正在生成回答..."];
        let progressIdx = 0;
        const progressInterval = setInterval(() => {
          progressIdx = (progressIdx + 1) % progressStates.length;
          setMessages(prev => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant" && !last.content) {
              last.content = progressStates[progressIdx];
              last.streaming = true;
            }
            return [...next];
          });
        }, 600);

        while (Date.now() - pollStart < POLL_TIMEOUT_MS) {
          await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
          try {
            const turns = await getTurns(activeConvIdNew!);
            const latestTurn = turns.find(t => t.turn_index === turnIndex);
            if (latestTurn?.answer) {
              pollAnswer = latestTurn.answer;
              pollChunkIds = latestTurn.chunk_ids || [];
              break;  // 答案已就绪，停止轮询
            }
          } catch {
            // DB 暂时不可达，继续轮询
          }
        }

        clearInterval(progressInterval);

        if (pollAnswer) {
          // 轮询成功——显示答案
          setMessages(prev => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant") {
              last.content = pollAnswer!;
              last.sources = pollChunkIds.map((id: string) => ({ chunk_id: id, content: "" }));
              last.streaming = false;
            }
            return [...next];
          });
        } else {
          // 轮询超时——显示友好提示（用户可以稍后刷新查看）
          setMessages(prev => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last?.role === "assistant" && !last.content) {
              last.content = "回答生成时间较长，请稍后点击会话刷新查看结果。";
              last.streaming = false;
            }
            return [...next];
          });
        }
      }

      fetchConvs();
    } catch (err: any) {
      setMessages(prev => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.role === "assistant") {
          last.content = "请求失败: " + (err.message || "未知错误");
          last.streaming = false;
        }
        return [...next];
      });
    } finally {
      setLoading(false);
    }
  };

  // Retry the last query — sets state, user clicks Send to re-submit
  const handleRetry = () => {
    setRetryCountdown(0);
    const lastUserMsg = [...messages].reverse().find(m => m.role === "user");
    if (lastUserMsg) {
      setInput(lastUserMsg.content);
      setMessages(prev => prev.slice(0, -1));
      setStreamError(null);
    }
  };

  if (!token) return null;

  return (
    <div className="h-screen flex flex-col bg-white">
      <Header />
      <div className="flex flex-1 overflow-hidden">
        {/* Left sidebar — conversation list */}
        <div className="w-72 border-r border-gray-200 bg-gray-50 flex flex-col">
          <div className="p-3 border-b border-gray-200">
            <button
              onClick={handleNewConv}
              disabled={selectedKBs.length === 0}
              className="w-full py-2 text-sm font-medium bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition"
            >
              + 新会话
            </button>
          </div>
          <ConvList
            convs={convs}
            activeId={activeConvId}
            onSelect={handleSelectConv}
            onDelete={handleDeleteConv}
            onRename={handleRenameConv}
          />
        </div>

        {/* Main chat area */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Stream error bar — different styles per error code */}
          {streamError && (
            <div className={`px-4 py-2 border-b flex items-center justify-between text-sm ${
              streamError === "retrieve:vector_store_unavailable" || streamError === "auth:authz_unavailable"
                ? "bg-yellow-50 border-yellow-200"
                : "bg-red-50 border-red-200"
            }`}>
              <span className={streamError === "retrieve:vector_store_unavailable" || streamError === "auth:authz_unavailable"
                ? "text-yellow-800" : "text-red-700"}>
                {getErrorMessage(streamError)}
                {retryCountdown > 0 && streamError === "retrieve:vector_store_unavailable" && (
                  <span className="ml-2 text-yellow-600 font-mono font-bold">{retryCountdown}s 后自动重试</span>
                )}
              </span>
              <button onClick={handleRetry} className={`px-3 py-1 text-white rounded text-xs font-medium transition ${
                streamError === "retrieve:vector_store_unavailable" || streamError === "auth:authz_unavailable"
                  ? "bg-yellow-600 hover:bg-yellow-700"
                  : "bg-red-600 hover:bg-red-700"
              }`}>
                {retryCountdown > 0 ? `立即重试 (${retryCountdown})` : "重试"}
              </button>
            </div>
          )}
          <MessageList messages={messages} msgEndRef={msgEndRef} streamError={streamError} />
          <InputBar
            input={input}
            onInputChange={setInput}
            onSend={handleSend}
            disabled={loading}
            kbName={kbName}
            kbId={selectedKBs[0] || ""}
          />
        </div>
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────

function getErrorMessage(code: string): string {
  const m: Record<string, string> = {
    "chat:stream_timeout": "生成超时，请重试",
    "chat:stream_error": "流式传输中断",
    "auth:authz_unavailable": "权限服务暂时不可用，请稍后重试",
    "retrieve:insufficient_evidence": "未找到足够的相关信息",
  };
  return m[code] || code;
}
