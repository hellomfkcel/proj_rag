"""BGE-M3 Embedding & Rerank Service — Layer 3 GPU 资源管理层。

独立 FastAPI 服务，单进程持有 BGE-M3 + BGE-Reranker 全局单例，
所有 worker 通过 HTTP API 共享同一模型实例。

设计依据：
- RAG系统设计v14.md §11 P-MODEL 模块边界
- Layer 1: GPU 显存检查 + CPU 降级 (_get_device)
- ★ 模型常驻：加载后进程生命周期内不卸载（共享服务是唯一模型持有者，
  worker 经 HTTP 调用，不重复加载 → 无资源争夺）

启动方式:
  uvicorn src.services.embedding_service:app --host 0.0.0.0 --port 19500

端点:
  POST /v1/embed       文档批量嵌入 (dense + sparse)
  POST /v1/embed_query 查询嵌入 (dense + sparse)
  POST /v1/rerank      文档重排序
  GET  /healthz        存活检查
  GET  /readyz         就绪检查
"""

import math
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List

# 确保项目根在 sys.path 中（支持直接 python 执行和 uvicorn 导入）
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# ── OTel / 日志 初始化（fail-open） ──
try:
    from src.platform.obs.tracing import init_tracing
    init_tracing(service_name="embedding-service")
except Exception:
    pass

try:
    from src.platform.obs.logger import get_logger
    log = get_logger(__name__)
except Exception:
    import logging
    log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# 请求/响应模型
# ══════════════════════════════════════════════════════════════════

class EmbedRequest(BaseModel):
    texts: List[str] = Field(..., min_length=1, max_length=10000,
                             description="待嵌入的文本列表")
    normalize: bool = Field(default=True, description="是否 L2 归一化稠密向量")
    batch_size: int = Field(default=512, ge=32, le=2048,
                            description="内部编码批次大小")


class EmbedResponse(BaseModel):
    embeddings: List[List[float]] = Field(description="稠密向量列表, 1024d")
    sparse_embeddings: List[Dict[str, float]] = Field(description="稀疏词权重列表")
    count: int = Field(description="嵌入向量数量")
    elapsed_ms: int = Field(description="编码耗时 (毫秒)")


class EmbedQueryRequest(BaseModel):
    text: str = Field(..., min_length=1, description="查询文本")


class EmbedQueryResponse(BaseModel):
    embedding: List[float] = Field(description="稠密向量, 1024d")
    sparse_embedding: Dict[str, float] = Field(description="稀疏词权重")
    elapsed_ms: int = Field(description="编码耗时 (毫秒)")


class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1)
    documents: List[str] = Field(..., min_length=1, max_length=500,
                                 description="待重排序文档列表")
    top_k: int = Field(default=10, ge=1, le=100)


class RerankResponse(BaseModel):
    documents: List[str] = Field(description="按相关性降序排列的文档")
    scores: List[float] = Field(description="对应的相关性分数")
    elapsed_ms: int = Field(description="重排序耗时 (毫秒)")


class HealthResponse(BaseModel):
    status: str
    models_loaded: bool


# ══════════════════════════════════════════════════════════════════
# 应用生命周期
# ══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动/关闭生命周期。模型惰性加载，不在启动时占用 GPU。"""
    log.info("embedding_service_starting", port=19500)
    yield
    # 关闭时释放 GPU 资源
    try:
        from src.ingest.components.bge_m3_embedder import _model
        import torch
        if _model is not None:
            _model = None  # noqa: F841 (global clear via import side-effect)
            torch.cuda.empty_cache()
            log.info("embedding_service_gpu_released")
    except Exception:
        pass


app = FastAPI(
    title="BGE-M3 Embedding Service",
    version="1.0.0",
    lifespan=lifespan,
)


# ══════════════════════════════════════════════════════════════════
# 模型访问辅助
# ══════════════════════════════════════════════════════════════════

def _normalize_vector(vec: List[float]) -> List[float]:
    """L2 归一化。"""
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        return [v / norm for v in vec]
    return vec


# ══════════════════════════════════════════════════════════════════
# 端点
# ══════════════════════════════════════════════════════════════════

@app.post("/v1/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest):
    """文档批量嵌入——稠密 + 稀疏一次产出。

    供摄入 Pipeline (BGE_M3DocumentEmbedder) 和语义分割器使用。
    """
    from src.ingest.components.bge_m3_embedder import _get_model

    t0 = time.time()
    try:
        model = _get_model()
    except Exception as exc:
        log.error("embedding_model_load_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Embedding model not available")

    all_embeddings: List[List[float]] = []
    all_sparse: List[Dict[str, float]] = []

    try:
        for i in range(0, len(request.texts), request.batch_size):
            batch = request.texts[i : i + request.batch_size]
            output = model.encode(
                batch,
                return_dense=True,
                return_sparse=True,
                batch_size=len(batch),
            )
            dense_batch = output["dense_vecs"]
            sparse_batch = output.get("lexical_weights", [{}] * len(batch))

            for vec in dense_batch:
                vec_list = vec.tolist() if hasattr(vec, "tolist") else list(vec)
                if request.normalize:
                    vec_list = _normalize_vector(vec_list)
                all_embeddings.append(vec_list)

            all_sparse.extend(sparse_batch)

    except Exception as exc:
        log.error("embedding_encode_failed", error=str(exc), text_count=len(request.texts))
        raise HTTPException(status_code=500, detail=f"Embedding failed: {exc}")

    elapsed_ms = int((time.time() - t0) * 1000)
    return EmbedResponse(
        embeddings=all_embeddings,
        sparse_embeddings=all_sparse,
        count=len(all_embeddings),
        elapsed_ms=elapsed_ms,
    )


@app.post("/v1/embed_query", response_model=EmbedQueryResponse)
async def embed_query(request: EmbedQueryRequest):
    """查询嵌入——稠密 + 稀疏一次产出。

    供检索 Pipeline (BGE_M3TextEmbedder) 使用。
    """
    from src.ingest.components.bge_m3_embedder import _get_model

    t0 = time.time()
    try:
        model = _get_model()
    except Exception as exc:
        log.error("embedding_model_load_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Embedding model not available")

    try:
        output = model.encode([request.text], return_dense=True, return_sparse=True)
        dense_vec = output["dense_vecs"][0]
        vec_list = dense_vec.tolist() if hasattr(dense_vec, "tolist") else list(dense_vec)
        vec_list = _normalize_vector(vec_list)

        lexical = output.get("lexical_weights", [{}])
        sparse = lexical[0] if len(lexical) > 0 else {}

    except Exception as exc:
        log.error("embedding_query_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Query embedding failed: {exc}")

    elapsed_ms = int((time.time() - t0) * 1000)
    return EmbedQueryResponse(
        embedding=vec_list,
        sparse_embedding=sparse,
        elapsed_ms=elapsed_ms,
    )


@app.post("/v1/rerank", response_model=RerankResponse)
async def rerank(request: RerankRequest):
    """文档重排序——使用 BGE-Reranker-v2-m3。

    供检索 Pipeline (BGEReranker) 使用。
    """
    from src.platform.model.registry import _get_reranker

    t0 = time.time()
    try:
        ranker = _get_reranker()
    except Exception as exc:
        log.error("reranker_model_load_failed", error=str(exc))
        raise HTTPException(status_code=503, detail="Reranker model not available")

    try:
        scores = ranker.compute_score(
            [[request.query, d] for d in request.documents],
            normalize=True,
        )
        scored = sorted(
            zip(request.documents, scores),
            key=lambda x: x[1],
            reverse=True,
        )
        result_docs = [doc for doc, _ in scored[:request.top_k]]
        result_scores = [float(s) for _, s in scored[:request.top_k]]

    except Exception as exc:
        log.error("rerank_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Rerank failed: {exc}")

    elapsed_ms = int((time.time() - t0) * 1000)
    return RerankResponse(
        documents=result_docs,
        scores=result_scores,
        elapsed_ms=elapsed_ms,
    )


@app.get("/healthz", response_model=HealthResponse)
async def healthz():
    """存活检查——进程是否响应。"""
    return HealthResponse(status="ok", models_loaded=False)


@app.get("/readyz", response_model=HealthResponse)
async def readyz():
    """就绪检查——模型是否可用。

    注意：模型可能因空闲超时被卸载（Layer 2），此时 readyz 返回 not_ready。
    调用方应处理此状态：等待模型重新加载或降级到本地模式。
    """
    try:
        from src.ingest.components.bge_m3_embedder import _model as bge_model
        from src.platform.model.registry import _reranker_cache
        models_ok = bge_model is not None and len(_reranker_cache) > 0
    except Exception:
        models_ok = False
    return HealthResponse(
        status="ready" if models_ok else "not_ready",
        models_loaded=models_ok,
    )
