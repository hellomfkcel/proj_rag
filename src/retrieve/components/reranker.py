"""BGEReranker — Haystack @component wrapping P-MODEL invoke_rerank.

Uses BGE Reranker v2-m3 (via FlagEmbedding) to re-rank retrieved documents.
Falls back to no-op (identity sort) if reranker fails to load.
"""

from typing import Any, Dict, List
from haystack import component, Document


@component
class BGEReranker:
    """Re-rank documents using BGE Reranker v2-m3.

    Delegates to P-MODEL invoke_rerank which handles model loading and
    fail-open fallback (returns input order if reranker unavailable).
    """

    def __init__(self, top_k: int = 10):
        self.top_k = top_k

    @component.output_types(documents=List[Document])
    def run(self, query: str, documents: List[Document]) -> Dict[str, Any]:
        """Re-rank documents by relevance to query.

        If fewer than 2 documents, returns them as-is.
        Otherwise invokes the BGE reranker via P-MODEL.
        """
        if len(documents) <= 1:
            return {"documents": documents}

        from src.platform.model.registry import invoke_rerank

        contents = [d.content for d in documents]
        reranked = invoke_rerank(query, contents)

        # Map reranked contents back to Document objects, preserving order
        content_to_doc = {d.content: d for d in documents}
        result = []
        seen = set()
        for c in reranked:
            if c not in seen:
                result.append(content_to_doc.get(c, documents[len(result)]))
                seen.add(c)

        # Truncate to top_k
        result = result[:self.top_k]
        return {"documents": result}
