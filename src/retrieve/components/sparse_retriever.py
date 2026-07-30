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
        from pymilvus import connections, Collection
        from milvus_haystack.filters import parse_filters

        connections.connect(
            "default", host=self.milvus_host, port=str(self.milvus_port)
        )
        col = Collection(self.collection_name, using="default")
        col.load()

        try:
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

            search_params = {"metric_type": "IP", "params": {"nprobe": 16}}

            hits = col.search(
                [sparse_vec],
                "sparse_vector",
                search_params,
                self.top_k,
                expr,
                output_fields=["content", "document_id", "kb_id", "vis_version"],
            )

            docs = []
            for h in hits[0]:
                fields = h.entity.fields
                doc = Document(
                    content=fields.get("content", ""),
                    meta={
                        "document_id": fields.get("document_id", ""),
                        "kb_id": fields.get("kb_id", ""),
                        "score": h.distance,
                        "score": h.distance,
                        "source": "sparse",
                    },
                )
                doc.id = str(h.id)
                docs.append(doc)

            return {"documents": docs}
        finally:
            pass  # keep connection alive — shared with dense_retriever
