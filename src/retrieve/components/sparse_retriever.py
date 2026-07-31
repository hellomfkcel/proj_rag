"""MilvusSparseRetriever — Haystack @component for BM25 sparse vector retrieval.

Since milvus-haystack 0.0.18 does not ship MilvusBM25Retriever,
this custom component wraps pymilvus for sparse (lexical/IP) search.
"""

from typing import Any, Dict, List, Optional
from haystack import component, Document


@component
class MilvusSparseRetriever:
    """Retrieve documents from Milvus using sparse (BM25 lexical) vectors.

    Connects to Milvus directly via pymilvus. Supports passing a filter dict
    that gets compiled to a Milvus expression via milvus-haystack's parse_filters.
    """

    def __init__(
        self,
        collection_name: str = "rag_documents",
        top_k: int = 20,
        milvus_host: str = "localhost",
        milvus_port: int = 19530,
    ):
        self.collection_name = collection_name
        self.top_k = top_k
        self.milvus_host = milvus_host
        self.milvus_port = milvus_port

    @component.output_types(documents=List[Document])
    def run(
        self,
        query_sparse_embedding: Any,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute sparse vector search against Milvus.

        query_sparse_embedding: SparseEmbedding from BGE_M3SparseTextEmbedder
        filters: Milvus expression string (from _compile_filter_expr) or dict
        """
        from dataclasses import replace
        from pymilvus import MilvusClient
        from milvus_haystack.filters import parse_filters

        # MilvusClient API 不会自动 load collection → 必须显式调用 load_collection。
        # load_collection 已加载时是快速空操作（幂等）。
        client = MilvusClient(uri=f"http://{self.milvus_host}:{self.milvus_port}")
        client.load_collection(self.collection_name)

        # Build sparse vector for search.
        # BGE_M3SparseTextEmbedder outputs {token_id: weight} directly —
        # this is already a valid pymilvus sparse vector, no extra unwrapping needed.
        # Only unwrap if the dict has an explicit "sparse_vector" key (legacy wrapper).
        if hasattr(query_sparse_embedding, "sparse_vector"):
            sparse_vec = query_sparse_embedding.sparse_vector
        elif isinstance(query_sparse_embedding, dict):
            if "sparse_vector" in query_sparse_embedding:
                sparse_vec = query_sparse_embedding["sparse_vector"]
            else:
                sparse_vec = query_sparse_embedding
        else:
            sparse_vec = query_sparse_embedding

        # Compile filter expression: str → use directly, dict → parse_filters
        if isinstance(filters, str):
            expr = filters
        elif filters:
            try:
                expr = parse_filters(filters)
            except Exception:
                expr = None
        else:
            expr = None

        try:
            hits = client.search(
                collection_name=self.collection_name,
                data=[sparse_vec],
                anns_field="sparse_vector",
                filter=expr,
                limit=self.top_k,
                output_fields=["content", "document_id", "kb_id", "vis_version"],
                search_params={"metric_type": "IP", "params": {"nprobe": 16}},
            )
        except Exception as exc:
            # sparse_vector 字段可能尚未创建（首次摄入前 collection 为空 schema）。
            # 降级返回空结果——Hybrid 检索的 dense 路仍然工作，DocumentJoiner RRF 会将
            # 两路结果融合（dense 结果不受影响）。
            # 设计依据：docs/RAG系统设计v14.md §15.7 混合检索与融合。
            import logging
            _log = logging.getLogger(__name__)
            _log.warning(
                "sparse_search_failed_fallback_empty",
                collection=self.collection_name,
                error=str(exc)[:200],
            )
            return {"documents": []}

        docs = []
        if hits and hits[0]:
            for h in hits[0]:
                entity = h.get("entity", {})
                doc = Document(
                    content=entity.get("content", ""),
                    meta={
                        "document_id": entity.get("document_id", ""),
                        "kb_id": entity.get("kb_id", ""),
                        "score": h.get("distance", 0.0),
                        "source": "sparse",
                    },
                )
                doc = replace(doc, id=str(h.get("id", "")))
                docs.append(doc)

        return {"documents": docs}
