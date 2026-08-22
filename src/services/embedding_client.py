"""BGE-M3 Embedding Service 客户端 — Layer 3 HTTP 模式适配层。

当环境变量 EMBEDDING_SERVICE_URL 设置时，嵌入和重排序操作
通过 HTTP 调用独立的 Embedding Service，而非在本地进程加载模型。

所有函数均为 fail-open：HTTP 调用失败时降级到本地模型。
"""

import os
import time as _time
from typing import Any, Dict, List, Optional, Tuple


def _get_service_url() -> Optional[str]:
    return os.getenv("EMBEDDING_SERVICE_URL", "").strip() or None


def _http_post(endpoint: str, json_data: dict, timeout: int = 300) -> dict:
    """向 Embedding Service 发送 HTTP POST 请求。

    ★ 传播 OTel trace context（W3C traceparent）：
    不传时 embedding service 的 /v1/embed 每次新建一个根 trace（Tempo 碎片，
    一个 ingest 被拆成几十上百条），破坏可观测性完整性。
    propagate.inject 从当前 span（ingest pipeline 的 embedder 组件 span）
    注入 traceparent，使服务端 span 成为当前 trace 的子 span。
    """
    import requests
    base = _get_service_url().rstrip("/")
    headers: dict = {}
    try:
        from opentelemetry import propagate
        propagate.inject(headers)
    except Exception:
        pass  # 无 OTel 上下文时退化为无传播（不阻断业务）
    resp = requests.post(
        f"{base}{endpoint}", json=json_data, headers=headers, timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()


def embed_documents(
    texts: List[str],
    batch_size: int = 512,
    normalize: bool = True,
) -> Tuple[List[List[float]], List[Dict[str, float]], int]:
    """文档批量嵌入（通过 HTTP 服务或本地模型）。

    batch_size=512：一次 HTTP 请求携带一批文本，服务端本地 BGE-M3 一次前向
    产出稠密+稀疏。更小的 batch_size 会把单个文档拆成更多请求，放大每次
    请求的固定开销（HTTP + 模型前向）。

    Returns:
        (embeddings, sparse_embeddings, elapsed_ms)
    """
    service_url = _get_service_url()
    if service_url:
        t0 = _time.time()
        try:
            all_embeddings: List[List[float]] = []
            all_sparse: List[Dict[str, float]] = []
            for i in range(0, len(texts), batch_size):
                batch = texts[i : i + batch_size]
                # batch_size 传配置值（≥32，满足服务端 EmbedRequest.batch_size 校验），
                # 而非 len(batch)——末批/小批（如语义分割的少量句子）可能 <32 会 422。
                result = _http_post("/v1/embed", {
                    "texts": batch,
                    "batch_size": batch_size,
                    "normalize": normalize,
                })
                all_embeddings.extend(result["embeddings"])
                all_sparse.extend(result["sparse_embeddings"])
            return (
                all_embeddings,
                all_sparse,
                int((_time.time() - t0) * 1000),
            )
        except Exception:
            # fail-open: HTTP 失败降级到本地模型
            pass

    # 本地模式
    from src.ingest.components.bge_m3_embedder import _get_model
    import math

    t0 = _time.time()
    model = _get_model()
    all_embeddings: List[List[float]] = []
    all_sparse: List[Dict[str, float]] = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        output = model.encode(
            batch, return_dense=True, return_sparse=True, batch_size=len(batch)
        )
        dense_batch = output["dense_vecs"]
        sparse_batch = output.get("lexical_weights", [{}] * len(batch))

        for vec in dense_batch:
            vec_list = vec.tolist() if hasattr(vec, "tolist") else list(vec)
            if normalize:
                norm = math.sqrt(sum(v * v for v in vec_list))
                if norm > 0:
                    vec_list = [v / norm for v in vec_list]
            all_embeddings.append(vec_list)
        all_sparse.extend(sparse_batch)

    elapsed_ms = int((_time.time() - t0) * 1000)
    return all_embeddings, all_sparse, elapsed_ms


def embed_query(text: str) -> Tuple[List[float], Dict[str, float], int]:
    """查询嵌入（通过 HTTP 服务或本地模型）。

    Returns:
        (embedding, sparse_embedding, elapsed_ms)
    """
    service_url = _get_service_url()
    if service_url:
        t0 = _time.time()
        try:
            result = _http_post("/v1/embed_query", {"text": text}, timeout=30)
            return (
                result["embedding"],
                result["sparse_embedding"],
                int((_time.time() - t0) * 1000),
            )
        except Exception:
            pass

    # 本地模式
    from src.ingest.components.bge_m3_embedder import _get_model
    import math

    t0 = _time.time()
    model = _get_model()
    output = model.encode([text], return_dense=True, return_sparse=True)

    dense = output["dense_vecs"][0]
    vec_list = dense.tolist() if hasattr(dense, "tolist") else list(dense)
    norm = math.sqrt(sum(v * v for v in vec_list))
    if norm > 0:
        vec_list = [v / norm for v in vec_list]

    sparse = output.get("lexical_weights", [{}])
    sparse_dict = sparse[0] if len(sparse) > 0 else {}

    elapsed_ms = int((_time.time() - t0) * 1000)
    return vec_list, sparse_dict, elapsed_ms


def rerank(
    query: str, documents: List[str], top_k: int = 10, model_name: str = ""
) -> Tuple[List[str], List[float], int]:
    """文档重排序（通过 HTTP 服务或本地模型）。

    model_name 非空时透传给服务端（覆盖托管默认重排模型）。

    Returns:
        (reranked_documents, scores, elapsed_ms)
    """
    service_url = _get_service_url()
    if service_url:
        t0 = _time.time()
        try:
            payload = {
                "query": query,
                "documents": documents,
                "top_k": top_k,
            }
            if model_name:
                payload["model"] = model_name
            result = _http_post("/v1/rerank", payload, timeout=60)
            return (
                result["documents"],
                result.get("scores", [0.0] * len(result["documents"])),
                int((_time.time() - t0) * 1000),
            )
        except Exception:
            pass

    # 本地模式 — 直接使用全局单例 reranker
    from src.platform.model.registry import _get_reranker
    import time as _t

    t0 = _t.time()
    ranker = _get_reranker()
    scores = ranker.compute_score(
        [[query, d] for d in documents], normalize=True
    )
    scored = sorted(zip(documents, scores), key=lambda x: x[1], reverse=True)
    result = [doc for doc, _ in scored[:top_k]]
    result_scores = [float(s) for _, s in scored[:top_k]]
    elapsed_ms = int((_t.time() - t0) * 1000)
    return result, result_scores, elapsed_ms
