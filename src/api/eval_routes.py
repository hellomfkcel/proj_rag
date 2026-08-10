"""RAG 评测端点 — POST /api/v1/query/eval：透出检索 chunk / context / is_answerable。

评测系统（方式二：在线调 API）只打本端点，与线上 /query 彻底隔离。
检索 + 生成复用线上同一套核心（chat/service._run_retrieve_generate），
保证评测与线上逻辑完全一致，仅响应体多吐中间结果。

响应契约（评测 5.4）：
{
  "query": str,
  "answer": str,
  "is_answerable": bool,       # 系统自报"是否在知识库找到依据"
  "contexts": [str, ...],       # 实际喂给 LLM 的截断片段
  "retrieved_chunks": [         # 检索到的 chunk（与 gold set 同一套 chunk_id）
    {"chunk_id": str, "text": str, "score": float, "rank": int}, ...
  ]
}
"""

import asyncio
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.permission.context import RequestContext
from src.api.deps import get_request_context

router = APIRouter(prefix="/api/v1", tags=["eval"])


class EvalQueryRequest(BaseModel):
    query: str
    kb_ids: List[str]   # 检索必须在哪些 KB 搜（gold set 按 KB 组织，评测系统自带上）


@router.post("/query/eval")
async def evaluate_query(request: EvalQueryRequest,
                         ctx: RequestContext = Depends(get_request_context)):
    """评测：跑一条 query 的完整检索 + 生成，返回评测契约。

    计算在 retrieval-worker 中执行（§17 API 进程禁止跑 pipeline.run()）：
    1. mint ctx_token（JWT 原文不外传，TTL ≤600s）
    2. 调度 evaluate_query_task → 等待结果（线程池等待，不阻塞事件循环）
    3. 返回完整评测契约（answer/is_answerable/contexts/retrieved_chunks）
    """
    from src.permission.authz import mint_ctx_token
    from src.chat.service import evaluate_query_task
    from celery.exceptions import TimeoutError as CeleryTimeout

    if not request.kb_ids:
        raise HTTPException(status_code=422, detail="eval:kb_ids_required — 评测必须指定 kb_ids")

    # 铸造 ctx_token（替代 JWT 原文传给 worker，audience 与消费方一致）
    ctx_token = mint_ctx_token(ctx, audience="retrieval-worker", ttl_s=600)

    ar = evaluate_query_task.delay(
        query=request.query,
        kb_ids=request.kb_ids,
        tenant_id=ctx.tenant_id,
        ctx_token=ctx_token,
    )

    try:
        # 在线程池等 worker 结果（评测为低频离线调用，最长 180s），不阻塞事件循环
        result = await asyncio.to_thread(ar.get, 180)
    except CeleryTimeout:
        raise HTTPException(status_code=504, detail="eval:timeout — 评测任务超时")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"eval:task_failed — {str(exc)[:200]}")

    return result
