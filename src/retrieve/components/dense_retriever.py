"""MilvusDenseRetriever — Haystack @component for dense vector search.

Primary path: delegates to milvus-haystack MilvusEmbeddingRetriever (dict filters).
Fallback: direct PyMilvus search for pre-compiled expression strings (json_contains ops).
"""

from typing import Any, Dict, List, Optional
from haystack import component, Document
from pymilvus import MilvusClient

# 模块级 MilvusClient + MilvusDocumentStore 缓存，按 (host, port, collection) 复用。
# 消除每次 Pipeline.loads() 重建 DocumentStore/Retriever 的开销。
_clients: Dict[str, MilvusClient] = {}
_stores: Dict[str, Any] = {}
_retrievers: Dict[str, Any] = {}


def _get_milvus_client(host: str, port: str) -> MilvusClient:
    key = f"{host}:{port}"
    if key not in _clients:
        _clients[key] = MilvusClient(uri=f"http://{host}:{port}")
    return _clients[key]


def _get_document_store(collection_name: str, host: str, port: str):
    key = f"{host}:{port}:{collection_name}"
    if key not in _stores:
        from milvus_haystack.document_store import MilvusDocumentStore
        _stores[key] = MilvusDocumentStore(
            collection_name=collection_name,
            connection_args={"uri": f"http://{host}:{port}"},
        )
    return _stores[key]


@component
class MilvusDenseRetriever:
    """Dense vector retriever with dual filter support.

    - dict filters: standard milvus-haystack MilvusEmbeddingRetriever path
    - str filters:   direct PyMilvus search (for json_contains expressions)
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
        self._retriever = None

    def _get_retriever(self, top_k: int):
        key = f"{self.milvus_host}:{self.milvus_port}:{self.collection_name}:{top_k}"
        if key not in _retrievers:
            from milvus_haystack.milvus_embedding_retriever import MilvusEmbeddingRetriever
            doc_store = _get_document_store(self.collection_name, self.milvus_host, str(self.milvus_port))
            _retrievers[key] = MilvusEmbeddingRetriever(
                document_store=doc_store, top_k=top_k,
            )
        return _retrievers[key]

    @component.output_types(documents=List[Document])
    def run(
        self,
        query_embedding: List[float],
        filters: Any = None,
        top_k: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute dense vector search.

        Dict filters → milvus-haystack MilvusEmbeddingRetriever (standard path).
        Str filters  → direct PyMilvus search (json_contains/not_json_contains).
        top_k        → 覆盖 init top_k（检索服务传 k' = k × 1.5 过采样）。
        """
        effective_top_k = top_k or self.top_k

        # String expression → MilvusClient (json_contains operators)
        # MilvusClient API 不会自动 load collection → 必须显式调用 load_collection。
        # load_collection 已加载时是快速空操作（幂等）。
        if isinstance(filters, str):
            from dataclasses import replace

            client = _get_milvus_client(self.milvus_host, str(self.milvus_port))
            client.load_collection(self.collection_name)
            hits = client.search(
                collection_name=self.collection_name,
                data=[query_embedding],
                anns_field="vector",
                filter=filters,
                limit=effective_top_k,
                output_fields=["content", "document_id", "kb_id", "vis_version"],
                search_params={"metric_type": "IP", "params": {"nprobe": 16}},
            )
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
                            "source": "dense",
                        },
                    )
                    doc = replace(doc, id=str(h.get("id", "")))
                    docs.append(doc)
            return {"documents": docs}

        # Dict (or None) → standard milvus-haystack path
        retriever = self._get_retriever(effective_top_k)
        result = retriever.run(query_embedding=query_embedding, filters=filters)
        return {"documents": result.get("documents", [])}
