"""MilvusDenseRetriever — Haystack @component for dense vector search.

Primary path: delegates to milvus-haystack MilvusEmbeddingRetriever (dict filters).
Fallback: direct PyMilvus search for pre-compiled expression strings (json_contains ops).
"""

from typing import Any, Dict, List, Optional
from haystack import component, Document


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

    def _get_retriever(self):
        if self._retriever is None:
            from milvus_haystack.document_store import MilvusDocumentStore
            from milvus_haystack.milvus_embedding_retriever import MilvusEmbeddingRetriever
            doc_store = MilvusDocumentStore(
                collection_name=self.collection_name,
                connection_args={"uri": f"http://{self.milvus_host}:{self.milvus_port}"},
            )
            self._retriever = MilvusEmbeddingRetriever(
                document_store=doc_store, top_k=self.top_k,
            )
        return self._retriever

    @component.output_types(documents=List[Document])
    def run(
        self,
        query_embedding: List[float],
        filters: Any = None,
    ) -> Dict[str, Any]:
        """Execute dense vector search.

        Dict filters → milvus-haystack MilvusEmbeddingRetriever (standard path).
        Str filters  → direct PyMilvus search (json_contains/not_json_contains).
        """
        # String expression → MilvusClient (json_contains operators)
        # MilvusClient handles connection management internally — no explicit
        # connect/load needed. Replaces deprecated ORM-style API
        # (connections.connect / Collection / Collection.load / Collection.search).
        if isinstance(filters, str):
            from dataclasses import replace
            from pymilvus import MilvusClient

            client = MilvusClient(uri=f"http://{self.milvus_host}:{self.milvus_port}")
            hits = client.search(
                collection_name=self.collection_name,
                data=[query_embedding],
                anns_field="vector",
                filter=filters,
                limit=self.top_k,
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
        retriever = self._get_retriever()
        result = retriever.run(query_embedding=query_embedding, filters=filters)
        return {"documents": result.get("documents", [])}
