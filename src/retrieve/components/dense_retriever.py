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
        # String expression → direct PyMilvus (json_contains operators)
        if isinstance(filters, str):
            from pymilvus import connections, Collection
            connections.connect("default", host=self.milvus_host, port=str(self.milvus_port))
            col = Collection(self.collection_name)
            col.load()
            try:
                hits = col.search(
                    [query_embedding], "vector",
                    {"metric_type": "IP", "params": {"nprobe": 16}},
                    self.top_k, filters,
                    output_fields=["content", "document_id", "kb_id", "vis_version"],
                )
                docs = []
                if hits and hits[0]:
                    for h in hits[0]:
                        fields = h.entity.fields
                        doc = Document(
                            content=fields.get("content", ""),
                            meta={
                                "document_id": fields.get("document_id", ""),
                                "kb_id": fields.get("kb_id", ""),
                                "score": h.distance,
                                "source": "dense",
                            },
                        )
                        doc.id = str(h.id)
                        docs.append(doc)
                return {"documents": docs}
            finally:
                pass  # keep connection for shared use

        # Dict (or None) → standard milvus-haystack path
        retriever = self._get_retriever()
        result = retriever.run(query_embedding=query_embedding, filters=filters)
        return {"documents": result.get("documents", [])}
