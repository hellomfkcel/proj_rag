// SSE 转发 Route Handler — 绕过 Next.js rewrites 代理的 SSE 缓冲。
//
// 背景：Next.js `rewrites()` 把 `/api/:path*` 代理到后端，但该代理会**缓冲整个
// SSE 响应直到连接关闭**，导致前端一次性收到全部事件（retrieved/thinking/token/done
// 同时到达），表现为"非流式"。直连后端 8000 是增量的（已实测）。
//
// 方案：新增同源 `/stream/...` Route Handler，逐块透传后端的 ReadableStream 到浏览器。
// 同源（浏览器访问 :3001，请求 /stream/... 也是 :3001）→ 无 CORS 问题；
// 不命中 `/api/:path*` rewrite → 不被 Next.js 代理缓冲。
//
// 浏览器 EventSource 连接 /stream/v1/conversations/{id}/stream?turn_index=N&token=<jwt>
// → 本 Handler 转发到 {BACKEND_BASE}/api/v1/conversations/{id}/stream?turn_index=N&token=<jwt>

import { NextRequest } from "next/server";

const BACKEND_BASE = process.env.BACKEND_BASE_URL || "http://localhost:8000";

export async function GET(
  request: NextRequest,
  { params }: { params: { path: string[] } },
) {
  const path = params.path.join("/");
  const query = request.nextUrl.search;
  const upstreamUrl = `${BACKEND_BASE}/api/${path}${query}`;

  const upstream = await fetch(upstreamUrl, {
    headers: {
      // SSE 端点鉴权走 ?token=<jwt>（EventSource 不支持自定义 header），
      // 这里保留 Authorization header 以兼容未来 header 鉴权的场景。
      Authorization: request.headers.get("Authorization") || "",
    },
  });

  if (!upstream.ok || !upstream.body) {
    return new Response(`upstream error: ${upstream.status}`, {
      status: upstream.status,
    });
  }

  // 逐块透传 ReadableStream（Next.js Route Handler 支持流式响应体）
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("Content-Type") || "text/event-stream",
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
