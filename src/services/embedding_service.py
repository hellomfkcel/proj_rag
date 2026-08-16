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
from typing import Any, Dict, List, Tuple

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

# FastAPI 自动埋点：为 /v1/embed、/v1/embed_query、/v1/rerank 创建 OTel server span，
# 使 embedding_service 的请求链路在 Tempo 可见（此前仅 worker 侧 Haystack span）。
# 2026-08-16 补齐，满足可观测完整性要求（P-OBS 单一出口）。
try:
    from src.platform.obs.tracing import instrument_fastapi
    instrument_fastapi(app)
except Exception:
    pass


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
# Infinity Embedding Server Adapter（2026-08-16）
# ══════════════════════════════════════════════════════════════════
# 稠密嵌入 + rerank 转发给 Infinity（内置动态 batching、OpenAI/Cohere 兼容）；
# 稀疏向量（BGE-M3 lexical weights）Infinity 不提供，由本地 BGE-M3 生成。
# 契约不变：调用方（embedding_client / Haystack 组件）零改动。
# Infinity 不可达时 fail-open 回退本地 BGE-M3 / 本地 reranker。

INFINITY_URL = os.getenv("INFINITY_URL", "http://localhost:19501").rstrip("/")
INFINITY_EMBED_MODEL = os.getenv("INFINITY_EMBED_MODEL", "BAAI/bge-m3")
INFINITY_RERANK_MODEL = os.getenv("INFINITY_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")


def _infinity_dense(texts: List[str]) -> List[List[float]]:
    """经 Infinity 批量稠密嵌入（OpenAI /embeddings 兼容）。"""
    import httpx
    resp = httpx.post(
        f"{INFINITY_URL}/embeddings",
        json={"model": INFINITY_EMBED_MODEL, "input": texts},
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    # OpenAI 响应按 index 排序，保证与输入顺序一致
    dense = [d["embedding"] for d in sorted(data, key=lambda x: x.get("index", 0))]
    return dense


def _local_dense(texts: List[str]) -> List[List[float]]:
    """本地 BGE-M3 稠密嵌入（fail-open 兜底，与自研实现一致）。"""
    from src.ingest.components.bge_m3_embedder import _get_model
    model = _get_model()
    output = model.encode(texts, return_dense=True, return_sparse=False, batch_size=len(texts))
    return [v.tolist() if hasattr(v, "tolist") else list(v) for v in output["dense_vecs"]]


def _local_sparse(texts: List[str]) -> List[Dict[str, float]]:
    """本地 BGE-M3 稀疏词权重（唯一权威源，Infinity 不提供稀疏）。"""
    from src.ingest.components.bge_m3_embedder import _get_model
    model = _get_model()
    output = model.encode(
        texts, return_dense=False, return_sparse=True, batch_size=len(texts)
    )
    return output.get("lexical_weights", [{}] * len(texts))


def _infinity_rerank(query: str, documents: List[str], top_k: int) -> Tuple[List[str], List[float]]:
    """经 Infinity rerank（Cohere /rerank 兼容）。返回 (排序后文档, 分数)。"""
    import httpx
    resp = httpx.post(
        f"{INFINITY_URL}/rerank",
        json={"model": INFINITY_RERANK_MODEL, "query": query,
              "documents": documents, "top_n": top_k},
        timeout=60.0,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    # results 已按 relevance_score 降序；index 为原 documents 下标
    sorted_docs = [documents[r["index"]] for r in results if r.get("index") is not None]
    scores = [float(r.get("relevance_score", 0.0)) for r in results]
    return sorted_docs, scores


# ══════════════════════════════════════════════════════════════════
# 端点
# ══════════════════════════════════════════════════════════════════

@app.post("/v1/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest):
    """文档批量嵌入——稠密 + 稀疏一次产出。

    2026-08-16 Adapter：稠密经 Infinity（/embeddings），稀疏经本地 BGE-M3。
    Infinity 不可达时 fail-open 回退本地稠密。
    供摄入 Pipeline (BGE_M3DocumentEmbedder) 和语义分割器使用。
    """
    t0 = time.time()
    all_embeddings: List[List[float]] = []
    all_sparse: List[Dict[str, float]] = []

    try:
        # ── 稠密：Infinity 优先，失败回退本地 BGE-M3 ──
        try:
            all_embeddings = _infinity_dense(request.texts)
            dense_source = "infinity"
        except Exception as exc:
            log.warning("infinity_dense_failed_fallback_local", error=str(exc)[:200],
                        text_count=len(request.texts))
            all_embeddings = _local_dense(request.texts)
            dense_source = "local"
        if request.normalize:
            all_embeddings = [_normalize_vector(v) for v in all_embeddings]

        # ── 稀疏：本地 BGE-M3（唯一权威源）──
        all_sparse = _local_sparse(request.texts)
    except Exception as exc:
        log.error("embedding_encode_failed", error=str(exc), text_count=len(request.texts))
        raise HTTPException(status_code=500, detail=f"Embedding failed: {exc}")

    elapsed_ms = int((time.time() - t0) * 1000)
    log.info("embed_completed", count=len(all_embeddings),
             dense_source=dense_source, elapsed_ms=elapsed_ms)
    return EmbedResponse(
        embeddings=all_embeddings,
        sparse_embeddings=all_sparse,
        count=len(all_embeddings),
        elapsed_ms=elapsed_ms,
    )


@app.post("/v1/embed_query", response_model=EmbedQueryResponse)
async def embed_query(request: EmbedQueryRequest):
    """查询嵌入——稠密 + 稀疏一次产出。

    2026-08-16 Adapter：稠密经 Infinity，稀疏经本地 BGE-M3；Infinity 不可达回退本地。
    供检索 Pipeline (BGE_M3TextEmbedder) 使用。
    """
    t0 = time.time()
    try:
        try:
            dense_raw = _infinity_dense([request.text])
            dense_source = "infinity"
        except Exception as exc:
            log.warning("infinity_query_dense_failed_fallback_local", error=str(exc)[:200])
            dense_raw = _local_dense([request.text])
            dense_source = "local"
        vec_list = _normalize_vector(dense_raw[0])
        sparse = _local_sparse([request.text])[0]
    except Exception as exc:
        log.error("embedding_query_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Query embedding failed: {exc}")

    elapsed_ms = int((time.time() - t0) * 1000)
    log.info("embed_query_completed", dense_source=dense_source, elapsed_ms=elapsed_ms)
    return EmbedQueryResponse(
        embedding=vec_list,
        sparse_embedding=sparse,
        elapsed_ms=elapsed_ms,
    )


@app.post("/v1/rerank", response_model=RerankResponse)
async def rerank(request: RerankRequest):
    """文档重排序——BGE-Reranker-v2-m3。

    2026-08-16 Adapter：优先经 Infinity（Cohere /rerank 兼容）；
    Infinity 不可达时 fail-open 回退本地 reranker。
    供检索 Pipeline (BGEReranker) 使用。
    """
    t0 = time.time()
    try:
        try:
            result_docs, result_scores = _infinity_rerank(
                request.query, request.documents, request.top_k
            )
            rerank_source = "infinity"
        except Exception as exc:
            log.warning("infinity_rerank_failed_fallback_local", error=str(exc)[:200])
            from src.platform.model.registry import _get_reranker
            ranker = _get_reranker()
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
            rerank_source = "local"
    except Exception as exc:
        log.error("rerank_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Rerank failed: {exc}")

    elapsed_ms = int((time.time() - t0) * 1000)
    log.info("rerank_completed", doc_count=len(request.documents),
             rerank_source=rerank_source, elapsed_ms=elapsed_ms)
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
    """就绪检查——推理能力可用。

    Adapter 架构（2026-08-16）：稠密 + rerank 由 Infinity 承担，稀疏由本地 BGE-M3。
    就绪条件 = Infinity 可达 或 本地 BGE-M3 已加载（任一可用即可服务）。
    """
    try:
        import httpx
        infinity_ok = False
        try:
            r = httpx.get(f"{INFINITY_URL}/models", timeout=2.0)
            infinity_ok = r.status_code == 200
        except Exception:
            infinity_ok = False

        from src.ingest.components.bge_m3_embedder import _model as bge_model
        models_ok = infinity_ok or bge_model is not None
    except Exception:
        models_ok = False
    return HealthResponse(
        status="ready" if models_ok else "not_ready",
        models_loaded=models_ok,
    )
