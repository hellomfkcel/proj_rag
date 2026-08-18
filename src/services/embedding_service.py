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
import threading
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
    need_sparse: bool = Field(
        default=True,
        description="是否需要稀疏向量。默认本地 BGE-M3 一次前向同时产出稠密+稀疏；"
                    "纯稠密场景传 false 走 Infinity（单模型单前向）。",
    )


class EmbedResponse(BaseModel):
    embeddings: List[List[float]] = Field(description="稠密向量列表, 1024d")
    sparse_embeddings: List[Dict[str, float]] = Field(description="稀疏词权重列表")
    count: int = Field(description="嵌入向量数量")
    elapsed_ms: int = Field(description="编码耗时 (毫秒)")


class EmbedQueryRequest(BaseModel):
    text: str = Field(..., min_length=1, description="查询文本")
    need_sparse: bool = Field(
        default=True,
        description="是否需要稀疏向量。默认本地 BGE-M3 一次前向（稠密+稀疏）；"
                    "纯稠密场景传 false 走 Infinity。",
    )


class EmbedQueryResponse(BaseModel):
    embedding: List[float] = Field(description="稠密向量, 1024d")
    sparse_embedding: Dict[str, float] = Field(description="稀疏词权重")
    elapsed_ms: int = Field(description="编码耗时 (毫秒)")


class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1)
    documents: List[str] = Field(..., min_length=1, max_length=500,
                                 description="待重排序文档列表")
    top_k: int = Field(default=10, ge=1, le=100)
    model: str = Field("", description="重排模型名（可选；空则用默认/托管模型）")


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
    # 从 model_registry 解析默认 embedding/reranker 模型名（管理台可切换，重启生效）
    _resolve_managed_models()
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


# ── 模型访问串行化锁 ──
# BGE-M3 / BGE-Reranker 的 encode()/compute_score() 是同步、非线程安全的。
# 处理器改为 def（FastAPI 线程池执行，避免阻塞事件循环）后，并发请求会进入
# 多线程；锁保证同一时刻只有一个模型前向传播，杜绝线程竞争。
#
# 锁粒度：BGE-M3（稠密回退 + 稀疏）与 BGE-Reranker 是**不同模型实例**，
# 各自独立加锁 → rerank 与 embed 可并行，互不阻塞（"只锁本地模型"的粒度）。
# 未用 Semaphore(>1)：BGE-M3 并发 encode 的线程安全无法保证（共享权重前向
# 可能竞争），允许多并发会冒结果损坏/崩溃的风险 —— 属于"为提速牺牲正确性"，
# 架构上禁止。吞吐靠降低 ingest 批次（batch_size）缩短单次锁持有时间换取。
_BGE_MODEL_LOCK = threading.Lock()
_RERANK_MODEL_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════
# Infinity Embedding Server Adapter（2026-08-16 引入，2026-08-18 收敛用途）
# ══════════════════════════════════════════════════════════════════
# Infinity 只用于"纯稠密"场景：
#   - /v1/embed、/v1/embed_query 在 need_sparse=false 时走 Infinity（单前向）；
#   - /v1/rerank 走 Infinity（Cohere 兼容，无稀疏参与）。
# 默认（need_sparse=true）走本地 BGE-M3 一次前向同时产出稠密+稀疏。
# 原因：Infinity 的 /embeddings 只回稠密，不能产出 BGE-M3 稀疏 lexical weights，
# 稠密拆给 Infinity + 稀疏留本地 = 两个模型实例常驻 + 两次 GPU 前向，单卡
# 12GB 上净收益为负（实测 ingest 嵌入慢 8-10 倍）。纯稠密场景才值得用 Infinity。
# Infinity 不可达时 fail-open 回退本地 BGE-M3 / 本地 reranker。

INFINITY_URL = os.getenv("INFINITY_URL", "http://localhost:19501").rstrip("/")
# Infinity 模型名：启动时从 model_registry 默认行解析（管理台可切换），
# 回落 env（INFINITY_EMBED_MODEL/INFINITY_RERANK_MODEL）→ 硬编码默认。
INFINITY_EMBED_MODEL = os.getenv("INFINITY_EMBED_MODEL", "BAAI/bge-m3")
INFINITY_RERANK_MODEL = os.getenv("INFINITY_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")


def _resolve_managed_models() -> None:
    """从 model_registry 解析默认 embedding/reranker 模型名，覆盖 Infinity 模型名。

    使管理台改默认模型后，embedding_service 重启即生效（本地 BGE-M3 加载路径
    保持默认 bge-m3，模型类不可动态替换）。
    """
    global INFINITY_EMBED_MODEL, INFINITY_RERANK_MODEL
    try:
        from src.platform.model.registry import resolve_managed_model_name
        managed_embed = resolve_managed_model_name(
            "embedding", env_key="INFINITY_EMBED_MODEL", default="BAAI/bge-m3")
        managed_rerank = resolve_managed_model_name(
            "reranker", env_key="INFINITY_RERANK_MODEL", default="BAAI/bge-reranker-v2-m3")
        if managed_embed:
            INFINITY_EMBED_MODEL = managed_embed
        if managed_rerank:
            INFINITY_RERANK_MODEL = managed_rerank
        log.info(
            "embedding_managed_models_resolved",
            embed_model=INFINITY_EMBED_MODEL, rerank_model=INFINITY_RERANK_MODEL,
        )
    except Exception as exc:
        log.warning("embedding_managed_models_resolve_failed", error=str(exc)[:200])


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
    with _BGE_MODEL_LOCK:
        model = _get_model()
        output = model.encode(
            texts, return_dense=True, return_sparse=False, batch_size=len(texts)
        )
    return [v.tolist() if hasattr(v, "tolist") else list(v) for v in output["dense_vecs"]]


def _infinity_rerank(query: str, documents: List[str], top_k: int,
                     model: str = "") -> Tuple[List[str], List[float]]:
    """经 Infinity rerank（Cohere /rerank 兼容）。返回 (排序后文档, 分数)。

    model 为空时用托管默认模型（INFINITY_RERANK_MODEL，源自 model_registry）。
    """
    import httpx
    resp = httpx.post(
        f"{INFINITY_URL}/rerank",
        json={"model": model or INFINITY_RERANK_MODEL, "query": query,
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
def embed(request: EmbedRequest):
    """文档批量嵌入——默认本地 BGE-M3 一次前向产出稠密 + 稀疏。

    2026-08-18 修复：撤销 2026-08-16 的 Infinity 双模型架构。BGE-M3 一次前向
    即产出稠密+稀疏（lexical weights 来自同一隐藏态），拆成 Infinity(稠密)+
    本地(稀疏) 是两次 GPU 前向 + 双模型常驻，单卡 12GB 上净收益为负（实测
    ingest 嵌入慢 8-10 倍）。纯稠密场景（need_sparse=false，无需稀疏）才走
    Infinity——单模型单前向，且稀疏本就不需要。

    用 def 而非 async def：内部 model.encode() 是同步 CPU/GPU 阻塞调用，
    async def 会阻塞事件循环导致整个服务（含 /healthz）不可响应。
    def 由 FastAPI 放入线程池执行，事件循环保持可用；模型访问由 _BGE_MODEL_LOCK 串行化。
    """
    t0 = time.time()
    try:
        if not request.need_sparse:
            # ── 纯稠密场景：Infinity（单模型单前向），失败回退本地稠密 ──
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
            return EmbedResponse(
                embeddings=all_embeddings,
                sparse_embeddings=[{} for _ in all_embeddings],
                count=len(all_embeddings),
                elapsed_ms=int((time.time() - t0) * 1000),
            )

        # ── 默认：本地 BGE-M3 一次前向（稠密 + 稀疏一次产出）──
        from src.ingest.components.bge_m3_embedder import _get_model
        with _BGE_MODEL_LOCK:
            model = _get_model()
            output = model.encode(
                request.texts,
                return_dense=True,
                return_sparse=True,
                batch_size=request.batch_size,
            )
        dense_vecs = output["dense_vecs"]
        lexical_weights = output.get("lexical_weights", [{}] * len(request.texts))
        all_embeddings = []
        for vec in dense_vecs:
            vec_list = vec.tolist() if hasattr(vec, "tolist") else list(vec)
            if request.normalize:
                vec_list = _normalize_vector(vec_list)
            all_embeddings.append(vec_list)
        all_sparse = [dict(w) if isinstance(w, dict) else {} for w in lexical_weights]
        dense_source = "local"
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
def embed_query(request: EmbedQueryRequest):
    """查询嵌入——默认本地 BGE-M3 一次前向（稠密 + 稀疏）。

    检索 Pipeline 混合检索同时用稠密 + 稀疏（BGE_M3TextEmbedder 消费两者），
    故默认走本地单次前向（同一隐藏态一次产出）。纯稠密场景
    （need_sparse=false）走 Infinity，稀疏本就不需要。
    def（线程池）原因同 /v1/embed：避免同步模型调用阻塞事件循环。
    """
    t0 = time.time()
    try:
        if not request.need_sparse:
            # ── 纯稠密场景：Infinity，失败回退本地稠密 ──
            try:
                dense_raw = _infinity_dense([request.text])
                dense_source = "infinity"
            except Exception as exc:
                log.warning("infinity_query_dense_failed_fallback_local", error=str(exc)[:200])
                dense_raw = _local_dense([request.text])
                dense_source = "local"
            vec_list = _normalize_vector(dense_raw[0])
            return EmbedQueryResponse(
                embedding=vec_list,
                sparse_embedding={},
                elapsed_ms=int((time.time() - t0) * 1000),
            )

        # ── 默认：本地 BGE-M3 一次前向（稠密 + 稀疏）──
        from src.ingest.components.bge_m3_embedder import _get_model
        with _BGE_MODEL_LOCK:
            model = _get_model()
            output = model.encode(
                [request.text], return_dense=True, return_sparse=True,
            )
        vec = output["dense_vecs"][0]
        vec_list = vec.tolist() if hasattr(vec, "tolist") else list(vec)
        vec_list = _normalize_vector(vec_list)
        sparse_raw = output.get("lexical_weights", [{}])
        sparse = dict(sparse_raw[0]) if sparse_raw and isinstance(sparse_raw[0], dict) else {}
        dense_source = "local"
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
def rerank(request: RerankRequest):
    """文档重排序——BGE-Reranker-v2-m3。

    2026-08-16 Adapter：优先经 Infinity（Cohere /rerank 兼容）；
    Infinity 不可达时 fail-open 回退本地 reranker。
    供检索 Pipeline (BGEReranker) 使用。
    def（线程池）原因同 /v1/embed：避免同步模型调用阻塞事件循环。
    """
    t0 = time.time()
    try:
        try:
            result_docs, result_scores = _infinity_rerank(
                request.query, request.documents, request.top_k, model=request.model
            )
            rerank_source = "infinity"
        except Exception as exc:
            log.warning("infinity_rerank_failed_fallback_local", error=str(exc)[:200])
            from src.platform.model.registry import _get_reranker
            with _RERANK_MODEL_LOCK:
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
