"""REST API 路由。

对外暴露的 HTTP 端点，职责仅限于：
- 参数校验与解析
- 分发 Celery 任务到 worker
- 转发 SSE 流式结果给客户端
"""

import json
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.permission.context import RequestContext
from src.api.deps import get_request_context

router = APIRouter(prefix="/api/v1")


# ══════════════════════════════════════════════════════════════
# Request / Response models
# ══════════════════════════════════════════════════════════════

class UploadResponse(BaseModel):
    document_id: str
    mount_id: str
    parse_status: str
    duplicate: bool


class QueryRequest(BaseModel):
    question: str
    kb_ids: List[str]
    conversation_id: Optional[str] = None
    # Per-query retrieval overrides (写入 turn 级 retrieval_configs)
    retrieval_mode: Optional[str] = None       # hybrid / vector_only / keyword_only
    fusion_method: Optional[str] = None         # rrf / weighted_sum
    strict: Optional[bool] = None               # 实时权限复核
    top_k: Optional[int] = None                 # 返回文档数
    dense_weight: float = 0.5                   # dense 路权重 (weighted_sum 模式)
    sparse_weight: float = 0.5                  # sparse 路权重 (weighted_sum 模式)
    synthesis_mode: Optional[str] = None         # compact / refine / tree_summarize / no_synthesis / auto
    oversample_factor: Optional[float] = None    # 过采样系数（默认 1.5）
    min_results: Optional[int] = None            # 最小结果数（补检索触发阈值，默认 3）
    refetch_max_rounds: Optional[int] = None     # 最大补检索轮数（默认 2）
    refine_batch_size: Optional[int] = None       # Refine 每批 chunk 数
    doc_preview_max_chars: Optional[int] = None   # Chunk 截断长度


class QueryResponse(BaseModel):
    answer: str
    chunk_ids: List[str]
    conversation_id: str
    turn_index: int
    error_code: Optional[str] = None   # retrieve:insufficient_evidence | retrieve:vector_store_unavailable
    # 链路可观测：本次查询的 OTel trace_id + Grafana Tempo 查看链路（空串表示未捕获到 trace）
    trace_id: str = ""
    trace_ui_url: str = ""


class DeleteResponse(BaseModel):
    status: str
    doc_id: str = ""
    kb_id: str = ""


# ══════════════════════════════════════════════════════════════
# Upload
# ══════════════════════════════════════════════════════════════

@router.post("/documents/upload", response_model=UploadResponse)
def upload_document(
    req: Request,
    file: UploadFile = File(...),
    kb_id: str = Form(...),
    tenant_id: str = Form(default="tenant-dev"),
    user_id: str = Form(default="dev-user"),
    auto_parse: bool = Form(default=True),
):
    """上传文档：登记 + 挂载 + 触发解析。"""
    from src.doc.service import submit_ingest_task
    from src.permission.authz import check

    ctx = getattr(req.state, "ctx", None)
    request_id = ctx.request_id if ctx else ""
    credential = ctx.credential if ctx else ""
    user_id_from_ctx = ctx.user_id if ctx else user_id
    tenant_id_from_ctx = ctx.tenant_id if ctx else tenant_id

    if ctx:
        decision = check(ctx, "kb:write", "kb", kb_id)
        if decision.get("decision") != "allow":
            raise HTTPException(status_code=403, detail="auth:forbidden — 您没有上传文档的权限（需要 kb:write）")

    if not kb_id or kb_id in ("null", "undefined"):
        raise HTTPException(status_code=422, detail="Please select a knowledge base before uploading")
    import asyncpg as _apg
    from src.config import Settings as _Settings
    _dsn = _Settings().database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _check_kb():
        conn = await _apg.connect(_dsn)
        try:
            kb = await conn.fetchrow(
                "SELECT id FROM knowledge_bases WHERE id=$1 AND tenant_id=$2",
                kb_id, tenant_id_from_ctx)
            if not kb:
                raise HTTPException(status_code=404, detail="Knowledge base not found or not in current tenant")
        finally:
            await conn.close()
    import asyncio as _asyncio
    _asyncio.run(_check_kb())

    content = file.file.read()

    # 从 OtelTraceCaptureMiddleware（原始 ASGI）获取预捕获的 trace context
    _otel_trace_id = req.scope.get("otel_trace_id", "")
    _otel_span_id = req.scope.get("otel_span_id", "")

    result = submit_ingest_task(
        user_id=user_id_from_ctx,
        tenant_id=tenant_id_from_ctx,
        kb_id=kb_id,
        filename=file.filename or "unknown.txt",
        file_content=content,
        auto_parse=auto_parse,
        request_id=request_id,
        credential=credential,
        otel_trace_id=_otel_trace_id,
        otel_span_id=_otel_span_id,
    )
    return UploadResponse(**result)


# ══════════════════════════════════════════════════════════════
# Query — dispatch-only（§9.1 / §17 红线：API 禁止调 pipeline.run()）
# ══════════════════════════════════════════════════════════════

def _build_trace_ui_url(trace_id: str) -> str:
    """构造 Grafana Tempo 按 trace_id 查看的深链（无 trace 时返回空串）。

    §8.2 单一查询出口 = Grafana；tempo 数据源 UID 由 Settings 配置（观测栈 provision 固定 tempo-uid）。
    """
    if not trace_id:
        return ""
    try:
        import json as _json
        from urllib.parse import quote
        from src.config import Settings
        s = Settings()
        if not s.grafana_url:
            return ""
        pane_id = "v1"
        panes = {
            pane_id: {
                "datasource": s.grafana_tempo_datasource_uid,
                "queries": [
                    {"refId": "A", "query": trace_id,
                     "queryType": "traceql", "queryType2": "traceql"}
                ],
                "range": {"from": "now-6h", "to": "now"},
            }
        }
        qs = quote(_json.dumps(panes))
        return f"{s.grafana_url}/explore?schemaVersion=1&panes={qs}"
    except Exception:
        return ""


@router.post("/conversations/query", response_model=QueryResponse)
async def query(request: QueryRequest, ctx: RequestContext = Depends(get_request_context),
                http_request: Request = None, response: Response = None):
    """分发检索+生成任务到 Celery retrieval-worker。

    API 进程只做：参数校验 → mint ctx_token → delay() 提交任务 → 立即返回。
    实际检索/rerank/LLM 全在 retrieval-worker 内通过 retrieve_and_generate_task 执行。
    结果通过 SSE (GET /conversations/{id}/stream) 流式推送到前端。

    设计依据：§17 "API 进程禁止调 pipeline.run()——计算密集与 I/O 密集抢占同组进程"。
    响应携带 trace_id / trace_ui_url（§2.1 request_id=trace_id，供前端跳 Tempo 查看链路）。
    """
    import uuid as _uuid
    from src.config import Settings
    from src.permission.authz import mint_ctx_token
    from src.chat.service import retrieve_and_generate_task

    s = Settings()

    # 从 ASGI scope 取 OTel trace_id（OtelTraceCaptureMiddleware 已捕获）
    trace_id = ""
    try:
        trace_id = (http_request or {}).scope.get("otel_trace_id", "") if http_request else ""
    except Exception:
        trace_id = ""
    conv_id = request.conversation_id or str(_uuid.uuid4())

    # 1. 确保 conversation 存在
    await _ensure_conversation_async(conv_id, request.kb_ids, ctx.user_id, ctx.tenant_id)

    # 2. 获取下一个 turn_index
    turn_index = await _next_turn_index(conv_id)

    # 3. 铸造 ctx_token（替代 JWT 原文传给 worker，TTL ≤600s）
    ctx_token = mint_ctx_token(ctx, audience="retrieval-worker", ttl_s=600)

    # 4. 从 P-CONFIG 解析 Pipeline 名称（§12.1 haystack_pipeline_name）
    #    检索时实际 Pipeline 由 retrieve() 按 retrieval_mode + fusion_mode 动态选择；
    #    haystack_pipeline_name 作为 KB 粒度的默认/后备 Pipeline 标识，
    #    写入 conversation_turn 参数快照供审计追溯。
    from src.platform.config.service import resolve_retrieval_config
    kb_id = request.kb_ids[0] if request.kb_ids else ""
    retrieval_cfg = resolve_retrieval_config(kb_id=kb_id, tenant_id=ctx.tenant_id)

    # 5. 分发到 retrieval_queue（API 进程不执行任何 Pipeline 计算）
    retrieve_and_generate_task.delay(
        conversation_id=conv_id,
        turn_index=turn_index,
        user_question=request.question,
        kb_ids=request.kb_ids,
        tenant_id=ctx.tenant_id,
        ctx_token=ctx_token,
        pipeline_name=retrieval_cfg.haystack_pipeline_name,
        yaml_version="v1",
        retrieval_mode=request.retrieval_mode,
        fusion_method=request.fusion_method,
        strict=request.strict,
        top_k=request.top_k,
        dense_weight=request.dense_weight,
        sparse_weight=request.sparse_weight,
        synthesis_mode=request.synthesis_mode,
        oversample_factor=request.oversample_factor,
        min_results=request.min_results,
        refetch_max_rounds=request.refetch_max_rounds,
        refine_batch_size=request.refine_batch_size,
        doc_preview_max_chars=request.doc_preview_max_chars,
    )

    # 6. 立即返回 — answer 和 chunk_ids 由 worker 经 Redis Pub/Sub → SSE 推送
    if response is not None and trace_id:
        response.headers["X-Trace-Id"] = trace_id
    return QueryResponse(
        answer="",          # dispatched to worker — results via SSE
        chunk_ids=[],       # dispatched to worker — sources via SSE "retrieved" event
        conversation_id=conv_id,
        turn_index=turn_index,
        error_code=None,
        trace_id=trace_id,
        trace_ui_url=_build_trace_ui_url(trace_id),
    )


@router.get("/conversations/{conversation_id}/stream")
async def query_stream(conversation_id: str, turn_index: int = 1):
    """SSE 流式订阅查询结果。

    1. 首先检查 DB 中该 turn 是否已有答案（worker 在 SSE 订阅前已完成）
    2. 若有 → 直接以 SSE 事件返回（retrieved + token + done）
    3. 若无 → 订阅 Redis Pub/Sub 等待 worker 发布
    §9.3: 设 60s 空闲超时——无消息即发 error 断开。
    """
    from src.config import Settings
    import redis
    import time as _time

    s = Settings()
    SSE_IDLE_TIMEOUT = 60  # 秒 — 匹配 LLM 生成最长等待时间

    # 取该 turn 已持久化的 trace_id（= 查询链路 trace_id），作为响应头供前端关联 Tempo。
    # worker 尚未完成时 turn 未落库 → 为空，此时前端已从 POST 响应拿到 trace_id。
    persisted_trace_id = ""
    try:
        import asyncpg as _apg
        _conn = await _apg.connect(
            s.database_url.replace("postgresql+asyncpg://", "postgresql://"))
        _row = await _conn.fetchrow(
            "SELECT trace_id FROM conversation_turns "
            "WHERE conversation_id=$1 AND turn_index=$2",
            conversation_id, turn_index,
        )
        if _row:
            persisted_trace_id = _row["trace_id"] or ""
        await _conn.close()
    except Exception:
        pass

    async def _stream():
        # ── Step 1: 检查 DB 中是否已有答案（worker prior-art race condition）──
        import asyncpg as _apg
        try:
            db_conn = await _apg.connect(
                s.database_url.replace("postgresql+asyncpg://", "postgresql://")
            )
            row = await db_conn.fetchrow(
                "SELECT answer, retrieved_chunk_ids FROM conversation_turns "
                "WHERE conversation_id=$1 AND turn_index=$2",
                conversation_id, turn_index,
            )
            await db_conn.close()

            if row and row["answer"]:
                # Worker 已完成——直接返回持久化结果，无需等待 Redis
                chunk_ids = row["retrieved_chunk_ids"] or []
                yield f"event: retrieved\ndata: {json.dumps({'chunk_ids': chunk_ids, 'chunks': []}, default=str)}\n\n"
                yield f"event: token\ndata: {json.dumps({'content': row['answer']}, default=str)}\n\n"
                yield f"event: done\ndata: {json.dumps({})}\n\n"
                return
        except Exception:
            pass  # DB 不可达时回退到 Redis Pub/Sub

        # ── Step 2: 订阅 Redis Pub/Sub 等待 worker 发布 ──
        r = redis.from_url(s.redis_url)
        pubsub = r.pubsub()
        channel = f"query-stream:{conversation_id}:{turn_index}"
        pubsub.subscribe(channel)

        last_msg_time = _time.time()

        try:
            while True:
                message = pubsub.get_message(timeout=1.0)
                if message is None:
                    if _time.time() - last_msg_time > SSE_IDLE_TIMEOUT:
                        error_data = json.dumps({
                            "event": "error",
                            "error_code": "chat:stream_timeout",
                            "message": "流式传输超时，请重试",
                        }, default=str)
                        yield f"event: error\ndata: {error_data}\n\n"
                        break
                    continue

                if message["type"] != "message":
                    continue

                last_msg_time = _time.time()
                data = json.loads(message["data"])
                event_type = data.get("event", "unknown")

                if event_type == "retrieved":
                    yield f"event: retrieved\ndata: {json.dumps(data, default=str)}\n\n"
                elif event_type == "thinking":
                    # 推理增量（deepseek reasoning_content），透传给前端"思考过程"
                    yield f"event: thinking\ndata: {json.dumps(data, default=str)}\n\n"
                elif event_type == "token":
                    yield f"event: token\ndata: {json.dumps(data, default=str)}\n\n"
                elif event_type == "done":
                    yield f"event: done\ndata: {json.dumps(data, default=str)}\n\n"
                    break
                elif event_type == "error":
                    yield f"event: error\ndata: {json.dumps(data, default=str)}\n\n"
                    break
        finally:
            pubsub.close()
            r.close()

    _headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    if persisted_trace_id:
        _headers["X-Trace-Id"] = persisted_trace_id

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers=_headers,
    )


# ══════════════════════════════════════════════════════════════
# Delete
# ══════════════════════════════════════════════════════════════

@router.delete("/documents/{doc_id}/kb/{kb_id}", response_model=DeleteResponse)
def delete_document(doc_id: str, kb_id: str, purge: bool = False,
                     ctx: RequestContext = Depends(get_request_context)):
    """从 KB 移除文档。需要 doc:unmount 权限。"""
    from src.doc.service import delete_document_from_kb
    from src.permission.authz import check

    # ★ 权限检查：移除文档需要 doc:unmount（通道类动词，必须带 channel.kb）
    decision = check(ctx, "doc:unmount", "document", doc_id, channel_kb=kb_id)
    if decision.get("decision") != "allow":
        raise HTTPException(status_code=403, detail="auth:forbidden — 您没有移除此文档的权限（需要 doc:unmount）")

    request_id = ctx.request_id
    credential = ctx.credential
    user_id = ctx.user_id
    tenant_id = ctx.tenant_id

    try:
        result = delete_document_from_kb(
            user_id=user_id, doc_id=doc_id, kb_id=kb_id,
            tenant_id=tenant_id, purge=purge,
            request_id=request_id, credential=credential,
        )
        return DeleteResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"移除文档失败: {str(e)[:200]}")


# ══════════════════════════════════════════════════════════════
# Health / Util
# ══════════════════════════════════════════════════════════════

@router.get("/ping")
async def ping():
    return {"ping": "pong"}


@router.get("/readyz")
async def readyz():
    """就绪检查：依赖就绪（不含权限服务）。"""
    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════
# 内部 helpers（async — 端点已是 async def，无需 asyncio.run）
# ══════════════════════════════════════════════════════════════

import asyncpg as _asyncpg

async def _ensure_conversation_async(
    conv_id: str, kb_ids: List[str], user_id: str, tenant_id: str,
) -> None:
    """创建 conversation（如果不存在）。"""
    from src.config import Settings
    s = Settings()
    conn = await _asyncpg.connect(
        s.database_url.replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        existing = await conn.fetchrow(
            "SELECT id FROM conversations WHERE id=$1", conv_id,
        )
        if not existing:
            await conn.execute(
                "INSERT INTO conversations (id, tenant_id, user_id, bound_kb_ids) VALUES ($1,$2,$3,$4)",
                conv_id, tenant_id, user_id, kb_ids,
            )
    finally:
        await conn.close()


async def _next_turn_index(conv_id: str) -> int:
    """返回下一个 turn_index（当前最大 + 1）。"""
    from src.config import Settings
    s = Settings()
    conn = await _asyncpg.connect(
        s.database_url.replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        max_idx = await conn.fetchval(
            "SELECT coalesce(max(turn_index), 0) FROM conversation_turns WHERE conversation_id=$1",
            conv_id,
        )
        return (max_idx or 0) + 1
    finally:
        await conn.close()
