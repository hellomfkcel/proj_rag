"""MilvusHybridRetriever — Haystack @component 封装 Milvus 原生混合检索。

单次 hybrid_search 往返内并行执行稠密 + 稀疏两路 ANN，并由 Milvus 内核融合：
- RRFRanker(k=60)        —— 服务端 Reciprocal Rank Fusion
- WeightedRanker(w1, w2) —— 服务端加权融合（norm_score=True 内部归一）

权限纪律：
- 本组件**不含任何权限逻辑**。`filters` 是外部（B-RETRIEVE 入口 compile_filter）
  注入的 Milvus 表达式字符串，两个 AnnSearchRequest 共享同一 filters —— 保证
  双路检索施加完全相同的过滤条件（六条件 prefilter）。
- 检索结果仅按需带上命中 metadata，不做任何本地判定/事后过滤。

可观测性：Haystack 自动为 Component.run() 创建 OTel span。
"""

from dataclasses import replace
from typing import Any, Dict, List, Optional

from haystack import component, Document

# 模块级 MilvusClient 缓存，按 (host, port) 复用连接。
_clients: Dict[str, Any] = {}


def _get_milvus_client(host: str, port: str) -> Any:
    from pymilvus import MilvusClient
    key = f"{host}:{port}"
    if key not in _clients:
        _clients[key] = MilvusClient(uri=f"http://{host}:{port}")
    return _clients[key]


def _normalize_sparse(sparse_embedding: Any) -> Dict[int, float]:
    """归一化稀疏向量为 {int token_id: float weight}。

    BGE-M3 lexical_weights 的 key 可能为 numpy int/str，Milvus 稀疏向量要求
    int -> float 的映射。
    """
    if hasattr(sparse_embedding, "sparse_vector"):
        sparse_embedding = sparse_embedding.sparse_vector
    if isinstance(sparse_embedding, dict):
        if "sparse_vector" in sparse_embedding:
            sparse_embedding = sparse_embedding["sparse_vector"]
        return {int(k): float(v) for k, v in sparse_embedding.items()}
    return {}


@component
class MilvusHybridRetriever:
    """Milvus 原生混合检索：dense + sparse 双路 ANN，服务端内核融合。"""

    def __init__(
        self,
        collection_name: str = "rag_documents",
        milvus_host: str = "localhost",
        milvus_port: int = 19530,
        top_k: int = 30,
    ):
        self.collection_name = collection_name
        self.milvus_host = milvus_host
        self.milvus_port = milvus_port
        self.top_k = top_k

    @component.output_types(documents=List[Document])
    def run(
        self,
        query_embedding: List[float],
        query_sparse_embedding: Any,
        filters: Optional[str] = None,
        fusion_method: str = "rrf",
        dense_weight: float = 0.5,
        sparse_weight: float = 0.5,
        top_k: Optional[int] = None,
    ) -> Dict[str, Any]:
        """执行 Milvus 原生混合检索。

        :param query_embedding:      稠密查询向量（BGE-M3 dense，1024d）
        :param query_sparse_embedding: 稀疏查询向量（BGE-M3 lexical_weights）
        :param filters:              权限预过滤表达式（compile_filter 编译产物，
                                     六条件，含 json_contains/not_json_contains）
        :param fusion_method:        "rrf" | "weighted"
        :param dense_weight/sparse_weight: weighted 融合权重
        :param top_k:                最终融合结果条数（覆盖 init；检索服务传 k_prime）
        """
        from pymilvus import AnnSearchRequest, RRFRanker, WeightedRanker

        k = top_k or self.top_k
        sparse_vec = _normalize_sparse(query_sparse_embedding)

        # ── 双路 ANN 请求，两个 request 共享同一 filters（权限过滤一致）──
        reqs = [
            AnnSearchRequest(
                data=[list(query_embedding)],
                anns_field="vector",
                param={"metric_type": "IP", "params": {"nprobe": 16}},
                limit=k,
                filter=filters or "",
            ),
            AnnSearchRequest(
                data=[sparse_vec] if sparse_vec else [{}],
                anns_field="sparse_vector",
                param={"metric_type": "IP", "params": {"drop_ratio_build": 0.2}},
                limit=k,
                filter=filters or "",
            ),
        ]

        # 兼容配置层的 "weighted_sum"（设计文档 §12.1）与本组件规范名 "weighted"
        if fusion_method in ("weighted", "weighted_sum"):
            # WeightedRanker(norm_score=True) 服务端按路归一后加权；
            # 与应用层 min-max 归一语义相近（非逐位一致），权重语义不变。
            ranker = WeightedRanker(dense_weight, sparse_weight, norm_score=True)
        else:
            ranker = RRFRanker(k=60)

        client = _get_milvus_client(self.milvus_host, str(self.milvus_port))
        # MilvusClient API 不会自动 load collection → 显式 load（已加载时幂等空操作）。
        client.load_collection(self.collection_name)

        try:
            hits = client.hybrid_search(
                collection_name=self.collection_name,
                reqs=reqs,
                ranker=ranker,
                limit=k,
                output_fields=[
                    "content", "document_id", "kb_id", "vis_version",
                    "level", "chunk_index", "parent_id",
                ],
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "hybrid_search_failed",
                extra={"error": str(exc)[:300], "collection": self.collection_name},
            )
            return {"documents": []}

        docs: List[Document] = []
        if hits and hits[0]:
            for h in hits[0]:
                entity = h.get("entity", {})
                doc = Document(
                    content=entity.get("content", ""),
                    meta={
                        "document_id": entity.get("document_id", ""),
                        "kb_id": entity.get("kb_id", ""),
                        "score": h.get("distance", 0.0),
                        "source": "hybrid",
                        "level": entity.get("level", 1),
                        "chunk_index": entity.get("chunk_index", 0),
                        "parent_id": entity.get("parent_id", "") or "",
                    },
                )
                doc = replace(doc, id=str(h.get("id", "")))
                docs.append(doc)

        return {"documents": docs}
